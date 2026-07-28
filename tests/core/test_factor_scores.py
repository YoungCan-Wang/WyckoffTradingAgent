"""Composite scoring and tradability masks for the cross-sectional factor path."""

from __future__ import annotations

import pandas as pd
import pytest

from core.factor_scores import (
    add_normalized_prices,
    add_tradability,
    add_value_factors,
    apply_universe_filters,
    limit_pct_series,
    value_composite,
)
from core.limit_move import limit_pct


def _panel(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


class TestValueFactors:
    def test_inverts_ratios_and_drops_non_positive_denominators(self) -> None:
        panel = _panel(
            [
                {"date": "2024-01-02", "symbol": 1, "pb": 2.0, "pe_ttm": 10.0, "ps_ttm": 4.0, "dv_ttm": 3.0},
                {"date": "2024-01-02", "symbol": 2, "pb": -1.0, "pe_ttm": -5.0, "ps_ttm": 0.0, "dv_ttm": 0.0},
            ]
        )
        out = add_value_factors(panel)
        assert out.loc[0, "bp"] == pytest.approx(0.5)
        assert out.loc[0, "sp_ttm"] == pytest.approx(0.25)
        # 净资产为负不等于极便宜，置空；亏损股的 EP 保留负值，自然排到最低分位。
        assert pd.isna(out.loc[1, "bp"])
        assert pd.isna(out.loc[1, "sp_ttm"])
        assert out.loc[1, "ep_ttm"] == pytest.approx(-0.2)

    def test_composite_needs_three_valid_factors(self) -> None:
        panel = _panel(
            [
                {"date": "2024-01-02", "symbol": 1, "bp": 1.0, "ep_ttm": 1.0, "sp_ttm": 1.0, "dv_ttm": 1.0},
                {"date": "2024-01-02", "symbol": 2, "bp": 0.5, "ep_ttm": 0.5, "sp_ttm": 0.5, "dv_ttm": 0.5},
                {"date": "2024-01-02", "symbol": 3, "bp": 0.1, "ep_ttm": None, "sp_ttm": None, "dv_ttm": None},
            ]
        )
        scores = value_composite(panel)
        assert scores.iloc[0] > scores.iloc[1]
        assert pd.isna(scores.iloc[2])

    def test_composite_is_rank_based_not_scale_based(self) -> None:
        """单个极端值不能改变其它标的的相对分数，否则右偏比率会主导合成分。"""
        base = [
            {"date": "2024-01-02", "symbol": s, "bp": v, "ep_ttm": v, "sp_ttm": v, "dv_ttm": v}
            for s, v in ((1, 1.0), (2, 2.0), (3, 3.0))
        ]
        outlier = [*base[:2], {**base[2], "bp": 1e6, "ep_ttm": 1e6, "sp_ttm": 1e6, "dv_ttm": 1e6}]
        assert list(value_composite(_panel(base))) == list(value_composite(_panel(outlier)))


class TestLimitPct:
    @pytest.mark.parametrize(
        ("symbol", "is_st"),
        [(600000, False), (600000, True), (300001, False), (300001, True), (688001, False), (830001, False)],
    )
    def test_matches_scalar_source_of_truth(self, symbol: int, is_st: bool) -> None:
        series = limit_pct_series(pd.Series([symbol]), pd.Series([is_st]))
        assert series.iloc[0] == limit_pct(f"{symbol:06d}", "ST测试" if is_st else "测试")


class TestTradability:
    def test_limit_up_open_blocks_buy_and_limit_down_blocks_sell(self) -> None:
        panel = _panel(
            [
                {"date": "2024-01-02", "symbol": 600000, "pre_close": 10.0, "open": 11.0, "vol": 100},
                {"date": "2024-01-02", "symbol": 600001, "pre_close": 10.0, "open": 9.0, "vol": 100},
                {"date": "2024-01-02", "symbol": 600002, "pre_close": 10.0, "open": 10.2, "vol": 100},
                {"date": "2024-01-02", "symbol": 600003, "pre_close": 10.0, "open": 10.0, "vol": 0},
            ]
        )
        out = add_tradability(panel)
        assert list(out["can_buy"]) == [False, True, True, False]
        assert list(out["can_sell"]) == [True, False, True, False]


class TestNormalizedPrices:
    def test_scales_each_symbol_to_its_first_observation(self) -> None:
        panel = _panel(
            [
                {"date": "2024-01-02", "symbol": 1, "close": 10.0, "open": 10.0, "adj_factor": 4.0},
                {"date": "2024-01-03", "symbol": 1, "close": 11.0, "open": 10.5, "adj_factor": 8.0},
            ]
        )
        out = add_normalized_prices(panel)
        # 首日按原价，次日的分红/拆并调整体现为 2 倍复权比。
        assert out.loc[0, "close_adj"] == pytest.approx(10.0)
        assert out.loc[1, "close_adj"] == pytest.approx(22.0)


class TestUniverseFilters:
    def test_uses_real_listing_date_not_panel_position(self) -> None:
        panel = _panel(
            [
                {
                    "date": "2024-01-02",
                    "symbol": 1,
                    "score": 0.4,
                    "amount": 9_999.0,
                    "is_st": False,
                    "list_date": pd.Timestamp("2010-01-01"),
                },
                {
                    "date": "2024-01-02",
                    "symbol": 2,
                    "score": 0.3,
                    "amount": 9_999.0,
                    "is_st": False,
                    "list_date": pd.Timestamp("2023-12-01"),
                },
            ]
        )
        out = apply_universe_filters(panel, exclude_st=False, min_amount_thousand=0.0, min_listed_days=120)
        assert out.loc[0, "score"] == pytest.approx(0.4)
        assert pd.isna(out.loc[1, "score"])

    def test_thin_turnover_and_st_are_dropped_by_nulling_score(self) -> None:
        panel = _panel(
            [
                {
                    "date": "2024-01-02",
                    "symbol": 1,
                    "score": 0.4,
                    "amount": 100.0,
                    "is_st": False,
                    "list_date": pd.Timestamp("2010-01-01"),
                },
                {
                    "date": "2024-01-02",
                    "symbol": 2,
                    "score": 0.3,
                    "amount": 9_999.0,
                    "is_st": True,
                    "list_date": pd.Timestamp("2010-01-01"),
                },
            ]
        )
        out = apply_universe_filters(panel, exclude_st=True, min_amount_thousand=500.0, min_listed_days=0)
        assert out["score"].isna().all()
        # 行不能被删掉：已有持仓仍要用它们的收盘价估值。
        assert len(out) == 2
