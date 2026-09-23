"""提示词模式的工具调用：上游不接受 `tools`，由代理折进 system 再解析回来。

协议移植自 FastGPT 的 `promptCall`：每轮输出必须以 `0:`（直接回答）或 `1:`（调用工具）
开头，`1:` 后跟 `{"name": ..., "arguments": {...}}`；工具结果以 `<ToolResponse>` 包成
user 消息回灌。客户端拿到的仍是规范的 `tool_calls` / `finish_reason: "tool_calls"`。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from . import schemas as sc

#: 协议标记：注入的 system 段以它开头（用它判断是否已注入过）
MARKER = "<ToolSkill>"

#: `0` = 直接回答，`1` = 调用工具（全角冒号也认，模型常写错）
NO_CALL = "0"
CALL = "1"

#: 追加声明里的工具清单占位符（用 replace 而不是 format，避免转义示例里的花括号）
_TOOLS_SLOT = "{tools}"

_PROMPT = """<ToolSkill>
你是一个可以调用工具的智能机器人。回答用户问题时，优先考虑用工具获取信息，而不是仅凭已有知识作答。

工具使用 JSON Schema 声明，格式为 {name: 工具名; description: 工具描述; parameters: 工具参数}，其中 name 是工具的唯一标识，parameters 包含参数名、类型、描述与必填项。

## 可用工具列表

{tools}

## 工具使用原则

- 优先调用工具：只要问题落在某个工具的用途范围内，就调用该工具。
- 涉及实时数据、校内数据、用户个人信息时，必须调用工具，不要凭记忆作答。
- 参数尽量从上下文推断；不要因为不确定参数而放弃调用或反问用户。
- 只有当问题与所有工具都明显无关时（打招呼、闲聊、纯常识问答），才用 0 直接回答。

## 输出格式

你的每次输出都必须以 0 或 1 开头，代表是否需要调用工具：
0: 不使用工具，直接回答内容。
1: 使用工具，返回工具调用的参数。

## 回答示例

0: 你好，有什么可以帮你的？
1: {"name": "get_weather", "arguments": {"city": "杭州"}}
0: 今天杭州晴，气温 31℃。

调用工具时只输出一行 JSON，不要输出任何解释；工具结果会以 <ToolResponse> 包起来发给你。
</ToolSkill>"""


# ---------------------------------------------------------------------------
# 请求侧：工具声明注入 + 消息改写
# ---------------------------------------------------------------------------


def enabled(req: sc.ChatCompletionRequest, tool_call: bool = True) -> bool:
    """是否启用提示词工具调用（服务端开关 + 有工具 + 没有显式 `tool_choice: "none"`）。"""
    return bool(tool_call) and bool(req.tools) and req.tool_choice != "none"


def tool_specs(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """只留 `name` / `description` / `parameters`，剥掉 OpenAI 的 `{"type":"function"}` 外壳。"""
    specs: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            fn = tool if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        spec: dict[str, Any] = {"name": fn["name"]}
        if fn.get("description"):
            spec["description"] = fn["description"]
        spec["parameters"] = fn.get("parameters") or {"type": "object", "properties": {}}
        specs.append(spec)
    return specs


def build_tool_prompt(tools: list[dict[str, Any]] | None) -> str:
    """生成注入 system 的协议说明（工具 JSON Schema + 输出格式 + few-shot 示例）。"""
    payload = json.dumps(tool_specs(tools), ensure_ascii=False, separators=(",", ":"))
    return _PROMPT.replace(_TOOLS_SLOT, payload)


def prompt_overhead(req: sc.ChatCompletionRequest, tool_call: bool = True) -> int:
    """注入工具协议带来的额外字符数（供 usage 估算，工具多时可达数万）。"""
    return len(build_tool_prompt(req.tools)) if enabled(req, tool_call) else 0


def _assistant_call_text(tool_calls: list[dict[str, Any]]) -> str:
    """把 assistant 的 `tool_calls` 还原成协议文本（`arguments` 要还原成对象）。"""
    fn = (tool_calls[0] or {}).get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        parsed = load_json_loose(args)
        args = parsed if parsed is not None else {}
    if not isinstance(args, dict):
        args = {}
    body = {"name": fn.get("name") or "", "arguments": args}
    return f"{CALL}: " + json.dumps(body, ensure_ascii=False)


def rewrite_messages(messages: list[sc.Message],
                     tools: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """把 OpenAI 消息改写成上游能懂的 `(role, content)` 序列。

    - `system` 追加协议说明（已有 `MARKER` 则原样保留，避免多轮重复注入）；
    - `assistant` 的 `tool_calls` → `1: {...}`，纯正文 → `0: ...`；
    - `tool` 角色 → `user` 角色 + `<ToolResponse>` 包裹（上游只认 user/assistant/system）。
    """
    out: list[tuple[str, Any]] = []
    seen_system = False

    for msg in messages:
        if msg.role == "system":
            seen_system = True
            text = sc.message_text(msg)
            if MARKER in text:
                out.append((msg.role, msg.content))
            else:
                out.append((msg.role, f"{text}\n\n{build_tool_prompt(tools)}".strip()))
            continue

        if msg.role == "assistant":
            if msg.tool_calls:
                out.append((msg.role, _assistant_call_text(msg.tool_calls)))
            else:
                out.append((msg.role, f"{NO_CALL}: {sc.message_text(msg)}"))
            continue

        if msg.role == "tool":
            out.append(("user", f"<ToolResponse>\n{sc.message_text(msg)}\n</ToolResponse>"))
            continue

        out.append((msg.role, msg.content if msg.content is not None else ""))

    if not seen_system:
        out.insert(0, ("system", build_tool_prompt(tools)))
    return out


# ---------------------------------------------------------------------------
# 响应侧：解析 `0:` / `1:` 协议
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """解析结果：`tool_calls` 非空表示调用工具，否则看 `answer`。"""

    answer: str = ""
    tool_calls: list[dict[str, Any]] | None = None


def _matching(text: str, start: int) -> int | None:
    """找与 `text[start]` 配对的闭合括号（跳过字符串里的括号与转义）。"""
    close = "}" if text[start] == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i if ch == close else None
    return None


def extract_json(text: str) -> str | None:
    """截出第一段配对的 JSON（对象或数组），失败返回 None。"""
    for start, ch in enumerate(text):
        if ch in "{[":
            end = _matching(text, start)
            if end is not None:
                return text[start:end + 1]
    return None


def _unwrap_fence(text: str) -> str | None:
    """去掉 ```json ... ``` 围栏（模型常自作聪明加围栏）。"""
    if "```" not in text:
        return None
    body = text.split("```", 2)
    if len(body) < 3:
        return None
    inner = body[1]
    if inner[:4].lower() == "json":
        inner = inner[4:]
    return inner.strip() or None


def load_json_loose(text: str) -> Any | None:
    """宽松 JSON：先原样解析，再试去掉围栏、截取第一个配对值。"""
    stripped = (text or "").strip()
    for candidate in (stripped, _unwrap_fence(stripped), extract_json(stripped)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def coerce_call(item: Any) -> dict[str, Any] | None:
    """把模型给的一项归一成规范 `tool_call`；认不出名字则返回 None。"""
    if not isinstance(item, dict):
        return None
    fn = item.get("function") if isinstance(item.get("function"), dict) else item
    name = fn.get("name") or item.get("tool") or item.get("tool_name")
    if not isinstance(name, str) or not name.strip():
        return None
    args = fn.get("arguments", fn.get("parameters", fn.get("args")))
    if isinstance(args, str):
        parsed = load_json_loose(args)
        args = parsed if parsed is not None else {}
    if not isinstance(args, dict):
        args = {}
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def split_prefix(text: str) -> tuple[str, str] | None:
    """拆出 `(标记, 正文)`；不是 `0:`/`1:` 协议则返回 None。

    容错：标记后可带空白（不含换行），冒号允许全角；实测模型偶尔**漏写冒号**
    （`1 {json}`），此时只有后面紧跟 JSON 起始符才当作调用，避免误削正文。
    """
    body = (text or "").lstrip()
    if len(body) < 2 or body[0] not in ("0", "1"):
        return None
    i = 1
    while i < len(body) and body[i] in " \t":
        i += 1
    if i < len(body) and body[i] in ":：":
        return body[0], body[i + 1:].strip()
    if body[0] == CALL and i < len(body) and body[i] in "{[":
        return body[0], body[i:].strip()
    return None


def _strip_prefix(text: str) -> str:
    """存在 `0:`/`1:` 前缀就剥掉；没有（或不是协议）则原样返回。"""
    parts = split_prefix(text)
    return parts[1] if parts else text


def parse_reply(text: str) -> Reply:
    """解析模型输出：`1:` → `tool_calls`，`0:` / 无前缀 → 正文。

    工具 JSON 解析失败时退回正文（宁可多一段难看的文本，也不要丢内容）。
    """
    raw = text or ""
    parts = split_prefix(raw)
    if parts is None:
        return Reply(answer=raw)
    mark, body = parts
    if mark == CALL:
        calls = parse_calls(body)
        return Reply(tool_calls=calls) if calls else Reply(answer=body)
    return Reply(answer=body)


def parse_calls(text: str) -> list[dict[str, Any]] | None:
    """从 `1:` 后面的文本里抠出工具调用（支持一次给多个）。"""
    obj = load_json_loose(text)
    if obj is None:
        return None
    items = obj if isinstance(obj, list) else [obj]
    calls = [c for c in (coerce_call(i) for i in items) if c]
    return calls or None


# ---------------------------------------------------------------------------
# 流式：把协议标记挡在可见正文外
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Piece:
    """流式过滤的一步产物：`text` 立刻下发，`tool_calls` 在流结束时一次性给出。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] | None = None


def _decide(buf: str) -> str | None:
    """只看前缀定性：`"text"` / `"tool"` / `None`（还看不出，继续等）。"""
    body = buf.lstrip()
    if not body:
        return None
    if body[0] not in ("0", "1"):
        return "text"
    i = 1
    while i < len(body) and body[i] in " \t":
        i += 1
    if i >= len(body):
        return None if len(body) <= 4 else "text"   # 只有 "0"/"1"，再等一点
    if body[i] in ":：":
        return "tool" if body[0] == "1" else "text"
    if body[0] == "1" and body[i] in "{[":
        return "tool"                              # 漏写冒号的 `1 {json}`
    return "text"                                  # "0.5" / "1. 首先" 之类


class ReplyFilter:
    """流式摘除 `0:` / `1:` 协议。

    - `0:` → 前缀剥掉后正文照常流出（只多等前缀那几个字符）；
    - `1:` → 整段缓冲，`finish()` 时给出 `tool_calls`；
    - 模型没按格式来 → 原样流出，不做任何加工。
    """

    def __init__(self, enabled: bool = True) -> None:  # noqa: A002 与协议开关同名
        self.enabled = enabled
        self._buf = ""
        self._mode: str | None = None               # None 未定 / "text" / "tool"

    def feed(self, delta: str) -> Piece:
        """喂入一段正文增量，返回可以立刻下发的部分。"""
        if not self.enabled:
            return Piece(text=delta)
        if not delta:
            return Piece()

        self._buf += delta
        if self._mode == "text":
            return self._take()
        if self._mode == "tool":
            return Piece()                          # 整段缓冲，等 finish()

        decision = _decide(self._buf)
        if decision is None:
            return Piece()
        self._mode = decision
        return Piece() if decision == "tool" else self._take()

    def finish(self) -> Piece:
        """流结束：吐掉残留（工具调用在这里成帧）。"""
        buffered, self._buf = self._buf, ""
        if not self.enabled:
            return Piece(text=buffered)
        if self._mode == "tool":
            self._mode = "text"
            reply = parse_reply(buffered)
            return Piece(tool_calls=reply.tool_calls) if reply.tool_calls \
                else Piece(text=reply.answer)
        if self._mode is None:
            self._mode = "text"
            return Piece(text=parse_reply(buffered).answer)
        return Piece(text=buffered)

    def _take(self) -> Piece:
        """把未定阶段的缓冲吐出去（顺手剥掉可能存在的 `0:` 前缀）。"""
        buffered, self._buf = self._buf, ""
        return Piece(text=_strip_prefix(buffered))


# ---------------------------------------------------------------------------
# 非流式响应改写
# ---------------------------------------------------------------------------


def apply_to_response(resp: dict[str, Any]) -> dict[str, Any]:
    """非流式：把 `1:` 正文换成规范的 `tool_calls`（顺带改 `finish_reason`）。"""
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return resp
    choice = choices[0]
    msg = choice.get("message") or {}
    text = msg.get("content")
    if not isinstance(text, str):
        return resp

    reply = parse_reply(text)
    if reply.tool_calls:
        replaced: dict[str, Any] = {"role": "assistant", "content": None,
                                    "tool_calls": reply.tool_calls}
        if msg.get("reasoning_content"):
            replaced["reasoning_content"] = msg["reasoning_content"]
        choice["message"] = replaced
        choice["finish_reason"] = "tool_calls"
    elif reply.answer != text:
        msg["content"] = reply.answer
    return resp
