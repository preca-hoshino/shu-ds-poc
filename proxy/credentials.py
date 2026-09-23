"""凭据文件（`.credentials.json`）的读写与取值：环境变量 > 文件 > 默认值。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from sso.config import DEFAULT_SHARE_ID

# 项目根（proxy/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / ".credentials.json"

_ENV_COOKIE = "SHU_COOKIE"
_ENV_SHARE_ID = "SHU_SHARE_ID"
_ENV_USER = "SHU_USER_ID"
_ENV_ACCESS_TOKEN = "SHU_ACCESS_TOKEN"
_ENV_PRIVATE_KEY = "SHU_PRIVATE_KEY"


class CredentialsError(RuntimeError):
    """凭据缺失或不可用。"""


def load(path: str | Path | None = None) -> dict:
    """读取凭据文件；不存在或不可解析时抛 `CredentialsError`（附下一步提示）。"""
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        raise CredentialsError(
            f"未找到凭据文件：{p}\n"
            f"  请先运行登录脚本生成本次凭据： python login.py"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                           # noqa: BLE001
        raise CredentialsError(
            f"凭据文件无法解析（{type(exc).__name__}: {exc}）：{p}\n"
            f"  删掉它重新登录即可： python login.py"
        ) from exc
    if not isinstance(data, dict):
        raise CredentialsError(f"凭据文件格式不对（应为 JSON 对象）：{p}")
    return data


def save(payload: dict, path: str | Path | None = None) -> Path:
    """写入凭据文件（0600 权限，不进版本库）。"""
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return p


def _mask(value: str, keep: int = 6) -> str:
    """逐项脱敏（本地实现，免得 proxy 为了打日志把 cryptography 也拖进来）。"""
    return value[:keep] + "..." + value[-keep:] if len(value) > keep * 2 else value


def describe(creds: dict | None, path: str | Path | None = None) -> str:
    """一行摘要（Cookie 逐项脱敏），给 `login.py --check` 这类诊断用。"""
    creds = creds or {}
    p = Path(path) if path else DEFAULT_PATH
    items = [c.strip() for c in cookie_header(creds, path).split(";") if c.strip()]
    masked = "; ".join(f"{name}={_mask(value)}"
                       for name, _, value in (i.partition("=") for i in items))
    return (f"文件 {p} ｜ 账号 {creds.get('username') or '(未知)'}"
            f" ｜ 上游 {upstream_base(creds)}"
            f" ｜ Cookie {len(items)} 项：{masked or '(空)'}"
            f" ｜ 生成于 {creds.get('created_at') or '?'}")


# ---- 取值 ---------------------------------------------------------------

def cookie_header(creds: dict | None, path: str | Path | None = None) -> str:
    """上游请求该用的 `Cookie:` 头。"""
    env = os.getenv(_ENV_COOKIE)
    if env:
        return env.strip()
    if creds is None:
        creds = load(path)
    return (creds.get("cookie_header") or "").strip()


def share_id(creds: dict | None) -> str:
    """默认分享 id（R1 模型会被 transform 换成固定值）。"""
    return (os.getenv(_ENV_SHARE_ID)
            or (creds or {}).get("share_id")
            or DEFAULT_SHARE_ID)


def upstream_base(creds: dict | None) -> str:
    """上游站点根地址。"""
    up = (creds or {}).get("upstream") or {}
    return (os.getenv("SHU_UPSTREAM_BASE") or up.get("base") or "https://aiagent.shu.edu.cn").rstrip("/")


def upstream_chat_path(creds: dict | None) -> str:
    """上游对话接口路径。"""
    up = (creds or {}).get("upstream") or {}
    return up.get("chat_path") or "/api/v2/chat/completions"


def user_id(creds: dict | None) -> str:
    """学号/工号（用于 FastGPT 的 `variables.userId`）。"""
    if os.getenv(_ENV_USER):
        return os.getenv(_ENV_USER, "").strip()
    ds = (creds or {}).get("ds") or {}
    return str(ds.get("userid") or (creds or {}).get("username") or "")


def access_token(creds: dict | None) -> str:
    """ds 后端下发的 accessToken（按抓包结果原样回传）。"""
    if os.getenv(_ENV_ACCESS_TOKEN):
        return os.getenv(_ENV_ACCESS_TOKEN, "").strip()
    return str(((creds or {}).get("ds") or {}).get("access_token") or "")


def private_key(creds: dict | None) -> str:
    """ds 后端的 privatekey（同上）。"""
    if os.getenv(_ENV_PRIVATE_KEY):
        return os.getenv(_ENV_PRIVATE_KEY, "").strip()
    return str(((creds or {}).get("ds") or {}).get("private_key") or "")


# ---- 诊断 ---------------------------------------------------------------

def age_hours(creds: dict) -> float | None:
    """凭据生成至今的小时数；无时间戳时返回 None。"""
    ts = creds.get("created_at")
    if not ts:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
    except Exception:
        return None


def summarize_age(creds: dict) -> str:
    """把凭据生成时间说成人话（用于启动提示）。"""
    age = age_hours(creds)
    if age is None:
        return "未知"
    if age < 1:
        return f"{age * 60:.0f} 分钟前"
    if age < 48:
        return f"{age:.1f} 小时前"
    return f"{age / 24:.1f} 天前"


__all__ = [
    "CredentialsError", "DEFAULT_PATH", "DEFAULT_SHARE_ID", "load", "save",
    "cookie_header", "share_id", "upstream_base", "upstream_chat_path",
    "user_id", "access_token", "private_key", "age_hours", "summarize_age",
]
