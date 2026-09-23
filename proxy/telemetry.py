"""服务端控制台遥测：每个请求一行「输入状态」+ 一块「输出统计」。

只写日志，不进响应体 —— 客户端拿到的仍是严格规范的 OpenAI 结构。
logger 名与 `proxy/` 其余模块一致（`shu-ds-poc`），由 `poc.py` 接到 stdout，
`--log-level` 决定是否显示（默认 `info` 就能看到）。

输出样例::

    → deepseek-v3  stream=true  消息=3  prompt=412 字符
    ← 输出完毕
      输入          224 tokens
      输出          652 tokens
      模型生成 TPS   66.0 tok/s
      首 Token 耗时  1.20 s
      端到端吞吐     24.4 tok/s
      端到端耗时     35.88 s

口径
----
- 「首 Token 耗时」= 请求发出 → 上游首个内容分片下发；非流式没有增量下发，
  退化为上游响应头到达时刻（等同 TTFB）。
- 「模型生成 TPS」= 输出 tokens / (首 Token → 结束)；生成窗口过短时不报数值。
- 「端到端吞吐」= (输入 + 输出) tokens / 端到端耗时，含排队与传输开销。
- tokens 取自上游 chatNode 统计；上游不给时按字符数估算，此时块头会标注。
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("shu-ds-poc")

#: 生成窗口小于该值（秒）就不报「模型生成 TPS」——非流式下窗口恒为 0
_MIN_GEN_WINDOW = 0.05

#: 「模型生成 TPS」不可用时的占位
_NO_VALUE = "—"


def _tps(tokens: int, seconds: float) -> str:
    """算 TPS；窗口过短或没有 token 时给占位符。"""
    if tokens <= 0 or seconds < _MIN_GEN_WINDOW:
        return _NO_VALUE
    return f"{tokens / seconds:.1f} tok/s"


class RequestLog:
    """单个请求的计时与统计。

    用法::

        rl = RequestLog(model, stream=req.stream, messages=len(req.messages),
                        prompt_chars=sc.count_chars(req.messages))
        rl.input_line()
        ...                                    # 上游响应头到达
        rl.mark_ttfb()
        ...                                    # 流式：首个内容分片下发
        rl.mark_first_token()
        rl.finish(prompt_tokens, completion_tokens, estimated=False)

    失败路径调 `fail()`，避免只打了输入行却没有下文。
    """

    def __init__(self, model: str, *, stream: bool, messages: int,
                 prompt_chars: int) -> None:
        self.model = model
        self.stream = stream
        self.messages = messages
        self.prompt_chars = prompt_chars
        self._t0 = time.perf_counter()
        self._ttfb: float | None = None
        self._first: float | None = None
        self._done = False

    # ---- 打点 --------------------------------------------------------

    def input_line(self) -> None:
        """请求进来时打一行：模型 / 是否流式 / 消息数 / prompt 字符数。"""
        log.info("→ %s  stream=%s  消息=%d  prompt=%d 字符", self.model,
                 "true" if self.stream else "false", self.messages,
                 self.prompt_chars)

    def mark_ttfb(self) -> None:
        """上游响应头到达（第一次打点生效）。"""
        if self._ttfb is None:
            self._ttfb = time.perf_counter() - self._t0

    def mark_first_token(self) -> None:
        """首个内容分片下发（第一次打点生效）。"""
        if self._first is None:
            self._first = time.perf_counter() - self._t0

    # ---- 收尾 --------------------------------------------------------

    def finish(self, prompt_tokens: int, completion_tokens: int, *,
               estimated: bool = False) -> None:
        """输出完毕：打一块统计。重复调用只生效一次。"""
        if self._done:
            return
        self._done = True

        elapsed = time.perf_counter() - self._t0
        head = self._first if self._first is not None else self._ttfb
        gen_window = 0.0 if head is None else max(0.0, elapsed - head)

        log.info(
            "← 输出完毕%s\n"
            "  输入          %d tokens\n"
            "  输出          %d tokens\n"
            "  模型生成 TPS  %s\n"
            "  首 Token 耗时 %s\n"
            "  端到端吞吐    %s\n"
            "  端到端耗时    %.2f s",
            "（tokens 为字符估算）" if estimated else "",
            prompt_tokens,
            completion_tokens,
            _tps(completion_tokens, gen_window),
            f"{head:.2f} s" if head is not None else _NO_VALUE,
            _tps(prompt_tokens + completion_tokens, elapsed),
            elapsed,
        )

    def fail(self, reason: str) -> None:
        """请求没跑完（上游出错 / 客户端断开）：打一行收尾，避免留个没下文的输入行。"""
        if self._done:
            return
        self._done = True
        log.warning("← 失败 %s  （历时 %.2f s）", reason,
                    time.perf_counter() - self._t0)
