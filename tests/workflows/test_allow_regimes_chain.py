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
from workflows.step4_decisions import complete_step4_decisions
from workflows.step4_models import DecisionItem, PortfolioState, Step4RuntimeConfig

LIMITS = NewBuyLimits(neutral=3, caution=1)
PROD_BLOCK = "UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN"


@pytest.fixture(autouse=True)
def _prod_env(monkeypatch):
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", PROD_BLOCK)
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")


def _blocked() -> frozenset[str]:
    from workflows.step4_order_config import step4_order_config_from_env

    return frozenset(step4_order_config_from_env().buy_block_regimes)


def _new_buy(code: str, score: float) -> DecisionItem:
    return DecisionItem(
        code=code,
        name="测试",
        action="PROBE",
        entry_zone_min=10.0,
        entry_zone_max=11.0,
        stop_loss=9.0,
        trim_ratio=None,
        tape_condition="",
        invalidate_condition="",
        is_add_on=False,
        reason="主线买点确认",
        confidence=0.7,
        funnel_score=score,
    )


def _complete(regime: str, decisions: list[DecisionItem], *, pass_order_config: bool = True):
    """跑生产那条真实链路：``_run_step4_decision_flow`` → ``complete_step4_decisions``。"""
    from workflows.step4_order_config import step4_order_config_from_env

    return complete_step4_decisions(
        decisions,
        PortfolioState(positions=[], free_cash=100_000.0, total_equity=100_000.0),
        {},
        regime,
        Step4RuntimeConfig(new_buy_limits=LIMITS),
        step4_order_config_from_env() if pass_order_config else None,
    )


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

    def test_actual_trim_keeps_the_new_buy(self):
        """第四个断点：实际裁剪那一层。

        #308 打通了 OMS 闸门、提示词配额与 trade mode 三处，却漏了
        ``complete_step4_decisions`` 里真正执行裁剪的调用 —— 它不传 blocked_regimes，
        落回硬编码集合把 BEAR_REBOUND 重新拦成 0。2026-08-31 那轮就此丢掉唯一新开
        候选 002292（funnel_score=108.00）。
        """
        out = _complete("BEAR_REBOUND", [_new_buy("002292", 108.0)])
        # 与生产同一套判据：step4_decision_parser 按 `not dec.system_reject_reason` 取值，
        # 未被拒时字段是空串而非 None。
        assert [d.code for d in out if not d.system_reject_reason] == ["002292"]

    def test_prompt_quota_and_actual_trim_agree(self):
        """提示词说几只、就得真能留几只 —— 两处必须读同一份禁买集合。

        错位的形态是「告诉模型可以买 1 只，模型照给，系统再以配额 0 丢掉」，
        日志只留一行 ``组合级限购拦截``，看不出配额是从哪来的。
        """
        promised = max_new_buy_names("BEAR_REBOUND", LIMITS, _blocked())
        out = _complete("BEAR_REBOUND", [_new_buy(f"00000{i}", 100.0 - i) for i in range(1, 5)])
        kept = [d for d in out if not d.system_reject_reason]
        assert promised > 0
        assert len(kept) == promised


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

    def test_actual_trim_still_drops_the_new_buy(self):
        """把 order_config 传下去不等于全面放开：未豁免的档位仍须归零。"""
        out = _complete("PANIC_REPAIR", [_new_buy("002292", 108.0)])
        assert out[0].system_reject_reason is not None
        assert "max_new_buy_names=0" in out[0].system_reject_reason


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
        # 退路必须在真实裁剪那一层也生效，否则「移出 ALLOW」只关掉三层。
        out = _complete("BEAR_REBOUND", [_new_buy("002292", 108.0)])
        assert out[0].system_reject_reason is not None

    def test_omitting_order_config_keeps_old_behavior(self):
        """不传 order_config 时保持旧语义，与 max_new_buy_names(..., None) 一致。

        这不是「建议这么调用」——生产必须传。留这条是为了让默认值的语义有明确断言，
        避免以后有人以为省略参数也能放行。
        """
        out = _complete("BEAR_REBOUND", [_new_buy("002292", 108.0)], pass_order_config=False)
        assert out[0].system_reject_reason is not None
        assert "max_new_buy_names=0" in out[0].system_reject_reason

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
