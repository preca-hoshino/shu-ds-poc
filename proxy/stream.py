"""流式响应转换：上游 SSE → OpenAI SSE。

上游事件：`answer`（正文 / 推理增量）、`flowNodeResponse`（chatNode 节点带真实
token 数、`reasoningText`、`finishReason`）、`flowNodeStatus`（忽略）。

下发帧序列与 OpenAI 一致：role 帧 → 内容 / 推理帧 → `delta:{}`+`finish_reason`
→ usage 帧（仅 `stream_options.include_usage`）→ `[DONE]`。

推理链走 `reasoning_content`：上游给增量就原样转出；只在收尾统计里给了
`reasoningText` 时，在 `finish_reason` 帧之前补一帧。

请求带 `tools` 时正文额外过一遍提示词协议过滤器（`toolcall.ReplyFilter`）：`0:` 前缀
剥掉后照常流出，`1:` 则整段缓冲成 `tool_calls` 帧。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from . import fastgpt as fg
from . import schemas as sc
from . import telemetry as tm
from . import toolcall as tc

#: SSE 帧分隔符（上游两种都可能出现）
_SEPARATORS = (b"\n\n", b"\r\n\r\n")

#: 尾帧
DONE = "data: [DONE]\n\n"


class FrameSplitter:
    """增量式 SSE 分帧器：跨分片的残帧保留，`\\n\\n` 与 `\\r\\n\\r\\n` 都认。"""

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
    """构造一个 `chat.completion.chunk`；`logprobs` / `finish_reason` 显式给 null。"""
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
        #: 提示词模式解析出的工具调用（非空时 `finish_reason` 用 `tool_calls`）
        self.tool_calls: list[dict[str, Any]] | None = None

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
        """映射成规范的 `finish_reason`（调了工具就是 `tool_calls`）。"""
        if self.tool_calls:
            return "tool_calls"
        return "length" if self.upstream_finish == "length" else "stop"


def _handle_frame(frame: bytes, stats: _Stats) -> list[dict[str, Any]]:
    """处理一帧，返回要下发的 delta 字典（无需下发时为空列表）。"""
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

    return [out]


async def translate_stream(byte_iter: AsyncIterator[bytes],
                           req: sc.ChatCompletionRequest,
                           model: str,
                           telemetry: tm.RequestLog | None = None,
                           tool_call: bool = True) -> AsyncIterator[str]:
    """把上游字节流翻译成 OpenAI SSE 文本流。

    `telemetry` 非空时：首个内容分片下发时打点，流结束时（含客户端断开）打统计。
    `tool_call` 为假时不做任何协议处理（与不带 `tools` 时行为一致）。
    """
    created = int(time.time())
    prompt_chars = sc.count_chars(req.messages) + tc.prompt_overhead(req, tool_call)

    stats = _Stats()
    splitter = FrameSplitter()
    # 请求带 tools 时，正文先过一遍 `0:` / `1:` 协议过滤器
    reply = tc.ReplyFilter(enabled=tc.enabled(req, tool_call))

    def tokens() -> tuple[int, int, bool]:
        """(输入, 输出, 是否估算)。上游 chatNode 统计优先。"""
        estimated = stats.actual_prompt is None or stats.actual_completion is None
        return (
            stats.actual_prompt if stats.actual_prompt is not None
            else fg.estimate_tokens(prompt_chars),
            stats.actual_completion if stats.actual_completion is not None
            else fg.estimate_tokens(stats.completion_chars),
            estimated,
        )

    def note() -> None:
        """标记首个内容分片已下发（重复调用只生效一次）。"""
        if telemetry is not None:
            telemetry.mark_first_token()

    def emit(delta: dict[str, Any]) -> list[str]:
        """delta → SSE 帧；`content` 要等协议过滤器放行才下发。"""
        frames: list[str] = []
        reasoning = delta.get("reasoning_content")
        if reasoning:
            frames.append(_chunk(model, created, {"reasoning_content": reasoning}))
        text = delta.get("content")
        if text:
            piece = reply.feed(text)
            if piece.text:
                frames.append(_chunk(model, created, {"content": piece.text}))
        return frames

    def drain() -> list[str]:
        """流结束：吐掉过滤器残留（工具调用在这里成帧）。

        工具调用要等整段 JSON 到齐才能解析，所以只能缓冲到最后；解析失败退回正文。
        """
        piece = reply.finish()
        if not piece.tool_calls:
            return [_chunk(model, created, {"content": piece.text})] if piece.text else []

        stats.tool_calls = piece.tool_calls
        stats.completion_chars += sum(len(c["function"]["arguments"]) for c in piece.tool_calls)
        return [_chunk(model, created, {"tool_calls": [
            {"index": i, "id": call["id"],
             "type": "function", "function": call["function"]}
        ]}) for i, call in enumerate(piece.tool_calls)]

    try:
        # 首帧：OpenAI 固定先发一个只有 role 的 delta
        yield _chunk(model, created, {"role": "assistant", "content": ""})

        async for raw in byte_iter:
            for frame in splitter.feed(raw):
                for delta in _handle_frame(frame, stats):
                    for piece in emit(delta):
                        note()
                        yield piece

        tail = splitter.flush()
        if tail.strip():
            for delta in _handle_frame(tail, stats):
                for piece in emit(delta):
                    note()
                    yield piece

        # 上游没在增量里给推理链、只在收尾统计里给了 reasoningText → 补一帧再收尾
        if stats.reasoning_fallback and not stats.reasoning_chars:
            stats.reasoning_chars = len(stats.reasoning_fallback)
            stats.completion_chars += stats.reasoning_chars
            yield _chunk(model, created, {"reasoning_content": stats.reasoning_fallback})

        # 过滤器残留（工具调用帧）—— 必须在取 finish_reason 之前
        for piece in drain():
            note()
            yield piece

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
