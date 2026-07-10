"""Batch report payload helper tests."""

from __future__ import annotations

import warnings

import pandas as pd


def test_generate_stock_payload_includes_structure_and_conflict_context():
    """模型输入需要保留 TR 边界、A/B/C 释义、冲突提示与 VSA 标签。"""
    from tools.report_builder import generate_stock_payload

    dates = pd.date_range("2026-04-01", periods=25, freq="D")
    rows = []
    for idx, date in enumerate(dates):
        close = 10.0 + idx * 0.03
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "open": close - 0.05,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1000,
                "amount": close * 1000,
            }
        )
    rows[-1].update({"open": 10.2, "high": 12.0, "low": 10.0, "close": 12.0, "volume": 3000})
    payload = generate_stock_payload(
        "300001",
        "测试股份",
        "sos",
        pd.DataFrame(rows),
        exit_signal="stop_loss",
        exit_price=10.5,
        exit_reason="主升趋势破位",
        springboard_grade="B+C",
        candidate_source="二次确认",
        signal_status="confirmed",
        confirm_date="2026-05-10",
        confirm_reason="缩量站稳",
    )

    assert "[结构支撑/阻力] Creek(箱体上沿)" in payload
    assert "[候选类型] 冲突复核" in payload
    assert "[交易闸门] 来源:二次确认 | 二次确认:confirmed" in payload
    assert "[冲突提示]" in payload
    assert "B+C（B=放量高收突破 + C=支撑多次测试）" in payload
    assert "宽幅高收放量" in payload
    assert "放量突破" in payload


def test_generate_stock_payload_flat_range_does_not_warn():
    from tools.report_builder import generate_stock_payload

    rows = []
    for idx, date in enumerate(pd.date_range("2026-04-01", periods=25, freq="D")):
        close = 10.0 + idx * 0.03
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
                "amount": close * 1000,
            }
        )

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        payload = generate_stock_payload("300001", "测试股份", "sos", pd.DataFrame(rows))

    assert "[结构支撑/阻力]" in payload
