#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检（不联网）：模块装配 + 转换 / 解析 / 规范形状。

覆盖：sso 注册表、凭据、模型校验、请求转换、响应形状、usage、SSE 分帧、
流式帧序列与推理链、上游真流式、sso 子模块契约、控制台遥测。

用法: .venv\\Scripts\\python.exe tests\\test_offline.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proxy import credentials as creds_mod               # noqa: E402
from proxy import fastgpt as fg                          # noqa: E402
from proxy import schemas as sc                          # noqa: E402
from proxy import stream as st                           # noqa: E402
from proxy import telemetry as tm                        # noqa: E402
from proxy import toolcall as tc                         # noqa: E402
from proxy import transform as tf                        # noqa: E402
from proxy import upstream as up_mod                     # noqa: E402
import sso                                               # noqa: E402
from sso import config                                   # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []

#: 规范里 `chat.completion` 响应的顶层键
RESPONSE_KEYS = {"id", "object", "created", "model", "choices", "usage"}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# ---------------------------------------------------------------------------

def test_registry() -> None:
    print("\n[1] sso 注册表")
    check("只注册了 ds", list(config.SYSTEMS) == ["ds"], str(list(config.SYSTEMS)))
    check("ds 的 client_id 正确",
          config.SYSTEMS["ds"]["client_id"] == "re0owG1g776ng2eix7x3o8sa20W6OdA2")


def test_credentials(tmp: Path) -> None:
    print("\n[2] 凭据读写")
    creds = {
        "version": 1,
        "username": "25123368",
        "display_name": "测试同学",
        "created_at": "2026-09-23T10:00:00",
        "share_id": "from-file-share-id",
        "cookie_header": "uname=25123368; _webvpn_key=abc",
        "upstream": {"base": "https://aiagent.shu.edu.cn",
                     "chat_path": "/api/v2/chat/completions"},
        "ds": {"userid": "25123368", "access_token": "tok", "private_key": "pk"},
    }
    p = creds_mod.save(creds, tmp / ".credentials.json")
    loaded = creds_mod.load(p)

    check("落盘后可读回", loaded["username"] == "25123368")
    check("cookie_header 取自文件",
          creds_mod.cookie_header(loaded) == "uname=25123368; _webvpn_key=abc")
    check("share_id 取自文件", creds_mod.share_id(loaded) == "from-file-share-id")
    check("user_id 取自 ds.userid", creds_mod.user_id(loaded) == "25123368")
    check("access_token / private_key",
          creds_mod.access_token(loaded) == "tok" and creds_mod.private_key(loaded) == "pk")
    check("凭据年龄可算", creds_mod.age_hours(loaded) is not None)

    os.environ["SHU_COOKIE"] = "env=1"
    check("环境变量优先于文件", creds_mod.cookie_header(loaded) == "env=1")
    del os.environ["SHU_COOKIE"]

    try:
        creds_mod.load(tmp / "nope.json")
        check("缺文件时抛 CredentialsError", False, "未抛异常")
    except creds_mod.CredentialsError as exc:
        check("缺文件时抛 CredentialsError", "login.py" in str(exc), str(exc))


def test_model_resolution() -> None:
    print("\n[3] 模型名校验")
    check("deepseek-v3 认识", fg.resolve_model("deepseek-v3") == "deepseek-v3")
    check("deepseek-r1 认识", fg.resolve_model("deepseek-r1") == "deepseek-r1")
    check("deepseek-chat 归一到 v3", fg.resolve_model("deepseek-chat") == "deepseek-v3")
    check("deepseek-reasoner 归一到 r1",
          fg.resolve_model("deepseek-reasoner") == "deepseek-r1")
    check("大小写不敏感", fg.resolve_model("DeepSeek-R1") == "deepseek-r1")
    check("未知模型返回 None", fg.resolve_model("gpt-4") is None)
    check("空名返回 None", fg.resolve_model("") is None)

    check("v3 用默认分享链",
          fg.resolve_share_id("deepseek-v3", "S") == "S")
    check("r1 用独立分享链",
          fg.resolve_share_id("deepseek-r1", "S") == fg.R1_SHARE_ID)
    check("reasoner 别名也走独立链",
          fg.resolve_share_id("deepseek-reasoner", "S") == fg.R1_SHARE_ID)
    check("/v1/models 只列两个模型",
          [m["id"] for m in fg.MODELS] == ["deepseek-v3", "deepseek-r1"])


def _request(**overrides) -> sc.ChatCompletionRequest:
    body = {"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return sc.ChatCompletionRequest.model_validate(body)


def test_request_schema() -> None:
    print("\n[4] 请求模型")
    req = _request(temperature=0.7, top_p=0.9, max_tokens=100, seed=1,
                   stop=["a", "b"], n=1, tools=[{"type": "function",
                                                 "function": {"name": "f"}}],
                   vendor_field="x")
    check("规范参数都能解析",
          req.temperature == 0.7 and req.top_p == 0.9 and req.max_tokens == 100
          and req.seed == 1 and req.stop == ["a", "b"] and req.n == 1)
    check("tools 能解析", req.tools and req.tools[0]["function"]["name"] == "f")
    check("未知字段进 extra_fields", req.extra_fields == {"vendor_field": "x"},
          str(req.extra_fields))

    check("include_usage 默认为假", _request().include_usage is False)
    check("include_usage 可开启",
          _request(stream=True, stream_options={"include_usage": True}).include_usage is True)

    parts = _request(messages=[{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]}])
    check("分段消息可解析", isinstance(parts.messages[0].content, list))
    check("字符数统计含文本段", sc.count_chars(parts.messages) == 2,
          str(sc.count_chars(parts.messages)))

    # 回灌工具调用时 assistant 的 content 就是 null（OpenAI 客户端真实形状）
    backfill = _request(messages=[{"role": "assistant", "content": None,
                                   "tool_calls": [{"id": "call_1", "type": "function",
                                                    "function": {"name": "f",
                                                                 "arguments": "{}"}}]}])
    check("content 允许 null", backfill.messages[0].content is None)
    check("null 正文算 0 字符", sc.count_chars(backfill.messages) == 0,
          str(sc.count_chars(backfill.messages)))

    err = sc.error_body("boom", code="c", param="p")
    check("错误体含规范四字段",
          set(err["error"]) == {"message", "type", "param", "code"}, str(err))


def test_transform() -> None:
    print("\n[5] OpenAI → 上游请求")
    creds = {"share_id": "default-share", "user_id": "25123368",
             "access_token": "tok", "private_key": "pk"}
    req = _request(user="u-1", messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在"},
    ])
    out = tf.build_fastgpt_request(req, creds, "deepseek-v3")

    check("所有角色原样透传（含 system）",
          [m.role for m in out.messages] == ["system", "user", "assistant"],
          str([m.role for m in out.messages]))
    check("内容未被改动", out.messages[1].content == "你好")
    check("shareId 用默认值", out.share_id == "default-share", out.share_id)
    check("outLinkUid 带上 user", out.out_link_uid == "shareChat-u-1", out.out_link_uid)
    check("outLinkUid 缺省时随机",
          tf.build_fastgpt_request(_request(), creds, "deepseek-v3")
          .out_link_uid.startswith("shareChat-"))
    check("variables 四个固定字段齐备",
          set(out.variables) == {"userId", "accessToken", "privatekey", "cTime"},
          str(out.variables))
    check("detail 恒为 True", out.detail is True)
    check("stream 透传", tf.build_fastgpt_request(_request(stream=True), creds,
                                                 "deepseek-v3").stream is True)

    r1 = tf.build_fastgpt_request(_request(model="deepseek-r1"), creds, "deepseek-r1")
    check("R1 走独立分享链", r1.share_id == fg.R1_SHARE_ID, r1.share_id)

    raw = json.dumps(out.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)
    for key in ("shareId", "responseChatItemId", "chatId", "outLinkUid", "dataId",
                "hideInUI", "cTime", "userId", "accessToken", "privatekey"):
        check(f"上游字段用 camelCase：{key}", f'"{key}"' in raw)
    check("上游字段不含规范参数", "temperature" not in raw and "max_tokens" not in raw)


def test_response_shape() -> None:
    print("\n[6] 上游响应 → OpenAI 响应")
    body = {
        "choices": [{"message": {"content": [
            {"type": "reasoning", "reasoning": {"content": "想一想"}},
            {"type": "text", "text": {"content": "答案是 42"}},
        ]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    parsed = fg.FastGptResponse.model_validate(body)
    resp = fg.to_openai_response(parsed, "deepseek-r1", 12345, prompt_chars=30)

    check("顶层字段与规范一致", set(resp) == RESPONSE_KEYS, str(sorted(resp)))
    check("object/created/model 正确",
          resp["object"] == "chat.completion" and resp["created"] == 12345
          and resp["model"] == "deepseek-r1")
    check("id 形如 chatcmpl-*", resp["id"].startswith("chatcmpl-"), resp["id"])

    choice = resp["choices"][0]
    check("choice 字段与规范一致",
          set(choice) == {"index", "message", "logprobs", "finish_reason"},
          str(sorted(choice)))
    check("logprobs 显式为 null", choice["logprobs"] is None)
    check("finish_reason 为 stop", choice["finish_reason"] == "stop")
    check("正文归到 content", choice["message"]["content"] == "答案是 42")
    check("推理归到 reasoning_content",
          choice["message"]["reasoning_content"] == "想一想")
    check("message 只含 role/content/reasoning_content",
          set(choice["message"]) == {"role", "content", "reasoning_content"},
          str(sorted(choice["message"])))

    usage = resp["usage"]
    check("usage 字段与规范一致",
          set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens",
                         "prompt_tokens_details", "completion_tokens_details"},
          str(sorted(usage)))
    check("usage 沿用上游真实值",
          usage["prompt_tokens"] == 10 and usage["completion_tokens"] == 20
          and usage["total_tokens"] == 30, str(usage))
    # 推理 token 按字符占比从真实 completion_tokens 里切（不是另算一个估算值）
    reasoning_tokens = usage["completion_tokens_details"]["reasoning_tokens"]
    check("reasoning_tokens 按字符占比切分",
          reasoning_tokens == round(20 * len("想一想") / (len("想一想") + len("答案是 42"))),
          str(usage))
    check("reasoning_tokens 不超过 completion_tokens",
          0 < reasoning_tokens <= usage["completion_tokens"], str(usage))
    check("prompt_tokens_details 含 cached_tokens",
          usage["prompt_tokens_details"] == {"cached_tokens": 0}, str(usage))
    check("无 total_points 等自造字段",
          "total_points" not in json.dumps(resp), "含 total_points")

    # content 语义
    only_reasoning = fg.to_openai_response(
        fg.FastGptResponse.model_validate({"choices": [{"message": {"content": [
            {"type": "reasoning", "reasoning": {"content": "r"}}]}}]}),
        "m", 1, prompt_chars=0)
    check("仅 reasoning 时 content 为空串",
          only_reasoning["choices"][0]["message"]["content"] == "",
          repr(only_reasoning["choices"][0]["message"]["content"]))

    empty = fg.to_openai_response(
        fg.FastGptResponse.model_validate({"choices": [{"message": {"content": []}}]}),
        "m", 1, prompt_chars=0)
    check("两者皆空时 content 为 None",
          empty["choices"][0]["message"]["content"] is None)
    check("无推理时不带 reasoning_content",
          "reasoning_content" not in empty["choices"][0]["message"])

    # 上游返回纯字符串（实测形态）
    flat = fg.to_openai_response(
        fg.FastGptResponse.model_validate(
            {"choices": [{"message": {"content": "纯文本"}}]}),
        "m", 1, prompt_chars=6)
    check("content 为字符串时也能解析",
          flat["choices"][0]["message"]["content"] == "纯文本",
          str(flat["choices"][0]["message"]))
    check("无推理时 reasoning_tokens 为 0",
          flat["usage"]["completion_tokens_details"]["reasoning_tokens"] == 0,
          str(flat["usage"]))

    # 推理链放在 message.reasoning_content 字段上（FastGPT 的另一种形态）
    field = fg.to_openai_response(
        fg.FastGptResponse.model_validate({"choices": [{"message": {
            "content": "答案", "reasoning_content": "字段里的推理"}}]}),
        "m", 1, prompt_chars=6)
    check("message.reasoning_content 归到推理链",
          field["choices"][0]["message"]["reasoning_content"] == "字段里的推理",
          str(field["choices"][0]["message"]))
    check("字段形态下正文不受影响",
          field["choices"][0]["message"]["content"] == "答案",
          str(field["choices"][0]["message"]))

    both = fg.to_openai_response(
        fg.FastGptResponse.model_validate({"choices": [{"message": {
            "content": [{"type": "reasoning", "reasoning": {"content": "分段推理"}}],
            "reasoning_content": "字段里的推理"}}]}),
        "m", 1, prompt_chars=6)
    check("分段与字段同时存在时不重复",
          both["choices"][0]["message"]["reasoning_content"] == "分段推理",
          str(both["choices"][0]["message"]))


def test_usage_estimation() -> None:
    print("\n[7] usage 估算（上游占位值时）")
    placeholder = fg.to_openai_response(
        fg.FastGptResponse.model_validate({
            "choices": [{"message": {"content": "abcdef"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1},
        }), "m", 1, prompt_chars=30)
    usage = placeholder["usage"]
    check("占位值 1/1/1 被识别并改用估算",
          usage["prompt_tokens"] == fg.estimate_tokens(30)
          and usage["completion_tokens"] == fg.estimate_tokens(6), str(usage))
    check("total = prompt + completion",
          usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])

    real = fg.to_openai_response(
        fg.FastGptResponse.model_validate({
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 175, "completion_tokens": 102, "total_tokens": 277},
        }), "m", 1, prompt_chars=999)
    check("上游给真实值时优先采用",
          real["usage"]["prompt_tokens"] == 175 and real["usage"]["total_tokens"] == 277,
          str(real["usage"]))

    check("estimate_tokens 是 2/3",
          fg.estimate_tokens(30) == 20 and fg.estimate_tokens(0) == 0)


def test_sse_framing() -> None:
    print("\n[8] 上游 SSE 分帧")
    answer = json.dumps({"choices": [{"delta": {"content": "你好"}}]})

    # 1) 整块两种分隔符混用
    raw = (f"event: answer\ndata: {answer}\n\n"
           f"event: flowNodeResponse\ndata: {json.dumps({'moduleType': 'chatNode'})}\r\n\r\n"
           f"event: answer\ndata: {answer}\n\n"
           "event: answer\ndata: [DONE]\n\n").encode()
    frames = st.split_frames(raw)
    check("分帧同时认两种分隔符", len(frames) == 4, f"{len(frames)} 帧")
    parsed = [st.parse_event(f) for f in frames]
    check("event 名解析正确",
          [p["event"] for p in parsed] == ["answer", "flowNodeResponse", "answer", "answer"],
          str([p["event"] for p in parsed]))
    check("data 可解析为 JSON",
          json.loads(parsed[0]["data"])["choices"][0]["delta"]["content"] == "你好")

    # 2) 跨分片残帧：把同一条流按字节切开
    sp = st.FrameSplitter()
    got: list[bytes] = []
    for i in range(0, len(raw), 7):          # 每 7 字节喂一次，切点必然落在帧中间
        got.extend(sp.feed(raw[i:i + 7]))
    got.extend(sp.feed(sp.flush()))
    check("跨分片残帧能拼回", len(got) == 4, f"{len(got)} 帧")


def test_stream_frames() -> None:
    print("\n[9] 流式帧序列")
    raw = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'reasoning_content': '推理'}}]})}\n\n"
           f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '你好'}}]})}\r\n\r\n"
           f"event: flowNodeResponse\ndata: {json.dumps({'moduleType': 'chatNode', 'inputTokens': 7, 'outputTokens': 9})}\n\n"
           f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '！'}}]})}\n\n"
           "event: answer\ndata: [DONE]\n\n").encode()

    out = asyncio.run(_collect(raw, include_usage=True))
    payloads = [json.loads(x[6:]) for x in out if x.startswith("data: {")]

    check("首帧是 role 帧",
          payloads[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""},
          str(payloads[0]))
    check("末尾是 [DONE]", out[-1].strip() == "data: [DONE]", out[-1])

    body = [p for p in payloads if p["choices"] and p["choices"][0]["delta"]]
    check("推理增量在 delta.reasoning_content",
          any("推理" in (p["choices"][0]["delta"].get("reasoning_content") or "")
              for p in body))
    check("正文增量在 delta.content（含 CRLF 分隔那帧）",
          "".join(p["choices"][0]["delta"].get("content", "")
                  for p in body if "reasoning_content" not in p["choices"][0]["delta"])
          == "你好！",
          str([p["choices"][0]["delta"] for p in body]))

    last_choice = payloads[-2] if payloads[-1].get("usage") is None else payloads[-3]
    finish = [p for p in payloads if p["choices"]
              and p["choices"][0]["finish_reason"] == "stop"]
    check("有 finish_reason=stop 的收尾帧", len(finish) == 1, str(finish))
    check("收尾帧 delta 为空", finish and finish[0]["choices"][0]["delta"] == {},
          str(finish))
    check("每帧 choice 都带 logprobs/finish_reason",
          all(set(p["choices"][0]) == {"index", "delta", "logprobs", "finish_reason"}
              for p in payloads if p["choices"]),
          "字段不全")
    base_keys = {"id", "object", "created", "model", "choices"}
    check("chunk 顶层没有多余字段",
          all(set(p) == base_keys | ({"usage"} if p.get("usage") else set())
              for p in payloads),
          str([sorted(p) for p in payloads]))
    _ = last_choice

    usage_frames = [p for p in payloads if p.get("usage")]
    check("有 usage 帧", len(usage_frames) == 1, str(usage_frames))
    if usage_frames:
        check("usage 帧的 choices 为空", usage_frames[0]["choices"] == [])
        u = usage_frames[0]["usage"]
        check("usage 采用 flowNodeResponse 真实值",
              u["prompt_tokens"] == 7 and u["completion_tokens"] == 9, str(u))
        check("usage 结构与规范一致",
              set(u) == {"prompt_tokens", "completion_tokens", "total_tokens",
                         "prompt_tokens_details", "completion_tokens_details"},
              str(sorted(u)))
        check("usage 里无 total_points", "total_points" not in json.dumps(u))

    out2 = asyncio.run(_collect(raw, include_usage=False))
    check("未开 include_usage 时不发 usage 帧",
          not any('"usage"' in x for x in out2), str(out2[-2:]))


def test_stream_reasoning() -> None:
    print("\n[10] 流式推理链（DeepSeek 的 reasoning_content）")

    # 1) 上游只在收尾统计里给推理链（chatNode.reasoningText）
    raw = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '答案'}}]})}\n\n"
           f"event: flowNodeResponse\ndata: {json.dumps({'moduleType': 'chatNode', 'inputTokens': 5, 'outputTokens': 20, 'reasoningText': '收尾统计里的推理', 'finishReason': 'stop'})}\n\n"
           f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': None}, 'finish_reason': 'stop'}]})}\n\n"
           "event: answer\ndata: [DONE]\n\n").encode()
    out = asyncio.run(_collect(raw, include_usage=True))
    payloads = [json.loads(x[6:]) for x in out if x.startswith("data: {")]
    non_usage = [p for p in payloads if p["choices"]]

    reasoning_frames = [p for p in non_usage
                        if p["choices"][0]["delta"].get("reasoning_content")]
    check("收尾统计里的推理被补成一帧",
          len(reasoning_frames) == 1
          and reasoning_frames[0]["choices"][0]["delta"]["reasoning_content"]
          == "收尾统计里的推理",
          str([p["choices"][0]["delta"] for p in non_usage]))
    finish_idx = [i for i, p in enumerate(non_usage)
                  if p["choices"][0]["finish_reason"] == "stop"]
    reason_idx = [i for i, p in enumerate(non_usage)
                  if p["choices"][0]["delta"].get("reasoning_content")]
    check("补的推理帧在收尾帧之前",
          reason_idx and finish_idx and reason_idx[0] < finish_idx[0],
          f"reason={reason_idx} finish={finish_idx}")

    usage = [p for p in payloads if p.get("usage")][0]["usage"]
    check("usage 里 reasoning_tokens 不为 0",
          usage["completion_tokens_details"]["reasoning_tokens"] > 0, str(usage))
    check("reasoning_tokens 不超过 completion_tokens",
          usage["completion_tokens_details"]["reasoning_tokens"]
          <= usage["completion_tokens"], str(usage))

    # 2) 上游给了增量推理 → 不再补 reasoningText（避免重复）
    raw2 = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'reasoning_content': '增量推理'}}]})}\n\n"
            f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '答案'}}]})}\n\n"
            f"event: flowNodeResponse\ndata: {json.dumps({'moduleType': 'chatNode', 'reasoningText': '增量推理'})}\n\n"
            "event: answer\ndata: [DONE]\n\n").encode()
    out2 = asyncio.run(_collect(raw2, include_usage=False))
    deltas = [json.loads(x[6:])["choices"][0]["delta"] for x in out2 if x.startswith("data: {")]
    joined = "".join(d.get("reasoning_content", "") for d in deltas)
    check("增量推理不被 reasoningText 重复", joined == "增量推理", repr(joined))

    # 3) 上游给 finishReason=length → 规范里也是 length
    raw3 = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': 'x'}}]})}\n\n"
            f"event: flowNodeResponse\ndata: {json.dumps({'moduleType': 'chatNode', 'finishReason': 'length'})}\n\n"
            "event: answer\ndata: [DONE]\n\n").encode()
    out3 = asyncio.run(_collect(raw3, include_usage=False))
    finishes = [json.loads(x[6:])["choices"][0]["finish_reason"] for x in out3
                if x.startswith("data: {") and json.loads(x[6:])["choices"]]
    check("finishReason=length 映射为 length",
          finishes[-1] == "length" and finishes.count("length") == 1, str(finishes))

    # 4) delta.content 是分段数组时取文本（防御上游换形态）
    raw4 = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': [{'type': 'text', 'text': {'content': '分段正文'}}]}}]})}\n\n"
            "event: answer\ndata: [DONE]\n\n").encode()
    out4 = asyncio.run(_collect(raw4, include_usage=False))
    texts = [json.loads(x[6:])["choices"][0]["delta"].get("content") for x in out4
             if x.startswith("data: {") and json.loads(x[6:])["choices"]]
    check("分段 delta 能取出正文",
          "分段正文" in [t for t in texts if t], str(texts))


def _tools() -> list[dict]:
    """一个最小工具声明（规范形状）。"""
    return [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]},
        },
    }]


def test_toolcall_protocol() -> None:
    """[14] 提示词工具调用：注入 / 改写 / 解析。

    上游是应用级接口，客户端传的 `tools` 无处可去（见 README「上游的原生 tool call」）。
    代理把工具声明折进 system，让模型按 `0:` / `1:` 协议输出，再解析回规范 `tool_calls`。
    """
    print("\n[14] 提示词工具调用：注入 / 改写 / 解析")
    tools = _tools()

    specs = tc.tool_specs(tools)
    check("工具声明只留 name/description/parameters",
          specs and set(specs[0]) == {"name", "description", "parameters"}, str(specs))
    check("缺 name 的声明被丢掉", tc.tool_specs([{"type": "function"}]) == [])

    prompt = tc.build_tool_prompt(tools)
    check("协议说明含工具名与参数 schema",
          "get_weather" in prompt and '"city"' in prompt, prompt[:80])
    check("协议说明讲清 0/1 与回灌标记",
          "0:" in prompt and "1:" in prompt and "<ToolResponse>" in prompt)
    check("示例里的 JSON 花括号没有被转义",
          '1: {"name": "get_weather", "arguments": {"city": "杭州"}}' in prompt,
          prompt[-260:])
    # 提示词要「倾向使用工具」，否则模型会拿常识作答而不调工具
    check("协议说明鼓励优先调用工具",
          "优先调用工具" in prompt and "必须调用工具" in prompt, prompt[:400])
    check("保留「与工具无关才直接回答」的出口",
          "明显无关" in prompt, prompt[-320:])

    req = _request(tools=tools, messages=[
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "杭州天气"},
        {"role": "assistant", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "杭州"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "晴 31℃"},
    ])
    pairs = tc.rewrite_messages(req.messages, tools)
    check("system 追加协议说明且原内容保留",
          pairs[0][0] == "system" and pairs[0][1].startswith("你是助手")
          and tc.MARKER in pairs[0][1], pairs[0][1][:60])
    check("assistant 的 tool_calls 还原成 `1: {...}`",
          pairs[2][1].startswith("1: ")
          and json.loads(pairs[2][1][3:]) == {"name": "get_weather",
                                              "arguments": {"city": "杭州"}},
          pairs[2][1])
    check("tool 角色改成 user + <ToolResponse>",
          pairs[3][0] == "user"
          and pairs[3][1] == "<ToolResponse>\n晴 31℃\n</ToolResponse>", pairs[3][1])
    check("没有 system 时自动补一条",
          tc.rewrite_messages([sc.Message(role="user", content="hi")], tools)[0][0] == "system")
    check("已注入过就不重复注入",
          tc.rewrite_messages([sc.Message(role="system", content=tc.MARKER)],
                              tools)[0][1] == tc.MARKER)

    # 真实回灌形状：content 是 null + tool_calls
    backfill = _request(tools=tools, messages=[
        {"role": "user", "content": "杭州天气"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "杭州"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "晴 31℃"},
    ])
    pairs_null = tc.rewrite_messages(backfill.messages, tools)
    # 没有 system → 会自动补一条在头部，所以 assistant / tool 顺延一格
    check("content=null 的工具回灌也能改写",
          pairs_null[2][1].startswith("1: ") and pairs_null[3][1].startswith("<ToolResponse>"),
          str([(r, str(c)[:40]) for r, c in pairs_null]))
    check("content=null 的请求能过 transform（不炸）",
          tf.build_fastgpt_request(backfill, {"share_id": "s"}, "deepseek-v3") is not None)

    up = tf.build_fastgpt_request(_request(tools=tools), {"share_id": "s"}, "deepseek-v3")
    check("上游请求里真的带上了协议说明",
          up.messages[0].role == "system" and tc.MARKER in up.messages[0].content)
    plain = tf.build_fastgpt_request(_request(), {"share_id": "s"}, "deepseek-v3")
    check("没带 tools 时不动消息（回归）",
          tc.MARKER not in plain.messages[0].content, plain.messages[0].content)

    one = tc.parse_reply('1: {"name": "get_weather", "arguments": {"city": "杭州"}}')
    check("`1:` 解析成 tool_calls",
          one.tool_calls and one.tool_calls[0]["function"]["name"] == "get_weather"
          and one.tool_calls[0]["type"] == "function", str(one))
    check("arguments 是 JSON 字符串（规范要求）",
          json.loads(one.tool_calls[0]["function"]["arguments"]) == {"city": "杭州"})
    check("tool_call 带 call_* 形式的 id", one.tool_calls[0]["id"].startswith("call_"))

    check("`0:` 剥掉前缀", tc.parse_reply("0: 今天晴").answer == "今天晴")
    check("全角冒号也认",
          bool(tc.parse_reply('1：{"name": "f", "arguments": {}}').tool_calls))
    check("没有前缀时原样返回", tc.parse_reply("今天晴").answer == "今天晴")
    check("`0.5 个` 这类正文不被误剥", tc.parse_reply("0.5 个").answer == "0.5 个")
    check("`1. 首先` 这类正文不被误剥", tc.parse_reply("1. 首先").answer == "1. 首先")
    check("`1 个苹果` 这种量词不被误剥", tc.parse_reply("1 个苹果").answer == "1 个苹果")
    # 压测实测（210 工具档 1/112）：模型偶尔漏写冒号 `1 {json}`
    check("漏写冒号的 `1 {json}` 也认",
          bool(tc.parse_reply('1 {"name": "f", "arguments": {"a": 1}}').tool_calls))
    check("漏写冒号且参数名写成 parameters 也认",
          json.loads(tc.parse_reply('1 {"name": "f", "parameters": {"a": 1}}')
                     .tool_calls[0]["function"]["arguments"]) == {"a": 1})
    check("`0 {json}` 不加冒号时不算调用", tc.parse_reply('0 {"a": 1}').tool_calls is None)
    check("围栏 JSON 能解析",
          tc.parse_reply('1: ```json\n{"name": "f", "arguments": {"a": 1}}\n```')
          .tool_calls[0]["function"]["name"] == "f")
    check("JSON 前后有废话也能抠出来",
          bool(tc.parse_reply('1: 好的 {"name": "f", "arguments": {}}').tool_calls))
    check("arguments 是字符串时也能吃下",
          json.loads(tc.parse_reply('1: {"name": "f", "arguments": "{\\"a\\": 1}"}')
                     .tool_calls[0]["function"]["arguments"]) == {"a": 1})
    check("一次给多个调用", len(tc.parse_reply(
        '1: [{"name": "a", "arguments": {}}, {"name": "b", "arguments": {}}]'
    ).tool_calls) == 2)
    check("JSON 坏了退回正文（不丢内容）",
          tc.parse_reply("1: {不是 json").answer == "{不是 json")
    check("名字缺失时不算调用", tc.parse_reply('1: {"arguments": {}}').tool_calls is None)

    check("有 tools 默认启用", tc.enabled(_request(tools=tools)) is True)
    check("tool_choice=none 关掉",
          tc.enabled(_request(tools=tools, tool_choice="none")) is False)
    check("没 tools 时不启用", tc.enabled(_request()) is False)

    # 注入的协议说明要计入 prompt 估算，否则工具多时 usage 严重低报
    check("prompt_overhead 反映了注入量",
          tc.prompt_overhead(_request(tools=tools)) == len(tc.build_tool_prompt(tools))
          and tc.prompt_overhead(_request(tools=tools)) > 100,
          str(tc.prompt_overhead(_request(tools=tools))))
    check("没 tools 时 overhead 为 0", tc.prompt_overhead(_request()) == 0)
    many = _tools() * 20
    one_cost = tc.prompt_overhead(_request(tools=_tools()))
    many_cost = tc.prompt_overhead(_request(tools=many))
    # 模板底数固定，多出来的就是工具声明的 JSON（`per_tool` 含一对方括号，故减 1）
    per_tool = len(json.dumps(tc.tool_specs(_tools()), ensure_ascii=False,
                              separators=(",", ":")))
    check("每多一个工具，overhead 就多一份工具声明",
          many_cost - one_cost == 19 * (per_tool + 1) - 38,
          f"增量 {many_cost - one_cost}，预期 {19 * (per_tool - 1)}")

    # 服务端开关（poc.py --no-tool-call）关闭时，整条链路回到「接受但忽略」
    check("服务端开关关闭时不启用",
          tc.enabled(backfill, tool_call=False) is False)
    check("服务端开关关闭时 overhead 为 0",
          tc.prompt_overhead(backfill, tool_call=False) == 0)
    off = tf.build_fastgpt_request(backfill, {"share_id": "s"}, "deepseek-v3",
                                  tool_call=False)
    check("开关关闭时不注入协议说明",
          tc.MARKER not in "".join(str(m.content) for m in off.messages),
          [str(m.content)[:40] for m in off.messages])
    check("开关关闭时 tool 消息不被改写",
          [m.role for m in off.messages] != ["system", "user", "assistant", "user"]
          or "<ToolResponse>" not in str(off.messages[-1].content),
          [m.role for m in off.messages])

    resp = fg.to_openai_response(fg.FastGptResponse.model_validate({"choices": [{"message": {
        "content": '1: {"name": "get_weather", "arguments": {"city": "杭州"}}'}}]}),
        "deepseek-v3", 1, prompt_chars=10)
    out = tc.apply_to_response(resp)
    choice = out["choices"][0]
    check("非流式：message.content 变 null",
          choice["message"]["content"] is None, str(choice["message"]))
    check("非流式：message 带 tool_calls",
          choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather")
    check("非流式：finish_reason=tool_calls", choice["finish_reason"] == "tool_calls")
    check("非流式：顶层字段没多没少", set(out) == RESPONSE_KEYS, str(sorted(out)))

    resp0 = fg.to_openai_response(fg.FastGptResponse.model_validate(
        {"choices": [{"message": {"content": "0: 今天晴"}}]}),
        "deepseek-v3", 1, prompt_chars=10)
    tc.apply_to_response(resp0)
    check("非流式：`0:` 只剥前缀、不加 tool_calls",
          resp0["choices"][0]["message"]["content"] == "今天晴"
          and "tool_calls" not in resp0["choices"][0]["message"],
          str(resp0["choices"][0]["message"]))


def test_toolcall_stream() -> None:
    """[15] 提示词工具调用的流式帧。

    两个关键点：工具协议不能漏进可见正文；`1:` 要跨分片缓冲到 JSON 完整才能解析。
    """
    print("\n[15] 提示词工具调用的流式帧")

    def frames(chunks: list[str]) -> list[dict]:
        raw = ("".join(f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n"
                        for c in chunks) + "event: answer\ndata: [DONE]\n\n").encode()
        req = sc.ChatCompletionRequest.model_validate({
            "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
            "stream": True, "tools": _tools()})
        out = asyncio.run(_collect_req(raw, req))
        return [json.loads(x[6:]) for x in out if x.startswith("data: {")]

    def deltas(payloads: list[dict]) -> list[dict]:
        return [p["choices"][0]["delta"] for p in payloads if p["choices"]]

    # 1) `1:` 跨三个分片 → 只出 tool_calls 帧，正文为空
    payloads = frames(['1: {"name": "get_', 'weather", "arguments": {"city"', ': "杭州"}}'])
    ds = deltas(payloads)
    text = "".join(d.get("content") or "" for d in ds)
    check("协议标记不漏进正文", text == "", repr(text))

    calls = [d["tool_calls"][0] for d in ds if d.get("tool_calls")]
    check("流式出一条 tool_calls 帧", len(calls) == 1, str(ds))
    check("流式 tool_calls 带 index/id/type",
          calls and calls[0]["index"] == 0 and calls[0]["id"].startswith("call_")
          and calls[0]["type"] == "function", str(calls))
    check("流式 tool_calls 名字与参数正确",
          calls and calls[0]["function"]["name"] == "get_weather"
          and json.loads(calls[0]["function"]["arguments"]) == {"city": "杭州"})

    finishes = [p["choices"][0]["finish_reason"] for p in payloads if p["choices"]]
    check("finish_reason=tool_calls 且不出 stop",
          finishes.count("tool_calls") == 1 and "stop" not in finishes, str(finishes))
    check("tool_calls 帧排在收尾帧之前",
          ds.index(next(d for d in ds if d.get("tool_calls"))) < len(ds) - 1, str(ds))

    # 2) `0:` 跨分片 → 剥掉前缀后正文照常流
    payloads = frames(["0", ": 今天", "杭州晴"])
    text = "".join(d.get("content") or "" for d in deltas(payloads))
    check("`0:` 前缀被剥掉且正文完整", text == "今天杭州晴", repr(text))
    check("文本回复仍然是 stop",
          [p["choices"][0]["finish_reason"] for p in payloads
           if p["choices"]][-1] == "stop")

    # 3) 模型没按协议来 → 原样流出
    payloads = frames(["今天", "杭州晴"])
    text = "".join(d.get("content") or "" for d in deltas(payloads))
    check("不按协议时原样透传", text == "今天杭州晴", repr(text))

    # 4) 工具 JSON 没解析成功 → 退回正文（不吞内容）
    payloads = frames(["1: {不是 json"])
    text = "".join(d.get("content") or "" for d in deltas(payloads))
    check("解析失败时退回正文", text == "{不是 json", repr(text))

    # 5) 没带 tools → 不做任何加工（回归）
    raw = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '0: 今天晴'}}]})}\n\n"
           "event: answer\ndata: [DONE]\n\n").encode()
    req = sc.ChatCompletionRequest.model_validate({
        "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
        "stream": True})
    out = asyncio.run(_collect_req(raw, req))
    text = "".join((json.loads(x[6:])["choices"][0]["delta"].get("content") or "")
                   for x in out if x.startswith("data: {")
                   and json.loads(x[6:])["choices"])
    check("没带 tools 时不剥前缀（回归）", text == "0: 今天晴", repr(text))

    # 6) 带 tools 但服务端开关关闭 → 同样不做加工（`--no-tool-call`）
    raw6 = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '1: {\\"name\\": \\"f\\"}'}}]})}\n\n"
            "event: answer\ndata: [DONE]\n\n").encode()
    req6 = sc.ChatCompletionRequest.model_validate({
        "model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}],
        "stream": True, "tools": _tools()})
    out6_steps = []

    async def _collect_off() -> None:
        async for x in st.translate_stream(st.iter_stream_text(raw6.decode()),
                                          req6, model=req6.model, tool_call=False):
            out6_steps.append(x)

    asyncio.run(_collect_off())
    out6 = out6_steps
    payloads6 = [json.loads(x[6:]) for x in out6 if x.startswith("data: {")]
    text6 = "".join(p["choices"][0]["delta"].get("content") or ""
                    for p in payloads6 if p["choices"])
    check("开关关闭时正文原样透传（不剥前缀）", text6.startswith("1: "), repr(text6))
    check("开关关闭时不出 tool_calls",
          not any(p["choices"] and p["choices"][0]["delta"].get("tool_calls")
                  for p in payloads6), str(payloads6))
    check("开关关闭时 finish_reason 仍为 stop",
          [p["choices"][0]["finish_reason"] for p in payloads6
           if p["choices"]][-1] == "stop")

    # 7) 漏写冒号（`1 {json}`）在流式下也要缓冲成调用，不能漏进正文
    payloads7 = frames(["1 ", '{"name": "get_weather", ',
                        '"arguments": {"city": "杭州"}}'])
    ds7 = deltas(payloads7)
    text7 = "".join(d.get("content") or "" for d in ds7)
    check("漏写冒号时正文仍为空", text7 == "", repr(text7))
    check("漏写冒号也能出 tool_calls",
          any(d.get("tool_calls") and d["tool_calls"][0]["function"]["name"] == "get_weather"
              for d in ds7), str(ds7))
    check("漏写冒号时 finish_reason 仍是 tool_calls",
          [p["choices"][0]["finish_reason"] for p in payloads7
           if p["choices"]][-1] == "tool_calls")


class _AsyncGenStream(httpx.AsyncByteStream):
    """把异步生成器包成 httpx 的流（`MockTransport` 不认裸生成器）。"""

    def __init__(self, gen) -> None:
        self._gen = gen

    async def __aiter__(self):
        async for chunk in self._gen:
            yield chunk


def test_upstream_streaming() -> None:
    """上游连接必须真流式。

    回归点：`Upstream.chat` 曾用 `client.post()`，它预读整段正文，之后 `aiter_bytes()`
    只是回放内存 —— 表现为所有 SSE 帧同时到达、TTFB == 总耗时。
    """
    print("\n[11] 上游连接必须真流式")
    produced: list[int] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        async def body():
            for i in range(3):
                produced.append(i)
                data = json.dumps({"choices": [{"delta": {"content": str(i)}}]})
                yield f"event: answer\ndata: {data}\n\n".encode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_AsyncGenStream(body()))

    def make_upstream() -> up_mod.Upstream:
        return up_mod.Upstream({"username": "u", "cookie_header": "a=1"},
                               base="https://up.invalid")

    payload = fg.FastGptRequest(messages=[fg.FastGptMessage(role="user", content="hi")],
                                share_id="share-1", stream=True)

    async def run() -> tuple[tuple[bool, bool, list[int]], bytes]:
        up = make_upstream()
        spare = up._client                       # noqa: SLF001 测试里直接换掉传输层
        up._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            resp = await up.chat(payload)
            snap = (resp.is_stream_consumed, resp.is_closed, list(produced))
            raw = b"".join([c async for c in resp.aiter_bytes()])
            await resp.aclose()
            return snap, raw
        finally:
            await up.aclose()
            await spare.aclose()

    (consumed, closed, produced_at_return), raw = asyncio.run(run())
    check("chat() 返回时正文没被预读", consumed is False, f"is_stream_consumed={consumed}")
    check("chat() 返回时响应未关闭", closed is False, f"is_closed={closed}")
    check("chat() 返回时上游尚未被消费", produced_at_return == [], str(produced_at_return))
    check("随后能逐段读到上游分片", raw.count(b"event: answer") == 3, str(raw[:120]))

    async def control() -> tuple[bool, bool]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            resp = await client.post("https://up.invalid/chat", json={})
            return resp.is_stream_consumed, resp.is_closed
        finally:
            await client.aclose()

    c_consumed, c_closed = asyncio.run(control())
    check("对照：便捷方法 post() 确实会预读并关闭（说明上面几条断言有效）",
          c_consumed is True and c_closed is True,
          f"consumed={c_consumed} closed={c_closed}")

    async def run_403() -> up_mod.UpstreamError | None:
        up = make_upstream()
        spare = up._client
        up._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _r: httpx.Response(403, text='{"message":"forbidden"}')))
        try:
            await up.chat(payload)
            return None
        except up_mod.UpstreamError as exc:
            return exc
        finally:
            await up.aclose()
            await spare.aclose()

    exc = asyncio.run(run_403())
    check("非 2xx 抛 UpstreamError 并保留状态码与正文",
          exc is not None and exc.status_code == 403 and "forbidden" in exc.body, repr(exc))


async def _collect_req(raw: bytes, req: sc.ChatCompletionRequest) -> list[str]:
    """按给定请求收集流式 SSE 文本。"""
    return [x async for x in st.translate_stream(st.iter_stream_text(raw.decode()),
                                                req, model=req.model)]


async def _collect(raw: bytes, include_usage: bool) -> list[str]:
    req = sc.ChatCompletionRequest.model_validate({
        "model": "deepseek-v3",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        **({"stream_options": {"include_usage": True}} if include_usage else {}),
    })
    return await _collect_req(raw, req)


class _Capture(logging.Handler):
    """把遥测日志收进列表（离线自检用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def _collect_with_telemetry(raw: bytes) -> list[str]:
    """带遥测的流式收集（`telemetry` 由 `translate_stream` 自己收尾）。"""
    req = sc.ChatCompletionRequest.model_validate({
        "model": "deepseek-v3",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    rl = tm.RequestLog(req.model, stream=True, messages=len(req.messages),
                       prompt_chars=sc.count_chars(req.messages))
    rl.input_line()
    return [x async for x in st.translate_stream(st.iter_stream_text(raw.decode()),
                                                req, model=req.model, telemetry=rl)]


def test_telemetry() -> None:
    """[13] 控制台遥测：请求进来一行输入状态，输出完毕一块统计。

    只校验**_日志文本**（遥测不进响应体）；响应仍是规范结构，由 [5]/[9] 卡着。
    """
    print("\n[13] 控制台遥测（输入状态 / 输出统计）")

    check("_tps 正常算数值", tm._tps(652, 10.0) == "65.2 tok/s", tm._tps(652, 10.0))
    check("生成窗口过短不报 TPS", tm._tps(652, 0.01) == "—", tm._tps(652, 0.01))
    check("没有 token 不报 TPS", tm._tps(0, 10.0) == "—", tm._tps(0, 10.0))

    logger = logging.getLogger("shu-ds-poc")
    saved = (logger.level, logger.propagate)
    logger.setLevel(logging.INFO)
    logger.propagate = False                 # 免得 WARNING 落到 lastResort 去刷 stderr
    cap = _Capture()
    logger.addHandler(cap)
    try:
        # 1) 非流式：一行输入 + 一块统计
        rl = tm.RequestLog("deepseek-v3", stream=False, messages=3, prompt_chars=412)
        rl.input_line()
        rl.mark_ttfb()
        rl.finish(224, 652)
        rl.finish(1, 1)                      # 重复收尾不再打
        rl.fail("UpstreamError")             # 已收尾 → 也不打失败行

        check("输入行：模型 / 流式 / 消息数 / prompt 字符数",
              cap.lines[0] == "→ deepseek-v3  stream=false  消息=3  prompt=412 字符",
              cap.lines[0])
        block = cap.lines[1]
        for label in ("输入", "输出", "模型生成 TPS", "首 Token 耗时",
                      "端到端吞吐", "端到端耗时"):
            check(f"统计块含「{label}」", label in block, block)
        check("统计块用调用方传入的 tokens",
              "  输入          224 tokens" in block
              and "  输出          652 tokens" in block, block)
        check("非流式不报模型生成 TPS",
              re.search(r"模型生成 TPS  —", block) is not None, block)
        check("重复 finish / 收尾后 fail 都不再打", len(cap.lines) == 2, str(cap.lines))

        # 2) 失败路径有收尾行，不留半截日志
        cap.lines.clear()
        rl3 = tm.RequestLog("m", stream=False, messages=1, prompt_chars=1)
        rl3.input_line()
        rl3.fail("UpstreamError")
        check("失败路径有收尾行",
              cap.lines[1].startswith("← 失败 UpstreamError"), cap.lines[1])

        # 3) 流式：首个分片打点后生成窗口可量 → 报数值 TPS
        cap.lines.clear()
        rl2 = tm.RequestLog("deepseek-r1", stream=True, messages=1, prompt_chars=10)
        rl2.input_line()
        rl2.mark_ttfb()
        rl2.mark_first_token()
        time.sleep(0.06)
        rl2.finish(100, 200)
        check("流式报出数值 TPS",
              re.search(r"模型生成 TPS  \d+\.\d tok/s", cap.lines[1]) is not None,
              cap.lines[1])

        # 4) 上游 chatNode 的真实 token 数走到统计块
        cap.lines.clear()
        raw = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '你好'}}]})}\n\n"
               f"event: flowNodeResponse\ndata: {json.dumps({'moduleType': 'chatNode', 'inputTokens': 224, 'outputTokens': 652})}\n\n"
               "event: answer\ndata: [DONE]\n\n").encode()
        asyncio.run(_collect_with_telemetry(raw))
        block = cap.lines[1]
        check("流式统计用上游 chatNode 的真实 tokens",
              "  输入          224 tokens" in block
              and "  输出          652 tokens" in block, block)
        check("真实值时不标注估算", "字符估算" not in block, block)
        check("流式有首 Token 耗时数值",
              re.search(r"首 Token 耗时 \d+\.\d+ s", block) is not None, block)

        # 5) 上游不给统计 → 按字符估算，块头要标注
        cap.lines.clear()
        raw2 = (f"event: answer\ndata: {json.dumps({'choices': [{'delta': {'content': '你好'}}]})}\n\n"
                "event: answer\ndata: [DONE]\n\n").encode()
        asyncio.run(_collect_with_telemetry(raw2))
        block = cap.lines[1]
        check("上游不给统计时块头标注字符估算", "字符估算" in block, block)
        check("估算的输入 = prompt 字符数 × 2/3",
              f"  输入          {fg.estimate_tokens(2)} tokens" in block, block)
    finally:
        logger.removeHandler(cap)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


def test_sso_submodule() -> None:
    """[12] 本地层与上游子模块的契约。

    `sso/` 是薄适配：本地只留 client/config/runner/ui/utils，其余（qr/registry/
    rsa_key/system_api）别名到子模块。这里把「哪些归本地、上游该有哪些方法」定成
    断言，上游改签名或挪文件时离线自检先挂。
    """
    print("\n[12] sso 本地层 ← 子模块契约")
    vendor = Path(sso.VENDOR_DIR)
    check("子模块已就位", (vendor / "src" / "client.py").is_file(), str(vendor))

    for name in ("qr", "registry", "rsa_key", "system_api"):
        mod = getattr(sso, name)
        path = Path(getattr(mod, "__file__", ""))
        check(f"sso.{name} 就是上游模块",
              vendor in path.parents or path.parent == vendor / "src", str(path))
        check(f"sso.{name} 已登记在 sys.modules",
              sys.modules.get(f"sso.{name}") is mod)

    for name in ("client", "config", "runner", "ui", "utils"):
        mod = __import__(f"sso.{name}", fromlist=[name])
        path = Path(getattr(mod, "__file__", ""))
        check(f"sso.{name} 是本地实现",
              path.parent.name == "sso", str(path))

    # 本地子类必须继承上游，且上游的公开方法一个都不能少（防签名漂移）
    from src.client import ShuSSO as UpstreamShuSSO          # noqa: PLC0415
    from sso.client import ShuSSO as LocalShuSSO             # noqa: PLC0415
    check("ShuSSO 继承自上游", issubclass(LocalShuSSO, UpstreamShuSSO))
    for method in ("login", "send_2fa_code", "verify_2fa_code", "authorize",
                   "redeem", "bootstrap_state", "wecom_qrcode_info",
                   "wecom_wait_scan", "wecom_redeem", "record"):
        check(f"上游 ShuSSO.{method}() 仍在",
              callable(getattr(LocalShuSSO, method, None)))
    check("本地加了 cookies()", callable(getattr(LocalShuSSO, "cookies", None)))
    check("本地加了 cookie_header()",
          callable(getattr(LocalShuSSO, "cookie_header", None)))

    # 上游的落盘路径必须被改写，否则会往子模块目录里写东西
    from sso import rsa_key as local_rsa                     # noqa: PLC0415
    check("rsa 公钥缓存落在项目根",
          Path(local_rsa.CACHE_PATH).parent == ROOT, str(local_rsa.CACHE_PATH))
    import src.config as upstream_cfg                        # noqa: PLC0415
    check("上游 CAPTURE_DIR 指向本项目 captures/",
          Path(upstream_cfg.CAPTURE_DIR) == ROOT / "captures",
          str(upstream_cfg.CAPTURE_DIR))

    check("SYSTEMS 只留 ds", list(config.SYSTEMS) == ["ds"],
          str(list(config.SYSTEMS)))
    check("ds 配置来自子模块",
          Path(_ds_folder()).is_relative_to(vendor), str(_ds_folder()))

    # 脱敏要比上游严：凭据类的键一律不能进证据文件
    from sso import utils as local_utils                     # noqa: PLC0415
    masked = local_utils.redact({"accessToken": "t", "privatekey": "p",
                                 "cookie_header": "uname=x", "SHU_OAUTH2": "y",
                                 "password": "z"})
    check("本地 redact 连令牌/Cookie 一起抹",
          all(v == "***REDACTED***" for v in masked.values()), str(masked))
    check("mask_cookie_header 逐项脱敏",
          "uname=abcdef...klmnop" in local_utils.mask_cookie_header("uname=abcdefghijklmnop"),
          local_utils.mask_cookie_header("uname=abcdefghijklmnop"))


def _ds_folder() -> Path:
    """ds 系统配置所在的目录（用来证明它来自子模块而不是本地副本）。"""
    return Path(sso.registry.systems_dirs()["ds"])


def main() -> int:
    print("=" * 62)
    print(" shu-ds-poc 离线自检（不联网）")
    print("=" * 62)

    test_registry()
    with tempfile.TemporaryDirectory() as d:
        test_credentials(Path(d))
    test_model_resolution()
    test_request_schema()
    test_transform()
    test_response_shape()
    test_usage_estimation()
    test_sse_framing()
    test_stream_frames()
    test_stream_reasoning()
    test_upstream_streaming()
    test_telemetry()
    test_toolcall_protocol()
    test_toolcall_stream()
    test_sso_submodule()

    print()
    print("=" * 62)
    print(f" 通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   ✗ {name}: {detail}")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
