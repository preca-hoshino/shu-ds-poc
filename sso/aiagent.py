"""aiagent.shu.edu.cn 会话引导：把认证会话扩展成对话后端认的 Cookie。

背景（为什么需要这个模块）
--------------------------------------------------------------------------
对话后端使用它自己下发的 Cookie（`uname` / `fid` / `xxtenc` / `_webvpn_key` /
`webvpn_username` ...），与统一身份认证侧的 Cookie 不是同一套 —— 登录能保证认证侧
（及已接入的业务系统）有会话，但这些 Cookie 不会被对话后端认可。

本模块用「登录后顺手探一次」的方式把这段差距补上：

1. 拿认证会话里 `*.shu.edu.cn` 的 Cookie 当作「广域 Cookie」，用
   **空 domain** 的方式塞进一个临时会话（空 domain 的 Cookie 会被发给任意主机，
   等价于手工写 `Cookie:` 头）；
2. `GET https://aiagent.shu.edu.cn/`，记录整条跳转链与 `Set-Cookie`；
3. 若被重定向到认证站点的登录页/授权页，就从 URL 里把 `client_id` / `redirect_uri`
   解出来（`/oauth2/login/<base64url(params)>` 路径就是这个格式），用已有的认证会话
   走一次 `authorize` 拿 code，再跟随回调回到上游换会话。

任何一步失败都只记录、不抛出 —— 上游站点可能改版，登录流程不能因此整体失败；
引导不到时 `login.py` 会给出「手工粘 Cookie」的兜底提示。
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, urlparse

import requests

from . import config
from .utils import log

# 认证站点把 OAuth 参数塞在路径里：/oauth2/login/<base64url>
_LOGIN_PATH_RE = re.compile(r"/oauth2/login/([A-Za-z0-9_\-]+)")
# 授权页/登录页 URL 上直接带参数的情形
_QUERY_KEYS = ("client_id", "clientId", "redirect_uri", "redirectUri")
#: 认证站点主机名：识别「探测时被重定向到认证站点」，随子模块配置变化
_AUTH_HOST = urlparse(config.SSO_BASE).netloc

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")


def _b64url_decode(seg: str) -> dict | None:
    """解路径段里的 base64url（无填充）JSON；失败返回 None。"""
    seg = seg.strip()
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def extract_oauth_params(url: str) -> dict | None:
    """从认证站点的登录/授权 URL 里解出 OAuth 参数。

    支持两种形态：
      - `/oauth2/login/<base64url>`：路径段是 base64url 的 JSON（前端对参数取值）
      - `/oauth/authorize?client_id=...&redirect_uri=...`：标准查询串
    """
    if not url:
        return None
    p = urlparse(url)

    q = parse_qs(p.query)
    if any(k in q for k in _QUERY_KEYS):
        out = {}
        for k in _QUERY_KEYS:
            if k in q and q[k]:
                out[k] = q[k][0]
        client_id = out.get("client_id") or out.get("clientId")
        redirect_uri = out.get("redirect_uri") or out.get("redirectUri")
        if client_id and redirect_uri:
            return {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": (q.get("scope") or [""])[0],
                "state": (q.get("state") or [""])[0],
                "source": "query",
            }

    m = _LOGIN_PATH_RE.search(p.path)
    if m:
        data = _b64url_decode(m.group(1))
        if data:
            client_id = data.get("clientId") or data.get("client_id")
            redirect_uri = data.get("redirectUri") or data.get("redirect_uri")
            if client_id and redirect_uri:
                return {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": data.get("scope") or "",
                    "state": data.get("state") or "",
                    "source": "path-base64",
                }
    return None


def _chain(resp: requests.Response) -> list[dict]:
    """把跳转链整理成可落盘的列表（含每一跳的 Location）。"""
    out = []
    for h in resp.history:
        out.append({"status": h.status_code,
                    "url": h.url,
                    "location": h.headers.get("Location")})
    out.append({"status": resp.status_code, "url": resp.url, "location": None})
    return out


def _probe_session(cookies: dict[str, str]) -> requests.Session:
    """建一个只带「空 domain Cookie」的临时会话。

    `RequestsCookieJar.update()` 走的是 `set(name, value)`，生成的 Cookie
    `domain=""`；`http.cookiejar` 对空 domain 的判定是「匹配任意主机」，
    因此这等价于手工写 `Cookie:` 请求头 —— 正是跨主机复用 Cookie 需要的效果。
    """
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    s.cookies.update(cookies)
    return s


def _join_cookies(sess: requests.Session) -> dict[str, str]:
    """把会话 jar 里的 Cookie 合并成 {name: value}（后出现者覆盖）。"""
    out: dict[str, str] = {}
    for c in sess.cookies:
        out[c.name] = c.value
    return out


def bootstrap_aiagent(client, base: str | None = None, timeout: int | None = None,
                      follow_oauth: bool = True, land_paths: tuple[str, ...] = ("/",)) -> dict:
    """探测上游站点并尽力建立会话；返回诊断信息（**不抛异常**）。

    参数
    ----
    client
        `sso.client.ShuSSO` 实例，需已登录（提供 `sess` / `authorize()` / `record()`）。
    base
        上游站点根，默认 `config.AIA_BASE`。
    follow_oauth
        被重定向到认证站点时，是否自动补一次 authorize 拿 code 再跟回调。

    返回
    ----
    dict，字段：
        ok              是否拿到了「比种入的更多」的上游 Cookie
        seeded          种入的 Cookie 名（来自 SSO 会话）
        probes          每个落地路径的探测结果（http_status / 跳转链 / 新 Cookie）
        oauth_params    解出的 client_id / redirect_uri（若被重定向到认证站点）
        authorize       本地 authorize 的结果（若执行了）
        cookies         最终 Cookie 集合（= 上游请求该用的那一套）
        cookie_header   可直接放进 `Cookie:` 头的字符串
        hint            失败时给人看的下一步提示
    """
    base = (base or config.AIA_BASE).rstrip("/")
    timeout = timeout or client.timeout

    seeded = client.cookies("shu.edu.cn") or client.cookies()
    sess = _probe_session(seeded)

    out: dict = {
        "base": base,
        "seeded": sorted(seeded.keys()),
        "probes": [],
        "oauth_params": None,
        "authorize": None,
        "redirects": None,
        "ok": False,
        "hint": None,
    }

    for path in land_paths:
        url = base + path
        try:
            r = sess.get(url, allow_redirects=True, timeout=timeout)
        except Exception as exc:                       # noqa: BLE001 - 探测不致命
            out["probes"].append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue

        chain = _chain(r)
        added = sorted(set(_join_cookies(sess)) - set(seeded))
        probe = {
            "url": url,
            "http_status": r.status_code,
            "final_url": r.url,
            "chain": chain,
            "set_cookie": [f"{c.name}={c.value[:6]}..." for c in sess.cookies],
            "new_cookies": added,
            "body_bytes": len(r.content or b""),
        }
        out["probes"].append(probe)
        log(f"     [probe] GET {url} → HTTP {r.status_code}"
            f"{' → ' + r.url if r.url != url else ''}"
            + (f"（新增 Cookie: {', '.join(added)}）" if added else ""))

        # 被重定向到认证站点：把参数解出来，必要时补一次授权
        if _AUTH_HOST in r.url and out["oauth_params"] is None:
            params = extract_oauth_params(r.url)
            if params:
                out["oauth_params"] = params
                log(f"     [probe] 解出 OAuth 参数（{params['source']}）: "
                    f"client_id={params['client_id']}, redirect_uri={params['redirect_uri']}")
                if follow_oauth:
                    out["authorize"], out["redirects"] = _complete_oauth(
                        client, sess, params, timeout)
            break

    final = _join_cookies(sess)
    out["cookies"] = final
    out["cookie_header"] = "; ".join(f"{k}={v}" for k, v in final.items())
    out["ok"] = bool(set(final) - set(seeded))

    if not out["ok"]:
        out["hint"] = (
            "未能从上游站点换到新 Cookie。上游可能走前端路由授权（无服务端跳转），"
            "此时请用浏览器打开上游站点，从 DevTools 复制完整 Cookie，"
            "再用 `python login.py --cookie \"<粘贴>\"` 直接写入凭据。"
        )

    client.record("aiagent/bootstrap", {
        "base": base,
        "seeded": out["seeded"],
        "probes": [{k: v for k, v in p.items() if k != "chain"} for p in out["probes"]],
        "oauth_params": out["oauth_params"],
        "new_cookies": sorted(set(final) - set(seeded)),
        "ok": out["ok"],
    })
    return out


def probe_cookies(cookie_header: str, base: str | None = None,
                  timeout: int = 30) -> dict:
    """仅用一串 Cookie 探测上游落地页（不依赖 SSO 会话）。

    给 `login.py --check` 与排查用：验证「手上的 Cookie 还能不能进站」。
    """
    cookies: dict[str, str] = {}
    for item in (cookie_header or "").split(";"):
        item = item.strip()
        if not item:
            continue
        name, _, value = item.partition("=")
        cookies[name.strip()] = value.strip()

    base = (base or config.AIA_BASE).rstrip("/")
    sess = _probe_session(cookies)
    try:
        r = sess.get(base + "/", allow_redirects=True, timeout=timeout)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "base": base, "sent": sorted(cookies.keys())}

    final = _join_cookies(sess)
    return {
        "ok": True,
        "base": base,
        "sent": sorted(cookies.keys()),
        "http_status": r.status_code,
        "final_url": r.url,
        "chain": _chain(r),
        "cookies": final,
        "new_cookies": sorted(set(final) - set(cookies)),
        "cookie_header": "; ".join(f"{k}={v}" for k, v in final.items()),
    }


def _complete_oauth(client, sess: requests.Session, params: dict,
                    timeout: int) -> tuple[dict, dict]:
    """用已有 SSO 会话授权取码，再在上游会话里跟随回调换会话。"""
    auth = client.authorize(params["client_id"], params["redirect_uri"],
                            params.get("scope", ""), params.get("state", ""))
    if auth.get("needs_login"):
        log("     [probe] ✗ 授权被要求重新登录（SSO 会话未复用）")
        return {"ok": False, "reason": "session_not_reused"}, {}
    location = auth.get("location") or ""
    if not location:
        log(f"     [probe] ✗ 授权未返回重定向（HTTP {auth.get('http_status')}）")
        return {"ok": False, "reason": "no_redirect", "http_status": auth.get("http_status")}, {}

    log(f"     [probe] ✓ 取到 code，跟随回调换上游会话 → {location[:80]}")
    try:
        r = sess.get(location, allow_redirects=True, timeout=timeout)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "reason": "callback_request_failed",
                "error": f"{type(exc).__name__}: {exc}"}, {}

    added = sorted(set(_join_cookies(sess)) - set(client.cookies("shu.edu.cn")))
    log(f"     [probe] 回调 HTTP {r.status_code} → {r.url[:80]}"
        + (f"（新增 Cookie: {', '.join(added)}）" if added else "（无新增 Cookie）"))
    return (
        {"ok": True, "http_status": r.status_code, "final_url": r.url, "new_cookies": added},
        {"chain": _chain(r), "location": location},
    )
