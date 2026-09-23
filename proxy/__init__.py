"""`shu-ds-poc` —— 上海大学 DeepSeek 代理 POC。

两个入口：`login.py`（登录 → 产出凭据）、`poc.py`（读凭据 → 起 OpenAI 兼容 API）。
子包：`sso/`（统一身份认证，薄适配层 + 子模块）、`proxy/`（转换与服务端）。

本文件刻意不做 eager import，避免轻量用法把 fastapi/httpx 一起拉起来。
"""

__all__ = ["credentials", "schemas", "fastgpt", "transform", "upstream",
           "stream", "chat", "server"]
