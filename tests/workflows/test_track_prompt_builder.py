from __future__ import annotations


def test_build_track_user_message_includes_regime_scope_gate_and_payloads():
    from tools.track_prompt_builder import build_track_user_message

    message = build_track_user_message(
        "Accum",
        ["[宏观水温 / Benchmark Context]", "regime=RISK_OFF"],
        ["• 000001 平安银行\n  [交易闸门] 来源:二次确认 | 二次确认:confirmed"],
        compressed=True,
        raw_count=9,
        selected_count=3,
        regime="risk_off",
    )

    assert "regime=RISK_OFF" in message
    assert "当前 RISK_OFF 弱势环境" in message
    assert "Accum轨" in message
    assert "候选已从 9 只压缩到 3 只" in message
    assert "二次确认=confirmed" in message
    assert "满足的硬门槛" in message
    assert "BUY-APPROVED" in message
    assert "000001 平安银行" in message


def test_build_track_user_message_defaults_unknown_track_to_trend_scope():
    from tools.track_prompt_builder import build_track_user_message

    message = build_track_user_message(
        "Other",
        [],
        ["payload"],
        compressed=False,
        raw_count=1,
        selected_count=1,
        regime="CRASH",
    )

    assert "Trend轨" in message
    assert "当前 CRASH 环境" in message
    assert "右侧突破全部视为诱多" in message
    assert "payload" in message
