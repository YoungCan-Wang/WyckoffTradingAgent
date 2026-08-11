from __future__ import annotations

from workflows.holding_diagnosis_llm import _parse_holding_llm


def test_warning_sell_is_downgraded_to_hold() -> None:
    parsed = _parse_holding_llm(
        '{"action":"EXIT","signal_severity":"WARNING","action_timing":"NOW","reason":"盘中走弱","confidence":0.8}'
    )

    assert parsed is not None
    assert parsed["action"] == "HOLD"
    assert "降级为 HOLD" in parsed["reason"]


def test_confirmed_break_next_session_remains_executable() -> None:
    parsed = _parse_holding_llm(
        '{"action":"TRIM","signal_severity":"CONFIRMED_BREAK","action_timing":"NEXT_SESSION_IF",'
        '"reason":"收盘破位","confidence":0.8}'
    )

    assert parsed is not None
    assert parsed["action"] == "TRIM"
    assert parsed["signal_severity"] == "CONFIRMED_BREAK"
