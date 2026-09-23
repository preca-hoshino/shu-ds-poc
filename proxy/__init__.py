"""`shu-ds-poc` —— 上海大学 DeepSeek 代理 POC。

两个入口：
    login.py   登录（统一身份认证 → ds 会话）→ 产出凭据 `.credentials.json`
    poc.py     读凭据 → 起 OpenAI 兼容 API

子包：
    sso/       统一身份认证（薄适配层 + git 子模块 vendor/shu-sso-poc）
    proxy/     OpenAI ↔ FastGPT 转换与服务端

本文件刻意不做 eager import，保证 `from proxy.credentials import ...`
这类轻量用法不会把 fastapi/httpx 一起拉起来。
"""

__all__ = ["credentials", "schemas", "fastgpt", "transform", "upstream",
           "stream", "chat", "server"]
