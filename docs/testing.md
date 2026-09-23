# 自测

> 返回 [项目 README](../README.md) · [文档索引](README.md)

## 离线（不联网，秒级）

```bash
.venv\Scripts\python.exe tests\test_offline.py      # 228 项：转换 / 解析 / 规范形状 / 推理链 / 帧序列 / 真流式 / 工具调用 / 子模块契约
.venv\Scripts\python.exe tests\test_e2e_mock.py     # 69 项：HTTP 层 / 鉴权 / 错误体 / SSE
```

## 真机（需服务在跑）

```bash
python docs/scripts/test_toolcall.py           # 工具调用：单轮 / 流式 / 多轮闭环 / 多工具择一
python docs/scripts/test_nonstream.py          # 非流式
python docs/scripts/test_stream.py             # 流式（含 usage 与推理链提示）
python docs/scripts/test_openai_sdk.py         # 官方 OpenAI SDK 端到端（需 pip install openai）
python docs/scripts/check_proxy_stream.py --mimic cherry   # 流式体检：分片是否**实时**到达

# 规模压测：工具数量对准确率的影响（结论见 docs/tool-calling.md → 规模压测）
python docs/scripts/bench_toolcall.py --sizes 20,100,210 --queries 4 --repeats 3 --concurrency 2
```

## 直连上游的探针

绕过代理直接看上游真身，会用到 `.credentials.json`：

```bash
python docs/scripts/probe_upstream_raw.py -m deepseek-r1 --stream   # 原始 SSE 帧
python docs/scripts/probe_upstream_raw.py --matrix                  # 两条链 × 流式/非流式
python docs/scripts/probe_share_info.py --page                      # 分享页标题与应用线索
python docs/scripts/probe_toolcall.py                               # 接口面 / MCP 出口 / 模型能力
```

> **客户端（Cherry Studio 等）报「没有流式」时**，先跑 `check_proxy_stream.py` ——
> 它量的是**帧到达时间**：`帧到达跳度 > 0` 且 `TTFB < 总耗时` 才算真流式；
> 跳度 0 说明中间被缓冲了（httpx 便捷方法预读、或中间层攒包）。
