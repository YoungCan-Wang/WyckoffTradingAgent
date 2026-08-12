"""基准候选必须连续：只看行数会选中停更的 ETF。"""

from __future__ import annotations

import pandas as pd
import pytest

from workflows import market_funnel_data as mfd


class _Runtime:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.benchmark_symbols = symbols
        self.kline_count = 320


def _series(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"date": dates, "close": [10.0 + i * 0.1 for i in range(len(dates))]})


def _continuous(n: int = 200, end: str = "2026-08-12") -> pd.DataFrame:
    days = pd.bdate_range(end=end, periods=n).strftime("%Y-%m-%d").tolist()
    return _series(days)


def _stale_etf() -> pd.DataFrame:
    """复刻生产形态：319 根到 2026-03-20，再接一根孤立的 2026-08-12。"""
    days = pd.bdate_range(end="2026-03-20", periods=199).strftime("%Y-%m-%d").tolist()
    return _series([*days, "2026-08-12"])


def _patch(monkeypatch: pytest.MonkeyPatch, table: dict[str, pd.DataFrame | None]) -> list[str]:
    asked: list[str] = []

    def _fetch(_client, symbol, _count):
        asked.append(symbol)
        frame = table.get(symbol)
        if isinstance(frame, Exception):
            raise frame
        return frame

    monkeypatch.setattr(mfd, "_fetch_one_benchmark_history", _fetch)
    return asked


def test_stale_etf_is_rejected_in_favour_of_continuous_series(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：02800.HK 停更后仍返回 320 行，行数检查通过但中间缺 145 天。"""
    _patch(monkeypatch, {"02800.HK": _stale_etf(), "00939.HK": _continuous()})

    df, symbol = mfd.fetch_benchmark_history(None, _Runtime(("02800.HK", "00939.HK")))

    assert symbol == "00939.HK"
    assert df is not None


def test_first_continuous_candidate_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续时仍按配置顺序取第一个，不因新增兜底而改变既有选择。"""
    asked = _patch(monkeypatch, {"02800.HK": _continuous(), "00939.HK": _continuous()})

    _df, symbol = mfd.fetch_benchmark_history(None, _Runtime(("02800.HK", "00939.HK")))

    assert symbol == "02800.HK"
    assert asked == ["02800.HK"]


def test_all_stale_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"02800.HK": _stale_etf(), "03033.HK": _stale_etf()})

    df, symbol = mfd.fetch_benchmark_history(None, _Runtime(("02800.HK", "03033.HK")))

    assert df is None
    assert symbol == ""


def test_short_series_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"02800.HK": _continuous(n=30), "00939.HK": _continuous()})

    _df, symbol = mfd.fetch_benchmark_history(None, _Runtime(("02800.HK", "00939.HK")))

    assert symbol == "00939.HK"


def test_fetch_exception_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"02800.HK": RuntimeError("tickflow down"), "00939.HK": _continuous()})

    _df, symbol = mfd.fetch_benchmark_history(None, _Runtime(("02800.HK", "00939.HK")))

    assert symbol == "00939.HK"


def test_weekend_and_holiday_gaps_are_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常港股假期（如春节）可能连休一周多，不能把它判成停更。"""
    days = pd.bdate_range(end="2026-08-12", periods=200).strftime("%Y-%m-%d").tolist()
    trimmed = [d for d in days if not ("2026-02-14" <= d <= "2026-02-22")]
    _patch(monkeypatch, {"02800.HK": _series(trimmed)})

    _df, symbol = mfd.fetch_benchmark_history(None, _Runtime(("02800.HK",)))

    assert symbol == "02800.HK"


def test_gap_helper_reports_zero_for_empty() -> None:
    assert mfd._max_recent_gap_days(pd.DataFrame()) == 0
