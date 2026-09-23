"""OpenAI Chat Completions 请求模型与错误体。

只放 [OpenAI 规范](https://platform.openai.com/docs/api-reference/chat) 里定义的东西；
上游（FastGPT）专有的概念一律不在这里出现。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


class MessagePartText(BaseModel):
    """分段内容里的文本段。"""

    type: Literal["text"] = "text"
    text: str


class ImageUrlContent(BaseModel):
    """图片段的 URL 载体。"""

    url: str


class MessagePartImage(BaseModel):
    """分段内容里的图片段。"""

    type: Literal["image_url"] = "image_url"
    image_url: ImageUrlContent


class Message(BaseModel):
    """一条对话消息。`content` 允许纯文本或分段数组两种形态。"""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] = ""
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class StreamOptions(BaseModel):
    """`stream_options`。本代理只认 `include_usage`。"""

    include_usage: bool = False


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class ChatCompletionRequest(BaseModel):
    """`POST /v1/chat/completions` 的请求体。

    字段与规范一一对应。**上游接口不接受、因而不会下发的参数**照常接受
    （见下面标注），只是对结果没有影响；规范之外的字段（供应商扩展）同样接受并忽略。
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message]
    stream: bool = False
    stream_options: StreamOptions | None = None
    #: 会下发给上游：用于生成上游的 `outLinkUid`
    user: str | None = None

    # --- 规范定义、但上游接口不支持：接受但不下发 ---
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    seed: int | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    parallel_tool_calls: bool | None = None

    @property
    def extra_fields(self) -> dict[str, Any]:
        """规范之外的字段（供应商扩展），接受但忽略。"""
        return dict(self.__pydantic_extra__ or {})

    @property
    def include_usage(self) -> bool:
        """流式响应是否要在末尾附一个 usage 分片。"""
        return bool(self.stream_options and self.stream_options.include_usage)


# ---------------------------------------------------------------------------
# 错误体
# ---------------------------------------------------------------------------


def error_body(message: str, err_type: str = "invalid_request_error",
               code: str | None = None, param: str | None = None) -> dict[str, Any]:
    """构造 OpenAI 错误体：`{"error": {message, type, param, code}}`。"""
    return {"error": {"message": message, "type": err_type,
                      "param": param, "code": code}}


# ---------------------------------------------------------------------------
# 文本辅助
# ---------------------------------------------------------------------------


def message_text(msg: Message) -> str:
    """取一条消息的纯文本（分段消息则拼接其中的文本段）。"""
    if isinstance(msg.content, str):
        return msg.content
    parts: list[str] = []
    for p in msg.content or []:
        if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
            parts.append(p["text"])
    return "".join(parts)


def count_chars(messages: list[Message]) -> int:
    """统计全部消息的字符数（用于估算 prompt_tokens）。"""
    return sum(len(message_text(m)) for m in messages)
