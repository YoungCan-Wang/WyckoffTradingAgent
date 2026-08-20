"""Tests for regime-tiered liquidity thresholds.

2026-08-20 复盘：8/19 水温为 CRASH，流动性门槛 12000 万把 73/200 只涨停股拦在 L1 之外，
其中神州细胞（11949.7万）、南模生物（11945.7万）差不到 55 万。

实测（466 个交易日 / 全市场 20 日均额分档，T+5 净超额已扣 0.202% 往返成本）显示
抬高门槛没有选股优势——4000 万 -0.24、8000 万 -0.31、12000 万 -0.35，单调递减；
被砍掉的 11000~12000 万边缘区间单独看是 -0.20，反而优于 12000 档整体。

故 CRASH 与深度 RISK_OFF 统一下调至 8000（与 RISK_OFF 齐平）。保留 8000 而非降到
默认 4000，是因为该测试未建模低流动性票的滑点差异——门槛的真实价值在滑点保护。
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


class TestDefaults:
    def test_crash_lowered_to_8000(self):
        assert market_regime_config_from_env().crash_min_avg_amount_wan == pytest.approx(8000.0)

    def test_risk_off_deep_lowered_to_8000(self):
        assert market_regime_config_from_env().risk_off_deep_min_avg_amount_wan == pytest.approx(8000.0)

    def test_defensive_tiers_no_longer_exceed_risk_off(self):
        """防守档不再比 RISK_OFF 更严——那是本次改动的核心。"""
        config = market_regime_config_from_env()
        assert config.crash_min_avg_amount_wan <= config.risk_off_min_avg_amount_wan
        assert config.risk_off_deep_min_avg_amount_wan <= config.risk_off_min_avg_amount_wan

    def test_still_above_engine_default(self):
        """仍高于 FunnelConfig 默认 4000：保留对真正低流动性标的的滑点保护。"""
        from core.wyckoff_engine import FunnelConfig

        config = market_regime_config_from_env()
        assert config.crash_min_avg_amount_wan > FunnelConfig().min_avg_amount_wan

    def test_the_2026_08_20_edge_cases_would_now_pass(self):
        """神州细胞 11949.7万 / 南模生物 11945.7万 在新门槛下应放行。"""
        threshold = market_regime_config_from_env().crash_min_avg_amount_wan
        for observed_wan in (11949.7, 11945.7, 11790.3, 11731.8, 11108.0):
            assert observed_wan >= threshold


class TestEnvOverride:
    def test_env_still_wins(self, monkeypatch):
        monkeypatch.setenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "15000")
        assert market_regime_config_from_env().crash_min_avg_amount_wan == pytest.approx(15000.0)

    def test_can_restore_previous_behaviour(self, monkeypatch):
        """可回退：设回 12000/10000 即恢复改动前行为。"""
        monkeypatch.setenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "12000")
        monkeypatch.setenv("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", "10000")
        config = market_regime_config_from_env()
        assert config.crash_min_avg_amount_wan == pytest.approx(12000.0)
        assert config.risk_off_deep_min_avg_amount_wan == pytest.approx(10000.0)

    def test_all_amount_thresholds_are_positive(self):
        config = market_regime_config_from_env()
        for key in _AMOUNT_KEYS:
            assert getattr(config, key) > 0, key
