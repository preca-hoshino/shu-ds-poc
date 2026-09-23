"""上游 `ShuSSO` 的本地子类：只加本 POC 需要、上游没有的「会话导出」。

- `cookies()` / `cookie_header()` —— `login.py` 靠它把 SSO 会话落盘成
  `.credentials.json`（`poc.py` 转发时要带这份 Cookie）。

其余能力（RSA 加密登录、两步验证、授权取码、跟随跳转换会话、企微扫码）**全部用上游实现**，
见 `vendor/shu-sso-poc/src/client.py`；上游改那部分逻辑时，这里不用跟着动。
"""

from __future__ import annotations

from src.client import ShuSSO as _UpstreamShuSSO

__all__ = ["ShuSSO"]


class ShuSSO(_UpstreamShuSSO):
    """统一身份认证客户端：上游实现 + 会话 Cookie 导出。"""

    def cookies(self, domain_contains: str | None = None) -> dict[str, str]:
        """导出会话里的 Cookie（`{name: value}`）。

        `domain_contains` 非空时只导出域名包含该串的项（如 `"shu.edu.cn"`），
        并按域名排序 —— 同名 Cookie 以更靠后的域为准（requests 的发送顺序）。
        """
        out: dict[str, str] = {}
        for c in sorted(self.sess.cookies, key=lambda c: c.domain or ""):
            if domain_contains and domain_contains not in (c.domain or ""):
                continue
            out[c.name] = c.value
        return out

    def cookie_header(self, domain_contains: str | None = None) -> str:
        """把 Cookie 拼成可直接放进 `Cookie:` 请求头的字符串。"""
        return "; ".join(f"{k}={v}" for k, v in self.cookies(domain_contains).items())
