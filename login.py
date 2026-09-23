#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录脚本：完成统一身份认证 → 换取 ds 会话 → 产出凭据 `.credentials.json`。

流程（认证部分由子模块 shu-sso-poc 实现，本项目只把「批量登录多系统」收敛到
「拿到代理要用的凭据」）：

    ① 认证    账号密码（RSA 加密）+ 可选两步验证   —— 或 ——   企业微信扫码
    ② 换会话  GET /oauth/authorize 取 code → ds 后端 getSsoUser 换 token
    ③ 引导    用已建立的 shu.edu.cn Cookie 探一次 aiagent.shu.edu.cn，
              尽量把它自己下发的 Cookie 也收进来（失败只记录，不中断）
    ④ 落盘    写出 `.credentials.json`（0600），供 poc.py 读取

安全：密码经 getpass 读取，不落盘、不打印；证据 JSON 走脱敏，
      凭据文件含 Cookie（等价于登录态），已在 .gitignore 中忽略。

用法:
    python login.py                       # 交互式（推荐）
    python login.py --login password      # 账号密码 + 两步验证
    python login.py --login wecom_scan    # 企业微信扫码
    python login.py --method sms          # 账号密码模式指定 2FA 方式
    python login.py --cookie "uname=..."  # 手工粘贴 Cookie（跳过认证环节的兜底路径）
    python login.py --check               # 用已有凭据复探上游，不重新登录
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime

from proxy import credentials as creds_mod
from sso import config
from sso.aiagent import bootstrap_aiagent, probe_cookies
from sso.client import ShuSSO
from sso.runner import login_all_systems, save_evidence, session_params
from sso.ui import (banner, choose_login_mode, print_error_hint, print_summary,
                    print_wecom_qr)
from sso.utils import (log, mask_cookie_header, rsa_encrypt_password, save_json)


# ---------------------------------------------------------------------------
# 凭据组装
# ---------------------------------------------------------------------------

def _pick(d: dict, *keys: str) -> str:
    """按候选键名（大小写不敏感）从 dict 里取第一个非空字符串值。"""
    lowered = {str(k).lower(): v for k, v in (d or {}).items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def build_credentials(client: ShuSSO, ds_result: dict, aia: dict,
                      args, username: str = "") -> dict:
    """把本次登录的成果整理成凭据结构。"""
    sso_cookies = client.cookies("shu.edu.cn") or client.cookies()
    sso_header = "; ".join(f"{k}={v}" for k, v in sso_cookies.items())

    ds_result = ds_result or {}
    raw = ds_result.get("raw_user") or {}

    # 上游请求优先用「探测后」的 Cookie（含上游自己下发的那部分），
    # 探测失败时退回认证侧 Cookie —— 至少让 poc.py 能发出请求便于排查。
    cookie_header = (aia or {}).get("cookie_header") or sso_header

    return {
        "version": 1,
        "created_at": datetime.now().isoformat(),
        "tenant": args.tenant,
        "username": username or str(ds_result.get("user_id") or ""),
        "display_name": str(ds_result.get("username") or ""),
        "upstream": {
            "base": args.base,
            "chat_path": config.AIA_CHAT_PATH,
        },
        "share_id": os.getenv("SHU_SHARE_ID") or config.DEFAULT_SHARE_ID,
        "cookie_header": cookie_header,
        "cookies": (aia or {}).get("cookies") or sso_cookies,
        "sso_cookies": sso_cookies,
        "ds": {
            "userid": _pick(raw, "userid", "userId") or ds_result.get("user_id"),
            "name": _pick(raw, "username", "name") or ds_result.get("username"),
            "access_token": _pick(raw, "accessToken", "access_token"),
            "private_key": _pick(raw, "privatekey", "privateKey", "private_key"),
            "raw": raw,
        },
        "aiagent": {
            "ok": bool((aia or {}).get("ok")),
            "seeded": (aia or {}).get("seeded") or [],
            "oauth_params": (aia or {}).get("oauth_params"),
            "hint": (aia or {}).get("hint"),
        },
    }


def write_credentials(args, payload: dict) -> int:
    """落盘凭据并打印摘要；返回退出码（0 = 写成功）。"""
    try:
        path = creds_mod.save(payload, args.out)
    except Exception as exc:                           # noqa: BLE001
        log(f"\n  ✗ 凭据写入失败（{type(exc).__name__}: {exc}）")
        return 9

    aia_ok = payload["aiagent"]["ok"]
    log("")
    log("=" * 62)
    log(" 凭据已生成")
    log("=" * 62)
    log(f"   文件       : {path}")
    log(f"   账号       : {payload['username'] or '(未知)'}"
        f"{'  ' + payload['display_name'] if payload['display_name'] else ''}")
    log(f"   上游       : {payload['upstream']['base']}")
    log(f"   Cookie 项数: {len([c for c in payload['cookie_header'].split(';') if c.strip()])}")
    log(f"   上游会话   : {'✓ 已换到上游 Cookie' if aia_ok else '✗ 仅 SSO Cookie（见下方提示）'}")
    log("=" * 62)
    if payload["aiagent"].get("hint"):
        log("")
        log(f"  ! {payload['aiagent']['hint']}")
    log("")
    log(" 下一步：.venv/Scripts/python.exe poc.py    # 起 OpenAI 兼容服务")
    log("         .venv/Scripts/python.exe login.py --check   # 复探上游，确认凭据可用")
    return 0


def build_evidence(client: ShuSSO, results: dict, aia: dict, args,
                   username: str = "", **extra) -> None:
    """把本次运行的脱敏证据写入 captures/。"""
    path = save_evidence("login-result.json", client, results,
                         tenant=args.tenant, username=username,
                         aiagent={k: v for k, v in (aia or {}).items()
                                  if k in ("base", "seeded", "ok", "hint",
                                           "oauth_params", "probes")},
                         **extra)
    log(f"\n证据已保存: {path}（密码 / 授权码 / 令牌已脱敏）")


# ---------------------------------------------------------------------------
# 收尾：换会话 → 探上游 → 落盘
# ---------------------------------------------------------------------------

def finalize(client: ShuSSO, args, username: str = "", **evidence_extra) -> int:
    """登录成功后的公共收尾：登录业务系统 → 探上游 → 写凭据。"""
    results = login_all_systems(client, username=username)
    print_summary(results)

    ds_result = results.get("ds") or {}
    if not ds_result.get("logged_in"):
        log("\n  ! ds 未登录成功 —— 业务系统换会话失败，但认证会话本身可能仍然可用；"
            "下面继续探上游并写出凭据，便于排查。")

    aia: dict = {"ok": False}
    if args.no_bootstrap:
        log(f"\n[上游] 已按 --no-bootstrap 跳过 {args.base} 探测")
    else:
        log(f"\n[上游] 探测 {args.base} 并尝试建立会话 ...")
        aia = bootstrap_aiagent(client, base=args.base)

    payload = build_credentials(client, ds_result, aia, args, username=username)
    build_evidence(client, results, aia, args, username=username, **evidence_extra)
    return write_credentials(args, payload)


# ---------------------------------------------------------------------------
# 入口 ①：账号密码
# ---------------------------------------------------------------------------

def password_flow(args) -> int:
    """返回退出码：0 成功，5 部分失败，1~4 认证阶段失败。"""
    username = input("学号/工号: ").strip()
    if not username:
        print("错误：学号不能为空")
        return 1

    password = getpass.getpass("密码（输入时不显示）: ")
    if not password:
        print("错误：密码不能为空")
        return 1

    method = args.method
    if not method:
        print("\n请选择两步验证方式：")
        print("  [1] 企业微信 (wecom)")
        print("  [2] 手机短信 (sms)")
        choice = input("输入 1 或 2 [默认 1]: ").strip() or "1"
        method = "sms" if choice == "2" else "wecom"

    client = ShuSSO(tenant=args.tenant)
    login_params = session_params()      # params 必填（缺了直接返回 badRequestParams）

    # ---------- 第 1 步：登录 ----------
    log("\n[OAuth ②｜用户认证] POST /oauth/userLogin (RSA 加密密码)")
    login_data = client.login(username, password, login_params)
    msg = login_data.get("message")
    if msg != "success":
        log(f"  ✗ 登录失败：{msg}")
        save_json("login-01-failed.json",
                  {"username": username, "response": login_data, "trace": client.trace})
        print_error_hint(msg)
        return 2
    log("  ✓ 认证成功")

    # ---------- 第 2 步：两步验证 ----------
    wecom: dict = {}
    if login_data.get("twoStepRequired"):
        methods = login_data.get("twoStepMethods") or {}
        log(f"  → 需要两步验证，可用：{', '.join(methods.keys()) or '(无)'}")
        if method not in methods:
            log(f"  ! 所选 '{method}' 不可用，改用 {list(methods)[0] if methods else 'sms'}")
            method = list(methods)[0] if methods else "sms"

        log(f"[OAuth ②｜2FA] POST /oauth/twoStep/send (方式: {method})")
        send_resp = client.send_2fa_code(method)
        if send_resp.get("message") != "success":
            log(f"  ✗ 发码失败：{send_resp.get('message')}")
            save_json("login-02-send-code-failed.json",
                      {"method": method, "response": send_resp, "trace": client.trace})
            return 3
        log("  ✓ 验证码已发送")

        code = input("请输入收到的验证码: ").strip()
        if not code:
            print("错误：验证码不能为空")
            return 3

        log("[OAuth ②｜2FA] POST /oauth/twoStep/verify")
        verify_resp = client.verify_2fa_code({
            "username": username,
            "password": rsa_encrypt_password(password),
            "tenantId": args.tenant,
            "params": login_params,
        }, code, method)
        if verify_resp.get("message") != "success":
            log(f"  ✗ 验证失败：{verify_resp.get('message')}")
            save_json("login-03-verify-failed.json",
                      {"method": method, "response": verify_resp, "trace": client.trace})
            print_error_hint(verify_resp.get("message"))
            return 4
        log("  ✓ 验证通过，SSO 会话已建立")
    else:
        log("  ✓ 登录成功（无需两步验证），SSO 会话已建立")

    log("  ✓ 已建立统一身份认证会话")

    # ---------- 企微确认页（纯展示，失败不影响后续） ----------
    if method == "wecom":
        try:
            wecom = client.wecom_qrcode_info(state=login_params)
        except Exception as exc:                       # noqa: BLE001 - 展示环节，不致命
            log(f"\n[企微扫码] 获取二维码信息失败（{type(exc).__name__}: {exc}），跳过展示")
            wecom = {}
        if wecom.get("key"):
            log("\n[企微扫码] 本次会话的二维码与唤起链接：")
            print_wecom_qr(wecom["confirm_url"], args)
            log(f"     二维码图片 : {wecom['qr_img_url']}")
            log(f"     确认页地址 : {wecom['confirm_url']}")
            log(f"     URI 跳转   : {wecom['wxwork_scheme']}")

    return finalize(client, args, username=username,
                    two_factor_method=method, wecom_qrcode=wecom)


# ---------------------------------------------------------------------------
# 入口 ②：企业微信扫码
# ---------------------------------------------------------------------------

_SCAN_STATUS_TEXT = {
    "QRCODE_SCAN_NEVER": "等待扫码",
    "QRCODE_SCAN_ING": "已扫码，请在手机上确认",
    "QRCODE_SCAN_SUCC": "已确认，正在换取会话",
    "QRCODE_SCAN_ERR": "二维码已过期",
}


def wecom_scan_flow(args) -> int:
    """返回退出码：0 成功，5 部分失败，6~8 扫码阶段失败。"""
    client = ShuSSO(tenant=args.tenant)

    # state 用本次会话参数（与上游前端的 WwLogin 一致）
    state = session_params()

    log("\n[企微扫码] 请求 qrConnect，生成本次会话二维码 ...")
    info = client.wecom_qrcode_info(state=state)
    if not info.get("key"):
        log(f"  ✗ 未能解析出 key（HTTP {info.get('http_status')}）")
        save_json("login-00-wecom-scan-failed.json",
                  {"response": info, "trace": client.trace})
        return 6

    line = "─" * 62
    print()
    print(line)
    if print_wecom_qr(info["confirm_url"], args):
        print(" 请用企业微信扫描下方二维码，并在手机上点「确认登录」")
    else:
        print(" 请用企业微信扫码并在手机上点「确认登录」（二维码见下方 ①）")
    print(line)
    print()
    print(" ① 二维码图片 URL（浏览器打开即可扫码）")
    print(f"    {info['qr_img_url']}")
    print()
    print(" ② 二维码内容 = 扫码后企微打开的确认页")
    print(f"    {info['confirm_url']}")
    print()
    print(" ③ 包装后的 URI 跳转（在企微内直接打开该确认页）")
    print(f"    {info['wxwork_scheme']}")
    print(line)
    print(f" 等待扫码确认（最多 {args.scan_timeout} 秒，Ctrl+C 可中断）...")

    wait = client.wecom_wait_scan(
        info["key"], state=state, timeout=args.scan_timeout,
        poll_log=lambda status, _code: log(f"     {_SCAN_STATUS_TEXT.get(status, status)}"))

    if not wait.get("ok"):
        log(f"  ✗ 未取得 auth_code（{wait.get('reason') or wait.get('status')}）")
        save_json("login-00-wecom-scan-failed.json",
                  {"wait": wait, "wecom_qrcode": info, "trace": client.trace})
        return 7

    log("  ✓ 已取得 auth_code，换取 SSO 会话 ...")
    red = client.wecom_redeem(wait["auth_code"], state)
    if not red["ok"]:
        log(f"  ✗ 换取会话失败（HTTP {red['http_status']}）")
        save_json("login-00-wecom-redeem-failed.json",
                  {"redeem": red, "trace": client.trace})
        return 8

    log("  ✓ 统一身份认证会话已建立")
    log(f"     → {red['location'][:90]}")

    # 扫码模式拿不到学号，ds 换会话后由 build_credentials 回填
    code = finalize(client, args, tenant=args.tenant, mode="wecom_scan", state=state,
                    wecom_qrcode=info, wait=wait, redeem=red)
    return code


# ---------------------------------------------------------------------------
# 入口 ③：手工 Cookie / 复探
# ---------------------------------------------------------------------------

def cookie_flow(args) -> int:
    """跳过 SSO，直接把手工粘贴的 Cookie 写成凭据（兜底路径）。"""
    header = (args.cookie or "").strip()
    if not header:
        print("错误：--cookie 不能为空")
        return 1

    log("\n[手工 Cookie] 探测上游，确认这串 Cookie 可用 ...")
    probe = probe_cookies(header, base=args.base)
    if not probe.get("ok"):
        log(f"  ✗ 探测失败：{probe.get('error')}")
        return 6
    log(f"  ✓ HTTP {probe['http_status']} → {probe['final_url'][:80]}")
    log(f"     种入 {len(probe['sent'])} 项，探测后 {len(probe['cookies'])} 项"
        f"（新增：{', '.join(probe['new_cookies']) or '无'}）")
    log(f"     Cookie: {mask_cookie_header(probe['cookie_header'])}")

    payload = {
        "version": 1,
        "created_at": datetime.now().isoformat(),
        "tenant": args.tenant,
        "username": os.getenv("SHU_USER_ID") or "",
        "display_name": "",
        "upstream": {"base": args.base, "chat_path": config.AIA_CHAT_PATH},
        "share_id": os.getenv("SHU_SHARE_ID") or config.DEFAULT_SHARE_ID,
        "cookie_header": probe["cookie_header"],
        "cookies": probe["cookies"],
        "sso_cookies": {},
        "ds": {"userid": "", "name": "", "access_token": "", "private_key": "", "raw": {}},
        "aiagent": {"ok": bool(probe["new_cookies"]),
                    "seeded": probe["sent"],
                    "oauth_params": None,
                    "hint": "手工 Cookie 模式：若上游接口报鉴权失败，请重新抓包更新。"},
        "source": "manual-cookie",
    }
    save_json("login-manual-cookie.json",
              {"probe": probe, "cookie_header": probe["cookie_header"]})
    return write_credentials(args, payload)


def check_flow(args) -> int:
    """用已有凭据复探上游（不重新登录），判断凭据是否还能用。"""
    try:
        creds = creds_mod.load(args.out)
    except creds_mod.CredentialsError as exc:
        log(f"\n  ✗ {exc}")
        return 9

    log("\n[复探] 用已有凭据探上游 ...")
    log(f"  {creds_mod.describe(creds, args.out)}")

    probe = probe_cookies(creds_mod.cookie_header(creds, args.out), base=args.base)
    if not probe.get("ok"):
        log(f"  ✗ 探测失败：{probe.get('error')}")
        return 6
    log(f"  ✓ HTTP {probe['http_status']} → {probe['final_url'][:80]}")
    if probe["new_cookies"]:
        log(f"     上游下发了新 Cookie：{', '.join(probe['new_cookies'])}")
        log("     （如需更新凭据，重新运行 python login.py）")

    log("\n 提示：Cookie 能不能真的用，最终要看上游接口的返回；")
    log("       跑一次 docs/scripts/test_nonstream.py 最准（会真发一次最小请求）。")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="登录上海大学统一身份认证，生成 DeepSeek 代理可用的凭据")
    p.add_argument("--tenant", default=config.DEFAULT_TENANT,
                   help=f"院校（默认 {config.DEFAULT_TENANT}）")
    p.add_argument("--login", choices=["password", "wecom_scan"],
                   help="登录方式（省略则交互选择）")
    p.add_argument("--method", choices=["wecom", "sms"],
                   help="账号密码登录时的 2FA 方式（省略则交互选择）")
    p.add_argument("--scan-timeout", type=int, default=180,
                   help="企微扫码等待秒数（默认 180）")
    p.add_argument("--no-qr", action="store_true",
                   help="不在终端渲染二维码（默认渲染，可直接用手机扫）")
    p.add_argument("--qr-style", choices=["block", "ascii"], default="block",
                   help="二维码样式：block=ANSI 半块（默认）；ascii=无颜色整块")
    p.add_argument("--out", default=None,
                   help=f"凭据输出路径（默认 {creds_mod.DEFAULT_PATH}）")
    p.add_argument("--base", default=config.AIA_BASE,
                   help=f"上游站点根地址（默认 {config.AIA_BASE}）")
    p.add_argument("--no-bootstrap", action="store_true",
                   help="跳过对上游站点的探测（只拿 SSO 侧 Cookie）")
    p.add_argument("--cookie", default=None,
                   help="手工粘贴的 Cookie 串（跳过 SSO 登录，兜底路径）")
    p.add_argument("--check", action="store_true",
                   help="用已有凭据复探上游，不重新登录")
    return p


def main() -> int:
    args = build_parser().parse_args()
    banner()

    if args.check:
        return check_flow(args)
    if args.cookie:
        return cookie_flow(args)

    mode = choose_login_mode(args)
    if mode == "wecom_scan":
        return wecom_scan_flow(args)
    return password_flow(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
