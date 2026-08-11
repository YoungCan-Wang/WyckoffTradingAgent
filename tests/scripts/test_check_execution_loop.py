"""市场信号就绪判据：非空不等于就绪。"""

from __future__ import annotations

import pytest

from integrations.supabase_market_signal import market_signal_readiness
from scripts.check_execution_loop import market_signal_row_ready


def _row(premarket: str, benchmark: str, trade_date: str = "2026-08-07") -> dict:
    return {"trade_date": trade_date, "premarket_regime": premarket, "benchmark_regime": benchmark}


def test_both_fields_present_and_valid_is_ready() -> None:
    assert market_signal_row_ready(_row("NORMAL", "BEAR_REBOUND")) is True


@pytest.mark.parametrize("premarket", ["UNKNOWN", "unknown", "", "   "])
def test_unknown_or_blank_premarket_is_not_ready(premarket: str) -> None:
    """回归：UNKNOWN 非空但本身就是禁买状态，「两字段非空」会给出假通过。

    生产 2026-08-07 即此形态：A50 缺失导致盘前写入 UNKNOWN。
    """
    assert market_signal_row_ready(_row(premarket, "BEAR_REBOUND")) is False


def test_missing_benchmark_is_not_ready() -> None:
    assert market_signal_row_ready(_row("NORMAL", "")) is False


@pytest.mark.parametrize(
    ("premarket", "benchmark"),
    [("NORMAL", "BEAR_REBOUND"), ("UNKNOWN", "BEAR_REBOUND"), ("NORMAL", ""), ("", "NEUTRAL")],
)
def test_matches_production_readiness_verdict(premarket: str, benchmark: str) -> None:
    """判据必须与 Step4 实际使用的 readiness 同口径，否则体检结论与生产放行不一致。"""
    row = _row(premarket, benchmark)
    expected = market_signal_readiness(row, "2026-08-07")["status"] == "ready"

    assert market_signal_row_ready(row) is expected
