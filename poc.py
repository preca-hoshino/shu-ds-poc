#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主脚本：起一个 OpenAI 兼容 API，把请求转到上游（aiagent.shu.edu.cn）。

    读凭据(.credentials.json) → 起 FastAPI →
    客户端 POST /v1/chat/completions → 转换 → 上游 → 转回 OpenAI 格式

只暴露两个端点（与 OpenAI 一致）：
    POST /v1/chat/completions
    GET  /v1/models

用法:
    python poc.py                      # 默认 127.0.0.1:3000
    python poc.py --port 8080 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import os
import sys

from proxy import credentials as creds_mod
from proxy import server as server_mod

DEFAULT_PORT = 3000
DEFAULT_HOST = "127.0.0.1"
DEFAULT_API_KEY = "sk-shu-secret-key-12345"


def env_api_key() -> str:
    """API Key 取值：环境变量 > 默认。"""
    return os.getenv("PROXY_API_KEY") or DEFAULT_API_KEY


def banner(args, creds: dict) -> None:
    """打印启动信息（**不含 Cookie 明文**）。"""
    cookie = creds_mod.cookie_header(creds)
    cookie_items = len([c for c in cookie.split(";") if c.strip()]) if cookie else 0
    base = args.base or creds_mod.upstream_base(creds)

    print("=" * 62)
    print(" SHU DeepSeek Proxy — OpenAI 兼容 API")
    print("=" * 62)
    print(f" 上游     : {base.rstrip('/')}{creds_mod.upstream_chat_path(creds)}")
    print(f" 凭据     : {args.credentials or creds_mod.DEFAULT_PATH}")
    print(f"           账号={creds.get('username') or '?'}"
          f"  生成于 {creds_mod.summarize_age(creds)}  Cookie {cookie_items} 项")
    print(f" 监听     : http://{args.host}:{args.port}")
    print(f" API Key  : {args.api_key}")
    print(" 端点     : POST /v1/chat/completions")
    print("           GET  /v1/models")
    print("=" * 62)

    age = creds_mod.age_hours(creds)
    if age is not None and age > 48:
        print(f" ! 凭据生成于 {creds_mod.summarize_age(creds)}，可能已过期；"
              f"若客户端收到 401/403 请重新运行 python login.py")
        print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OpenAI 兼容 API → aiagent.shu.edu.cn")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"监听端口（默认 {DEFAULT_PORT}）")
    p.add_argument("--api-key", default=env_api_key(),
                   help="本服务的 API Key（默认读 PROXY_API_KEY 环境变量）")
    p.add_argument("--credentials", default=None,
                   help=f"凭据文件路径（默认 {creds_mod.DEFAULT_PATH}）")
    p.add_argument("--base", default=None, help="覆盖上游站点根地址")
    p.add_argument("--timeout", type=float, default=300.0, help="上游请求超时秒数")
    p.add_argument("--insecure", action="store_true",
                   help="跳过上游 TLS 证书校验（默认校验）")
    p.add_argument("--log-level", default="info",
                   choices=["critical", "error", "warning", "info", "debug", "trace"])
    p.add_argument("--access-log", action="store_true", help="打印每条请求的访问日志")
    return p


def main() -> int:
    args = build_parser().parse_args()

    try:
        creds = creds_mod.load(args.credentials)
    except creds_mod.CredentialsError as exc:
        print()
        print("=" * 62)
        print(" 凭据不可用")
        print("=" * 62)
        print(f" {exc}")
        print("=" * 62)
        return 9

    banner(args, creds)

    try:
        import uvicorn
    except ImportError:
        print("  ✗ 未安装 uvicorn：pip install -r requirements.txt")
        return 2

    app = server_mod.make_app(creds, api_key=args.api_key, base=args.base,
                              timeout=args.timeout, verify=not args.insecure)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level,
                access_log=args.access_log)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止")
        sys.exit(130)
