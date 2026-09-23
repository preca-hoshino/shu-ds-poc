# 命令行参数

> 返回 [项目 README](../README.md) · [文档索引](README.md)

## login.py

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--tenant` | `上海大学` | 院校标识（`tenantId`） |
| `--login {password,wecom_scan}` | 交互选择 | 登录方式 |
| `--method {wecom,sms}` | 交互选择 | 账号密码模式下的两步验证方式 |
| `--scan-timeout` | `180` | 企微扫码等待秒数 |
| `--no-qr` | 关闭 | 不在终端渲染二维码 |
| `--qr-style {block,ascii}` | `block` | 二维码渲染样式 |
| `--out` | `.credentials.json` | 凭据输出路径 |
| `--base` | `https://aiagent.shu.edu.cn` | 上游站点根地址 |
| `--cookie "<串>"` | 无 | 手工粘贴 Cookie（跳过认证环节的兜底路径） |
| `--check` | 关闭 | 用已有凭据真发一次最小请求，确认还能用（不重新登录） |

## poc.py

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `3000` | 监听端口 |
| `--api-key` | `$PROXY_API_KEY` | 本服务的 Key |
| `--credentials` | `.credentials.json` | 凭据文件路径 |
| `--base` | 凭据里的值 | 覆盖上游站点根地址 |
| `--timeout` | `300` | 上游超时秒数 |
| `--insecure` | 关闭 | 跳过上游 TLS 证书校验（默认校验） |
| `--tool-call` / `--no-tool-call` | 开启 | 提示词模式的工具调用；`--no-tool-call` 则回到「`tools` 接受但忽略」（见 [`tool-calling.md`](tool-calling.md)） |
| `--log-level` | `info` | `debug` 会打印未生效的请求参数 |
| `--access-log` | 关闭 | 打印每条请求的访问日志 |
