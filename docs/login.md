# 登录与凭据

> 返回 [项目 README](../README.md) · [文档索引](README.md)

`login.py` 负责完成 **认证 → 换 ds 会话 → 产出凭据** 的全过程，全程只需跑一次，
产出的 `.credentials.json` 供 `poc.py` 长期复用。

整体流程图与文件职责见 [README「流程解析」](../README.md#流程解析)。

## ① 认证：建立统一身份认证会话

| 步骤 | 端点 | 说明 |
| :--- | :--- | :--- |
| 登录 | `POST /oauth/userLogin` | 账号 + 密码（RSA PKCS#1 v1.5 加密后 base64 提交） |
| 发码 | `POST /oauth/twoStep/send` | `method` 取 `wecom` / `sms` |
| 校验 | `POST /oauth/twoStep/verify` | 校验通过即建立可复用的认证会话 |

账号密码（可叠加短信或企微两步验证）与企业微信扫码两条路径，均在终端内完成 ——
扫码方式直接用半块字符渲染二维码，不必打开浏览器。

这一步由子模块 [shu-sso-poc](https://github.com/preca-hoshino/shu-sso-poc) 。

## ② 换会话：ds（千学百科）

ds 的 Web 端是纯前端 SPA，换会话由它的**后端**接口完成 —— 前端只把 `code` 递过去：

```text
① GET {认证站点}/oauth/authorize
        ?response_type=code&client_id=<ds 的 client_id>
        &redirect_uri=https://ds.shu.edu.cn/login          ← 无 state、无 scope
② 302 → https://ds.shu.edu.cn/login?code=...
③ GET https://ds.shu.edu.cn/dsssologin/getSsoUser
        ?code=<授权码>&url=https://ds.shu.edu.cn/login
   ← {"isSussess": true, "datas": "{\"userid\": ...}"}     ← 判定依据
```

| 项 | 值 |
| :--- | :--- |
| `client_id` | `re0owG1g776ng2eix7x3o8sa20W6OdA2` |
| `redirect_uri` | `https://ds.shu.edu.cn/login` |
| `scope` | 空（不发送） |
| `state` | 不传（前端直接拼 URL） |

实测约束：

- **字段名确实拼作 `isSussess`**（少一个 s）—— 代码按原样比对，不要「纠正」；
- **`datas` 是 JSON 字符串而非对象** —— 前端写的是 `JSON.parse(u.datas)`，本项目同样二次解析；
- **`url` 必须与授权时的 `redirect_uri` 完全一致**，否则判 `invalid_grant`；
- **ds 的会话不在 Cookie 里** —— 返回的 `userid` 才是关键；`datas` 中一并带出学号、姓名
  与 `accessToken` / `privatekey`（原样带进上游请求的 `variables`）。

## ③ 产出：凭据文件

`.credentials.json`（权限 `0600`，已在 `.gitignore`）：

```jsonc
{
  "version": 1,
  "created_at": "2026-09-23T18:30:00",
  "username": "25123368",
  "display_name": "同学",
  "upstream": { "base": "https://aiagent.shu.edu.cn",
                "chat_path": "/api/v2/chat/completions" },
  "share_id": "49imnzpquhvt2gnj8xqxmcch",
  "cookie_header": "SHU_OAUTH2=...",                       // 上游请求直接用这串
  "cookies": { "...": "..." },
  "ds": { "userid": "25123368", "name": "同学",
          "access_token": "...", "private_key": "...", "raw": {} }
}
```

`cookie_header` 是发给对话接口的那一串；`ds` 段是换会话时 ds 后端返回的用户信息，
其中 `userid` 用作上游 `variables.userId`，`access_token` / `private_key` 一并带上。

实测约束：**上游对话接口不校验 Cookie** —— 用真实 Cookie、编造 Cookie
（`uname=x; _webvpn_key=demo`）、完全不带 Cookie 发同一请求，三种都返回 HTTP 200。
真正起作用的是公开分享链（`shareId`）与请求形状（`Referer` / `variables`），
因此登录侧拿到分享链与 ds 用户信息即可，不必再去引导上游会话。
要确认手上这份凭据还能用，直接 `python login.py --check`（真发一次最小请求）。

## 关键结论

1. **一次登录、长期复用** —— ① ② ③ 只在 `login.py` 里跑一次，产出的凭据供 `poc.py` 长期使用；
   失效后上游会报错（状态码原样透传），重新 `python login.py` 即可；
2. **各环节互不影响** —— ds 换会话失败不影响认证会话本身，`login.py` 仍会写出凭据以便排查；
   上游对话接口报错只影响当次请求（分别见 `sso/runner.py` 的 `login_all_systems()`
   与 [`proxy/upstream.py`](../proxy/upstream.py) 的 `UpstreamError`）；
3. **API 面严格等于 OpenAI 规范** —— 只暴露两个端点，字段、错误体、SSE 帧序列一律照规范，
   不出现任何自造字段（[`tests/test_offline.py`](../tests/test_offline.py) 有
   `set(resp) == RESPONSE_KEYS` 一类断言兜着）；
4. **原生 function calling 不可得，代理侧用提示词补齐** —— 上游是应用级接口，`tools` 无处可传
   （`/v1/*` 实测全 404），因此客户端带 `tools` 时由代理把它折进 system 提示词、
   按 `0:` / `1:` 协议取回，对外仍呈现规范 `tool_calls`（见 [`tool-calling.md`](tool-calling.md)）；
   思维链能否输出则取决于上游那个应用的配置，当前不输出（见 [`reasoning.md`](reasoning.md)）。
