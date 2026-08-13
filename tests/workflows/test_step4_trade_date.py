from datetime import date
from types import SimpleNamespace

import workflows.step4_results as step4_results
from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES
from workflows import step4_portfolio
from workflows import step4_rebalancer as step4
from workflows.step4_decision_parser import max_new_buy_names, trim_new_buy_decisions
from workflows.step4_decisions import backfill_step4_decision_market_data, complete_step4_decisions
from workflows.step4_models import (
    CandidateMeta,
    DecisionItem,
    ExecutionTicket,
    NewBuyLimits,
    PortfolioState,
    PositionItem,
)
from workflows.step4_order_config import step4_order_config_from_env
from workflows.step4_order_engine import WyckoffOrderEngine
from workflows.step4_runtime_config import step4_runtime_config_from_env
from workflows.step4_ticket import render_trade_ticket


def _decision(action: str, *, is_add_on: bool = False) -> DecisionItem:
    return DecisionItem(
        code="000001",
        name="平安银行",
        action=action,
        entry_zone_min=9.4,
        entry_zone_max=9.7,
        stop_loss=8.9,
        trim_ratio=None,
        tape_condition="放量站回VWAP",
        invalidate_condition="跌破VWAP",
        is_add_on=is_add_on,
        reason="模型建议加仓",
        confidence=0.8,
    )


def _ticket(
    *,
    code: str = "000001",
    status: str = "APPROVED",
    effective_stop_loss: float | None = 8.8,
    audit: str = "risk-ok",
) -> ExecutionTicket:
    return ExecutionTicket(
        code=code,
        name="平安银行",
        action="HOLD",
        status=status,
        shares=1000,
        price_hint=9.5,
        amount=9500.0,
        stop_loss=8.9,
        max_loss=600.0,
        drawdown_ratio=0.06,
        reason="系统风控",
        tape_condition="放量站回VWAP",
        invalidate_condition="跌破VWAP",
        is_holding=True,
        atr14=0.2,
        original_stop_loss=8.7,
        effective_stop_loss=effective_stop_loss,
        slippage_bps=5.0,
        audit=audit,
    )


def test_step4_trade_context_uses_latest_market_trade_date(monkeypatch):
    monkeypatch.setattr(step4, "resolve_end_calendar_day", lambda: date(2026, 5, 17))
    runtime_config = step4.Step4RuntimeConfig()

    def fake_resolve_trading_window(end_calendar_day, trading_days):
        assert end_calendar_day == date(2026, 5, 17)
        assert trading_days == runtime_config.trading_days
        return SimpleNamespace(end_trade_date=date(2026, 5, 15))

    monkeypatch.setattr(step4, "resolve_trading_window", fake_resolve_trading_window)

    end_day, window, trade_date = step4._resolve_step4_trade_context(runtime_config)

    assert end_day == date(2026, 5, 17)
    assert window.end_trade_date == date(2026, 5, 15)
    assert trade_date == "2026-05-15"


def test_existing_position_probe_is_treated_as_add_on_and_requires_profit():
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001",
                name="平安银行",
                cost=10.0,
                buy_dt="2026-05-10",
                shares=1000,
                stop_loss=8.8,
            )
        },
        latest_price_map={"000001": 9.5},
        atr_map={"000001": 0.2},
        market_regime="NEUTRAL",
    )

    tickets, _cash = engine.process([_decision("PROBE", is_add_on=False)])

    assert tickets[0].action == "HOLD"
    assert tickets[0].status == "APPROVED"
    assert "当前未浮盈" in tickets[0].reason


def _hold_decision(*, stop_loss: float | None = 8.9) -> DecisionItem:
    return DecisionItem(
        code="000001",
        name="平安银行",
        action="HOLD",
        entry_zone_min=None,
        entry_zone_max=None,
        stop_loss=stop_loss,
        trim_ratio=None,
        tape_condition="放量站回VWAP",
        invalidate_condition="跌破VWAP",
        is_add_on=False,
        reason="模型建议继续持有",
        confidence=0.6,
    )


def test_hold_is_forced_to_exit_when_price_breaches_stop_loss():
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001",
                name="平安银行",
                cost=10.0,
                buy_dt="2026-05-10",
                shares=1000,
                stop_loss=8.9,
            )
        },
        latest_price_map={"000001": 8.8},
        market_regime="NEUTRAL",
    )

    tickets, cash = engine.process([_hold_decision(stop_loss=8.9)])

    ticket = tickets[0]
    assert ticket.action == "EXIT"
    assert ticket.status == "APPROVED"
    assert ticket.shares == 1000
    assert "system_stop_breach_override" in ticket.audit
    assert "forced_exit_stop_breach" in ticket.audit
    assert "系统强制止损" in ticket.reason
    assert cash > 50000


def test_hold_is_kept_when_price_has_not_breached_stop_loss():
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001",
                name="平安银行",
                cost=10.0,
                buy_dt="2026-05-10",
                shares=1000,
                stop_loss=8.9,
            )
        },
        latest_price_map={"000001": 9.5},
        market_regime="NEUTRAL",
    )

    tickets, cash = engine.process([_hold_decision(stop_loss=8.9)])

    ticket = tickets[0]
    assert ticket.action == "HOLD"
    assert ticket.status == "APPROVED"
    assert ticket.shares == 0
    assert "system_stop_breach_override" not in ticket.audit
    assert cash == 50000


def _exit_decision(action: str = "EXIT", *, trim_ratio: float | None = None) -> DecisionItem:
    return DecisionItem(
        code="000001",
        name="平安银行",
        action=action,
        entry_zone_min=None,
        entry_zone_max=None,
        stop_loss=8.9,
        trim_ratio=trim_ratio,
        tape_condition="",
        invalidate_condition="",
        is_add_on=False,
        reason="模型建议离场",
        confidence=0.7,
    )


def _t1_engine(*, buy_dt: str, trade_date: str, price: float = 9.5) -> WyckoffOrderEngine:
    return WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(code="000001", name="平安银行", cost=10.0, buy_dt=buy_dt, shares=1000, stop_loss=8.9)
        },
        latest_price_map={"000001": price},
        market_regime="NEUTRAL",
        trade_date=trade_date,
    )


def test_duplicate_exit_decisions_do_not_oversell_or_double_count_cash() -> None:
    engine = _t1_engine(buy_dt="2026-05-14", trade_date="2026-05-15", price=9.5)

    tickets, cash = engine.process([_exit_decision(), _exit_decision()])

    approved = [ticket for ticket in tickets if ticket.status == "APPROVED" and ticket.action == "EXIT"]
    rejected = [ticket for ticket in tickets if ticket.status != "APPROVED"]
    assert len(approved) == 1
    assert approved[0].shares == 1000
    assert len(rejected) == 1
    assert "无可卖持仓" in rejected[0].reason
    assert cash == 50000 + 1000 * 9.5 * (1.0 - WyckoffOrderEngine.SLIPPAGE_BPS)


def test_exit_is_rejected_for_shares_bought_today() -> None:
    engine = _t1_engine(buy_dt="2026-05-15", trade_date="2026-05-15")

    tickets, cash = engine.process([_exit_decision()])

    assert tickets[0].status == "NO_TRADE"
    assert "T+1限制" in tickets[0].reason
    assert cash == 50000


def test_trim_is_rejected_for_shares_bought_today_in_compact_date_format() -> None:
    engine = _t1_engine(buy_dt="20260515", trade_date="2026-05-15")

    tickets, cash = engine.process([_exit_decision("TRIM", trim_ratio=0.5)])

    assert tickets[0].status == "NO_TRADE"
    assert "T+1限制" in tickets[0].reason
    assert cash == 50000


def test_exit_is_rejected_for_compact_buy_dt_with_time_suffix() -> None:
    engine = _t1_engine(buy_dt="20260515 09:30:00", trade_date="2026-05-15")

    tickets, cash = engine.process([_exit_decision()])

    assert tickets[0].status == "NO_TRADE"
    assert "T+1限制" in tickets[0].reason
    assert cash == 50000


def test_rejected_t1_exit_does_not_persist_model_stop_loss(monkeypatch) -> None:
    captured: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: captured.append((portfolio_id, updates)) or True,
    )
    engine = _t1_engine(buy_dt="2026-05-15", trade_date="2026-05-15")
    decision = _exit_decision()
    decision.stop_loss = 7.5

    tickets, _cash = engine.process([decision])
    ok = step4_results.update_step4_position_stops("P1", tickets)

    assert tickets[0].status == "NO_TRADE"
    assert ok is True
    assert captured == []


def test_approved_exit_does_not_persist_stop_loss(monkeypatch) -> None:
    captured: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: captured.append((portfolio_id, updates)) or True,
    )
    engine = _t1_engine(buy_dt="2026-05-14", trade_date="2026-05-15", price=8.8)

    tickets, _cash = engine.process([_exit_decision()])
    ok = step4_results.update_step4_position_stops("P1", tickets)

    assert tickets[0].status == "APPROVED"
    assert ok is True
    assert captured == []


def test_only_actionable_hold_stop_is_persisted(monkeypatch) -> None:
    captured: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: captured.append((portfolio_id, updates)) or True,
    )
    ticket = _ticket(effective_stop_loss=8.8)

    ok = step4_results.update_step4_position_stops("P1", [ticket])

    assert ok is True
    assert captured == [("P1", [{"code": "000001", "stop_loss": 8.8}])]


def test_exit_is_allowed_for_shares_bought_before_today() -> None:
    engine = _t1_engine(buy_dt="2026-05-14", trade_date="2026-05-15")

    tickets, cash = engine.process([_exit_decision()])

    assert tickets[0].status == "APPROVED"
    assert tickets[0].shares == 1000
    assert cash > 50000


def test_recent_inverted_stop_exit_is_downgraded_to_hold() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001", name="平安银行", cost=50.0, buy_dt="2026-05-14", shares=1000, stop_loss=None
            )
        },
        latest_price_map={"000001": 50.3},
        trade_date="2026-05-15",
    )
    decision = _exit_decision()
    decision.stop_loss = 112.0

    tickets, cash = engine.process([decision])

    assert tickets[0].action == "HOLD"
    assert tickets[0].effective_stop_loss is None
    assert "新仓止损倒挂" in tickets[0].reason
    assert "reject_inverted_recent_decision_stop" in tickets[0].audit
    assert cash == 50000


def test_recent_persisted_trailing_stop_is_authoritative_after_pullback() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001", name="平安银行", cost=50.0, buy_dt="2026-05-14", shares=1000, stop_loss=52.0
            )
        },
        latest_price_map={"000001": 51.0},
        trade_date="2026-05-15",
    )
    decision = _hold_decision(stop_loss=None)

    tickets, cash = engine.process([decision])

    assert tickets[0].action == "EXIT"
    assert tickets[0].status == "APPROVED"
    assert tickets[0].effective_stop_loss == 52.0
    assert "forced_exit_stop_breach" in tickets[0].audit
    assert cash > 50000


def test_recent_valid_breached_stop_still_allows_exit() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001", name="平安银行", cost=50.0, buy_dt="2026-05-14", shares=1000, stop_loss=46.7
            )
        },
        latest_price_map={"000001": 45.0},
        trade_date="2026-05-15",
    )
    decision = _exit_decision()
    decision.stop_loss = 46.7

    tickets, cash = engine.process([decision])

    assert tickets[0].action == "EXIT"
    assert tickets[0].status == "APPROVED"
    assert cash > 50000


def test_old_profitable_trailing_stop_still_allows_exit() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(
                code="000001", name="平安银行", cost=50.0, buy_dt="2026-04-01", shares=1000, stop_loss=90.0
            )
        },
        latest_price_map={"000001": 80.0},
        trade_date="2026-05-15",
    )
    decision = _exit_decision()
    decision.stop_loss = 90.0

    tickets, _cash = engine.process([decision])

    assert tickets[0].action == "EXIT"
    assert tickets[0].status == "APPROVED"


def test_missing_buy_dt_inverted_stop_exit_is_downgraded_to_hold() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "000001": PositionItem(code="000001", name="平安银行", cost=50.0, buy_dt="", shares=1000, stop_loss=None)
        },
        latest_price_map={"000001": 50.3},
        trade_date="2026-05-15",
    )
    decision = _exit_decision()
    decision.stop_loss = 112.0

    tickets, cash = engine.process([decision])

    assert tickets[0].action == "HOLD"
    assert "新仓止损倒挂" in tickets[0].reason
    assert "reject_inverted_recent_decision_stop" in tickets[0].audit
    assert cash == 50000


def test_forced_stop_loss_exit_waits_for_t1_to_clear() -> None:
    engine = _t1_engine(buy_dt="2026-05-15", trade_date="2026-05-15", price=8.8)

    tickets, cash = engine.process([_hold_decision(stop_loss=8.9)])

    assert tickets[0].action == "HOLD"
    assert tickets[0].shares == 0
    assert "T+1" in tickets[0].reason
    assert "stop_breach_blocked_by_t1" in tickets[0].audit
    assert cash == 50000


def test_order_engine_uses_explicit_buy_block_config():
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 9.5},
        atr_map={"000001": 0.2},
        market_regime="NEUTRAL",
        config=step4.Step4OrderConfig(buy_block_regimes=frozenset({"NEUTRAL"})),
    )

    tickets, cash = engine.process([_decision("PROBE")])

    assert cash == 50000
    assert tickets[0].status == "NO_TRADE"
    assert "regime=NEUTRAL" in tickets[0].reason


def _stale_exit_engine(**overrides):
    kwargs = {
        "total_equity": 100000,
        "free_cash": 50000,
        "position_map": {},
        "latest_price_map": {"000001": 9.5},
        "atr_map": {"000001": 0.2},
        "market_regime": "NEUTRAL",
        "stale_exit_codes": frozenset({"603661"}),
    }
    kwargs.update(overrides)
    return WyckoffOrderEngine(**kwargs)


def test_unexecuted_exit_blocks_attack_sized_buys():
    """止损没落地还上重仓，等于一边放任亏损扩大一边加码。"""
    tickets, cash = _stale_exit_engine().process([_decision("ATTACK")])

    assert cash == 50000
    assert tickets[0].status == "NO_TRADE"
    assert "禁止 ATTACK 重仓" in tickets[0].reason
    assert "603661" in tickets[0].reason


def test_unexecuted_exit_still_allows_small_probe():
    """PROBE 自带硬止损且额度受限，一刀切会让闸门变成永久停摆。"""
    tickets, cash = _stale_exit_engine().process([_decision("PROBE")])

    assert tickets[0].status == "APPROVED"
    assert cash < 50000


def test_unexecuted_exit_does_not_block_selling_or_holding():
    """闸门只拦重仓买入；离场和持有必须照常给出，否则会把仓位锁死。"""
    position = PositionItem(code="000001", name="平安银行", cost=10.0, buy_dt="20260701", shares=1000, stop_loss=8.9)
    engine = _stale_exit_engine(position_map={"000001": position})

    tickets, _cash = engine.process([_decision("EXIT")])

    assert tickets[0].status == "APPROVED"
    assert tickets[0].action == "EXIT"


def test_stale_exit_buy_block_can_be_switched_off():
    engine = _stale_exit_engine(config=step4.Step4OrderConfig(block_buy_on_stale_exit=False))

    tickets, _cash = engine.process([_decision("ATTACK")])

    assert tickets[0].status == "APPROVED"


def test_no_stale_exits_leaves_buys_alone():
    engine = _stale_exit_engine(stale_exit_codes=frozenset())

    tickets, _cash = engine.process([_decision("ATTACK")])

    assert tickets[0].status == "APPROVED"


def test_candidate_attribution_reaches_buy_ticket_and_persistence_row():
    decision = DecisionItem(
        code="000390",
        name="晨光",
        action="PROBE",
        entry_zone_min=9.8,
        entry_zone_max=10.1,
        stop_loss=9.0,
        trim_ratio=None,
        tape_condition="放量高收",
        invalidate_condition="跌破9.0",
        is_add_on=False,
        reason="起跳板确认",
        confidence=0.8,
    )
    decisions = complete_step4_decisions(
        [decision],
        PortfolioState(free_cash=50000, total_equity=100000, positions=[]),
        {
            "000390": CandidateMeta(
                code="000390",
                name="晨光",
                tag="confirmed",
                track="Trend",
                stage="Markup",
                funnel_score=91,
                capital_migration_bonus=4.5,
                source_type="supabase_recommendation_tracking",
            )
        },
        "NEUTRAL",
        step4.Step4RuntimeConfig(),
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000390": 10.0},
        atr_map={"000390": 0.2},
        market_regime="NEUTRAL",
    )

    tickets, _cash = engine.process(decisions)
    report = render_trade_ticket("NEUTRAL", 100000, 50000, _cash, tickets, atr_period=14)
    rows = step4_results.build_step4_ticket_rows(tickets)

    assert tickets[0].status == "APPROVED"
    assert tickets[0].entry_zone_min == 9.8
    assert tickets[0].entry_zone_max == 10.1
    assert "score=91.00" in tickets[0].wyckoff_context
    assert "资金迁移=+4.50" in report
    assert "明日允许区间 9.80–10.10 元" in report
    assert "参考价" not in report
    assert "防追高限价" not in report
    assert "source=supabase_recommendation_tracking" in rows[0]["reason"]
    assert rows[0]["wyckoff_context"] == tickets[0].wyckoff_context


def test_buy_ticket_uses_single_effective_entry_zone() -> None:
    decision = _decision("PROBE")
    decision.entry_zone_min = 9.8
    decision.entry_zone_max = 10.5
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 10.0},
        market_regime="NEUTRAL",
        config=step4.Step4OrderConfig(
            chase_gap_pct_min=0.0,
            chase_gap_pct_max=1.0,
            max_gap_up_pct=1.0,
        ),
    )

    tickets, cash = engine.process([decision])
    report = render_trade_ticket("NEUTRAL", 100000, 50000, cash, tickets, atr_period=14)

    assert tickets[0].status == "APPROVED"
    assert tickets[0].entry_zone_min == 9.8
    assert tickets[0].entry_zone_max == 10.1
    assert report.count("明日允许区间 9.80–10.10 元") == 1
    assert "参考价" not in report
    assert "防追高限价" not in report


def test_buy_is_rejected_when_entry_zone_exceeds_chase_limit() -> None:
    decision = _decision("PROBE")
    decision.entry_zone_min = 10.2
    decision.entry_zone_max = 10.5
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 10.0},
        market_regime="NEUTRAL",
        config=step4.Step4OrderConfig(
            chase_gap_pct_min=0.0,
            chase_gap_pct_max=1.0,
            max_gap_up_pct=1.0,
        ),
    )

    tickets, cash = engine.process([decision])

    assert cash == 50000
    assert tickets[0].status == "NO_TRADE"
    assert tickets[0].reason.startswith("买入区间与防追高上限无交集")


def test_buy_without_ai_zone_is_rejected() -> None:
    decision = _decision("PROBE")
    decision.entry_zone_min = None
    decision.entry_zone_max = None
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 10.0},
        market_regime="NEUTRAL",
    )

    tickets, cash = engine.process([decision])
    assert cash == 50000
    assert tickets[0].status == "NO_TRADE"
    assert tickets[0].reason.startswith("缺少买入区间")


def test_add_on_without_ai_zone_is_downgraded_to_hold() -> None:
    decision = _decision("PROBE", is_add_on=True)
    decision.entry_zone_min = None
    decision.entry_zone_max = None
    position = PositionItem(code="000001", name="平安银行", cost=9.0, buy_dt="20260701", shares=1000)
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={"000001": position},
        latest_price_map={"000001": 10.0},
        market_regime="NEUTRAL",
    )

    tickets, cash = engine.process([decision])

    assert cash == 50000
    assert tickets[0].action == "HOLD"
    assert tickets[0].status == "APPROVED"
    assert tickets[0].reason.startswith("缺少买入区间，降级为 HOLD")


def test_veto_only_policy_downgrades_external_attack_to_probe():
    decision = _decision("ATTACK")

    decisions = complete_step4_decisions(
        [decision],
        PortfolioState(free_cash=50000, total_equity=100000, positions=[]),
        {},
        "NEUTRAL",
        step4.Step4RuntimeConfig(ai_candidate_policy="veto_only"),
    )

    assert decisions[0].action == "PROBE"
    assert "不得把外部新仓升级为ATTACK" in decisions[0].reason


def test_candidate_guard_blocks_unlabeled_policy_buy_before_market_fetch():
    decision = DecisionItem(
        code="300750",
        name="宁德时代",
        action="PROBE",
        entry_zone_min=200.0,
        entry_zone_max=205.0,
        stop_loss=190.0,
        trim_ratio=None,
        tape_condition="放量高收",
        invalidate_condition="跌破190",
        is_add_on=False,
        reason="模型建议试探",
        confidence=0.8,
    )
    decisions = complete_step4_decisions(
        [decision],
        PortfolioState(free_cash=50000, total_equity=100000, positions=[]),
        {
            "300750": CandidateMeta(
                code="300750",
                name="宁德时代",
                action_status="ready_for_ai_review",
                label_ready=False,
                risk_factors=("评估标签尚未成熟",),
                next_step="生成 AI 研报并结合持仓形成攻防决策",
            )
        },
        "RISK_ON",
        step4.Step4RuntimeConfig(),
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={},
        market_regime="RISK_ON",
    )

    tickets, cash = engine.process(decisions)

    assert cash == 50000
    assert decisions[0].system_reject_reason.startswith("候选护栏拦截: 候选标签未成熟")
    assert tickets[0].status == "NO_TRADE"
    assert "候选标签未成熟" in tickets[0].reason
    assert "评估标签尚未成熟" in tickets[0].reason


def test_candidate_guard_rejected_buy_does_not_consume_new_buy_slot():
    guarded = _decision("PROBE")
    guarded.code = "300750"
    guarded.name = "宁德时代"
    guarded.funnel_score = 99
    valid = _decision("PROBE")
    valid.code = "000390"
    valid.name = "晨光"
    valid.funnel_score = 70

    decisions = complete_step4_decisions(
        [guarded, valid],
        PortfolioState(free_cash=50000, total_equity=100000, positions=[]),
        {
            "300750": CandidateMeta(
                code="300750",
                name="宁德时代",
                action_status="ready_for_ai_review",
                label_ready=False,
                risk_factors=("评估标签尚未成熟",),
            )
        },
        "NEUTRAL",
        step4.Step4RuntimeConfig(new_buy_limits=NewBuyLimits(neutral=1)),
    )

    by_code = {item.code: item for item in decisions}
    assert by_code["300750"].system_reject_reason.startswith("候选护栏拦截")
    assert "组合级限购拦截" not in by_code["000390"].system_reject_reason


def test_candidate_guard_blocks_research_only_policy_buy():
    decision = DecisionItem(
        code="300750",
        name="宁德时代",
        action="PROBE",
        entry_zone_min=200.0,
        entry_zone_max=205.0,
        stop_loss=190.0,
        trim_ratio=None,
        tape_condition="放量高收",
        invalidate_condition="跌破190",
        is_add_on=False,
        reason="模型建议试探",
        confidence=0.8,
    )
    decisions = complete_step4_decisions(
        [decision],
        PortfolioState(free_cash=50000, total_equity=100000, positions=[]),
        {
            "300750": CandidateMeta(
                code="300750",
                name="宁德时代",
                action_status="ready_for_ai_review",
                trade_readiness="research_only",
                new_buy_allowed=False,
                label_ready=True,
                next_step="生成 AI 研报并结合持仓形成攻防决策",
            )
        },
        "RISK_ON",
        step4.Step4RuntimeConfig(),
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={},
        market_regime="RISK_ON",
    )

    tickets, cash = engine.process(decisions)

    assert cash == 50000
    assert decisions[0].system_reject_reason.startswith("候选护栏拦截: 候选未开放新增买入")
    assert "new_buy_allowed=false" in decisions[0].system_reject_reason
    assert tickets[0].status == "NO_TRADE"
    assert "候选未开放新增买入" in tickets[0].reason


def test_blocked_buy_ticket_renders_candidate_context():
    decision = DecisionItem(
        code="000390",
        name="晨光",
        action="PROBE",
        entry_zone_min=9.8,
        entry_zone_max=10.1,
        stop_loss=9.0,
        trim_ratio=None,
        tape_condition="放量高收",
        invalidate_condition="跌破9.0",
        is_add_on=False,
        reason="起跳板确认",
        confidence=0.8,
        funnel_score=91,
        capital_migration_bonus=-3.25,
        source_type="supabase_recommendation_tracking",
        wyckoff_track="Accum",
        wyckoff_stage="Accum_C",
        wyckoff_tag="confirmed",
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000390": 10.0},
        atr_map={"000390": 0.2},
        market_regime="RISK_OFF",
    )

    tickets, cash = engine.process([decision])
    report = render_trade_ticket("RISK_OFF", 100000, 50000, cash, tickets, atr_period=14)

    assert tickets[0].status == "NO_TRADE"
    assert "资金迁移=-3.25" in report
    assert "source=supabase_recommendation_tracking" in report


def test_step4_order_config_from_env_normalizes_values(monkeypatch):
    monkeypatch.setenv("STEP4_BUY_STOP_MODE", "bad")
    monkeypatch.setenv("STEP4_PROBE_BUDGET_LIMIT", "-1")
    monkeypatch.setenv("STEP4_ATTACK_BUDGET_LIMIT", "2")
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "CRASH,COOLDOWN,neutral")

    cfg = step4_order_config_from_env()

    assert cfg.buy_stop_mode == "floor"
    assert cfg.probe_budget_limit == 0.0
    assert cfg.attack_budget_limit == 1.0
    assert cfg.buy_block_regimes == EXECUTE_BLOCK_NEW_BUY_REGIMES | {"NEUTRAL"}


def test_missing_market_regime_blocks_new_order() -> None:
    decision = DecisionItem(
        code="000001",
        name="平安银行",
        action="PROBE",
        entry_zone_min=9.8,
        entry_zone_max=10.1,
        stop_loss=9.0,
        trim_ratio=None,
        tape_condition="放量高收",
        invalidate_condition="跌破9.0",
        is_add_on=False,
        reason="测试",
        confidence=0.8,
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 10.0},
        market_regime=None,
    )

    tickets, _ = engine.process([decision])

    assert tickets[0].status == "NO_TRADE"
    assert "regime=UNKNOWN" in tickets[0].reason


def test_step4_order_config_default_blocks_weak_market_regimes(monkeypatch):
    monkeypatch.delenv("STEP4_BUY_BLOCK_REGIMES", raising=False)

    cfg = step4_order_config_from_env()

    assert EXECUTE_BLOCK_NEW_BUY_REGIMES <= cfg.buy_block_regimes


def test_max_new_buy_names_blocks_bear_rebound() -> None:
    limits = NewBuyLimits(caution=3, neutral=1)

    assert max_new_buy_names("RISK_ON", limits) == 0
    assert max_new_buy_names("BEAR_REBOUND", limits) == 0
    assert max_new_buy_names("PANIC_REPAIR", limits) == 0
    assert max_new_buy_names("PANIC_REPAIR_CONFIRMED", limits) == 1
    assert max_new_buy_names("CAUTION", limits) == 1


def test_confirmed_repair_allows_small_probe_but_blocks_attack() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 9.5},
        atr_map={"000001": 0.2},
        market_regime="PANIC_REPAIR_CONFIRMED",
    )

    probe_tickets, _ = engine.process([_decision("PROBE")])
    attack_tickets, _ = engine.process([_decision("ATTACK")])

    assert probe_tickets[0].status == "APPROVED"
    assert probe_tickets[0].amount <= 5000
    assert attack_tickets[0].status == "NO_TRADE"
    assert "只允许小额 PROBE" in attack_tickets[0].reason


def test_caution_allows_probe_but_blocks_attack() -> None:
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000001": 9.5},
        atr_map={"000001": 0.2},
        market_regime="CAUTION",
    )

    probe_tickets, _ = engine.process([_decision("PROBE")])
    attack_tickets, _ = engine.process([_decision("ATTACK")])

    assert probe_tickets[0].status == "APPROVED"
    assert attack_tickets[0].status == "NO_TRADE"
    assert "只允许小额 PROBE" in attack_tickets[0].reason


def test_step4_new_buy_trim_prefers_evidence_score_over_model_confidence() -> None:
    low_score_high_conf = _decision("PROBE")
    low_score_high_conf.code = "000001"
    low_score_high_conf.confidence = 0.95
    low_score_high_conf.funnel_score = 70
    high_score_low_conf = _decision("PROBE")
    high_score_low_conf.code = "000002"
    high_score_low_conf.confidence = 0.55
    high_score_low_conf.funnel_score = 90

    kept, dropped, max_new_names = trim_new_buy_decisions(
        [low_score_high_conf, high_score_low_conf],
        held_codes=set(),
        market_regime="NEUTRAL",
        limits=NewBuyLimits(neutral=1),
    )

    assert max_new_names == 1
    assert [item.code for item in kept] == ["000002"]
    assert dropped == ["000001"]


def test_step4_new_buy_trim_uses_capital_migration_as_score_tiebreaker() -> None:
    outflow = _decision("PROBE")
    outflow.code = "000001"
    outflow.funnel_score = 90
    outflow.capital_migration_bonus = -3.0
    inflow = _decision("PROBE")
    inflow.code = "000002"
    inflow.funnel_score = 90
    inflow.capital_migration_bonus = 4.5

    kept, dropped, _max_new_names = trim_new_buy_decisions(
        [outflow, inflow],
        held_codes=set(),
        market_regime="NEUTRAL",
        limits=NewBuyLimits(neutral=1),
    )

    assert [item.code for item in kept] == ["000002"]
    assert dropped == ["000001"]


def test_step4_new_buy_cap_rejected_candidate_becomes_no_trade_ticket() -> None:
    weak = _decision("PROBE")
    weak.code = "000001"
    weak.name = "弱候选"
    weak.funnel_score = 70
    weak.source_type = "supabase_recommendation_tracking"
    weak.wyckoff_track = "Accum"
    weak.wyckoff_stage = "Accum_C"
    strong = _decision("PROBE")
    strong.code = "000002"
    strong.name = "强候选"
    strong.funnel_score = 90
    strong.source_type = "supabase_recommendation_tracking"
    strong.wyckoff_track = "Trend"
    strong.wyckoff_stage = "Markup"

    decisions = complete_step4_decisions(
        [weak, strong],
        PortfolioState(free_cash=50000, total_equity=100000, positions=[]),
        {},
        "NEUTRAL",
        step4.Step4RuntimeConfig(new_buy_limits=NewBuyLimits(neutral=1)),
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000002": 9.5},
        atr_map={"000002": 0.2},
        market_regime="NEUTRAL",
    )

    tickets, cash = engine.process(decisions)
    report = render_trade_ticket("NEUTRAL", 100000, 50000, cash, tickets, atr_period=14)
    rows = step4_results.build_step4_ticket_rows(tickets)
    by_code = {ticket.code: ticket for ticket in tickets}

    assert by_code["000002"].status == "APPROVED"
    assert by_code["000001"].status == "NO_TRADE"
    assert "组合级限购拦截" in by_code["000001"].reason
    assert "max_new_buy_names=1" in by_code["000001"].reason
    assert "score=70.00" in by_code["000001"].wyckoff_context
    assert "组合级限购拦截" in report
    rejected_row = next(row for row in rows if row["code"] == "000001")
    assert rejected_row["status"] == "NO_TRADE"
    assert "audit=reject:组合级限购拦截" in rejected_row["reason"]


def test_step4_market_backfill_skips_system_rejected_decisions(monkeypatch) -> None:
    rejected = _decision("PROBE")
    rejected.code = "000001"
    rejected.system_reject_reason = "组合级限购拦截"
    active = _decision("PROBE")
    active.code = "000002"
    fetched: list[str] = []

    def fake_fetch(code, _window, _runtime_config):
        fetched.append(code)
        return code, 0.2, 9.5

    monkeypatch.setattr("workflows.step4_decisions._fetch_step4_decision_market_data", fake_fetch)
    latest_price_map: dict[str, float] = {}
    atr_map: dict[str, float] = {}

    backfill_step4_decision_market_data(
        [rejected, active],
        SimpleNamespace(),
        latest_price_map,
        atr_map,
        step4.Step4RuntimeConfig(max_workers=1),
    )

    assert fetched == ["000002"]
    assert latest_price_map == {"000002": 9.5}
    assert atr_map == {"000002": 0.2}


def test_step4_runtime_config_from_env_normalizes_values(monkeypatch):
    monkeypatch.setenv("STEP4_TRADING_DAYS", "0")
    monkeypatch.setenv("STEP4_MAX_OUTPUT_TOKENS", "bad")
    monkeypatch.setenv("STEP4_MAX_WORKERS", "-2")
    monkeypatch.setenv("STEP4_MAX_EXTERNAL_REPORT_CANDIDATES", "0")
    monkeypatch.setenv("STEP4_MAX_NEW_BUYS_CAUTION", "3")
    monkeypatch.setenv("STEP4_ENFORCE_TARGET_TRADE_DATE", "yes")
    monkeypatch.setenv("STEP4_AI_CANDIDATE_POLICY", "invalid")

    cfg = step4_runtime_config_from_env()

    assert cfg.trading_days == 1
    assert cfg.max_output_tokens == 8192
    assert cfg.max_workers == 1
    assert cfg.max_external_report_candidates == 0
    assert cfg.new_buy_limits.caution == 1
    assert cfg.enforce_target_trade_date is True
    assert cfg.ai_candidate_policy == "veto_only"


def test_step4_portfolio_loads_env_state_and_skips_invalid_positions(monkeypatch):
    monkeypatch.setenv(
        "MY_PORTFOLIO_STATE",
        """
        {
          "free_cash": 12345.6,
          "total_equity": 20000,
          "positions": [
            {"code": "000001", "name": "平安银行", "cost": 10, "shares": 1000, "buy_dt": "2026-05-10"},
            {"code": "bad", "shares": 1},
            "not-object"
          ]
        }
        """,
    )

    portfolio = step4_portfolio.load_portfolio_from_env()

    assert portfolio.free_cash == 12345.6
    assert portfolio.total_equity == 20000
    assert [p.code for p in portfolio.positions] == ["000001"]
    assert step4_portfolio.portfolio_state_signature(portfolio)


def test_send_trade_ticket_fails_when_telegram_fails(monkeypatch):
    captured: dict[str, bool | str] = {}
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(
        step4,
        "send_to_telegram",
        lambda message, **_kwargs: captured.update({"telegram": True, "message": message}) and False,
    )

    assert step4._send_trade_ticket("# ticket", "token", "chat") is False
    assert captured == {"telegram": True, "message": "# ticket"}


def test_send_trade_ticket_uses_only_telegram_even_when_feishu_env_exists(monkeypatch):
    captured: dict[str, bool | str] = {}
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(
        step4,
        "send_to_telegram",
        lambda message, **_kwargs: captured.update({"telegram": True, "message": message}) or True,
    )

    assert step4._send_trade_ticket("# ticket", "token", "chat") is True
    assert captured == {"telegram": True, "message": "# ticket"}


def test_send_trade_ticket_requires_telegram(monkeypatch):
    captured: dict[str, bool] = {}
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(step4, "send_to_telegram", lambda *_args, **_kwargs: captured.update({"telegram": True}))

    assert step4._send_trade_ticket("# ticket", "", "") is False
    assert captured == {}


def test_step4_result_record_defers_stop_updates_until_orders_are_saved(monkeypatch):
    calls: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: calls.append((portfolio_id, updates)) or True,
    )

    record = step4_results.prepare_step4_result_record(
        tickets=[_ticket()],
        state_signature="ABC123",
    )

    assert "_sigabc123" in record.run_id
    assert calls == []
    assert record.ticket_rows[0]["reason"] == "系统风控 | audit=risk-ok"


def _persist_portfolio(*, stop_loss: float | None = 8.1) -> SimpleNamespace:
    return SimpleNamespace(
        free_cash=50000.0,
        positions=[SimpleNamespace(code="000001", stop_loss=stop_loss)],
    )


def test_step4_save_orders_and_nav_uses_persistence_boundaries(monkeypatch):
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        step4_results,
        "save_ai_trade_orders",
        lambda **kwargs: calls.setdefault("orders", kwargs) is not None,
    )
    monkeypatch.setattr(
        step4_results,
        "cancel_trade_orders",
        lambda **kwargs: calls.setdefault("cancel", kwargs) or 1,
    )
    monkeypatch.setattr(
        step4_results,
        "upsert_daily_nav",
        lambda **kwargs: calls.setdefault("nav", kwargs) is not None,
    )
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: calls.setdefault("stops", (portfolio_id, updates)) is not None,
    )
    options = SimpleNamespace(portfolio_id="P1", model="model-x")
    # 账户真实现金 50000、持仓 70000；OMS 若把清仓建议算进去会得到 120000 的模拟现金。
    context = SimpleNamespace(
        trade_date="2026-05-15",
        total_equity=120000.0,
        portfolio=_persist_portfolio(stop_loss=8.1),
    )

    result = step4_results.save_step4_orders_and_nav(
        options=options,
        context=context,
        run_id="run-1",
        rendered_market_view="市场视图",
        tickets=[_ticket()],
        ticket_rows=[{"code": "000001"}],
    )

    assert result == step4_results.Step4PersistenceResult(True, orders_written=True)
    assert calls["orders"] == {
        "run_id": "run-1",
        "portfolio_id": "P1",
        "model": "model-x",
        "trade_date": "2026-05-15",
        "market_view": "市场视图",
        "orders": [{"code": "000001"}],
    }
    assert calls["cancel"] == {
        "portfolio_id": "P1",
        "trade_date": "2026-05-15",
        "exclude_run_id": "run-1",
        "raise_on_error": True,
    }
    assert calls["nav"] == {
        "portfolio_id": "P1",
        "trade_date": "2026-05-15",
        "free_cash": 50000.0,
        "total_equity": 120000.0,
        "positions_value": 70000.0,
    }


def test_step4_order_write_failure_does_not_mutate_stops_or_nav(monkeypatch):
    mutated: list[str] = []
    monkeypatch.setattr(step4_results, "save_ai_trade_orders", lambda **_kwargs: False)
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda *_args, **_kwargs: mutated.append("stops") or True,
    )
    monkeypatch.setattr(
        step4_results,
        "upsert_daily_nav",
        lambda **_kwargs: mutated.append("nav") or True,
    )

    result = step4_results.save_step4_orders_and_nav(
        options=SimpleNamespace(portfolio_id="P1", model="model-x"),
        context=SimpleNamespace(
            trade_date="2026-05-15",
            total_equity=120000.0,
            portfolio=_persist_portfolio(),
        ),
        run_id="run-1",
        rendered_market_view="市场视图",
        tickets=[_ticket()],
        ticket_rows=[{"code": "000001"}],
    )

    assert result == step4_results.Step4PersistenceResult(False, orders_written=False)
    assert mutated == []


def test_step4_auxiliary_failure_keeps_previous_orders_active(monkeypatch):
    cancelled: list[dict] = []
    monkeypatch.setattr(step4_results, "save_ai_trade_orders", lambda **_kwargs: True)
    monkeypatch.setattr(step4_results, "update_position_stops", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(step4_results, "upsert_daily_nav", lambda **_kwargs: True)
    monkeypatch.setattr(
        step4_results,
        "cancel_trade_orders",
        lambda **kwargs: cancelled.append(kwargs) or 1,
    )

    result = step4_results.save_step4_orders_and_nav(
        options=SimpleNamespace(portfolio_id="P1", model="model-x"),
        context=SimpleNamespace(
            trade_date="2026-05-15",
            total_equity=120000.0,
            portfolio=_persist_portfolio(stop_loss=8.1),
        ),
        run_id="run-1",
        rendered_market_view="市场视图",
        tickets=[_ticket()],
        ticket_rows=[{"code": "000001"}],
    )

    assert result == step4_results.Step4PersistenceResult(
        False,
        orders_written=True,
        stop_rollback=({"code": "000001", "stop_loss": 8.1},),
    )
    assert cancelled == []


def test_step4_previous_order_cancel_failure_requires_new_order_rollback(monkeypatch):
    monkeypatch.setattr(step4_results, "save_ai_trade_orders", lambda **_kwargs: True)
    monkeypatch.setattr(step4_results, "update_position_stops", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(step4_results, "upsert_daily_nav", lambda **_kwargs: True)

    def fail_cancel(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(step4_results, "cancel_trade_orders", fail_cancel)

    result = step4_results.save_step4_orders_and_nav(
        options=SimpleNamespace(portfolio_id="P1", model="model-x"),
        context=SimpleNamespace(
            trade_date="2026-05-15",
            total_equity=120000.0,
            portfolio=_persist_portfolio(stop_loss=8.1),
        ),
        run_id="run-1",
        rendered_market_view="市场视图",
        tickets=[_ticket()],
        ticket_rows=[{"code": "000001"}],
    )

    assert result == step4_results.Step4PersistenceResult(
        False,
        orders_written=True,
        stop_rollback=({"code": "000001", "stop_loss": 8.1},),
    )


def test_step4_nav_failure_after_stop_update_carries_stop_rollback(monkeypatch):
    monkeypatch.setattr(step4_results, "save_ai_trade_orders", lambda **_kwargs: True)
    monkeypatch.setattr(step4_results, "update_position_stops", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(step4_results, "upsert_daily_nav", lambda **_kwargs: False)
    monkeypatch.setattr(step4_results, "cancel_trade_orders", lambda **_kwargs: 0)

    result = step4_results.save_step4_orders_and_nav(
        options=SimpleNamespace(portfolio_id="P1", model="model-x"),
        context=SimpleNamespace(
            trade_date="2026-05-15",
            total_equity=120000.0,
            portfolio=_persist_portfolio(stop_loss=7.5),
        ),
        run_id="run-1",
        rendered_market_view="市场视图",
        tickets=[_ticket(effective_stop_loss=9.2)],
        ticket_rows=[{"code": "000001"}],
    )

    assert result == step4_results.Step4PersistenceResult(
        False,
        orders_written=True,
        stop_rollback=({"code": "000001", "stop_loss": 7.5},),
    )


def test_nav_snapshot_records_real_cash_not_the_post_execution_projection(monkeypatch):
    """工单没被执行时，净值快照不能把「假设已清仓」的现金当成账户现金。"""
    captured: dict[str, object] = {}
    monkeypatch.setattr(step4_results, "save_ai_trade_orders", lambda **_kwargs: True)
    monkeypatch.setattr(step4_results, "cancel_trade_orders", lambda **_kwargs: 0)
    monkeypatch.setattr(step4_results, "update_position_stops", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(step4_results, "upsert_daily_nav", lambda **kwargs: captured.update(kwargs) is None)

    step4_results.save_step4_orders_and_nav(
        options=SimpleNamespace(portfolio_id="P1", model="model-x"),
        context=SimpleNamespace(
            trade_date="2026-05-15",
            total_equity=87000.0,
            portfolio=SimpleNamespace(free_cash=71000.0, positions=[]),
        ),
        run_id="run-1",
        rendered_market_view="市场视图",
        tickets=[_ticket()],
        ticket_rows=[{"code": "000001"}],
    )

    assert captured["free_cash"] == 71000.0
    assert captured["positions_value"] == 16000.0


def _persist_context() -> SimpleNamespace:
    return SimpleNamespace(
        trade_date="2026-05-15",
        total_equity=120000.0,
        state_signature="abc123",
        portfolio=_persist_portfolio(stop_loss=8.1),
    )


def _persist_options() -> SimpleNamespace:
    return SimpleNamespace(
        portfolio_id="P1",
        model="model-x",
        tg_bot_token="token",
        tg_chat_id="chat",
        runtime_config=SimpleNamespace(atr_period=14),
    )


def _run_step4_result_flow(
    monkeypatch,
    persistence: step4_results.Step4PersistenceResult,
    *,
    notification_ok: bool = True,
    rollback_ok: bool = True,
) -> tuple[tuple[bool, str], dict[str, list]]:
    calls: dict[str, list] = {"notifications": [], "rollbacks": []}
    monkeypatch.setattr(
        step4,
        "prepare_step4_result_record",
        lambda **_kwargs: SimpleNamespace(run_id="run-new", ticket_rows=[{"code": "000001"}]),
    )
    monkeypatch.setattr(step4, "render_trade_ticket", lambda **_kwargs: "# ticket")
    monkeypatch.setattr(step4, "save_step4_orders_and_nav", lambda **_kwargs: persistence)
    monkeypatch.setattr(
        step4,
        "_send_trade_ticket",
        lambda *_args, **_kwargs: calls["notifications"].append(True) or notification_ok,
    )
    monkeypatch.setattr(
        step4,
        "rollback_step4_run",
        lambda **kwargs: calls["rollbacks"].append(kwargs) or rollback_ok,
    )
    result = step4._send_and_persist_step4_results(
        options=_persist_options(),
        context=_persist_context(),
        decisions=[],
        tickets=[_ticket()],
        free_cash_after=50000.0,
        rendered_market_view="市场视图",
        stale_exits=[],
        report_progress=lambda *_args, **_kwargs: None,
    )
    return result, calls


def test_notification_failure_preserves_written_orders(monkeypatch):
    """通知超时可能已经送达；保留工单才能阻止重跑 OMS 产生重复指令。"""
    result, calls = _run_step4_result_flow(
        monkeypatch,
        step4_results.Step4PersistenceResult(True, orders_written=True),
        notification_ok=False,
    )

    assert result == (False, "notification_failed_orders_preserved")
    assert calls == {"notifications": [True], "rollbacks": []}


def test_persistence_failure_rolls_back_written_orders(monkeypatch):
    stop_rollback = ({"code": "000001", "stop_loss": 8.1},)
    result, calls = _run_step4_result_flow(
        monkeypatch,
        step4_results.Step4PersistenceResult(False, orders_written=True, stop_rollback=stop_rollback),
    )

    assert result == (False, "persistence_failed")
    assert calls["notifications"] == []
    assert calls["rollbacks"] == [
        {
            "portfolio_id": "P1",
            "trade_date": "2026-05-15",
            "run_id": "run-new",
            "stop_rollback": stop_rollback,
        }
    ]


def test_order_write_failure_does_not_attempt_rollback(monkeypatch):
    result, calls = _run_step4_result_flow(
        monkeypatch,
        step4_results.Step4PersistenceResult(False),
    )

    assert result == (False, "persistence_failed")
    assert calls == {"notifications": [], "rollbacks": []}


def test_persistence_failure_surfaces_rollback_failure(monkeypatch):
    result, _calls = _run_step4_result_flow(
        monkeypatch,
        step4_results.Step4PersistenceResult(False, orders_written=True),
        rollback_ok=False,
    )

    assert result == (False, "persistence_failed_rollback_failed")


def test_rollback_step4_run_cancels_only_current_run_id(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        step4_results,
        "cancel_trade_orders",
        lambda **kwargs: captured.update(kwargs) or 2,
    )
    monkeypatch.setattr(step4_results, "update_position_stops", lambda *_args, **_kwargs: True)

    rolled_back = step4_results.rollback_step4_run(
        portfolio_id="P1",
        trade_date="2026-05-15",
        run_id="run-new",
    )

    assert rolled_back is True
    assert captured == {
        "portfolio_id": "P1",
        "trade_date": "2026-05-15",
        "only_run_id": "run-new",
        "raise_on_error": True,
    }


def test_rollback_step4_run_restores_previous_stop_losses(monkeypatch):
    cancelled: list[dict] = []
    restored: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        step4_results,
        "cancel_trade_orders",
        lambda **kwargs: cancelled.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: restored.append((portfolio_id, updates)) or True,
    )

    rolled_back = step4_results.rollback_step4_run(
        portfolio_id="P1",
        trade_date="2026-05-15",
        run_id="run-new",
        stop_rollback=({"code": "000001", "stop_loss": 8.1}, {"code": "600519", "stop_loss": None}),
    )

    assert rolled_back is True
    assert cancelled == [
        {
            "portfolio_id": "P1",
            "trade_date": "2026-05-15",
            "only_run_id": "run-new",
            "raise_on_error": True,
        }
    ]
    assert restored == [
        (
            "P1",
            [{"code": "000001", "stop_loss": 8.1}, {"code": "600519", "stop_loss": None}],
        )
    ]


def test_rollback_step4_run_surfaces_stop_restore_failure(monkeypatch):
    monkeypatch.setattr(step4_results, "cancel_trade_orders", lambda **_kwargs: 1)
    monkeypatch.setattr(step4_results, "update_position_stops", lambda *_args, **_kwargs: False)

    assert (
        step4_results.rollback_step4_run(
            portfolio_id="P1",
            trade_date="2026-05-15",
            run_id="run-new",
            stop_rollback=({"code": "000001", "stop_loss": 8.1},),
        )
        is False
    )


def test_rollback_step4_run_surfaces_cancel_error(monkeypatch):
    def fail_cancel(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(step4_results, "cancel_trade_orders", fail_cancel)

    assert (
        step4_results.rollback_step4_run(
            portfolio_id="P1",
            trade_date="2026-05-15",
            run_id="run-new",
        )
        is False
    )


class TestStopLossPersistence:
    """买入建仓与存量持仓都必须按执行状态持久化有效止损。"""

    @staticmethod
    def _ticket(action: str, is_holding: bool, stop: float = 9.0, price: float = 10.0, status: str = "APPROVED"):
        from workflows.step4_models import ExecutionTicket

        return ExecutionTicket(
            code="000001",
            name="x",
            action=action,
            status=status,
            shares=100,
            price_hint=price,
            amount=1000.0,
            stop_loss=stop,
            max_loss=100.0,
            drawdown_ratio=0.01,
            reason="",
            tape_condition="",
            invalidate_condition="",
            is_holding=is_holding,
            atr14=0.5,
            original_stop_loss=stop,
            effective_stop_loss=stop,
            slippage_bps=0.001,
            audit="",
        )

    def test_executable_entries_and_held_positions_persist_stop(self):
        from workflows.step4_results import _should_persist_stop

        cases = (("PROBE", False), ("ATTACK", False), ("HOLD", True), ("TRIM", True))
        assert all(_should_persist_stop(self._ticket(action, held)) for action, held in cases)

    def test_non_executable_or_invalid_stops_are_rejected(self):
        from workflows.step4_results import _should_persist_stop

        tickets = (
            self._ticket("EXIT", is_holding=True),
            self._ticket("PROBE", is_holding=False, stop=11.0, price=10.0),
            self._ticket("PROBE", is_holding=False, status="NO_TRADE"),
        )
        assert not any(_should_persist_stop(ticket) for ticket in tickets)
