"""RuntimeEvent → ai-sdk UIMessageChunk 的翻译，以及与前端共用的 golden 流。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cli.service.ui_stream import (
    DONE_FRAME,
    UiStreamTranslator,
    history_from_ui_messages,
    sse,
    ui_frames,
)

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "agent_ui_stream.sse"

NAN = float("nan")

SCRIPTED_TURN: list[dict[str, Any]] = [
    {"type": "stage_start", "stage": "model", "round": 1, "message": "正在分析"},
    {"type": "text_delta", "text": "我先看一下大盘。"},
    {
        "type": "tool_start",
        "name": "get_market_overview",
        "tool_call_id": "call_1",
        "args": {},
        "display_name": "大盘概览",
    },
    {
        "type": "tool_result",
        "name": "get_market_overview",
        "tool_call_id": "call_1",
        "result": {"regime": "NEUTRAL", "score": 0.5, "turnover_ratio": NAN, "series": [1.0, float("inf")]},
    },
    {"type": "tool_start", "name": "run_backtest", "tool_call_id": "call_2", "args": {"days": 30}},
    {"type": "tool_error", "name": "run_backtest", "tool_call_id": "call_2", "error": "数据不足"},
    {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 4}},
    {"type": "stage_start", "stage": "model", "round": 2, "message": "正在分析"},
    {"type": "text_delta", "text": "结论："},
    {"type": "text_delta", "text": "震荡偏弱。"},
    {"type": "done", "text": "我先看一下大盘。结论：震荡偏弱。", "streamed": True, "rounds": 2},
]


def _translate(events: list[dict[str, Any]], message_id: str = "msg-test") -> list[dict[str, Any]]:
    translator = UiStreamTranslator(message_id)
    chunks = translator.start()
    for event in events:
        chunks += translator.feed(event)
    return chunks + translator.close()


def _types(chunks: list[dict[str, Any]]) -> list[str]:
    return [chunk["type"] for chunk in chunks]


def test_text_turn_is_one_step_with_one_text_part():
    chunks = _translate(
        [
            {"type": "stage_start", "stage": "model", "round": 1},
            {"type": "text_delta", "text": "你"},
            {"type": "text_delta", "text": "好"},
            {"type": "usage", "usage": {}},
            {"type": "stage_done", "stage": "model", "round": 1, "success": True},
            {"type": "done", "text": "你好", "streamed": True},
        ]
    )

    assert _types(chunks) == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    assert chunks[0] == {"type": "start", "messageId": "msg-test"}
    assert chunks[-1] == {"type": "finish", "finishReason": "stop"}


def test_tool_round_splits_steps_and_text_parts():
    chunks = _translate(SCRIPTED_TURN)

    assert _types(chunks) == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-end",  # 工具调用前先收掉正文
        "tool-input-available",
        "tool-output-available",
        "tool-input-available",
        "tool-output-error",
        "finish-step",
        "start-step",  # 第二轮模型调用是新的一步
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    text_ids = [chunk["id"] for chunk in chunks if chunk["type"] == "text-start"]
    assert text_ids == ["text-1", "text-2"]
    assert all(chunk.get("dynamic") for chunk in chunks if chunk["type"].startswith("tool-"))


def test_done_text_is_used_when_the_model_was_not_streamed():
    chunks = _translate([{"type": "done", "text": "一次性回答", "streamed": False}])

    assert _types(chunks)[2:] == ["text-start", "text-delta", "text-end", "finish-step", "finish"]
    assert chunks[3]["delta"] == "一次性回答"


def test_failed_turn_ends_with_error_and_ignores_later_events():
    chunks = _translate(
        [
            {"type": "text_delta", "text": "半截"},
            {"type": "turn_failed", "error": "模型响应超时"},
            {"type": "text_delta", "text": "不该出现"},
            {"type": "done", "text": "不该出现"},
        ]
    )

    assert _types(chunks)[-4:] == ["text-end", "error", "finish-step", "finish"]
    assert chunks[-3]["errorText"] == "模型响应超时"
    assert chunks[-1]["finishReason"] == "error"
    assert "不该出现" not in json.dumps(chunks, ensure_ascii=False)


def test_cancelled_turn_aborts_without_finish():
    chunks = _translate([{"type": "text_delta", "text": "半截"}, {"type": "turn_cancelled"}])

    assert _types(chunks)[-3:] == ["text-end", "finish-step", "abort"]
    assert "finish" not in _types(chunks)


def test_stream_that_ends_without_a_terminal_event_is_closed_as_an_error():
    chunks = _translate([{"type": "text_delta", "text": "半截"}])

    assert _types(chunks)[-3:] == ["error", "finish-step", "finish"]


def test_frames_are_strict_json_even_when_tool_results_contain_nan():
    frames = b"".join(sse(chunk) for chunk in _translate(SCRIPTED_TURN))

    for line in frames.decode().strip().split("\n\n"):
        json.loads(line.removeprefix("data: "), parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    output = next(c for c in _translate(SCRIPTED_TURN) if c["type"] == "tool-output-available")["output"]
    assert output["turnover_ratio"] is None
    assert output["series"] == [1.0, None]


def test_oversized_tool_output_is_replaced_by_a_marker():
    big = {"rows": ["x" * 1000] * 300}
    chunks = _translate(
        [
            {"type": "tool_start", "name": "t", "tool_call_id": "c", "args": {}},
            {"type": "tool_result", "name": "t", "tool_call_id": "c", "result": big},
        ]
    )

    output = next(c for c in chunks if c["type"] == "tool-output-available")["output"]
    assert output["truncated"] is True
    assert len(output["preview"]) == 2000


def test_ui_frames_ends_with_done_marker_and_closes_the_runtime_stream():
    closed = []

    def events():
        try:
            yield {"type": "text_delta", "text": "a"}
            yield {"type": "done", "text": "a", "streamed": True}
        finally:
            closed.append(True)

    frames = list(ui_frames(events()))

    assert frames[-1] == DONE_FRAME
    assert closed == [True]


def test_ui_frames_turns_a_runtime_exception_into_an_error_chunk():
    def events():
        yield {"type": "text_delta", "text": "半截"}
        raise RuntimeError("boom")

    frames = list(ui_frames(events()))
    types = [json.loads(f.removeprefix(b"data: "))["type"] for f in frames[:-1]]

    assert types[-4:] == ["text-end", "error", "finish-step", "finish"]
    assert frames[-1] == DONE_FRAME


def test_closing_ui_frames_early_closes_the_runtime_stream():
    closed = []

    def events():
        try:
            while True:
                yield {"type": "text_delta", "text": "x"}
        finally:
            closed.append(True)

    frames = ui_frames(events())
    for frame in frames:  # 先消费到第一个正文帧，runtime 的生成器此时才真正启动
        if b"text-delta" in frame:
            break
    frames.close()

    assert closed == [True]


def test_history_keeps_text_only_and_requires_a_final_user_message():
    messages = [
        {"role": "user", "parts": [{"type": "text", "text": "第一问"}]},
        {"role": "assistant", "parts": [{"type": "step-start"}, {"type": "text", "text": "第一答"}]},
        {"role": "assistant", "parts": [{"type": "dynamic-tool", "toolName": "x"}]},
        {"role": "user", "parts": [{"type": "text", "text": "第二"}, {"type": "text", "text": "问"}]},
    ]

    text, history = history_from_ui_messages(messages)

    assert text == "第二问"
    assert history == [{"role": "user", "content": "第一问"}, {"role": "assistant", "content": "第一答"}]


def test_history_rejects_malformed_requests():
    assert history_from_ui_messages(None) is None
    assert history_from_ui_messages([]) is None
    assert history_from_ui_messages([{"role": "assistant", "parts": [{"type": "text", "text": "hi"}]}]) is None
    assert history_from_ui_messages([{"role": "user", "parts": [{"type": "text", "text": "x" * 20_001}]}]) is None


def test_golden_stream_matches_the_translator_output():
    """前端用真实的 ai SDK 解析同一份文件（web/apps/web 的契约测试），两边任何一边漂移都会红。"""
    produced = b"".join(sse(chunk) for chunk in _translate(SCRIPTED_TURN, "msg-golden")) + DONE_FRAME
    if os.getenv("UPDATE_GOLDEN") == "1":
        GOLDEN.write_bytes(produced)

    assert GOLDEN.read_bytes() == produced, "UPDATE_GOLDEN=1 pytest tests/cli/test_service_ui_stream.py 重新生成"
