"""Tests for backtest execution gate aligning with the live buy-block list.

回测 live 模式此前用硬编码的 EXECUTE_BLOCK_NEW_BUY_REGIMES，与实盘
STEP4_BUY_BLOCK_REGIMES 方向恰好相反：

    回测禁买  BEAR_REBOUND / RISK_ON（实测超额 +4.08 / +6.07pct，实盘已放开）
    回测放行  NEUTRAL（实测超额 -4.35pct，实盘已禁买）

后果是回测只在 NEUTRAL 下单、实盘恰好不在 NEUTRAL 下单，两者成交的交易日几乎不重叠，
任何按水温分档的回测结论都无法映射到实盘。
"""

from __future__ import annotations

import pytest

from core.backtest_replay import _execution_regime_allows
from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES

LIVE_BLOCKED = frozenset({"UNKNOWN", "NEUTRAL", "PANIC_REPAIR", "RISK_OFF", "CRASH", "BLACK_SWAN"})


class TestLiveGateUsesInjectedList:
    def test_neutral_blocked_when_live_blocks_it(self):
        """最关键的一条：实盘禁 NEUTRAL，回测必须跟随。"""
        assert _execution_regime_allows("NEUTRAL", "live", LIVE_BLOCKED) is False

    @pytest.mark.parametrize("regime", ["BEAR_REBOUND", "RISK_ON"])
    def test_positive_regimes_allowed(self, regime):
        """实测为正的两档必须放行——旧硬编码集合把它们禁掉了。"""
        assert _execution_regime_allows(regime, "live", LIVE_BLOCKED) is True

    @pytest.mark.parametrize("regime", ["CRASH", "RISK_OFF", "PANIC_REPAIR", "BLACK_SWAN", "UNKNOWN"])
    def test_defensive_regimes_still_blocked(self, regime):
        assert _execution_regime_allows(regime, "live", LIVE_BLOCKED) is False


class TestFallbackPreservesOldBehaviour:
    def test_none_falls_back_to_hardcoded(self):
        """不注入时保持旧行为，避免遗漏调用方静默改变语义。"""
        for regime in EXECUTE_BLOCK_NEW_BUY_REGIMES:
            assert _execution_regime_allows(regime, "live", None) is False

    def test_neutral_allowed_under_fallback(self):
        assert _execution_regime_allows("NEUTRAL", "live", None) is True


class TestOtherModesUnaffected:
    @pytest.mark.parametrize("regime", ["NEUTRAL", "CRASH", "BEAR_REBOUND"])
    def test_off_allows_everything(self, regime):
        assert _execution_regime_allows(regime, "off", LIVE_BLOCKED) is True

    def test_neutral_only_ignores_block_list(self):
        assert _execution_regime_allows("NEUTRAL", "neutral_only", LIVE_BLOCKED) is True
        assert _execution_regime_allows("BEAR_REBOUND", "neutral_only", LIVE_BLOCKED) is False


class TestConfigWiring:
    def test_reads_live_config(self, monkeypatch):
        from core.backtest_config import _live_buy_block_regimes

        monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "NEUTRAL,CRASH")
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "")
        blocked = _live_buy_block_regimes()
        assert "NEUTRAL" in blocked
        assert "CRASH" in blocked

    def test_allow_list_takes_precedence(self, monkeypatch):
        """ALLOW 豁免必须生效，否则放开的档位在回测里仍被禁。"""
        from core.backtest_config import _live_buy_block_regimes

        monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "NEUTRAL,BEAR_REBOUND")
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")
        blocked = _live_buy_block_regimes()
        assert "BEAR_REBOUND" not in blocked
        assert "NEUTRAL" in blocked
