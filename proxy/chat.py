"""对话接口处理：请求校验 → 转换 → 上游 → 转换回来。

对外只产出规范形状：非流式是 OpenAI 响应字典，流式是 OpenAI SSE 文本。
上游出错抛 `UpstreamError`，由服务层转成 OpenAI 错误体（状态码沿用上游）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

import httpx
from pydantic import ValidationError

from . import credentials as creds_mod
from . import fastgpt as fg
from . import schemas as sc
from . import stream as st
from . import telemetry as tm
from . import transform as tf
from .upstream import Upstream, UpstreamError

log = logging.getLogger("shu-ds-poc")


class ModelNotFoundError(LookupError):
    """请求的模型不在支持列表里（→ 404，与 OpenAI 一致）。"""

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.model = model


def creds_view(creds: dict) -> dict:
    """把凭据整理成 transform 需要的扁平视图。"""
    return {
        "share_id": creds_mod.share_id(creds),
        "user_id": creds_mod.user_id(creds),
        "access_token": creds_mod.access_token(creds),
        "private_key": creds_mod.private_key(creds),
    }


def _canonical_model(req: sc.ChatCompletionRequest) -> str:
    """校验模型名并归一；不认识则抛 `ModelNotFoundError`。"""
    canonical = fg.resolve_model(req.model)
    if canonical is None:
        raise ModelNotFoundError(req.model)
    return canonical


def _log_ignored(req: sc.ChatCompletionRequest) -> None:
    """把「接受但不起作用」的参数记到 debug，便于排查客户端配置。"""
    ignored = [k for k, v in req.model_dump(exclude_none=True).items()
               if k not in ("model", "messages", "stream", "stream_options", "user")]
    extra = sorted(req.extra_fields)
    if ignored or extra:
        log.debug("未生效的请求参数：%s%s", ",".join(ignored),
                  f"；未知字段：{','.join(extra)}" if extra else "")


async def acompletion(upstream: Upstream, req: sc.ChatCompletionRequest) -> dict[str, Any]:
    """非流式：返回 OpenAI 响应字典。"""
    model = _canonical_model(req)
    _log_ignored(req)

    prompt_chars = sc.count_chars(req.messages)
    rl = tm.RequestLog(req.model, stream=False, messages=len(req.messages),
                       prompt_chars=prompt_chars)
    rl.input_line()

    payload = tf.build_fastgpt_request(req, creds_view(upstream.creds), model)
    try:
        resp = await upstream.chat(payload)
        rl.mark_ttfb()
        try:
            raw = (await resp.aread()).decode("utf-8", "replace")
        finally:
            await resp.aclose()      # 上游响应是流式拿到的，读完必须显式释放

        try:
            parsed = fg.FastGptResponse.model_validate_json(raw)
        except ValidationError as exc:
            raise UpstreamError(f"上游响应不是预期的 JSON 结构：{exc}", 502, raw[:2000]) from exc

        out = fg.to_openai_response(parsed, req.model, int(time.time()),
                                    prompt_chars=prompt_chars)
    except Exception as exc:                        # noqa: BLE001
        rl.fail(type(exc).__name__)
        raise

    usage = out["usage"]
    rl.finish(int(usage["prompt_tokens"]), int(usage["completion_tokens"]),
              estimated=not fg.usage_from_upstream(parsed))
    return out


async def _closing_bytes(resp: httpx.Response) -> AsyncIterator[bytes]:
    """上游字节流，并在**任何**退出路径上释放连接。

    客户端中途断开时 Starlette 会取消这个生成器，`finally` 保证连接不泄漏。
    """
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    finally:
        await resp.aclose()


async def astream(upstream: Upstream, req: sc.ChatCompletionRequest) -> AsyncIterator[str]:
    """流式：返回 SSE 文本的异步生成器。

    上游的 HTTP 错误在这一步（还没开始发 SSE）就抛出，因此能被转成正常的 JSON 错误响应；
    中途断流则只能提前结束 SSE。
    """
    model = _canonical_model(req)
    _log_ignored(req)

    prompt_chars = sc.count_chars(req.messages)
    rl = tm.RequestLog(req.model, stream=True, messages=len(req.messages),
                       prompt_chars=prompt_chars)
    rl.input_line()

    payload = tf.build_fastgpt_request(req, creds_view(upstream.creds), model)
    try:
        resp = await upstream.chat(payload)
    except Exception as exc:                        # noqa: BLE001
        rl.fail(type(exc).__name__)
        raise
    rl.mark_ttfb()
    return st.translate_stream(_closing_bytes(resp), req, model=req.model, telemetry=rl)
