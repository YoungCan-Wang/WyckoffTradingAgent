"""Tests for STEP4_BLOCK_BUY_SIGNAL_TYPES — 按买点类型禁买的显式开关。

依据：生产回测（scripts/backtest_runner.py，66 个交易日 / 124 笔 / 含真实成本）
按买点归因 evr -2.30%、sos -4.92%（胜率 33.3%）、spring +0.05%；变体对比中
G（剔除 evr+sos）在每个指标上优于基线（总收益 -15.82%→-6.31%、回撤 -23.91%→-13.14%）。
但证据仅覆盖单段行情，故默认为空、必须显式开启。
"""

from __future__ import annotations

import pytest

from core.signal_confirmation import _confirmed_symbol_info
from workflows.step4_pipeline import _blocked_buy_signal_types, _rule_eligible_step4_candidate, step4_candidate_meta

ENV_KEY = "STEP4_BLOCK_BUY_SIGNAL_TYPES"


def _candidate(**overrides) -> dict:
    base = {
        "is_confirmed": True,
        "new_buy_allowed": True,
        "label_ready": True,
        "trade_readiness": "ready",
        "action_status": "ok",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)


class TestDefaultIsNoOp:
    def test_empty_by_default(self):
        assert _blocked_buy_signal_types() == frozenset()

    def test_all_signal_types_pass_by_default(self):
        """默认不改变任何行为——证据只覆盖单段行情，不该悄悄生效。"""
        for signal in ("evr", "sos", "spring", "lps", "compression", "trend_pullback"):
            assert _rule_eligible_step4_candidate(_candidate(signal_type=signal)) is True

    def test_blank_and_comma_only_are_noop(self, monkeypatch):
        for raw in ("", "   ", ",", " , , "):
            monkeypatch.setenv(ENV_KEY, raw)
            assert _blocked_buy_signal_types() == frozenset()


class TestBlocking:
    def test_blocks_named_signals(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "evr,sos")
        assert _rule_eligible_step4_candidate(_candidate(signal_type="evr")) is False
        assert _rule_eligible_step4_candidate(_candidate(signal_type="sos")) is False

    def test_does_not_touch_unnamed_signals(self, monkeypatch):
        """精确禁买：没点名的买点一个都不能被牵连。"""
        monkeypatch.setenv(ENV_KEY, "evr,sos")
        for signal in ("spring", "lps", "compression", "trend_pullback"):
            assert _rule_eligible_step4_candidate(_candidate(signal_type=signal)) is True

    def test_falls_back_to_trigger_field(self, monkeypatch):
        """回测产物用 trigger 字段、线上用 signal_type，两者都要认。"""
        monkeypatch.setenv(ENV_KEY, "sos")
        assert _rule_eligible_step4_candidate(_candidate(trigger="sos")) is False
        assert _rule_eligible_step4_candidate(_candidate(trigger="spring")) is True

    def test_normalizes_case_and_separators(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, " EVR , SOS ")
        assert _rule_eligible_step4_candidate(_candidate(signal_type="evr")) is False
        assert _rule_eligible_step4_candidate(_candidate(signal_type="SOS")) is False

    def test_hyphen_and_space_variants_match(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "trend_pullback")
        assert _rule_eligible_step4_candidate(_candidate(signal_type="trend-pullback")) is False
        assert _rule_eligible_step4_candidate(_candidate(signal_type="trend pullback")) is False

    def test_unknown_token_blocks_nothing_real(self, monkeypatch):
        """写错买点名不该意外拦掉别的信号。"""
        monkeypatch.setenv(ENV_KEY, "typo_signal")
        for signal in ("evr", "sos", "spring"):
            assert _rule_eligible_step4_candidate(_candidate(signal_type=signal)) is True

    def test_missing_signal_field_is_not_blocked(self, monkeypatch):
        """缺字段时不应被误拦——宁可放行交由其它闸门处理。"""
        monkeypatch.setenv(ENV_KEY, "evr,sos")
        assert _rule_eligible_step4_candidate(_candidate()) is True


class TestOtherGatesStillApply:
    def test_unconfirmed_still_rejected(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "evr")
        assert _rule_eligible_step4_candidate(_candidate(is_confirmed=False, signal_type="spring")) is False

    def test_new_buy_not_allowed_still_rejected(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "evr")
        assert _rule_eligible_step4_candidate(_candidate(new_buy_allowed=False, signal_type="spring")) is False

    def test_blocked_action_still_rejected(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "evr")
        assert _rule_eligible_step4_candidate(_candidate(action_status="blocked_risk", signal_type="spring")) is False


class TestRegimeSignalGate:
    @pytest.mark.parametrize("regime", ["BEAR_REBOUND", "CAUTION", "caution"])
    def test_trend_breakout_blocked_on_execution_regime(self, regime):
        assert _rule_eligible_step4_candidate(_candidate(signal_type="trend_breakout"), regime) is False

    def test_other_signals_and_regimes_pass(self):
        assert _rule_eligible_step4_candidate(_candidate(signal_type="trend_lane_pullback"), "BEAR_REBOUND") is True
        assert _rule_eligible_step4_candidate(_candidate(signal_type="trend_breakout"), "NEUTRAL") is True

    def test_gate_ignores_regime_stamped_on_the_candidate(self):
        stale = _candidate(signal_type="trend_breakout", regime="NEUTRAL")
        assert _rule_eligible_step4_candidate(stale, "BEAR_REBOUND") is False

    def test_production_confirmed_row_reaches_the_gate(self):
        sig = {"signal_type": "trend_breakout", "signal_date": "2026-10-29", "signal_score": 80.0, "regime": "NEUTRAL"}
        today = {"close": 10.0}
        item = _confirmed_symbol_info(sig, "000001", today, "2026-10-30", "快速确认")

        assert step4_candidate_meta([item], [], regime="NEUTRAL")[0] == [item]
        assert step4_candidate_meta([item], [], regime="BEAR_REBOUND") == ([], 1)

    def test_supabase_shaped_row_uses_primary_signal_or_entry_type(self):
        """step4_from_supabase 回读行没有 signal_type 时仍须命中水温屏蔽。"""
        by_primary = _candidate(
            candidate_status="跨日确认观察",
            primary_signal="trend_breakout",
            entry_type="mainline",
        )
        by_entry = _candidate(candidate_status="跨日确认观察", entry_type="trend_breakout")
        assert _rule_eligible_step4_candidate(by_primary, "BEAR_REBOUND") is False
        assert _rule_eligible_step4_candidate(by_entry, "CAUTION") is False
        assert _rule_eligible_step4_candidate(by_primary, "NEUTRAL") is True

    def test_empty_regime_falls_back_to_market_signal(self, monkeypatch):
        """空 benchmark_context 不得对 regime×signal 闸门 fail-open。"""
        monkeypatch.setattr("workflows.step4_pipeline.latest_trade_date_str", lambda: "2026-11-02")
        monkeypatch.setattr(
            "workflows.step4_pipeline.resolve_step4_gate_regime",
            lambda _ctx, _date: "BEAR_REBOUND",
        )
        item = _candidate(code="000001", signal_type="trend_breakout", candidate_status="跨日确认观察")
        assert step4_candidate_meta([item], [], regime="") == ([], 1)
        assert step4_candidate_meta([item], [], regime=None) == ([], 1)
