"""Tests for RISK_ON being re-blocked after cross-period evidence reversed.

此前依「联动实测 +6.07pct（3/3 为正）」把 RISK_ON 放入 STEP4_BUY_ALLOW_REGIMES 豁免。
但两次独立的跨周期回测均为负，且样本扩大后更差：

    2026-08-21 六个月网格：4 笔  胜率 25.0%  均收 -2.96%
    2026-08-22 六周期网格：6 笔  胜率 16.7%  均收 -5.03%（中位 -6.81%）

回测生成的交易手册亦写「RISK_ON 禁止新仓｜生产市场闸门固定禁止，不以近期样本收益解禁」。
样本仍偏小，但两次同向为负且禁买是保守侧，故恢复禁买。

注意：代码默认禁买集（core.market_trade_mode.EXECUTE_BLOCK_NEW_BUY_REGIMES）含
BEAR_REBOUND 而不含 NEUTRAL，与生产 env 恰好相反——生产靠 BLOCK/ALLOW 两个 env 覆写。
故以下用例都显式设置 env，不依赖代码默认值。
"""

from __future__ import annotations

import pytest

from workflows.step4_order_config import step4_order_config_from_env

PROD_BLOCK = "UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN"
PROD_ALLOW = "BEAR_REBOUND"


@pytest.fixture(autouse=True)
def _prod_env(monkeypatch):
    """复现生产 env，而非代码默认值。"""
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", PROD_BLOCK)
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", PROD_ALLOW)


class TestRiskOnBlocked:
    def test_risk_on_blocked(self):
        """核心回归：RISK_ON 不在 ALLOW 里，即回落到默认禁买集。"""
        assert "RISK_ON" in step4_order_config_from_env().buy_block_regimes

    def test_bear_rebound_still_allowed(self):
        """BEAR_REBOUND 的放开依据（+4.08pct、4/4 为正）未被推翻，必须保留。"""
        assert "BEAR_REBOUND" not in step4_order_config_from_env().buy_block_regimes

    @pytest.mark.parametrize("regime", ["CRASH", "RISK_OFF", "NEUTRAL", "PANIC_REPAIR", "BLACK_SWAN", "UNKNOWN"])
    def test_defensive_regimes_still_blocked(self, regime):
        assert regime in step4_order_config_from_env().buy_block_regimes


class TestAllowListMechanics:
    def test_allow_list_can_reopen_risk_on(self, monkeypatch):
        """机制仍在：若将来证据反转，加回豁免即可放开。"""
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND,RISK_ON")
        assert "RISK_ON" not in step4_order_config_from_env().buy_block_regimes

    def test_empty_allow_list_blocks_bear_rebound_too(self, monkeypatch):
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "")
        assert "BEAR_REBOUND" in step4_order_config_from_env().buy_block_regimes

    def test_backtest_gate_follows_same_config(self):
        """回测闸门与实盘同源（PR #301 已对齐），否则回测结论无法映射到实盘。"""
        from core.backtest_config import _live_buy_block_regimes

        blocked = _live_buy_block_regimes()
        assert "RISK_ON" in blocked
        assert "NEUTRAL" in blocked
        assert "BEAR_REBOUND" not in blocked
