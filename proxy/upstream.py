"""上游 HTTP 客户端：`httpx.AsyncClient` + 手写 Cookie / Referer 头。

凭据从 `.credentials.json` 或环境变量读取（见 `credentials.py`）。
"""

from __future__ import annotations

from typing import Any

import httpx

from . import credentials as creds_mod
from . import fastgpt as fg
from .transform import build_referer

#: 与上游站点一致的浏览器 UA
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

#: 上游请求固定带的头
_BASE_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "DNT": "1",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": USER_AGENT,
    "accept": "text/event-stream",
}


class UpstreamError(RuntimeError):
    """上游返回错误或请求失败。"""

    def __init__(self, message: str, status_code: int = 502, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class Upstream:
    """上游（aiagent.shu.edu.cn）客户端。

    持有长效 `httpx.AsyncClient` 与一份凭据快照。凭据可在运行期用
    `reload()` 热更新（Cookie 过期后不必重启服务）。
    """

    def __init__(self, creds: dict, *, base: str | None = None, timeout: float = 300.0,
                 verify: bool = False, chat_path: str | None = None) -> None:
        self.creds = creds
        self.base = (base or creds_mod.upstream_base(creds)).rstrip("/")
        self.chat_path = chat_path or creds_mod.upstream_chat_path(creds)
        self._client = httpx.AsyncClient(timeout=timeout, verify=verify,
                                         follow_redirects=False)
        #: 每次请求实际用的头（供 `--check` 诊断）
        self.last_request: dict[str, Any] = {}

    # ---- 生命周期 ----------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Upstream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def reload(self, creds: dict | None = None) -> None:
        """重新读取凭据（`creds=None` 时从磁盘读）。"""
        self.creds = creds or creds_mod.load()
        self.base = creds_mod.upstream_base(self.creds)
        self.chat_path = creds_mod.upstream_chat_path(self.creds)

    # ---- 头 ----------------------------------------------------------

    def headers(self, share_id: str) -> dict[str, str]:
        """拼一次上游请求该带的头（Cookie / Origin / Referer）。"""
        h = dict(_BASE_HEADERS)
        h["Content-Type"] = "application/json"
        h["Origin"] = self.base
        h["Referer"] = build_referer(self.base, share_id)
        cookie = creds_mod.cookie_header(self.creds)
        if cookie:
            h["Cookie"] = cookie
        return h

    @property
    def url(self) -> str:
        return self.base + self.chat_path

    # ---- 请求 --------------------------------------------------------

    async def chat(self, payload: fg.FastGptRequest) -> httpx.Response:
        """POST 上游对话接口，返回**未读取正文**的响应对象。

        必须用 `send(request, stream=True)`，不能用 `client.post()`：httpx 的便捷
        方法会把响应体整段读完才返回，此后 `aiter_bytes()` 只是在回放内存里的字节，
        流式会被吃成一次性下发（实测帧到达跳度 0.000s、TTFB == 总耗时）。

        调用方负责读取并释放：

        | 场景 | 读法 | 释放 |
        | :--- | :--- | :--- |
        | 非流式 | `await resp.aread()` | `await resp.aclose()` |
        | 流式 | `async for c in resp.aiter_bytes()` | 读完自动释放 |

        出错时抛 `UpstreamError`，保留上游的状态码与正文（非 2xx 原样透传）。
        """
        headers = self.headers(payload.share_id)
        body = payload.model_dump(by_alias=True, exclude_none=True)
        self.last_request = {
            "url": self.url,
            "share_id": payload.share_id,
            "stream": payload.stream,
            "messages": len(payload.messages),
            "headers": {k: v for k, v in headers.items() if k != "Cookie"},
            "cookie_names": [c.split("=")[0].strip()
                             for c in (headers.get("Cookie") or "").split(";") if c.strip()],
        }

        request = self._client.build_request("POST", self.url, headers=headers, json=body)
        try:
            resp = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"上游请求失败（{type(exc).__name__}: {exc}）") from exc

        self.last_request["status"] = resp.status_code
        if resp.status_code >= 400:
            try:
                text = (await resp.aread()).decode("utf-8", "replace")
            finally:
                await resp.aclose()
            raise UpstreamError(
                f"上游返回 HTTP {resp.status_code}", resp.status_code, text[:4000])
        return resp
