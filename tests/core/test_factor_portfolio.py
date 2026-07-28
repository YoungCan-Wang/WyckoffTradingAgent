"""Cross-sectional portfolio engine: execution timing, frictions, and the null hypothesis.

这里的用例大多来自实测过的错误口径：不补位会按股价做隐性筛选、停牌用整段最后价是未来函数、
每日等权基准与 K 日持仓不可比。它们都能让随机打分刷出正 alpha，所以固定成断言。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.cash_portfolio import CashPortfolioConfig
from core.factor_portfolio import (
    FactorPortfolioConfig,
    build_matrices,
    holding_period_benchmark,
    run_factor_backtest,
)

FREE = CashPortfolioConfig(
    initial_cash=1_000_000.0, commission_rate=0.0, min_commission=0.0, stamp_duty_rate=0.0, transfer_fee_rate=0.0
)


def make_panel(spec: dict[int, list[dict]], dates: list[str]) -> pd.DataFrame:
    rows = []
    for symbol, days in spec.items():
        for date, day in zip(dates, days, strict=True):
            close = day.get("close")
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "symbol": symbol,
                    "close_adj": close,
                    "open_adj": day.get("open", close),
                    "score": day.get("score"),
                    "can_buy": day.get("can_buy", close is not None),
                    "can_sell": day.get("can_sell", close is not None),
                }
            )
    return pd.DataFrame(rows)


def flat(n: int, price: float, score: float | None) -> list[dict]:
    return [{"close": price, "score": score} for _ in range(n)]


class TestExecutionTiming:
    def test_decision_on_day_i_fills_at_day_i_plus_one_open(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        panel = make_panel(
            {
                1: [
                    {"close": 10.0, "open": 10.0, "score": 1.0},
                    {"close": 20.0, "open": 12.0, "score": 1.0},
                    {"close": 20.0, "open": 20.0, "score": 1.0},
                ],
            },
            dates,
        )
        cfg = FactorPortfolioConfig(top_n=1, rebalance_days=10, costs=FREE, slippage_bps=0.0)
        trades = run_factor_backtest(panel, cfg)["trades"]
        assert list(trades["date"]) == [pd.Timestamp("2024-01-03")]
        # 用的是次日开盘 12.0，不是决策日收盘 10.0，也不是次日收盘 20.0。
        assert trades.iloc[0]["price"] == pytest.approx(12.0)


class TestBufferedRebalance:
    def test_holding_survives_rank_slip_inside_buffer(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        # 第 2 个调仓日 symbol 1 跌到第 2 名：在 top_n=1、buffer=2 的保留区内，不该换手。
        panel = make_panel(
            {
                1: [
                    {"close": 10.0, "score": 0.9},
                    {"close": 10.0, "score": 0.9},
                    {"close": 10.0, "score": 0.5},
                    {"close": 10.0, "score": 0.5},
                ],
                2: [
                    {"close": 10.0, "score": 0.1},
                    {"close": 10.0, "score": 0.1},
                    {"close": 10.0, "score": 0.8},
                    {"close": 10.0, "score": 0.8},
                ],
            },
            dates,
        )
        cfg = FactorPortfolioConfig(top_n=1, rebalance_days=2, buffer_mult=2.0, costs=FREE, slippage_bps=0.0)
        buffered = run_factor_backtest(panel, cfg)["trades"]
        unbuffered = run_factor_backtest(
            panel, FactorPortfolioConfig(top_n=1, rebalance_days=2, buffer_mult=1.0, costs=FREE, slippage_bps=0.0)
        )["trades"]
        assert list(buffered["side"]) == ["buy"]
        assert sorted(unbuffered["side"]) == ["buy", "buy", "sell"]


class TestLotAffordability:
    def test_unaffordable_lot_falls_through_to_next_candidate(self) -> None:
        """一手买不起的标的不能白占一个仓位，否则等于按股价做隐性筛选。"""
        dates = ["2024-01-02", "2024-01-03"]
        panel = make_panel(
            {
                1: flat(2, 900.0, 0.9),  # 一手 90000 元，远超每格预算
                2: flat(2, 5.0, 0.8),
                3: flat(2, 5.0, 0.7),
            },
            dates,
        )
        cfg = FactorPortfolioConfig(
            top_n=2,
            rebalance_days=10,
            slippage_bps=0.0,
            costs=CashPortfolioConfig(
                initial_cash=10_000.0,
                commission_rate=0.0,
                min_commission=0.0,
                stamp_duty_rate=0.0,
                transfer_fee_rate=0.0,
            ),
        )
        result = run_factor_backtest(panel, cfg)
        assert sorted(result["trades"]["symbol"]) == [2, 3]
        assert result["positions"].iloc[-1] == 2

    def test_lot_remainder_is_absorbed_by_later_buys(self) -> None:
        dates = ["2024-01-02", "2024-01-03"]
        panel = make_panel({1: flat(2, 7.0, 0.9), 2: flat(2, 7.0, 0.8)}, dates)
        cfg = FactorPortfolioConfig(
            top_n=2,
            rebalance_days=10,
            slippage_bps=0.0,
            costs=CashPortfolioConfig(
                initial_cash=10_000.0,
                commission_rate=0.0,
                min_commission=0.0,
                stamp_duty_rate=0.0,
                transfer_fee_rate=0.0,
            ),
        )
        result = run_factor_backtest(panel, cfg)
        # 每格 5000 元、一手 700 元：不吸收零头只能各买 7 手（9800），闲置 200 元以上。
        assert float(result["cash"].iloc[-1]) < 700.0


class TestSuspensionAndDelisting:
    def test_suspended_position_is_valued_at_last_known_price_not_a_future_one(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        panel = make_panel(
            {
                1: [
                    {"close": 10.0, "score": 0.9},
                    {"close": 10.0, "score": 0.9},
                    {"close": None, "score": None},
                    {"close": 1.0, "score": 0.9},
                ],
            },
            dates,
        )
        cfg = FactorPortfolioConfig(top_n=1, rebalance_days=10, costs=FREE, slippage_bps=0.0)
        nav = run_factor_backtest(panel, cfg)["nav"]
        # 停牌日必须按停牌前的 10.0 估值；用复牌后的 1.0 就是提前知道了复牌价。
        assert nav.iloc[2] == pytest.approx(nav.iloc[1])
        assert nav.iloc[3] < nav.iloc[2]

    def test_delisted_position_is_liquidated_at_its_last_price(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        panel = make_panel(
            {
                1: [{"close": 10.0, "score": 0.9}, {"close": 8.0, "score": 0.9}, {"close": None, "score": None}],
                2: [{"close": 10.0, "score": 0.1}, {"close": 10.0, "score": 0.1}, {"close": 10.0, "score": 0.1}],
            },
            dates,
        )
        cfg = FactorPortfolioConfig(top_n=1, rebalance_days=10, costs=FREE, slippage_bps=0.0)
        result = run_factor_backtest(panel, cfg)
        assert "delisted" in set(result["trades"]["reason"])
        assert result["positions"].iloc[-1] == 0


class TestBenchmark:
    def test_holding_period_benchmark_matches_equal_weight_pool_return(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        panel = make_panel(
            {
                1: [
                    {"close": 10.0, "score": 0.9},
                    {"close": 10.0, "open": 10.0, "score": 0.9},
                    {"close": 12.0, "open": 12.0, "score": 0.9},
                ],
                2: [
                    {"close": 10.0, "score": 0.5},
                    {"close": 10.0, "open": 10.0, "score": 0.5},
                    {"close": 8.0, "open": 8.0, "score": 0.5},
                ],
            },
            dates,
        )
        nav = holding_period_benchmark(build_matrices(panel), rebalance_days=1)
        # 两只等权，一只 +20% 一只 -20%，整段应当回到原点。
        assert nav.iloc[-1] == pytest.approx(1.0)


class TestNullHypothesis:
    def test_random_scores_produce_no_alpha_without_frictions(self) -> None:
        """随机打分在零摩擦下必须贴着基准。偏离说明引擎某处系统性地偏好了某类标的。"""
        rng = np.random.default_rng(7)
        dates = pd.bdate_range("2024-01-02", periods=40).strftime("%Y-%m-%d").tolist()
        spec = {}
        for symbol in range(1, 61):
            path = 10.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, len(dates)))
            spec[symbol] = [{"close": float(p), "open": float(p), "score": float(rng.random())} for p in path]
        panel = make_panel(spec, dates)
        cfg = FactorPortfolioConfig(top_n=20, rebalance_days=5, costs=FREE, slippage_bps=0.0)
        result = run_factor_backtest(panel, cfg)
        benchmark = holding_period_benchmark(build_matrices(panel), rebalance_days=5)
        nav = result["nav"] / result["nav"].iloc[0]
        assert result["positions"].iloc[-1] == 20
        assert abs(float(nav.iloc[-1]) - float(benchmark.iloc[-1])) < 0.05
