"""Tests for STEP4_BUY_PROBE_REGIMES：把硬防守档降为小仓试探，而不是完全禁买。

起因是一个实测结论：NO_NEW_BUY 那句「回测全周期弱势、新开仓胜率不足」的依据不成立。
按全市场前瞻收益、用保留 run 结构的环移控制量（h ∈ {1,2,3,5,10}，非固定 T+5）：

- 49 天面板（2026-07-01~09-07，35 个可评估日）：禁新仓日**更差**，均值差
  -0.16 / -0.57 / -1.05 / -1.56 / -0.72。
- 97 天面板（2026-04-20~09-07，67 个可评估日）：禁新仓日**更好**，均值差
  +0.53 / +0.99 / +0.73 / +1.26，胜率差 +5.43 / +7.10 / +3.75 / +3.48 / +3.03。
- 两个面板环移 p 全在 0.147~0.977（无一显著），开启率在各次环移下守在 [48%,52%]；
  不重叠相位扫描在 h=2/3/5 上符号不一致。

换窗口就翻号，所以两个方向都不能单独引用——这个标签对市场强弱没有稳定信息。
但禁新仓日的全市场中位数在每个 h 上都为正（+0.13 / +0.29 / +0.45 / +0.50 / +2.08），
胜率 49.2%~59.1%，顶部十分位均值 +8.98~+25.41：那些天是买得到东西的。

反过来，漏斗自己在这些天的选票**没有**跑赢动量匹配对照（逐只 ±3.0pct 近邻替换、
不放回、5 个种子；禁新仓日 14/13/12/12/7 天，均低于 MIN_DAYS=20，判定是
「尚未可知（天数不足）」而非「跑输」）。所以不能整层放开，只能降档限仓。

因此本变量的语义是「小仓试探」而不是「解除拦截」：走既有 confirmation_only 档，
配额压到 1 只、禁 ATTACK、禁满配额 L4。默认不设该变量，行为与改动前逐位相同。

CRASH/BLACK_SWAN/UNKNOWN 不在白名单：左尾从未量过（上面全是均值与胜率），
且 UNKNOWN 兼作取数失败的兜底档，放开它等于把数据源抖动变成买入许可。
"""

from __future__ import annotations

import pytest

from core.market_trade_mode import (
    PROBE_DOWNGRADABLE_REGIMES,
    oms_buy_block_regimes,
    probe_only_regimes,
    resolve_market_trade_mode,
)
from workflows.step4_decision_parser import NewBuyLimits, max_new_buy_names
from workflows.step4_decisions import complete_step4_decisions
from workflows.step4_models import DecisionItem, PortfolioState, Step4RuntimeConfig

# neutral=3 是刻意的：两者都取 1 时「压到 1 只」与「满配额」数值相同，
# 断言会同时通过，看不出降档到底有没有生效。
LIMITS = NewBuyLimits(neutral=3, caution=1)
PROD_BLOCK = "UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN"


@pytest.fixture(autouse=True)
def _prod_env(monkeypatch):
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", PROD_BLOCK)
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")
    monkeypatch.delenv("STEP4_BUY_PROBE_REGIMES", raising=False)


def _blocked() -> frozenset[str]:
    from workflows.step4_order_config import step4_order_config_from_env

    return frozenset(step4_order_config_from_env().buy_block_regimes)


def _new_buy(code: str, score: float, *, action: str = "PROBE") -> DecisionItem:
    return DecisionItem(
        code=code,
        name="测试",
        action=action,
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


def _complete(regime: str, decisions: list[DecisionItem]):
    from workflows.step4_order_config import step4_order_config_from_env

    return complete_step4_decisions(
        decisions,
        PortfolioState(positions=[], free_cash=100_000.0, total_equity=100_000.0),
        {},
        regime,
        Step4RuntimeConfig(new_buy_limits=LIMITS),
        step4_order_config_from_env(),
    )


class TestDefaultClosed:
    """不设变量时必须与改动前逐位相同——这是回退路径，不是可选项。"""

    def test_risk_off_still_observe_only(self):
        mode = resolve_market_trade_mode("RISK_OFF")
        assert mode.mode == "observe_only"
        assert mode.allow_recommendation_write is False
        assert mode.allow_ai_review is False

    def test_risk_off_still_blocked_everywhere(self):
        from core.backtest_config import _live_buy_block_regimes

        assert "RISK_OFF" in _blocked()
        assert "RISK_OFF" in _live_buy_block_regimes()
        assert max_new_buy_names("RISK_OFF", LIMITS, _blocked()) == 0

    def test_probe_set_unchanged(self):
        assert probe_only_regimes() == frozenset({"CAUTION", "PANIC_REPAIR_CONFIRMED", "PANIC_REPAIR_INTRADAY"})

    def test_empty_value_is_not_a_wildcard(self, monkeypatch):
        """空串/纯逗号不得被解析成「全部降档」。"""
        for raw in ("", "   ", ",", " , ,"):
            monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", raw)
            assert resolve_market_trade_mode("RISK_OFF").mode == "observe_only", raw
            assert "RISK_OFF" in _blocked(), raw


class TestRiskOffDowngradedToProbe:
    @pytest.fixture(autouse=True)
    def _open_probe(self, monkeypatch):
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "RISK_OFF")

    def test_mode_is_confirmation_only(self):
        """复用既有 mode 字符串：下游十余处按 mode 分发，新值会静默落到 default 分支。"""
        mode = resolve_market_trade_mode("RISK_OFF")
        assert mode.mode == "confirmation_only"
        assert mode.label == "谨慎试探"

    def test_write_and_ai_open(self):
        """两者都要开：write 关着候选进不了推荐，等于降档白做。"""
        mode = resolve_market_trade_mode("RISK_OFF")
        assert mode.allow_recommendation_write is True
        assert mode.allow_ai_review is True

    def test_full_quota_paths_stay_shut(self):
        """降档只放开「买不买」，不放开「买多少」与「凭什么买」。"""
        mode = resolve_market_trade_mode("RISK_OFF")
        assert mode.allow_full_l4 is False
        assert mode.allow_bypass_review is False
        assert mode.allow_theme_promotion is False

    def test_reason_no_longer_claims_measured_weakness(self):
        """旧那句「回测全周期弱势／新开仓胜率不足」已被实测冲掉，不得再出现。"""
        reason = resolve_market_trade_mode("RISK_OFF").reason
        assert "胜率不足" not in reason
        assert "全周期弱势" not in reason

    def test_oms_no_longer_blocks(self):
        assert "RISK_OFF" not in _blocked()

    def test_backtest_gate_agrees(self):
        """回测与实盘同向，否则量的不是即将上线的这套闸门。"""
        from core.backtest_config import _live_buy_block_regimes

        assert "RISK_OFF" not in _live_buy_block_regimes()

    def test_quota_is_exactly_one_not_full(self):
        """这条是整个改动的要害。

        ``max_new_buy_names`` 先看 blocked（已减豁免）再看 probe 集合：RISK_OFF 过了
        第一道却不在第二道里，就会掉到 ``limits.neutral`` 满配额分支——「硬拦」直接
        跳成「满仓」，恰是用户要的「熊市小仓」的反面。
        """
        assert max_new_buy_names("RISK_OFF", LIMITS, _blocked()) == 1
        assert LIMITS.neutral > 1, "neutral 必须 >1，否则本断言无区分力"

    def test_actual_trim_keeps_exactly_one(self):
        """提示词说 1 只、真实裁剪也要留 1 只——两处读同一份集合。"""
        out = _complete("RISK_OFF", [_new_buy(f"00000{i}", 100.0 - i) for i in range(1, 5)])
        kept = [d for d in out if not d.system_reject_reason]
        assert len(kept) == 1
        assert kept[0].code == "000001"

    def test_order_engine_approves_probe_and_rejects_attack(self):
        """小仓的另一半在下单层：PROBE 过、ATTACK 拒。

        engine 先按 ``config.buy_block_regimes`` 整体拦截，再按 probe 集合拒 ATTACK。
        两处必须都读到降档后的集合，否则要么全拒（等于没降档）、要么 ATTACK 也放过
        （等于满仓）。
        """
        engine = self._engine("RISK_OFF")
        probe_tickets, _ = engine.process([_new_buy("000001", 100.0, action="PROBE")])
        attack_tickets, _ = engine.process([_new_buy("000001", 100.0, action="ATTACK")])

        assert probe_tickets[0].status == "APPROVED"
        assert attack_tickets[0].status == "NO_TRADE"
        assert "只允许小额 PROBE" in attack_tickets[0].reason

    def test_order_engine_rejects_both_when_var_unset(self, monkeypatch):
        """同一段代码，变量收回就该两边都拒——证明上一条测的是降档而非默认行为。"""
        monkeypatch.delenv("STEP4_BUY_PROBE_REGIMES", raising=False)
        engine = self._engine("RISK_OFF")
        for action in ("PROBE", "ATTACK"):
            tickets, _ = engine.process([_new_buy("000001", 100.0, action=action)])
            assert tickets[0].status == "NO_TRADE", action
            assert "系统性风控拦截" in tickets[0].reason, action

    @staticmethod
    def _engine(regime: str):
        from workflows.step4_order_config import step4_order_config_from_env
        from workflows.step4_order_engine import WyckoffOrderEngine

        return WyckoffOrderEngine(
            total_equity=100_000,
            free_cash=50_000,
            position_map={},
            latest_price_map={"000001": 10.5},
            atr_map={"000001": 0.2},
            market_regime=regime,
            config=step4_order_config_from_env(),
        )

    def test_replay_truncates_live_selection_to_one(self):
        """回测 live 模式的限仓也走同一函数，否则回测按满仓量、实盘按 1 只买。"""
        from core.backtest_replay import _limit_probe_only_selection, _RankedSelection

        selection = _RankedSelection(
            ["000001", "000002", "000003"],
            {"000001": 100.0, "000002": 90.0, "000003": 80.0},
            {},
            {},
        )
        kept, dropped = _limit_probe_only_selection(selection, "RISK_OFF", "live")
        assert kept.codes == ["000001"]
        assert dropped == 2
        assert kept.score_map == {"000001": 100.0}

    def test_replay_keeps_all_when_var_unset(self, monkeypatch):
        monkeypatch.delenv("STEP4_BUY_PROBE_REGIMES", raising=False)
        from core.backtest_replay import _limit_probe_only_selection, _RankedSelection

        selection = _RankedSelection(["000001", "000002"], {}, {}, {})
        kept, dropped = _limit_probe_only_selection(selection, "RISK_OFF", "live")
        assert kept.codes == ["000001", "000002"]
        assert dropped == 0


class TestWhitelistHolds:
    """白名单求交是唯一防线：变量里写别的档位必须无效。"""

    def test_whitelist_is_risk_off_only(self):
        assert PROBE_DOWNGRADABLE_REGIMES == frozenset({"RISK_OFF"})

    def test_left_tail_unmeasured_regimes_stay_shut(self, monkeypatch):
        """CRASH/BLACK_SWAN/UNKNOWN 左尾未量过，UNKNOWN 还兼取数失败兜底。"""
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "CRASH,BLACK_SWAN,UNKNOWN")
        for regime in ("CRASH", "BLACK_SWAN", "UNKNOWN"):
            mode = resolve_market_trade_mode(regime)
            assert mode.mode == "observe_only", regime
            assert mode.allow_recommendation_write is False, regime
            assert regime in _blocked(), regime
            assert max_new_buy_names(regime, LIMITS, _blocked()) == 0, regime

    def test_probe_var_cannot_open_neutral_or_panic_repair(self, monkeypatch):
        """非硬防守档也不能被这个变量顺带改语义。"""
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "NEUTRAL,PANIC_REPAIR,RISK_ON")
        assert resolve_market_trade_mode("NEUTRAL").mode == "execution_blocked"
        assert resolve_market_trade_mode("PANIC_REPAIR").mode == "repair_review"
        assert resolve_market_trade_mode("RISK_ON").allow_recommendation_write is False

    def test_mixed_value_keeps_only_whitelisted(self, monkeypatch):
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "CRASH,RISK_OFF,BLACK_SWAN")
        assert probe_only_regimes() & {"CRASH", "BLACK_SWAN"} == frozenset()
        assert "RISK_OFF" in probe_only_regimes()

    def test_value_is_case_and_space_insensitive(self, monkeypatch):
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "  risk_off , ")
        assert resolve_market_trade_mode("RISK_OFF").mode == "confirmation_only"


class TestCautionUnchanged:
    """CAUTION 与降档档共用构造器，权限位必须逐字一致，且 CAUTION 自身不能被改到。"""

    def test_caution_matches_downgraded_risk_off(self, monkeypatch):
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", "RISK_OFF")
        caution = resolve_market_trade_mode("CAUTION")
        risk_off = resolve_market_trade_mode("RISK_OFF")
        for field in (
            "mode",
            "label",
            "action",
            "allow_ai_review",
            "allow_recommendation_write",
            "allow_full_l4",
            "allow_bypass_review",
            "allow_theme_promotion",
        ):
            assert getattr(caution, field) == getattr(risk_off, field), field
        # 只有 reason 按水温区分，供运维回溯是哪条路径放行的。
        assert caution.reason != risk_off.reason

    def test_caution_quota_still_one(self):
        assert max_new_buy_names("CAUTION", LIMITS, _blocked()) == 1

    def test_caution_action_text_is_regime_agnostic(self):
        """不能复用 repair_probe：它的下游文案硬编码「修复成立」，
        在 RISK_OFF 日会印出一句假话。"""
        action = resolve_market_trade_mode("CAUTION").action
        assert "修复成立" not in action
        assert "PROBE" in action and "禁止 ATTACK" in action


class TestBothGatesMoveTogether:
    """写入侧与 OMS 侧必须同源。

    2026-08 的事故形态：运维把 NEUTRAL 加进 BLOCK，只有 OMS 照办，写入侧照写，
    留下 16 行买不到的正式推荐（20260720×9 / 20260721×4 / 20260723×3，全 l4_springboard）。
    降档是那次错位的镜像风险：写入侧放行而 OMS 仍禁买，报告写「谨慎试探」而一股买不到。
    """

    @pytest.mark.parametrize("probe_value", ["", "RISK_OFF"])
    def test_write_and_oms_agree(self, monkeypatch, probe_value):
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", probe_value)
        write_open = resolve_market_trade_mode("RISK_OFF").allow_recommendation_write
        oms_open = "RISK_OFF" not in oms_buy_block_regimes()
        assert write_open is oms_open

    @pytest.mark.parametrize("probe_value", ["", "RISK_OFF"])
    def test_quota_and_write_agree(self, monkeypatch, probe_value):
        monkeypatch.setenv("STEP4_BUY_PROBE_REGIMES", probe_value)
        write_open = resolve_market_trade_mode("RISK_OFF").allow_recommendation_write
        quota = max_new_buy_names("RISK_OFF", LIMITS, _blocked())
        assert write_open is (quota > 0)
