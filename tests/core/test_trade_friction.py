"""Tests for signal-level trade friction (net vs gross returns)."""

from __future__ import annotations

import pytest

from core.cash_portfolio import CashPortfolioConfig
from core.trade_friction import (
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    configure_slippage,
    friction_breakdown,
    net_return_pct,
    round_trip_cost_pct,
    slippage_bps_per_side,
)


class TestRoundTripCost:
    def test_positive_and_bounded(self):
        cost = round_trip_cost_pct()
        # A 股双边合计应在 0.1%~1% 这个量级；超出说明费率或滑点配错了。
        assert 0.1 < cost < 1.0

    def test_includes_stamp_duty_on_sell_only(self):
        """印花税只在卖出侧征收，所以卖出费率应高于买入。"""
        parts = friction_breakdown()
        assert parts["sell_fee_pct"] > parts["buy_fee_pct"]

    def test_breakdown_sums_to_total(self):
        parts = friction_breakdown()
        total = parts["buy_fee_pct"] + parts["sell_fee_pct"] + parts["slippage_pct"]
        assert parts["round_trip_pct"] == pytest.approx(total, abs=1e-6)

    def test_small_notional_costs_more(self):
        """佣金有 5 元最低收费，小额交易的百分比成本更高。"""
        assert round_trip_cost_pct(2_000.0) > round_trip_cost_pct(200_000.0)

    def test_respects_custom_rates(self):
        zero = CashPortfolioConfig(commission_rate=0.0, min_commission=0.0, stamp_duty_rate=0.0, transfer_fee_rate=0.0)
        # 费率归零后仍应剩下滑点，不能变成 0。
        assert round_trip_cost_pct(20_000.0, zero) == pytest.approx(
            DEFAULT_SLIPPAGE_BPS_PER_SIDE / 10_000.0 * 2.0 * 100.0, abs=1e-6
        )


class TestNetReturn:
    def test_subtracts_cost(self):
        gross = 5.0
        assert net_return_pct(gross) == pytest.approx(gross - round_trip_cost_pct(), abs=1e-6)

    def test_none_passes_through(self):
        assert net_return_pct(None) is None

    def test_marginal_positive_turns_negative(self):
        """回归：accumulation_ready 的 +0.10% 毛收益扣费后应转负。

        这是接成本模型的核心动机——微弱正收益撑不过摩擦。
        """
        assert net_return_pct(0.10) < 0

    def test_loss_gets_worse(self):
        assert net_return_pct(-4.47) < -4.47


class TestSlippageConfig:
    """core 不读 env（架构约束），滑点由 utils.runtime_friction 注入。"""

    @pytest.fixture(autouse=True)
    def _restore(self):
        yield
        configure_slippage(DEFAULT_SLIPPAGE_BPS_PER_SIDE)

    def test_configure(self):
        configure_slippage(25.0)
        assert slippage_bps_per_side() == 25.0

    def test_invalid_falls_back(self):
        configure_slippage("abc")
        assert slippage_bps_per_side() == DEFAULT_SLIPPAGE_BPS_PER_SIDE

    def test_none_falls_back(self):
        configure_slippage(None)
        assert slippage_bps_per_side() == DEFAULT_SLIPPAGE_BPS_PER_SIDE

    def test_negative_clamped(self):
        configure_slippage(-5)
        assert slippage_bps_per_side() == 0.0

    def test_env_injection_via_utils(self, monkeypatch):
        from utils.runtime_friction import apply_friction_config_from_env

        monkeypatch.setenv("WYCKOFF_SLIPPAGE_BPS_PER_SIDE", "30")
        assert apply_friction_config_from_env() == 30.0
        assert slippage_bps_per_side() == 30.0


class TestLifecycleIntegration:
    def test_outcome_carries_net_return(self):
        import pandas as pd

        from core.signal_lifecycle import evaluate_signal_lifecycle

        dates = pd.date_range("2026-06-01", periods=12, freq="D")
        frame = pd.DataFrame(
            {
                "date": dates,
                "close": [10.0] * 6 + [11.0] * 6,
                "low": [9.5] * 12,
            }
        )
        lifecycle = evaluate_signal_lifecycle(frame, code="000001", signal_date="2026-06-06", horizons=(5,))
        outcome = lifecycle.outcomes[0]
        assert outcome.status == "done"
        assert outcome.net_return_pct is not None
        assert outcome.net_return_pct < outcome.return_pct
