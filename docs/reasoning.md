# `reasoning_content` —— 思维链

> 返回 [项目 README](../README.md) · [文档索引](README.md)

`reasoning_content` 是 **DeepSeek 官方 API 定义的字段**（`deepseek-reasoner` 用它
返回推理链），不是本代理自造的，
因此原样保留并**尽力还原**。上游可能从四个位置给出推理链，四种都已支持：

| 上游形态 | 出现在 | 代理处理 |
| :--- | :--- | :--- |
| `content` 分段数组里的 `{"type":"reasoning","reasoning":{"content":"…"}}` | 非流式 | 归到 `message.reasoning_content` |
| `message.reasoning_content` 字段 | 非流式 | 同上（分段里已有推理时以分段为准，不重复） |
| `delta.reasoning_content` / `delta.content` 里的 reasoning 段 | 流式 | 原样转成 `delta.reasoning_content` 帧 |
| `flowNodeResponse`(chatNode) 的 `reasoningText` | 流式收尾 | 上游整段给、没有增量时，**在 `finish_reason` 帧之前补一帧** `reasoning_content` |

若上游两者都给了（增量 + 收尾统计），只用增量那份，不会重复。

**上游当前不给推理链（2026-09-23 实测）**：

- 两条分享链（V3 / R1）的非流式 `content` 都是**纯字符串**，没有 reasoning 段；
- 流式 `answer` 增量里只有 `content`，没有 `reasoning_content`；
- 收尾统计里的 `reasoningText` 恒为 `""` ——

所以正常情况下 `reasoning_content` 不会出现，`reasoning_tokens` 也是 0。
这是上游的行为，不是代理丢了数据；上游一旦改回上面任一形态，代理会自动归位
（[`tests/test_offline.py`](../tests/test_offline.py) 与 [`tests/test_e2e_mock.py`](../tests/test_e2e_mock.py)
里各形态都有用例兜着）。想复核就直连上游看原始帧：

```bash
python docs/scripts/probe_upstream_raw.py -m deepseek-r1 --stream    # 原始 SSE 帧
python docs/scripts/probe_upstream_raw.py --matrix                   # 两条链 × 流式/非流式 对照
```

`completion_tokens_details.reasoning_tokens` 一定有值：有真实 `completion_tokens`
时按「推理字符数 / 总字符数」的占比切分（DeepSeek 的总数本就含推理 token，
按占比切才自洽）；拿不到真实总数时按「字符数 × 2/3」估算。

## 流式细节（与上游的对应关系）

上游 SSE 只有三种事件，代理只转其中的增量：

| 上游事件 | 用途 | 代理是否下发 |
| :--- | :--- | :--- |
| `answer` | 正文 / 推理增量（`choices[0].delta`） | 是（转成 chunk） |
| `flowNodeResponse` | 节点统计：`chatNode` 帧带真实 token、`reasoningText`、`finishReason` | 否（用于 usage / 收尾） |
| `flowNodeStatus` | 节点状态（`"running"`） | 否（纯噪声） |

上游自己的收尾是「先发一帧 `delta: {"content": null}, finish_reason: "stop"`，再以
`event: answer` + `data: [DONE]` 结束」。代理不照抄，而是按规范自己构造收尾：
`delta: {}` + `finish_reason`（`stop`；上游 `finishReason` 为 `length` 时映射成 `length`）。

分片是**实时下发**的：上游逐帧到、代理逐帧转，不等整段收完（否则客户端会
「卡几秒后一次性蹦出全文」）。要确认这一点就跑 `docs/scripts/check_proxy_stream.py`，
看 `帧到达跳度` 是不是大于 0。

## usage 的真实性

| 场景 | 来源 |
| :--- | :--- |
| 流式（上游给了） | `flowNodeResponse` 中 `moduleType == "chatNode"` 帧里的 `inputTokens` / `outputTokens`，**是上游的真实值** |
| 流式（上游没给） | 「字符数 × 2/3」估算 |
| 非流式（上游给了） | 沿用上游 |
| 非流式（上游给的是占位值） | 改用估算 —— 实测上游非流式固定返回 `1/1/1`，与真实用量无关，沿用会对外给出明显错误的数字 |
