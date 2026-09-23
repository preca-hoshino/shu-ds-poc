"""流式响应转换：上游 SSE → OpenAI SSE。

上游事件格式（每帧 `event: <类型>` + `data: <JSON>`，空行分隔）：

    event: answer            ← 正文 / 推理增量，取 choices[0].delta
    event: flowNodeResponse  ← 节点统计，moduleType == "chatNode" 时带真实 token 数
                                与整段推理链（`reasoningText`）/ 完成原因（`finishReason`）
    event: flowNodeStatus    ← 节点状态，忽略

下发给客户端的帧序列**与 OpenAI 完全一致**：

    data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}
    data: {"choices":[{"index":0,"delta":{"reasoning_content":"…"},"logprobs":null,"finish_reason":null}]}
    data: {"choices":[{"index":0,"delta":{"content":"你"},"logprobs":null,"finish_reason":null}]}
    ...
    data: {"choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}
    data: {"choices":[],"usage":{...}}          ← 仅当 stream_options.include_usage
    data: [DONE]

推理链（思维链）走 DeepSeek 的 `reasoning_content`：
  - 上游在 `answer` 增量里给 `reasoning_content` → 原样转出（可能夹在正文之前）；
  - 上游只在收尾的 chatNode 统计里给 `reasoningText` → 在 `finish_reason` 帧**之前**
    补一帧 `reasoning_content`（上游不给增量时，这是唯一能拿到推理链的时机，
    客户端仍能把它渲染成思考块）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from . import fastgpt as fg
from . import schemas as sc
from . import telemetry as tm

#: SSE 帧分隔符（上游两种都可能出现）
_SEPARATORS = (b"\n\n", b"\r\n\r\n")

#: 尾帧
DONE = "data: [DONE]\n\n"


class FrameSplitter:
    """增量式 SSE 分帧器。

    用法::

        sp = FrameSplitter()
        for raw in chunks:              # 网络分片，切点任意
            for frame in sp.feed(raw):
                ...

    跨分片的残帧会被保留；同时支持 `\\n\\n` 与 `\\r\\n\\r\\n`。
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        """喂入一段字节，返回其中已完整的帧。"""
        self._buf += data
        frames: list[bytes] = []
        while True:
            hit: tuple[int, int] | None = None
            for sep in _SEPARATORS:
                idx = self._buf.find(sep)
                if idx != -1 and (hit is None or idx < hit[0]):
                    hit = (idx, len(sep))
            if hit is None:
                break
            idx, seplen = hit
            frames.append(self._buf[:idx])
            self._buf = self._buf[idx + seplen:]
        return frames

    def flush(self) -> bytes:
        """取出剩余未成帧的字节（流结束时调用）。"""
        tail, self._buf = self._buf, b""
        return tail


def split_frames(buffer: bytes) -> list[bytes]:
    """一次性切出全部完整帧（离线自检用；流式请用 `FrameSplitter`）。"""
    return FrameSplitter().feed(buffer)


def parse_event(frame: bytes) -> dict[str, Any]:
    """解析一帧：返回 `{"event": <类型>, "data": <原始 data 串>}`。"""
    event = ""
    data_lines: list[str] = []
    for line in frame.decode("utf-8", "replace").split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event:"):
            value = line[len("event:"):]
            event = value[1:] if value.startswith(" ") else value
        elif line.startswith("data:"):
            value = line[len("data:"):]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    return {"event": event, "data": "\n".join(data_lines)}


def _sse(payload: dict[str, Any]) -> str:
    """把字典编成一帧 SSE（`data: {...}\\n\\n`）。"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk(model: str, created: int, delta: dict[str, Any],
           finish_reason: str | None = None) -> str:
    """构造一个 `chat.completion.chunk`。

    `logprobs` 与 `finish_reason` 显式给 `null` —— OpenAI 的每一帧都带这两个键。
    """
    return _sse({
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
    })


def _usage_frame(model: str, created: int, usage: dict[str, Any]) -> str:
    """构造收尾的 usage 帧（`choices` 为空数组，与规范一致）。"""
    return _sse({
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": usage,
    })


class _Stats:
    """一个流里的累计状态。"""

    def __init__(self) -> None:
        self.completion_chars = 0
        self.reasoning_chars = 0
        self.actual_prompt: int | None = None
        self.actual_completion: int | None = None
        #: chatNode 统计里给的整段推理链（上游不给增量时的兜底）
        self.reasoning_fallback = ""
        #: 上游的完成原因（`stop` / `length`）
        self.upstream_finish: str | None = None

    def absorb_flow_node(self, val: dict[str, Any]) -> None:
        """从 `flowNodeResponse` 里吸收真实 token 数（仅 chatNode 节点）。"""
        if val.get("moduleType") != "chatNode":
            return
        for key, attr in (("inputTokens", "actual_prompt"),
                          ("outputTokens", "actual_completion")):
            v = val.get(key)
            if isinstance(v, (int, float)):
                setattr(self, attr, int(v))
        reasoning = val.get("reasoningText")
        if isinstance(reasoning, str) and reasoning.strip():
            self.reasoning_fallback = reasoning
        finish = val.get("finishReason")
        if isinstance(finish, str) and finish:
            self.upstream_finish = finish

    @property
    def finish_reason(self) -> str:
        """映射成规范的 `finish_reason`（上游只有 stop/length 两种有意义）。"""
        return "length" if self.upstream_finish == "length" else "stop"


def _handle_frame(frame: bytes, stats: _Stats, model: str,
                  created: int) -> list[str]:
    """处理一帧，返回要下发的 SSE 文本（无需下发时为空列表）。"""
    evt = parse_event(frame)
    etype, data_str = evt["event"], evt["data"]

    if not etype and not data_str:
        return []

    if etype == "flowNodeResponse":
        if data_str:
            try:
                val = json.loads(data_str)
            except json.JSONDecodeError:
                return []
            if isinstance(val, dict):
                stats.absorb_flow_node(val)
        return []

    if etype != "answer" or not data_str or data_str == "[DONE]":
        return []

    try:
        val = json.loads(data_str)
    except json.JSONDecodeError:
        return []

    choices = val.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    delta = choices[0].get("delta") or {}

    # `content` 上游实测是字符串；分段数组（含 reasoning 段）也认，见 fg.content_*
    raw_content = delta.get("content")
    content = fg.content_text(raw_content)
    reasoning = fg.content_reasoning(raw_content)
    extra_reasoning = delta.get("reasoning_content")
    if isinstance(extra_reasoning, str):
        reasoning += extra_reasoning
    if not content and not reasoning:
        return []

    out: dict[str, Any] = {}
    if content:
        out["content"] = content
        stats.completion_chars += len(content)
    if reasoning:
        out["reasoning_content"] = reasoning
        stats.completion_chars += len(reasoning)
        stats.reasoning_chars += len(reasoning)

    return [_chunk(model, created, out)]


async def translate_stream(byte_iter: AsyncIterator[bytes],
                           req: sc.ChatCompletionRequest,
                           model: str,
                           telemetry: tm.RequestLog | None = None) -> AsyncIterator[str]:
    """把上游字节流翻译成 OpenAI SSE 文本流。

    参数
    ----
    byte_iter
        上游响应的字节迭代器（`resp.aiter_bytes()`）。
    req
        原始请求（取 `stream_options.include_usage` 与用于估算的 prompt 字符数）。
    model
        返回给客户端的模型名（回显请求里的那个）。
    telemetry
        控制台遥测（可空）。给了就在首个内容分片下发时打点、流结束时打统计。
    """
    created = int(time.time())
    prompt_chars = sc.count_chars(req.messages)

    stats = _Stats()
    splitter = FrameSplitter()

    def tokens() -> tuple[int, int, bool]:
        """(输入, 输出, 是否为字符估算)。上游 chatNode 统计优先。"""
        estimated = stats.actual_prompt is None or stats.actual_completion is None
        return (
            stats.actual_prompt if stats.actual_prompt is not None
            else fg.estimate_tokens(prompt_chars),
            stats.actual_completion if stats.actual_completion is not None
            else fg.estimate_tokens(stats.completion_chars),
            estimated,
        )

    def note() -> None:
        """标记首个内容分片已下发（遥测用，重复调用只生效一次）。"""
        if telemetry is not None:
            telemetry.mark_first_token()

    try:
        # 首帧：OpenAI 固定先发一个只有 role 的 delta
        yield _chunk(model, created, {"role": "assistant", "content": ""})

        async for raw in byte_iter:
            for frame in splitter.feed(raw):
                for piece in _handle_frame(frame, stats, model, created):
                    note()
                    yield piece

        tail = splitter.flush()
        if tail.strip():
            for piece in _handle_frame(tail, stats, model, created):
                note()
                yield piece

        # 上游没在增量里给推理链、只在收尾统计里给了 reasoningText → 补一帧再收尾
        if stats.reasoning_fallback and not stats.reasoning_chars:
            stats.reasoning_chars = len(stats.reasoning_fallback)
            stats.completion_chars += stats.reasoning_chars
            yield _chunk(model, created, {"reasoning_content": stats.reasoning_fallback})

        # 收尾帧：finish_reason
        yield _chunk(model, created, {}, finish_reason=stats.finish_reason)

        if req.include_usage:
            prompt_tokens, completion_tokens, _ = tokens()
            yield _usage_frame(model, created, fg.build_usage(
                prompt_tokens, completion_tokens,
                fg.split_reasoning_tokens(completion_tokens, stats.reasoning_chars,
                                          stats.completion_chars - stats.reasoning_chars)))

        yield DONE
    finally:
        # 客户端中途断开时生成器被关闭，这里同样会执行 → 统计照打，不留半截日志
        if telemetry is not None:
            prompt_tokens, completion_tokens, estimated = tokens()
            telemetry.finish(prompt_tokens, completion_tokens, estimated=estimated)


def iter_stream_text(text: str) -> AsyncIterator[bytes]:
    """把一整段 SSE 文本包成字节迭代器（离线自检用）。"""
    async def _gen() -> AsyncIterator[bytes]:
        yield text.encode("utf-8")
    return _gen()
