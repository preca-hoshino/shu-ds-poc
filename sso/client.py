"""上游 `ShuSSO` 的本地子类：只加上游没有的「会话导出」。

其余能力（登录、2FA、授权取码、换会话、企微扫码）全用上游实现。
"""

from __future__ import annotations

from src.client import ShuSSO as _UpstreamShuSSO

__all__ = ["ShuSSO"]


class ShuSSO(_UpstreamShuSSO):
    """统一身份认证客户端：上游实现 + 会话 Cookie 导出。"""

    def cookies(self, domain_contains: str | None = None) -> dict[str, str]:
        """导出会话 Cookie（`{name: value}`），按域名排序（同名以更靠后的域为准）。

        `domain_contains` 非空时只导出域名含该串的项（如 `"shu.edu.cn"`）。
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
