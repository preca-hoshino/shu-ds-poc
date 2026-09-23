# API

> 返回 [项目 README](../README.md) · [文档索引](README.md)

## 端点

只有两个，除此之外**没有任何路由**（`/`、`/healthz`、`/docs`、`/redoc`、`/openapi.json`
全部 404）—— 规范里用不上的端点一律不开：

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `POST` | `/v1/chat/completions` | 对话（`stream: true` 走 SSE） |
| `GET` | `/v1/models` | 模型列表 |

## 鉴权

```
Authorization: Bearer <api key>
```

Key 取 `--api-key`，缺省读环境变量 `PROXY_API_KEY`，再缺省用 `sk-shu-secret-key-12345`。

## 模型

| 请求里的名字 | 归一到 | 说明 |
| :--- | :--- | :--- |
| `deepseek-v3` | v3 | 默认分享链 |
| `deepseek-chat` | v3 | DeepSeek 官方叫法 |
| `deepseek-r1` | r1 | R1 独立分享链 |
| `deepseek-reasoner` | r1 | DeepSeek 官方叫法 |

名字不区分大小写；其它名字一律 404 `model_not_found`（与 OpenAI 一致），
响应的 `model` 字段**回显请求里的名字**。

## 请求字段

规范里定义的字段全部接受，但真正下发给上游的只有四个：

| 字段 | 下游效果 |
| :--- | :--- |
| `model` | 决定分享链（R1 走独立分享链） |
| `messages` | 转成上游 `messages`（含 `system`，不裁内容）；**带 `tools` 时按协议改写**，见下 |
| `stream` | 上游 `stream` |
| `user` | 生成上游 `outLinkUid = shareChat-<user>` |

`tools` 走另一条路 —— 上游不接受它，代理改为**折进 system 提示词**再解析回来：

| 字段 | 效果 |
| :--- | :--- |
| `tools` | 工具声明注入 system；模型的 `1:` 输出被解析成规范 `tool_calls`（见 [`tool-calling.md`](tool-calling.md)） |
| `tool_choice` | 只有 `"none"` 有意义（关掉工具模式）；`"auto"` / `"required"` / 具名一律当 `"auto"` |

其余（`temperature` / `top_p` / `max_tokens` / `stop` / `n` / `seed` /
`response_format` / `logprobs` / `parallel_tool_calls` …）**接受但不下发** ——
上游接口不接受这些参数，发过去只会被忽略或报错。它们在 [`proxy/schemas.py`](../proxy/schemas.py)
里显式声明，便于对着规范核对。

规范之外的字段（供应商扩展）同样接受并忽略；用 `--log-level debug` 可以看到
哪些参数没生效（由 [`proxy/chat.py`](../proxy/chat.py) 的 `_log_ignored()` 记录）。

`stream_options.include_usage` 按规范生效：流式响应末尾会多一个 usage 帧。

## 响应结构

非流式（字段与 OpenAI 完全一致）：

```jsonc
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1790159602,
  "model": "deepseek-v3",                    // 回显请求值
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "答案",
      "reasoning_content": "..."             // 见 reasoning.md
    },
    "logprobs": null,
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 2,
    "completion_tokens": 48,
    "total_tokens": 50,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0}
  }
}
```

流式帧序列（与 OpenAI 一致）：

```text
data: {"...","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}
data: {"...","choices":[{"index":0,"delta":{"reasoning_content":"先想…"},"logprobs":null,"finish_reason":null}]}   ← 上游有推理链才有
data: {"...","choices":[{"index":0,"delta":{"content":"天空"},"logprobs":null,"finish_reason":null}]}
...
data: {"...","choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}
data: {"...","choices":[],"usage":{...}}      ← 仅当 stream_options.include_usage
data: [DONE]
```

## 错误结构

```jsonc
{"error": {"message": "...", "type": "...", "param": null, "code": "..."}}
```

| 场景 | HTTP | `type` | `code` | `param` |
| :--- | :--- | :--- | :--- | :--- |
| Key 缺失/错误 | 401 | `invalid_request_error` | `invalid_api_key` | `null` |
| 请求体非 JSON / 校验失败 | 400 | `invalid_request_error` | `null` | 出错字段路径 |
| 模型不存在 | 404 | `invalid_request_error` | `model_not_found` | `model` |
| 上游报错 | **沿用上游状态码** | `api_error` | `null` | `null` |
| 其它异常 | 500 | `server_error` | `null` | `null` |

上游原文会带在 `message` 里，方便直接读出「分享链失效 / 参数不对」这类原因。
