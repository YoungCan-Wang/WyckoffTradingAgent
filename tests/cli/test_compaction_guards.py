"""Compaction failure visibility and context-overflow hard limit."""

from __future__ import annotations

import logging

import pytest

from cli.compaction import (
    MIN_SUMMARY_CHARS,
    compact_messages,
    enforce_context_limit,
    estimate_text_tokens,
    estimate_tokens,
    get_compact_threshold,
)

_MODEL = "test-model"

# 实测于 poolside/laguna-xs-2.1（OpenRouter），(文本, 真实 input_tokens)。
# 估算必须 >= 真实值：低估会让超限兜底放过真正超窗的请求，直接吃 400。
_REAL_TOKEN_SAMPLES = [
    ("纯中文", "华勤技术奕瑞科技持仓复盘主力放量出货震荡洗盘吸筹派发" * 200, 6_622),
    ("中文长句", "今天大盘出现明显下杀，创业板指数跌幅扩大，市场宽度急剧收窄，主力资金转入派发阶段。" * 80, 3_541),
    ("纯英文", "The composite man accumulates supply before markup begins in earnest. " * 120, 1_461),
    ("中英混合", "华勤技术 603296 close=76.02 vol_ratio=3.2 SOS triggered 主力放量出货 " * 120, 4_101),
    ("JSON", '{"symbol":"603296.SH","close":76.02,"名称":"华勤技术"},' * 150, 3_772),
]


def _history(tool_rounds: int, *, chunk: int = 3000) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "分析持仓"}]
    for i in range(tool_rounds):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "name": "p", "args": {}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "p", "content": "数据" * chunk})
    messages.append({"role": "assistant", "content": "结论"})
    return messages


class _Provider:
    """Emits `text_delta`, which is the event type _collect_stream_text reads."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def chat_stream(self, *args, **kwargs):
        self.calls += 1
        yield {"type": "text_delta", "text": self.text}
        yield {"type": "finish", "reason": "stop"}


class _Boom:
    def chat_stream(self, *args, **kwargs):
        raise RuntimeError("gateway 500")


class _WrongEventType:
    """Mirrors a provider whose event names drift from what compaction expects."""

    def chat_stream(self, *args, **kwargs):
        yield {"type": "content", "text": "看起来像摘要但事件类型不对" * 5}
        yield {"type": "finish", "reason": "stop"}


class TestCompactionFailureIsVisible:
    def test_warns_when_summary_request_raises(self, caplog):
        messages = _history(11)
        with caplog.at_level(logging.WARNING, logger="cli.compaction"):
            out, compacted = compact_messages(list(messages), _Boom(), _MODEL, 64_000)
        assert compacted is False
        assert len(out) == len(messages)
        assert "上下文压缩失败" in caplog.text

    def test_warns_when_summary_too_short(self, caplog):
        messages = _history(11)
        with caplog.at_level(logging.WARNING, logger="cli.compaction"):
            _, compacted = compact_messages(list(messages), _Provider("太短"), _MODEL, 64_000)
        assert compacted is False
        assert f"低于 {MIN_SUMMARY_CHARS}" in caplog.text

    def test_warns_when_provider_event_type_drifts(self, caplog):
        # 摘要恒为空会让压缩静默失效，上下文一路涨到被网关拒绝。
        messages = _history(11)
        with caplog.at_level(logging.WARNING, logger="cli.compaction"):
            _, compacted = compact_messages(list(messages), _WrongEventType(), _MODEL, 64_000)
        assert compacted is False
        assert "上下文压缩失败" in caplog.text

    def test_no_warning_on_success(self, caplog, tmp_path):
        messages = _history(11)
        provider = _Provider("这是一段足够长的压缩摘要，覆盖之前的持仓分析上下文。" * 3)
        with caplog.at_level(logging.WARNING, logger="cli.compaction"):
            out, compacted = compact_messages(list(messages), provider, _MODEL, 64_000, archive_dir=str(tmp_path))
        assert compacted is True
        assert len(out) < len(messages)
        assert "压缩失败" not in caplog.text


class TestEnforceContextLimit:
    def test_noop_when_within_limit(self):
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "你好"}]
        out, info = enforce_context_limit(messages, _MODEL, 64_000)
        assert out == messages
        assert info is None

    def test_drops_oldest_until_within_limit(self, caplog):
        messages = _history(30)
        limit = get_compact_threshold(_MODEL, 64_000)
        assert estimate_tokens(messages) > limit
        with caplog.at_level(logging.WARNING, logger="cli.compaction"):
            out, info = enforce_context_limit(messages, _MODEL, 64_000)
        assert estimate_tokens(out) <= limit
        assert info is not None and info["dropped_messages"] > 0
        assert "仍超出上限" in caplog.text

    def test_keeps_newest_turn(self):
        messages = _history(30)
        messages.append({"role": "user", "content": "最新问题"})
        out, _ = enforce_context_limit(messages, _MODEL, 64_000)
        assert out[-1]["content"] == "最新问题"

    def test_never_starts_on_tool_result(self):
        # 孤立 tool result 缺少配对的 assistant tool_calls，provider 会直接报错。
        messages = _history(30)
        out, _ = enforce_context_limit(messages, _MODEL, 64_000)
        assert out[0].get("role") != "tool"

    def test_keeps_at_least_one_message(self):
        # 单条消息就超限时也必须留点东西，不能返回空列表。
        messages = [{"role": "user", "content": "数据" * 200_000}]
        out, _ = enforce_context_limit(messages, _MODEL, 64_000)
        assert len(out) >= 1

    @pytest.mark.parametrize("window", [64_000, 262_144])
    def test_respects_window_value(self, window):
        messages = _history(30)
        out, _ = enforce_context_limit(messages, _MODEL, window)
        assert estimate_tokens(out) <= get_compact_threshold(_MODEL, window)


class TestEstimatorIsConservative:
    """估算必须 >= 真实 token 数，否则兜底形同虚设。"""

    @pytest.mark.parametrize(("label", "text", "actual"), _REAL_TOKEN_SAMPLES)
    def test_never_underestimates_real_tokens(self, label, text, actual):
        assert estimate_text_tokens(text) >= actual, f"{label} 低估：兜底会放过超窗请求"

    def test_cjk_counted_above_one_token_per_char(self):
        # 原式 len//2 把中文按 0.5 token/字算，实测约 1.27，低估 2.5 倍。
        assert estimate_text_tokens("持仓复盘" * 100) > 400

    def test_ascii_not_regressed(self):
        text = "The composite man accumulates supply. " * 50
        assert estimate_text_tokens(text) == max(len(text) // 2, len(text.encode("utf-8")) // 3)

    def test_empty_and_mixed(self):
        assert estimate_text_tokens("") == 0
        # 混合文本必须两部分都计入，不能只算一边。
        assert estimate_text_tokens("持仓abc") > estimate_text_tokens("abc")

    def test_tool_call_args_use_same_estimator(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"name": "p", "args": {"名称": "华勤技术" * 50}}]}
        ]
        assert estimate_tokens(messages) > 200


class TestOverflowRegression:
    def test_history_that_gateway_rejected_is_now_truncated(self):
        """回归：这份历史旧估算 228,014 < 阈值被放行，实际 274,938 tokens 被网关 400。"""
        unit = "华勤技术奕瑞科技持仓复盘主力放量出货震荡洗盘吸筹派发形态确认"
        messages: list[dict] = [{"role": "user", "content": "起始"}]
        for i in range(76):
            messages.append(
                {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "name": "p", "args": {}}]}
            )
            messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "p", "content": unit * 100})

        limit = get_compact_threshold(_MODEL, 262_144)
        assert estimate_tokens(messages) > limit, "新估算应识别出这份历史超窗"
        kept, info = enforce_context_limit(messages, _MODEL, 262_144)
        assert info is not None
        assert estimate_tokens(kept) <= limit


class _RecordingScratchpad:
    def __init__(self):
        self.entries: list[dict] = []

    def record_compaction(self, **kwargs):
        self.entries.append(kwargs)


class TestRuntimeWiring:
    """压缩失败 + 仍超限时，runtime 必须截断并把有损事件透出去。"""

    def _runtime(self, window: int, scratchpad=None):
        from cli.runtime import AgentRuntime

        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.provider = _Boom()
        runtime.provider.context_window = window
        runtime.scratchpad = scratchpad
        return runtime

    def test_emits_context_overflow_event(self):
        runtime = self._runtime(64_000)
        messages = _history(30)
        out, event = runtime._compact_if_needed(messages, _MODEL, 64_000)

        assert event is not None
        assert event["type"] == "context_overflow"
        assert event["dropped_messages"] > 0
        assert estimate_tokens(out) <= get_compact_threshold(_MODEL, 64_000)
        # messages 必须就地更新，否则调用方继续用旧列表发包。
        assert messages is out

    def test_records_overflow_in_scratchpad(self):
        pad = _RecordingScratchpad()
        runtime = self._runtime(64_000, scratchpad=pad)
        runtime._compact_if_needed(_history(30), _MODEL, 64_000)

        assert len(pad.entries) == 1
        assert "contextOverflow" in pad.entries[0]["metadata"]

    def test_no_overflow_event_when_within_limit(self):
        runtime = self._runtime(262_144)
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "你好"}]
        _, event = runtime._compact_if_needed(messages, _MODEL, 262_144)
        assert event is None
