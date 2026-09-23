"""请求转换：OpenAI → 上游 FastGPT。

`messages[].role/content` → `messages[]{dataId, hideInUI, role, content}`；
`model` 决定 `shareId`（R1 走独立分享链）；`user` → `outLinkUid`；`stream` → `stream`。
其余规范参数（`temperature` / `max_tokens` / `tools` …）上游不接受，不下发。
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

    `creds` 是扁平字典：`share_id` / `user_id` / `access_token` / `private_key`；
    `model` 是归一后的模型名（用于选分享链）。
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
