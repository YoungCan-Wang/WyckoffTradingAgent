"""Tests for regime-tiered liquidity thresholds.

这些门槛是**死配置**：CRASH 与深度 RISK_OFF 都在 STEP4_BUY_BLOCK_REGIMES 里，
那些档位下压根不下单，门槛因此不参与任何成交决策，只影响候选池规模。

#295 曾依「全市场分档实测门槛越高净超额越差」把 12000/10000 下调到 8000，
但该测试未叠加禁买闸门。修复回测小盘基准（#299）后重跑对照：门槛 8000 与 12000/10000
两组的成交笔数、胜率、总收益、夏普、回撤、VaR95、Trend/Accum 分层**全部一字不差**
（103 笔 / 33.01% / +5.72% / 0.714 / -7.25% / -8.881%）。故已恢复原值。

这些用例守的是「恢复后的值」与「防守档比常规档更严」这条设计意图，
以及门槛本身的健壮性——而不是任何收益主张。
"""

from __future__ import annotations

import pytest

from workflows.market_regime_config import market_regime_config_from_env

_AMOUNT_KEYS = (
    "panic_repair_min_avg_amount_wan",
    "risk_off_min_avg_amount_wan",
    "risk_off_deep_min_avg_amount_wan",
    "crash_min_avg_amount_wan",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in (
        "FUNNEL_PANIC_REPAIR_MIN_AVG_AMOUNT_WAN",
        "FUNNEL_RISK_OFF_MIN_AVG_AMOUNT_WAN",
        "FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN",
        "FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN",
    ):
        monkeypatch.delenv(key, raising=False)


class TestRestoredDefaults:
    def test_crash_restored_to_12000(self):
        assert market_regime_config_from_env().crash_min_avg_amount_wan == pytest.approx(12000.0)

    def test_risk_off_deep_restored_to_10000(self):
        assert market_regime_config_from_env().risk_off_deep_min_avg_amount_wan == pytest.approx(10000.0)

    def test_tiers_get_stricter_as_risk_rises(self):
        """设计意图：越危险的档位要求越高的流动性。"""
        config = market_regime_config_from_env()
        assert (
            config.panic_repair_min_avg_amount_wan
            <= config.risk_off_min_avg_amount_wan
            <= config.risk_off_deep_min_avg_amount_wan
            <= config.crash_min_avg_amount_wan
        )

    def test_all_above_engine_default(self):
        from core.wyckoff_engine import FunnelConfig

        base = FunnelConfig().min_avg_amount_wan
        config = market_regime_config_from_env()
        for key in _AMOUNT_KEYS:
            assert getattr(config, key) > base, key


class TestEnvOverride:
    def test_env_still_wins(self, monkeypatch):
        monkeypatch.setenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "15000")
        assert market_regime_config_from_env().crash_min_avg_amount_wan == pytest.approx(15000.0)

    def test_can_experiment_with_lower_tiers(self, monkeypatch):
        """仍可通过 env 试验更低门槛——只是别指望它改变成交结果。"""
        monkeypatch.setenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "8000")
        monkeypatch.setenv("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", "8000")
        config = market_regime_config_from_env()
        assert config.crash_min_avg_amount_wan == pytest.approx(8000.0)
        assert config.risk_off_deep_min_avg_amount_wan == pytest.approx(8000.0)

    def test_all_amount_thresholds_are_positive(self):
        config = market_regime_config_from_env()
        for key in _AMOUNT_KEYS:
            assert getattr(config, key) > 0, key
