"""工具函数：RSA 加密、参数编码、脱敏、日志。

本地实现，与子模块 `src/utils.py` 的两点差别：
- `redact()` 抹得更全（令牌 / Cookie / 会话 Cookie 名，见 `_SECRET_KEYS`）；
- 多了 `write_private_json()`（凭据不脱敏、0600）与 `mask_cookie_header()`（日志用）。
RSA 公钥抓取仍走子模块的 `sso.rsa_key`（= `src.rsa_key`）。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config, rsa_key


def rsa_encrypt_password(plain: str) -> str:
    """用认证站点的 RSA 公钥加密密码（PKCS#1 v1.5），返回 base64 字符串。"""
    pub = serialization.load_pem_public_key(rsa_key.public_key_pem().encode())
    ciphertext = pub.encrypt(plain.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode()


def b64_params(oauth_params: dict) -> str:
    """把 OAuth 参数编码成上游前端使用的 **base64url（去掉 = 填充）**。

    例：`eyJyZXNwb25zZVR5cGUiOiJjb2RlIiwi...`（含 `_` 不含 `/`，无 padding）。
    注意与 WebVPN 的 state 不同 —— 那个是标准 base64（带填充）。
    """
    raw = json.dumps(oauth_params, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode().rstrip("=")


# URL 查询串里的一次性授权码：?code=xxx / &auth_code=xxx
_CODE_IN_URL_RE = re.compile(
    r"([?&](?:code|auth_?code|authorization_?code)=)[^&\s\"'>]+", re.IGNORECASE)

# 落盘前需要抹掉的敏感字段名（小写比较）
_SECRET_KEYS = {
    "password", "code",
    "accesstoken", "access_token", "privatekey", "private_key",
    "authorization", "cookie", "cookies", "cookie_header", "set-cookie",
    # Cookie **名**也可能是键（证据里的 cookies 是 {名: 值}），会话 Cookie 尤其要抹
    "shu_oauth2",
}


def redact(obj):
    """递归剔除敏感字段，避免密码/授权码/令牌写入证据文件。

    URL 查询串里的 `?code=xxx` 也要处理，否则 location / authorize_url
    这类字段会把一次性授权码原样写进证据文件。
    凭据本身不经过这里 —— 它用 `write_private_json()` 完整落盘（0600）。
    """
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if k.lower() in _SECRET_KEYS else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        return _CODE_IN_URL_RE.sub(r"\1***REDACTED***", obj)
    return obj


def save_json(name: str, payload) -> Path:
    """把脱敏后的 payload 写入 captures/，返回文件路径。"""
    config.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CAPTURE_DIR / name
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_private_json(path: Path, payload) -> Path:
    """把**不脱敏**的 payload 写成仅当前用户可读的文件（用于凭据）。

    与 `save_json`（脱敏、进 captures/）分开：凭据必须完整保存，
    但权限要收紧到 0600（POSIX；Windows 上 chmod 退化为只读位，可接受）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return path


def log(msg: str) -> None:
    """带 flush 的打印（便于实时看到进度）。"""
    print(msg, flush=True)


def mask(s: str, keep: int = 4) -> str:
    """脱敏显示：保留首尾各 keep 个字符，中间用 ... 代替。"""
    if not s:
        return ""
    return s[:keep] + "..." + s[-keep:] if len(s) > keep * 2 else s


def mask_cookie_header(header: str, keep: int = 6) -> str:
    """把 Cookie 头按 `name=value` 逐项脱敏，便于日志里安全展示。"""
    parts = []
    for item in (header or "").split(";"):
        item = item.strip()
        if not item:
            continue
        name, _, value = item.partition("=")
        parts.append(f"{name}={mask(value, keep)}")
    return "; ".join(parts)
