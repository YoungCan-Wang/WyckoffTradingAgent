"""Tests for STEP4_BUY_ALLOW_REGIMES 打通全链路。

此前豁免只作用于 OMS 的 buy_block_regimes，而 max_new_buy_names、
build_market_guardrail、resolve_market_trade_mode 三处仍按硬编码
EXECUTE_BLOCK_NEW_BUY_REGIMES 拦截，使 ALLOW 形同虚设。#301/#308 又让回测闸门读了
同一份 ALLOW，于是形成「回测能买、实盘买不到」的错位。

BEAR_REBOUND 实测（6 天 / 日均 126 只候选 / T+1 开盘买入 → T+5 / 扣 0.202%）：
净收益 +4.02%、市场 +4.06%、净超额 -0.04pct、为正日恰 50%——无 alpha 但跟得住 beta。
样本仅 8 天且集中在 2026-08-03~08-17 两周内，故这是**可回退的对齐**而非已证明的优势。

关键约束：BEAR_REBOUND 与 PANIC_REPAIR 原本共用 REPAIR_REVIEW_REGIMES，
不能整块打开——PANIC_REPAIR 实测 -4.09pct，必须保持禁买。
"""

from __future__ import annotations

import pytest

from core.market_trade_mode import resolve_market_trade_mode
from workflows.step4_decision_parser import NewBuyLimits, max_new_buy_names

LIMITS = NewBuyLimits(neutral=3, caution=1)
PROD_BLOCK = "UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN"


@pytest.fixture(autouse=True)
def _prod_env(monkeypatch):
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", PROD_BLOCK)
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")


def _blocked() -> frozenset[str]:
    from workflows.step4_order_config import step4_order_config_from_env

    return frozenset(step4_order_config_from_env().buy_block_regimes)


class TestAllowedRegimeOpensEveryLayer:
    def test_oms_allows(self):
        assert "BEAR_REBOUND" not in _blocked()

    def test_new_buy_quota_nonzero(self):
        """此前这里硬编码返回 0，是 ALLOW 失效的第一个断点。"""
        assert max_new_buy_names("BEAR_REBOUND", LIMITS, _blocked()) > 0

    def test_recommendation_write_open(self):
        """第三个断点：write 关闭会让候选进不了推荐，等于仍然买不到。"""
        assert resolve_market_trade_mode("BEAR_REBOUND").allow_recommendation_write is True

    def test_backtest_gate_agrees(self):
        """回测与实盘必须同向，否则回测结论无法映射。"""
        from core.backtest_config import _live_buy_block_regimes

        assert "BEAR_REBOUND" not in _live_buy_block_regimes()


class TestPanicRepairStaysBlocked:
    """BEAR_REBOUND 与 PANIC_REPAIR 共用 REPAIR_REVIEW_REGIMES，不得连带放行。"""

    def test_still_blocked_in_oms(self):
        assert "PANIC_REPAIR" in _blocked()

    def test_quota_zero(self):
        assert max_new_buy_names("PANIC_REPAIR", LIMITS, _blocked()) == 0

    def test_write_still_closed(self):
        mode = resolve_market_trade_mode("PANIC_REPAIR")
        assert mode.allow_recommendation_write is False
        assert mode.mode == "repair_review"


class TestRiskOnStaysBlocked:
    """RISK_ON 于 #305 移出 ALLOW（两次跨周期回测 -2.96% / -5.03%）。"""

    def test_write_closed(self):
        assert resolve_market_trade_mode("RISK_ON").allow_recommendation_write is False

    def test_quota_zero(self):
        assert max_new_buy_names("RISK_ON", LIMITS, _blocked()) == 0


class TestRollback:
    def test_empty_allow_restores_block(self, monkeypatch):
        """移出 ALLOW 即刻恢复禁买——样本只有 8 天，必须留这条退路。"""
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "")
        assert "BEAR_REBOUND" in _blocked()
        assert max_new_buy_names("BEAR_REBOUND", LIMITS, _blocked()) == 0
        assert resolve_market_trade_mode("BEAR_REBOUND").allow_recommendation_write is False

    def test_defensive_regimes_never_openable_by_allow(self, monkeypatch):
        """CRASH/RISK_OFF 属 NO_NEW_BUY，即便误加进 ALLOW 也不应打开写入。"""
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "CRASH,RISK_OFF")
        for regime in ("CRASH", "RISK_OFF"):
            assert resolve_market_trade_mode(regime).allow_recommendation_write is False


class TestQuotaFallback:
    def test_none_falls_back_to_hardcoded(self):
        """不传 blocked_regimes 时保持旧行为，避免遗漏调用方静默改变语义。"""
        assert max_new_buy_names("BEAR_REBOUND", LIMITS, None) == 0

    def test_caution_capped_at_one(self):
        assert max_new_buy_names("CAUTION", LIMITS, _blocked()) == 1
