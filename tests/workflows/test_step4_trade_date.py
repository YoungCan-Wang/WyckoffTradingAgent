from datetime import date
from types import SimpleNamespace

import workflows.step4_results as step4_results
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
        "RISK_ON",
        step4.Step4RuntimeConfig(),
    )
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={},
        latest_price_map={"000390": 10.0},
        atr_map={"000390": 0.2},
        market_regime="RISK_ON",
    )

    tickets, _cash = engine.process(decisions)
    report = render_trade_ticket("RISK_ON", 100000, 50000, _cash, tickets, atr_period=14)
    rows = step4_results.build_step4_ticket_rows(tickets)

    assert tickets[0].status == "APPROVED"
    assert "score=91.00" in tickets[0].wyckoff_context
    assert "资金迁移=+4.50" in report
    assert "source=supabase_recommendation_tracking" in rows[0]["reason"]
    assert rows[0]["wyckoff_context"] == tickets[0].wyckoff_context


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
    assert cfg.buy_block_regimes == frozenset({"CRASH", "NEUTRAL"})


def test_step4_order_config_default_blocks_weak_market_regimes(monkeypatch):
    monkeypatch.delenv("STEP4_BUY_BLOCK_REGIMES", raising=False)

    cfg = step4_order_config_from_env()

    assert {"BEAR_REBOUND", "PANIC_REPAIR", "RISK_OFF", "CRASH", "BLACK_SWAN"} <= cfg.buy_block_regimes


def test_max_new_buy_names_blocks_bear_rebound() -> None:
    limits = NewBuyLimits(risk_on=2, caution=1, neutral=1, risk_off=0)

    assert max_new_buy_names("BEAR_REBOUND", limits) == 0
    assert max_new_buy_names("PANIC_REPAIR", limits) == 0


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
    monkeypatch.setenv("STEP4_MAX_NEW_BUYS_RISK_ON", "-1")
    monkeypatch.setenv("STEP4_MAX_NEW_BUYS_CAUTION", "3")
    monkeypatch.setenv("STEP4_ENFORCE_TARGET_TRADE_DATE", "yes")

    cfg = step4_runtime_config_from_env()

    assert cfg.trading_days == 1
    assert cfg.max_output_tokens == 8192
    assert cfg.max_workers == 1
    assert cfg.max_external_report_candidates == 0
    assert cfg.new_buy_limits.risk_on == 0
    assert cfg.new_buy_limits.caution == 3
    assert cfg.enforce_target_trade_date is True


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


def test_step4_result_record_updates_stops_and_builds_ticket_rows(monkeypatch):
    calls: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        step4_results,
        "update_position_stops",
        lambda portfolio_id, updates: calls.append((portfolio_id, updates)) or True,
    )

    record = step4_results.prepare_step4_result_record(
        portfolio_id="P1",
        tickets=[_ticket()],
        state_signature="ABC123",
    )

    assert "_sigabc123" in record.run_id
    assert calls == [("P1", [{"code": "000001", "stop_loss": 8.8}])]
    assert record.ticket_rows[0]["reason"] == "系统风控 | audit=risk-ok"


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
    options = SimpleNamespace(portfolio_id="P1", model="model-x")
    context = SimpleNamespace(trade_date="2026-05-15", total_equity=120000.0)

    step4_results.save_step4_orders_and_nav(
        options=options,
        context=context,
        run_id="run-1",
        rendered_market_view="市场视图",
        ticket_rows=[{"code": "000001"}],
        free_cash_after=50000.0,
    )

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
    }
    assert calls["nav"] == {
        "portfolio_id": "P1",
        "trade_date": "2026-05-15",
        "free_cash": 50000.0,
        "total_equity": 120000.0,
        "positions_value": 70000.0,
    }
