"""FastAPI 服务：**只暴露 OpenAI 的端点**。

    POST /v1/chat/completions   对话（流式 / 非流式）
    GET  /v1/models             模型列表

除此之外没有任何路由 —— FastAPI 自带的 `/docs`、`/redoc`、`/openapi.json`
也一并关掉，保证 API 面与 OpenAI 一致。

鉴权、错误体、状态码全部按 OpenAI 规范：
    Authorization: Bearer <api key>
    {"error": {"message", "type", "param", "code"}}
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from . import chat as chat_mod
from . import fastgpt as fg
from . import schemas as sc
from .upstream import Upstream, UpstreamError

log = logging.getLogger("shu-ds-poc")

#: SSE 响应头
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",          # 避免中间层缓冲住流
}


class ApiKeyError(Exception):
    """API Key 缺失或不正确（→ 401）。"""


class Config:
    """服务端配置（在 `create_app` 时固定）。"""

    def __init__(self, creds: dict, api_key: str, base: str | None = None,
                 timeout: float = 300.0, verify: bool = True) -> None:
        self.creds = creds
        self.api_key = api_key
        self.base = base
        self.timeout = timeout
        self.verify = verify


def _validation_message(exc: ValidationError) -> tuple[str, str | None]:
    """把 pydantic 校验错误压成一条 OpenAI 风格的消息 + 出错字段路径。"""
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(x) for x in first.get("loc", ())) or None
    msg = first.get("msg") or "invalid request"
    return (f"Invalid value for '{loc}': {msg}" if loc else msg), loc


def create_app(cfg: Config) -> FastAPI:
    """装配 FastAPI 应用。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.upstream = Upstream(cfg.creds, base=cfg.base, timeout=cfg.timeout,
                                      verify=cfg.verify)
        log.info("上游: %s", app.state.upstream.url)
        try:
            yield
        finally:
            await app.state.upstream.aclose()

    app = FastAPI(title="OpenAI-Compatible DeepSeek Proxy", version="1.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # ---- 错误体 ----------------------------------------------------

    def fail(status: int, message: str, *, err_type: str = "invalid_request_error",
             code: str | None = None, param: str | None = None) -> JSONResponse:
        return JSONResponse(status_code=status,
                            content=sc.error_body(message, err_type, code, param))

    @app.exception_handler(ApiKeyError)
    async def _on_api_key(_request: Request, _exc: ApiKeyError) -> JSONResponse:
        return fail(401, "Incorrect API key provided. You can find your API key in "
                         "the PROXY_API_KEY environment variable or the --api-key option.",
                    code="invalid_api_key")

    @app.exception_handler(chat_mod.ModelNotFoundError)
    async def _on_model_not_found(_request: Request, exc: chat_mod.ModelNotFoundError) -> JSONResponse:
        names = ", ".join(sorted(fg.MODEL_ALIASES))
        return fail(404, f"The model '{exc.model}' does not exist. Supported: {names}.",
                    code="model_not_found", param="model")

    @app.exception_handler(UpstreamError)
    async def _on_upstream(_request: Request, exc: UpstreamError) -> JSONResponse:
        detail = exc.body.strip() or str(exc)
        log.warning("上游错误 HTTP %s：%s", exc.status_code, detail[:200])
        return fail(exc.status_code, f"Upstream returned HTTP {exc.status_code}: "
                                     f"{detail[:1500]}", err_type="api_error")

    @app.exception_handler(Exception)
    async def _on_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("未预期的异常")
        return fail(500, f"Internal server error: {type(exc).__name__}: {exc}",
                    err_type="server_error")

    # ---- 鉴权 ------------------------------------------------------

    async def require_key(authorization: str | None = Header(default=None)) -> None:
        """校验 `Authorization: Bearer <key>`。"""
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not cfg.api_key or token != cfg.api_key:
            raise ApiKeyError

    # ---- 路由 ------------------------------------------------------

    @app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
    async def chat_completions(request: Request) -> Any:
        """对话补全。`stream: true` 时返回 SSE。"""
        try:
            body = await request.json()
        except Exception as exc:                        # noqa: BLE001
            return fail(400, f"We could not parse the JSON body of your request: {exc}")

        try:
            req = sc.ChatCompletionRequest.model_validate(body)
        except ValidationError as exc:
            message, param = _validation_message(exc)
            return fail(400, message, param=param)

        upstream: Upstream = request.app.state.upstream
        if req.stream:
            gen = await chat_mod.astream(upstream, req)
            return StreamingResponse(gen, media_type="text/event-stream",
                                     headers=_SSE_HEADERS)
        return JSONResponse(content=await chat_mod.acompletion(upstream, req))

    @app.get("/v1/models", dependencies=[Depends(require_key)])
    async def models() -> dict[str, Any]:
        """模型列表。"""
        return {"object": "list", "data": fg.MODELS}

    return app


def make_app(creds: dict, api_key: str, **kwargs: Any) -> FastAPI:
    """便捷入口：`uvicorn` 用的应用工厂（见 `poc.py`）。"""
    return create_app(Config(creds, api_key, **kwargs))
