"""请求转换：OpenAI → 上游 FastGPT。

映射关系（只在两边语义真的对应时才映射）：

| OpenAI | 上游 |
| :--- | :--- |
| `messages[].role/content` | `messages[].{dataId, hideInUI, role, content}` |
| `model` | 决定 `shareId`（R1 走独立分享链） |
| `user` | `outLinkUid = shareChat-<user>`，缺省用随机 uuid |
| `stream` | `stream` |

其余参数（`temperature` / `max_tokens` / `tools` …）规范里有、但上游接口不接受，
因此不下发 —— 见 `proxy/schemas.py` 的标注与 README。
"""

from __future__ import annotations

import uuid

from . import fastgpt as fg
from . import schemas as sc
from .credentials import DEFAULT_SHARE_ID


def resolve_out_link_uid(user: str) -> str:
    """拼 `outLinkUid`：带上调用方给的 `user`，缺省用随机 uuid。"""
    return f"shareChat-{user}" if user else f"shareChat-{uuid.uuid4()}"


def build_fastgpt_request(req: sc.ChatCompletionRequest, creds: dict,
                          model: str) -> fg.FastGptRequest:
    """把 OpenAI 请求转成上游请求体。

    参数
    ----
    req
        已校验的 OpenAI 请求。
    creds
        扁平字典，键：`share_id` / `user_id` / `access_token` / `private_key`。
    model
        **归一后的模型名**（`fg.resolve_model` 的结果），用于选分享链。
    """
    return fg.FastGptRequest(
        messages=[fg.FastGptMessage(role=m.role, content=m.content)
                  for m in req.messages],
        variables=fg.make_variables(
            user_id=str(creds.get("user_id") or ""),
            access_token=str(creds.get("access_token") or ""),
            privatekey=str(creds.get("private_key") or ""),
        ),
        share_id=fg.resolve_share_id(model, str(creds.get("share_id") or "")
                                     or DEFAULT_SHARE_ID),
        out_link_uid=resolve_out_link_uid(req.user or ""),
        detail=True,
        stream=bool(req.stream),
    )


def build_referer(base: str, share_id: str) -> str:
    """上游要求带 Referer（与同一分享页一致，否则可能被拒）。"""
    return f"{base.rstrip('/')}/chat/share?shareId={share_id}"
