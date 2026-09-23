"""上游（FastGPT）侧的数据模型、解析与「响应 → OpenAI 字典」转换。

请求体走 camelCase：`alias_generator=to_camel` + 序列化 `by_alias=True`。
输出字典字段与 OpenAI 规范完全一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


# ---------------------------------------------------------------------------
# 模型名
# ---------------------------------------------------------------------------

#: `GET /v1/models` 返回的模型。
MODELS: list[dict[str, Any]] = [
    {"id": "deepseek-v3", "object": "model", "created": 1_686_935_002, "owned_by": "deepseek"},
    {"id": "deepseek-r1", "object": "model", "created": 1_686_935_002, "owned_by": "deepseek"},
]

#: 接受的模型名（含 DeepSeek 官方叫法）→ 规范名。
MODEL_ALIASES: dict[str, str] = {
    "deepseek-v3": "deepseek-v3",
    "deepseek-chat": "deepseek-v3",
    "deepseek-r1": "deepseek-r1",
    "deepseek-reasoner": "deepseek-r1",
}

#: R1 走上游的独立分享链
R1_SHARE_ID = "rjla9q8p3w7rrmqj9b24cth6"


def resolve_model(name: str) -> str | None:
    """把请求里的模型名归一到规范名；不认识则返回 None。"""
    return MODEL_ALIASES.get((name or "").strip().lower())


def resolve_share_id(model: str, default_share_id: str) -> str:
    """按模型选上游分享 id（R1 走独立链路）。"""
    return R1_SHARE_ID if resolve_model(model) == "deepseek-r1" else default_share_id


# ---------------------------------------------------------------------------
# 上游请求
# ---------------------------------------------------------------------------


class FastGptMessage(BaseModel):
    """上游的消息结构。"""

    model_config = _CAMEL

    data_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    hide_in_u_i: bool = False
    role: str
    content: str | list[dict[str, Any]] = ""


class FastGptRequest(BaseModel):
    """发给上游 `/api/v2/chat/completions` 的请求体。"""

    model_config = _CAMEL

    messages: list[FastGptMessage] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    share_id: str = ""
    response_chat_item_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    chat_id: str = ""
    out_link_uid: str = ""
    detail: bool = True
    stream: bool = False


def c_time_now() -> str:
    """上游前端用的时间格式：`2026-04-29 18:31:34 Wednesday`。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


def make_variables(user_id: str = "", access_token: str = "", privatekey: str = "") -> dict[str, Any]:
    """拼上游 `variables` 的四个固定字段。"""
    return {
        "userId": user_id,
        "accessToken": access_token,
        "privatekey": privatekey,
        "cTime": c_time_now(),
    }


# ---------------------------------------------------------------------------
# 上游响应
# ---------------------------------------------------------------------------


class FastGptResponseMessage(BaseModel):
    """上游响应里的消息体。

    `content` 可能是分段数组也可能是字符串，推理链可能在分段里（`type: "reasoning"`）
    或在 `reasoning_content` 字段上，两种形态都要能解析。
    """

    model_config = ConfigDict(extra="allow")

    content: str | list[dict[str, Any]] = ""
    reasoning_content: str | None = None


class FastGptChoice(BaseModel):
    """上游响应里的一条候选。"""

    model_config = ConfigDict(extra="allow")

    message: FastGptResponseMessage | None = None


class FastGptResponse(BaseModel):
    """上游响应。"""

    model_config = ConfigDict(extra="allow")

    choices: list[FastGptChoice] = Field(default_factory=list)
    usage: dict[str, Any] | None = None


def _part_text(node: Any) -> str:
    """取一段内容里的文本：既认 `{"content": "..."}` 也认直接给字符串。"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict) and isinstance(node.get("content"), str):
        return node["content"]
    return ""


def content_text(content: str | list[dict[str, Any]]) -> str:
    """从 `content` 里取正文：字符串直接用，分段数组只拼 `text` 段。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(_part_text(part.get("text")))
    return "".join(parts)


def content_reasoning(content: str | list[dict[str, Any]]) -> str:
    """从 `content` 里取推理链：只认分段数组里的 `type: "reasoning"` 段。"""
    if isinstance(content, str):
        return ""
    parts: list[str] = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "reasoning":
            parts.append(_part_text(part.get("reasoning")))
    return "".join(parts)


def parse_content(resp: FastGptResponse) -> tuple[str, str]:
    """拆出 (正文, 推理链)。

    `content` 为分段数组时按类型归位（`text` / `reasoning`），为字符串时整段算正文；
    推理也可能在 `reasoning_content` 字段上，分段里已有则以分段为准。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []

    for choice in resp.choices:
        msg = choice.message
        if msg is None:
            continue
        text_parts.append(content_text(msg.content))
        segmented = content_reasoning(msg.content)
        if segmented:
            reasoning_parts.append(segmented)
        elif isinstance(msg.reasoning_content, str):
            reasoning_parts.append(msg.reasoning_content)

    return "".join(text_parts), "".join(reasoning_parts)


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def estimate_tokens(chars: int) -> int:
    """按字符数估算 token（中英混排的经验值：字符数 × 2 / 3）。"""
    return max(0, chars) * 2 // 3


def split_reasoning_tokens(completion_tokens: int, reasoning_chars: int,
                           content_chars: int) -> int:
    """按字符占比从 `completion_tokens` 拆出 `reasoning_tokens`。

    该总数已含推理 token，只能按字符比切分，否则会算出比总数还大的值。
    """
    total_chars = reasoning_chars + content_chars
    if completion_tokens <= 0 or reasoning_chars <= 0 or total_chars <= 0:
        return 0
    return min(completion_tokens, round(completion_tokens * reasoning_chars / total_chars))


def _upstream_usage_usable(usage: dict[str, Any] | None) -> bool:
    """上游 usage 是否可信。

    实测非流式固定返回占位值 `1/1/1`（与真实用量无关），此时改用字符估算。
    任一字段 > 1 即认为给了真实值。
    """
    if not usage:
        return False
    return any(int(usage.get(k) or 0) > 1
               for k in ("prompt_tokens", "completion_tokens", "total_tokens"))


def usage_from_upstream(resp: FastGptResponse) -> bool:
    """上游是否给了可信 usage（`False` = 对外是字符估算值，供遥测标注）。"""
    return _upstream_usage_usable(resp.usage)


def build_usage(prompt_tokens: int, completion_tokens: int,
                reasoning_tokens: int = 0) -> dict[str, Any]:
    """构造规范里的 `usage` 结构。"""
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


# ---------------------------------------------------------------------------
# 上游响应 → OpenAI 响应
# ---------------------------------------------------------------------------


def to_openai_response(resp: FastGptResponse, model: str, created: int,
                       prompt_chars: int) -> dict[str, Any]:
    """把上游响应转成 OpenAI 兼容响应字典。

    `model` 是回显给客户端的名字（请求里那个）；`prompt_chars` 供估算 usage。
    """
    text, reasoning = parse_content(resp)

    est_prompt = estimate_tokens(prompt_chars)
    est_completion = estimate_tokens(len(text) + len(reasoning))

    upstream = resp.usage or {}
    if _upstream_usage_usable(upstream):
        prompt_tokens = int(upstream.get("prompt_tokens") or est_prompt)
        completion_tokens = int(upstream.get("completion_tokens") or est_completion)
    else:
        prompt_tokens = est_prompt
        completion_tokens = est_completion

    message: dict[str, Any] = {
        "role": "assistant",
        # 正文与推理都为空 → null；只有推理 → 空串（与上游语义一致）
        "content": text if (text or reasoning) else None,
    }
    if reasoning:
        message["reasoning_content"] = reasoning

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "logprobs": None,
            "finish_reason": "stop",
        }],
        "usage": build_usage(prompt_tokens, completion_tokens,
                             split_reasoning_tokens(completion_tokens,
                                                    len(reasoning), len(text))),
    }
