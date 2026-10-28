from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from core.backtest_control_selection import (
    control_trigger_name,
    eligible_history,
    rank_quality,
    rank_random,
    rank_relative_strength,
    rank_sector_strength,
)
from core.wyckoff_engine import FunnelConfig

_START = date(2026, 1, 1)


def _hist(closes: list[float], *, amount: float = 1e9, last_date: date | None = None) -> pd.DataFrame:
    dates = [_START + timedelta(days=i) for i in range(len(closes))]
    if last_date is not None:
        dates[-1] = last_date
    return pd.DataFrame({"date": dates, "close": closes, "amount": [amount] * len(closes)})


def _cfg() -> FunnelConfig:
    cfg = FunnelConfig()
    cfg.min_avg_amount_wan = 4000.0
    cfg.amount_avg_window = 3
    cfg.l1_min_close_price = 2.0
    return cfg


def test_ranks_by_excess_return_over_benchmark_descending() -> None:
    signal_date = _START + timedelta(days=4)
    bench = _hist([100, 100, 100, 100, 110])
    day_df_map = {
        "weak": _hist([10, 10, 10, 10, 10.5]),
        "strong": _hist([10, 10, 10, 10, 13]),
        "mid": _hist([10, 10, 10, 10, 12]),
    }
    ranked = rank_relative_strength(day_df_map, bench, signal_date, _cfg(), window=2)
    assert [code for code, _ in ranked] == ["strong", "mid", "weak"]
    assert ranked[0][1] == pytest.approx(30.0 - 10.0)


def test_reversal_is_the_same_ranking_weakest_first() -> None:
    signal_date = _START + timedelta(days=4)
    bench = _hist([100, 100, 100, 100, 110])
    day_df_map = {"weak": _hist([10, 10, 10, 10, 10.5]), "strong": _hist([10, 10, 10, 10, 13])}
    ranked = rank_relative_strength(day_df_map, bench, signal_date, _cfg(), window=2, weakest=True)
    assert [code for code, _ in ranked] == ["weak", "strong"]
    assert control_trigger_name("reversal", 60) == "REV60"


def test_drops_stale_short_illiquid_and_cheap_symbols() -> None:
    signal_date = _START + timedelta(days=4)
    bench = _hist([100, 100, 100, 100, 100])
    day_df_map = {
        "stale": _hist([10, 10, 10, 10, 20], last_date=signal_date - timedelta(days=1)),
        "short": _hist([10, 20]),
        "illiquid": _hist([10, 10, 10, 10, 20], amount=1e6),
        "penny": _hist([1.0, 1.0, 1.0, 1.0, 1.5]),
        "ok": _hist([10, 10, 10, 10, 11]),
    }
    ranked = rank_relative_strength(day_df_map, bench, signal_date, _cfg(), window=2)
    assert [code for code, _ in ranked] == ["ok"]


def test_returns_empty_without_benchmark_window() -> None:
    signal_date = _START + timedelta(days=4)
    day_df_map = {"ok": _hist([10, 10, 10, 10, 11])}
    assert rank_relative_strength(day_df_map, _hist([100, 101]), signal_date, _cfg(), window=2) == []


def test_random_control_is_seeded_per_day_and_respects_eligibility() -> None:
    signal_date = _START + timedelta(days=4)
    day_df_map = {
        "a": _hist([10, 10, 10, 10, 11]),
        "b": _hist([10, 10, 10, 10, 9]),
        "c": _hist([10, 10, 10, 10, 12]),
        "illiquid": _hist([10, 10, 10, 10, 20], amount=1e6),
    }
    first = rank_random(day_df_map, signal_date, _cfg(), seed=7)
    again = rank_random(day_df_map, signal_date, _cfg(), seed=7)
    other_seed = rank_random(day_df_map, signal_date, _cfg(), seed=8)
    other_day = rank_random(day_df_map, signal_date - timedelta(days=1), _cfg(), seed=7)

    assert first == again
    assert {code for code, _ in first} == {"a", "b", "c"}
    assert [score for _, score in first] == sorted((score for _, score in first), reverse=True)
    assert [code for code, _ in other_seed] != [code for code, _ in first] or other_seed != first
    assert other_day == []
    assert set(eligible_history(day_df_map, signal_date, _cfg())) == {"a", "b", "c"}
    assert control_trigger_name("random", 60) == "RANDOM"
    assert control_trigger_name("rs", 20) == "RS20"


def test_sector_strength_ranks_sectors_by_median_and_shuffles_inside() -> None:
    signal_date = _START + timedelta(days=4)
    bench = _hist([100, 100, 100, 100, 100])
    day_df_map = {f"hot{i}": _hist([10, 10, 10, 10, 11 + i * 0.1]) for i in range(5)}
    day_df_map |= {f"cold{i}": _hist([10, 10, 10, 10, 9.5]) for i in range(5)}
    day_df_map |= {"lonely": _hist([10, 10, 10, 10, 20])}
    sector_map = {code: code.rstrip("0123456789") for code in day_df_map}
    ranked = rank_sector_strength(day_df_map, bench, signal_date, _cfg(), sector_map, window=2, seed=0)
    codes = [code for code, _ in ranked]
    # 板块不足 5 只的 lonely 不参与；hot 板块整体排在 cold 前，板块内分数相同（中位数）、顺序由种子决定。
    assert "lonely" not in codes
    assert all(code.startswith("hot") for code in codes[:5]) and all(code.startswith("cold") for code in codes[5:])
    assert [score for _, score in ranked[:5]] == [pytest.approx(12.0)] * 5
    assert codes[:5] != sorted(codes[:5])
    assert ranked == rank_sector_strength(day_df_map, bench, signal_date, _cfg(), sector_map, window=2, seed=0)
    assert control_trigger_name("sector_rs", 60) == "SECTOR60"


def test_quality_uses_only_records_announced_before_signal_date_and_requires_all_fields() -> None:
    signal_date = _START + timedelta(days=4)
    day_df_map = {code: _hist([10, 10, 10, 10, 10]) for code in ("000001", "000002", "000003", "000004")}
    stale = (signal_date - timedelta(days=500)).isoformat()
    fresh = (signal_date - timedelta(days=30)).isoformat()
    history = {
        # 高 ROE、低负债、高现金转化：三项都该排第一。
        "000001": [
            {"announce_date": fresh, "roe": 20.0, "debt_to_asset_ratio": 20.0, "operating_cash_to_revenue": 1.2}
        ],
        # 最新一条是信号日当天公告的，不可用；退回更早那条（差）。
        "000002": [
            {"announce_date": fresh, "roe": 5.0, "debt_to_asset_ratio": 80.0, "operating_cash_to_revenue": 0.1},
            {
                "announce_date": signal_date.isoformat(),
                "roe": 99.0,
                "debt_to_asset_ratio": 1.0,
                "operating_cash_to_revenue": 9.0,
            },
        ],
        # 只有超过 400 日的旧记录：不参与。
        "000003": [{"announce_date": stale, "roe": 50.0, "debt_to_asset_ratio": 5.0, "operating_cash_to_revenue": 3.0}],
        # 缺现金流项：不参与。
        "000004": [{"announce_date": fresh, "roe_diluted": 30.0, "debt_to_asset_ratio": 10.0}],
    }
    ranked = rank_quality(day_df_map, signal_date, _cfg(), history)
    assert [code for code, _ in ranked] == ["000001", "000002"]
    assert ranked[0][1] == pytest.approx(0.5) and ranked[1][1] == pytest.approx(0.0)
    assert control_trigger_name("quality", 60) == "QUALITY"
