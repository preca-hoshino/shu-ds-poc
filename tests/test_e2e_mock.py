#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自检（不联网）：用本地 mock 上游验证 HTTP 层的规范一致性。

覆盖：鉴权 401、端点面（其余 404）、非流式字段、流式帧序列、模型校验、
请求体 400、上游错误透传、上游收到的请求体确实是上游格式。

用法: .venv\\Scripts\\python.exe tests\\test_e2e_mock.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proxy import fastgpt as fg                          # noqa: E402
from proxy import server as server_mod                   # noqa: E402

FAKE_CREDS = {
    "version": 1,
    "username": "25123368",
    "display_name": "测试同学",
    "created_at": "2026-09-23T10:00:00",
    "share_id": "default-share",
    "cookie_header": "uname=25123368; _webvpn_key=fake-key",
    "upstream": {"base": "http://mock", "chat_path": "/api/v2/chat/completions"},
    "ds": {"userid": "25123368", "access_token": "ACC", "private_key": "PK"},
}

API_KEY = "sk-test-key"

#: 最近一次 mock 上游收到的请求
CAPTURED: dict = {}

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []

RESPONSE_KEYS = {"id", "object", "created", "model", "choices", "usage"}
USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens",
              "prompt_tokens_details", "completion_tokens_details"}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# ---------------------------------------------------------------------------
# mock 上游
# ---------------------------------------------------------------------------

def make_mock_upstream() -> FastAPI:
    app = FastAPI()

    @app.post("/api/v2/chat/completions")
    async def chat(request: Request):                      # noqa: D103
        body = await request.json()
        CAPTURED.clear()
        CAPTURED.update({
            "body": body,
            "cookie": request.headers.get("cookie"),
            "referer": request.headers.get("referer"),
            "origin": request.headers.get("origin"),
        })

        if "bad" in (request.headers.get("cookie") or ""):
            return JSONResponse(status_code=403, content={"message": "shareId invalid"})

        # 末条消息开头的哨兵决定返回哪种上游形态（默认：分段数组）
        last = (body.get("messages") or [{}])[-1].get("content")
        mode = ""
        if isinstance(last, str):
            if last.startswith("field:"):
                mode = "field"
            elif last.startswith("stats:"):
                mode = "stats"

        if body.get("stream"):
            return StreamingResponse(_mock_sse(mode), media_type="text/event-stream")

        if mode == "field":
            return JSONResponse({
                "choices": [{"message": {"content": "答案是 42",
                                          "reasoning_content": "字段里的推理"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22,
                          "total_tokens": 33},
            })

        return JSONResponse({
            "choices": [{"message": {"content": [
                {"type": "reasoning", "reasoning": {"content": "我在思考"}},
                {"type": "text", "text": {"content": "答案是 42"}},
            ]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        })

    return app


async def _mock_sse(mode: str = ""):
    """与上游同构的 SSE：两个 answer + 一个 flowNodeResponse，混用分隔符。

    mode="stats" 模拟「上游只在收尾统计里给推理链」：answer 增量里没有任何
    reasoning_content，只有 chatNode 帧带 `reasoningText`。
    """
    if mode == "stats":
        yield b"event: answer\ndata: " + json.dumps(
            {"choices": [{"delta": {"content": "你好"}}]}
        ).encode() + b"\n\n"
        yield b"event: flowNodeResponse\ndata: " + json.dumps(
            {"moduleType": "chatNode", "inputTokens": 7, "outputTokens": 9,
             "tokens": 16, "reasoningText": "收尾统计里的推理",
             "finishReason": "stop"}
        ).encode() + b"\n\n"
        yield b"event: answer\ndata: " + json.dumps(
            {"choices": [{"delta": {"content": None}, "finish_reason": "stop"}]}
        ).encode() + b"\n\n"
        yield b"event: answer\ndata: [DONE]\n\n"
        return

    yield b"event: answer\ndata: " + json.dumps(
        {"choices": [{"delta": {"reasoning_content": "推理"}}]}
    ).encode() + b"\n\n"
    yield b"event: answer\ndata: " + json.dumps(
        {"choices": [{"delta": {"content": "你好"}}]}
    ).encode() + b"\r\n\r\n"
    yield b"event: flowNodeResponse\ndata: " + json.dumps(
        {"moduleType": "chatNode", "inputTokens": 7, "outputTokens": 9, "tokens": 16}
    ).encode() + b"\n\n"
    yield b"event: answer\ndata: " + json.dumps(
        {"choices": [{"delta": {"content": "！"}}]}
    ).encode() + b"\n\n"
    yield b"event: answer\ndata: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


def wire_mock(proxy_app: FastAPI, mock_app: FastAPI) -> None:
    """把代理的上游客户端指向 mock。"""
    proxy_app.state.upstream._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock")


def auth() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


def sse_payloads(text: str) -> list[dict]:
    """把 SSE 文本里的 JSON 帧解析出来（跳过 [DONE]）。"""
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            body = line[5:].strip()
            if body and body != "[DONE]":
                out.append(json.loads(body))
    return out


# ---------------------------------------------------------------------------

async def run_tests() -> None:
    mock_app = make_mock_upstream()
    proxy_app = server_mod.make_app(FAKE_CREDS, api_key=API_KEY, verify=False)

    async with client_for(proxy_app) as pc:
        async with proxy_app.router.lifespan_context(proxy_app):
            wire_mock(proxy_app, mock_app)

            print("\n[1] 鉴权")
            r = await pc.get("/v1/models")
            check("缺 key → 401", r.status_code == 401, str(r.status_code))
            check("401 是 OpenAI 错误体",
                  set(r.json()) == {"error"}
                  and set(r.json()["error"]) == {"message", "type", "param", "code"},
                  r.text[:200])
            check("401 的 code 是 invalid_api_key",
                  r.json()["error"]["code"] == "invalid_api_key", r.text[:200])
            check("401 的 type 是 invalid_request_error",
                  r.json()["error"]["type"] == "invalid_request_error", r.text[:200])

            r = await pc.get("/v1/models", headers={"Authorization": "Bearer wrong"})
            check("错 key → 401", r.status_code == 401, str(r.status_code))
            r = await pc.get("/v1/models", headers={"X-API-Key": API_KEY})
            check("X-API-Key 不再被接受 → 401", r.status_code == 401, str(r.status_code))

            r = await pc.get("/v1/models", headers=auth())
            check("对 key → 200", r.status_code == 200, str(r.status_code))
            check("models 形状与规范一致",
                  set(r.json()) == {"object", "data"}
                  and r.json()["object"] == "list"
                  and [m["id"] for m in r.json()["data"]] == ["deepseek-v3", "deepseek-r1"]
                  and all(set(m) == {"id", "object", "created", "owned_by"}
                          for m in r.json()["data"]),
                  r.text[:300])

            print("\n[2] 端点面")
            for path in ("/", "/healthz", "/docs", "/redoc", "/openapi.json"):
                r = await pc.get(path, headers=auth())
                check(f"{path} → 404", r.status_code == 404, str(r.status_code))
            r = await pc.get("/v1/chat/completions", headers=auth())
            check("GET 对话端点 → 405", r.status_code == 405, str(r.status_code))

            print("\n[3] 非流式")
            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "deepseek-r1",
                                    "messages": [{"role": "user", "content": "1+1=?"}],
                                    "stream": False})
            check("HTTP 200", r.status_code == 200, r.text[:300])
            data = r.json()
            check("顶层字段与规范一致", set(data) == RESPONSE_KEYS, str(sorted(data)))
            check("model 回显请求值", data["model"] == "deepseek-r1", data["model"])
            choice = data["choices"][0]
            check("choice 字段与规范一致",
                  set(choice) == {"index", "message", "logprobs", "finish_reason"},
                  str(sorted(choice)))
            check("正文归到 content",
                  choice["message"]["content"] == "答案是 42", str(choice["message"]))
            check("推理归到 reasoning_content",
                  choice["message"]["reasoning_content"] == "我在思考",
                  str(choice["message"]))
            check("usage 字段与规范一致", set(data["usage"]) == USAGE_KEYS,
                  str(sorted(data["usage"])))
            check("usage 沿用上游真实值",
                  data["usage"]["prompt_tokens"] == 11
                  and data["usage"]["total_tokens"] == 33, str(data["usage"]))
            check("响应里没有自造字段",
                  "total_points" not in r.text and "_raw" not in r.text, r.text[:200])

            # 上游把推理链放在 message.reasoning_content 字段上的形态
            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "deepseek-r1",
                                    "messages": [{"role": "user", "content": "field:走字段形态"}]})
            check("字段形态：HTTP 200", r.status_code == 200, r.text[:200])
            msg = r.json()["choices"][0]["message"]
            check("字段形态：推理归到 reasoning_content",
                  msg.get("reasoning_content") == "字段里的推理", str(msg))
            check("字段形态：正文归到 content", msg.get("content") == "答案是 42", str(msg))
            check("字段形态：message 键仍是规范三个",
                  set(msg) == {"role", "content", "reasoning_content"}, str(sorted(msg)))
            check("字段形态：reasoning_tokens 按占比切",
                  0 < r.json()["usage"]["completion_tokens_details"]["reasoning_tokens"]
                  <= r.json()["usage"]["completion_tokens"],
                  str(r.json()["usage"]))

            sent = CAPTURED["body"]
            raw_sent = json.dumps(sent, ensure_ascii=False)
            check("上游收到 camelCase 字段",
                  all(f'"{k}"' in raw_sent
                      for k in ("shareId", "responseChatItemId", "dataId", "hideInUI",
                                "outLinkUid", "detail")), raw_sent[:300])
            check("上游没收到规范参数",
                  "temperature" not in raw_sent and "max_tokens" not in raw_sent,
                  raw_sent[:300])
            check("R1 走独立分享链", sent["shareId"] == fg.R1_SHARE_ID, sent["shareId"])
            check("system 消息被透传",
                  all(m["role"] != "system" for m in sent["messages"]))
            check("variables 四个固定字段",
                  set(sent["variables"]) == {"userId", "accessToken", "privatekey", "cTime"},
                  str(sent["variables"]))
            check("Cookie 头已带",
                  CAPTURED["cookie"] == "uname=25123368; _webvpn_key=fake-key",
                  str(CAPTURED["cookie"]))
            check("Referer 指向分享页",
                  CAPTURED["referer"].endswith(
                      f"/chat/share?shareId={fg.R1_SHARE_ID}"),
                  str(CAPTURED["referer"]))

            print("\n[4] model 校验")
            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "gpt-4",
                                    "messages": [{"role": "user", "content": "x"}]})
            check("未知模型 → 404", r.status_code == 404, str(r.status_code))
            check("code 是 model_not_found",
                  r.json()["error"]["code"] == "model_not_found", r.text[:200])
            check("param 指向 model",
                  r.json()["error"]["param"] == "model", r.text[:200])

            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "deepseek-chat",
                                    "messages": [{"role": "user", "content": "x"}]})
            check("deepseek-chat 别名可用", r.status_code == 200, r.text[:200])
            check("别名不影响分享链（仍用默认链）",
                  CAPTURED["body"]["shareId"] == "default-share",
                  str(CAPTURED["body"]["shareId"]))
            check("model 回显别名", r.json()["model"] == "deepseek-chat",
                  r.json().get("model"))

            print("\n[5] 请求体错误")
            r = await pc.post("/v1/chat/completions", headers=auth(),
                              content=b"{not json")
            check("非 JSON → 400", r.status_code == 400, str(r.status_code))
            check("400 是 OpenAI 错误体", "error" in r.json(), r.text[:200])

            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "deepseek-v3"})
            check("缺 messages → 400", r.status_code == 400, str(r.status_code))
            check("400 的 param 指向出错字段",
                  r.json()["error"]["param"] == "messages", r.text[:200])

            print("\n[6] 流式")
            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "deepseek-r1",
                                    "messages": [{"role": "user", "content": "hi"}],
                                    "stream": True,
                                    "stream_options": {"include_usage": True}})
            check("HTTP 200", r.status_code == 200, r.text[:300])
            check("Content-Type 是 SSE",
                  "text/event-stream" in r.headers.get("content-type", ""),
                  r.headers.get("content-type", ""))
            check("末尾是 [DONE]", r.text.rstrip().endswith("[DONE]"), r.text[-40:])

            payloads = sse_payloads(r.text)
            non_usage = [p for p in payloads if p.get("choices")]
            check("首帧是 role 帧",
                  non_usage[0]["choices"][0]["delta"]
                  == {"role": "assistant", "content": ""}, str(non_usage[0]))
            check("有 finish_reason=stop 的收尾帧",
                  any(p["choices"][0]["finish_reason"] == "stop" for p in non_usage),
                  str([p["choices"][0]["finish_reason"] for p in non_usage]))
            check("收尾帧 delta 为空",
                  [p["choices"][0]["delta"] for p in non_usage
                   if p["choices"][0]["finish_reason"] == "stop"] == [{}],
                  str(non_usage[-1]))

            reasoning = "".join(p["choices"][0]["delta"].get("reasoning_content", "")
                                for p in non_usage)
            check("推理增量已转出", reasoning == "推理", reasoning)
            content = "".join(p["choices"][0]["delta"].get("content", "")
                              for p in non_usage)
            check("正文增量已转出（含 CRLF 分隔那帧）", content == "你好！", content)
            check("每帧 choice 都带 logprobs",
                  all("logprobs" in p["choices"][0] for p in non_usage), "缺 logprobs")

            usage_frames = [p for p in payloads if p.get("usage")]
            check("有 usage 帧", len(usage_frames) == 1, str(usage_frames))
            if usage_frames:
                u = usage_frames[0]["usage"]
                check("usage 帧的 choices 为空", usage_frames[0]["choices"] == [])
                check("usage 采用 flowNodeResponse 真实值",
                      u["prompt_tokens"] == 7 and u["completion_tokens"] == 9, str(u))
                check("usage 结构规范", set(u) == USAGE_KEYS, str(sorted(u)))
                check("usage 无 total_points", "total_points" not in u, str(u))

            # 上游只在收尾统计里给推理链（reasoningText）的形态
            r = await pc.post("/v1/chat/completions", headers=auth(),
                              json={"model": "deepseek-r1",
                                    "messages": [{"role": "user", "content": "stats:只看收尾统计"}],
                                    "stream": True,
                                    "stream_options": {"include_usage": True}})
            check("收尾统计形态：HTTP 200", r.status_code == 200, r.text[:200])
            check("收尾统计形态：末尾是 [DONE]",
                  r.text.rstrip().endswith("[DONE]"), r.text[-40:])

            payloads2 = sse_payloads(r.text)
            non_usage2 = [p for p in payloads2 if p.get("choices")]
            reasoning2 = "".join(p["choices"][0]["delta"].get("reasoning_content", "")
                                 for p in non_usage2)
            check("收尾统计形态：推理链仍被转出",
                  reasoning2 == "收尾统计里的推理", repr(reasoning2))
            content2 = "".join(p["choices"][0]["delta"].get("content", "")
                               for p in non_usage2)
            check("收尾统计形态：正文不受影响", content2 == "你好", repr(content2))
            idx_reason = [i for i, p in enumerate(non_usage2)
                          if p["choices"][0]["delta"].get("reasoning_content")]
            idx_finish = [i for i, p in enumerate(non_usage2)
                          if p["choices"][0]["finish_reason"] == "stop"]
            check("收尾统计形态：推理帧排在收尾帧之前",
                  idx_reason and idx_finish and idx_reason[0] < idx_finish[0],
                  f"reason={idx_reason} finish={idx_finish}")
            check("收尾统计形态：帧字段仍完整",
                  all(set(p["choices"][0]) == {"index", "delta", "logprobs", "finish_reason"}
                      for p in non_usage2), "字段不全")

            print("\n[7] 上游错误透传")
            broken = json.loads(json.dumps(FAKE_CREDS))
            broken["cookie_header"] = "uname=bad; _webvpn_key=bad"
            proxy2 = server_mod.make_app(broken, api_key=API_KEY, verify=False)
            async with proxy2.router.lifespan_context(proxy2):
                wire_mock(proxy2, mock_app)
                async with client_for(proxy2) as pc2:
                    r = await pc2.post("/v1/chat/completions", headers=auth(),
                                       json={"model": "deepseek-v3",
                                             "messages": [{"role": "user", "content": "x"}]})
                    check("上游 403 状态码沿用", r.status_code == 403, str(r.status_code))
                    body = r.json()
                    check("上游错误也是 OpenAI 错误体",
                          set(body) == {"error"} and set(body["error"])
                          == {"message", "type", "param", "code"}, str(body))
                    check("type 是 api_error", body["error"]["type"] == "api_error",
                          str(body))
                    check("上游原文带上", "shareId invalid" in body["error"]["message"],
                          str(body))


def main() -> int:
    print("=" * 62)
    print(" shu-ds-poc 端到端自检（本地 mock 上游，不联网）")
    print("=" * 62)
    asyncio.run(run_tests())
    print()
    print("=" * 62)
    print(f" 通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   ✗ {name}: {detail}")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
