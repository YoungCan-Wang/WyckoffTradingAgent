"""Stateful coverage for append-only protective application recommendations."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

import integrations.supabase_portfolio as portfolio_store
import workflows.step4_rebalancer as step4
import workflows.step4_results as step4_results
from integrations.fetch_a_share_csv import TradingWindow
from workflows.step4_models import (
    DecisionItem,
    PortfolioState,
    PositionItem,
    Step4DecisionResult,
    Step4InputContext,
    Step4OrderConfig,
    Step4RunOptions,
    Step4RuntimeConfig,
)


class _RecommendationQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.operation = "select"
        self.payload = None
        self.row_limit = None

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key) == value)
        return self

    def neq(self, key, value):
        self.filters.append(lambda row: row.get(key) != value)
        return self

    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values)
        return self

    def limit(self, count):
        self.row_limit = count
        return self

    def insert(self, payload):
        self.operation, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.operation, self.payload = "update", payload
        return self

    def execute(self):
        if self.operation == "insert":
            inserted = [{**row, "id": len(self.rows) + i + 1} for i, row in enumerate(self.payload)]
            self.rows.extend(deepcopy(inserted))
            return SimpleNamespace(data=inserted)
        matched = [row for row in self.rows if all(predicate(row) for predicate in self.filters)]
        matched = matched[: self.row_limit]
        if self.operation == "update":
            for row in matched:
                row.update(self.payload)
        return SimpleNamespace(data=deepcopy(matched))


def _prior_recommendations():
    prior_buy = {
        "id": 1,
        "portfolio_id": "P1",
        "trade_date": "2026-09-29",
        "run_id": "earlier-run",
        "code": "000001",
        "action": "ATTACK",
        "status": "APPROVED",
        "shares": 100,
        "reason": "Earlier same-day application recommendation",
    }
    return [
        prior_buy,
        {**prior_buy, "id": 2, "code": "000003", "action": "PROBE"},
        {**prior_buy, "id": 3, "code": "000002", "action": "HOLD"},
        {**prior_buy, "id": 4, "portfolio_id": "P2"},
        {**prior_buy, "id": 5, "trade_date": "2026-09-28"},
        {**prior_buy, "id": 6, "status": "CANCELLED"},
    ]


@pytest.fixture
def stored_recommendations(monkeypatch):
    rows = _prior_recommendations()
    stops, nav = [], []

    def table(name):
        assert name == portfolio_store.TABLE_TRADE_ORDERS
        return _RecommendationQuery(rows)

    def update_stops(portfolio_id, updates):
        stops.append((portfolio_id, deepcopy(updates)))
        return True

    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(portfolio_store, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio_store, "_get_supabase_admin_client", lambda: SimpleNamespace(table=table))
    monkeypatch.setattr(step4_results, "update_position_stops", update_stops)
    monkeypatch.setattr(step4_results, "upsert_daily_nav", lambda **kwargs: nav.append(kwargs) or True)
    monkeypatch.setattr(step4_results, "_build_step4_run_id", lambda _signature: "new-run")
    monkeypatch.setattr(step4, "_load_ticket_day_pnl", lambda *_args: None)
    monkeypatch.setattr(step4, "_send_trade_ticket", lambda *_args: True)
    monkeypatch.setattr(step4, "_audit_unexecuted_exits", lambda *_args: ([], frozenset()))
    return SimpleNamespace(rows=rows, stops=stops, nav=nav)


@pytest.fixture
def step4_inputs():
    options = Step4RunOptions(
        provider="test-provider",
        model="test-model",
        api_key="unused",
        llm_base_url="",
        portfolio_id="P1",
        tg_bot_token="unused",
        tg_chat_id="unused",
        runtime_config=Step4RuntimeConfig(),
        order_config=Step4OrderConfig(),
    )
    context = Step4InputContext(
        portfolio=PortfolioState(
            free_cash=50000.0,
            total_equity=70000.0,
            positions=[
                PositionItem("000001", "Stop breached", 10.0, "2026-09-01", 1000, stop_loss=9.0),
                PositionItem("000002", "Trailing protection", 10.0, "2026-09-01", 1000, stop_loss=9.0),
            ],
        ),
        state_signature="state",
        window=TradingWindow(date(2026, 9, 1), date(2026, 9, 29)),
        trade_date="2026-09-29",
        total_equity=70000.0,
        latest_price_map={"000001": 8.0, "000002": 12.0},
        atr_map={"000001": 0.5, "000002": 1.0},
        allowed_codes={"000001", "000002"},
        candidate_meta_map={},
        name_map={},
        market_regime="RISK_ON",
        system_market_view="Test market view",
        user_message="Test portfolio",
    )
    return options, context


def test_fallback_appends_protection_without_replacing_stored_same_day_recommendations(
    monkeypatch, stored_recommendations, step4_inputs
):
    store = stored_recommendations
    previous = deepcopy(store.rows)
    options, context = step4_inputs
    monkeypatch.setattr(step4, "call_step4_decision_model", lambda *_args: (False, "llm_failed", None))

    result = step4._run_step4_decision_flow(options=options, context=context, report_progress=lambda *_args: None)

    assert result == (False, "llm_failed_degraded")
    assert store.rows[: len(previous)] == previous
    appended = store.rows[len(previous) :]
    assert {(row["code"], row["action"], row["status"]) for row in appended} == {
        ("000001", "EXIT", "APPROVED"),
        ("000002", "HOLD", "APPROVED"),
    }
    assert len(appended) == 2
    assert all(row["run_id"] == "new-run" for row in appended)
    assert all(row["portfolio_id"] == "P1" and row["trade_date"] == context.trade_date for row in appended)
    active_same_symbol = [
        row
        for row in store.rows
        if row["portfolio_id"] == "P1"
        and row["trade_date"] == context.trade_date
        and row["code"] == "000001"
        and row["status"] == "APPROVED"
    ]
    # The append-only contract retains both recommendations; it does not resolve or execute them.
    assert [(row["run_id"], row["action"]) for row in active_same_symbol] == [
        ("earlier-run", "ATTACK"),
        ("new-run", "EXIT"),
    ]
    exit_row = next(row for row in appended if row["action"] == "EXIT")
    assert exit_row["shares"] == 1000
    assert "forced_exit_stop_breach" in exit_row["reason"]
    assert store.stops == [("P1", [{"code": "000002", "stop_loss": 10.0}])]
    assert store.nav == [
        {
            "portfolio_id": "P1",
            "trade_date": context.trade_date,
            "free_cash": 50000.0,
            "total_equity": 70000.0,
            "positions_value": 20000.0,
        }
    ]


def test_full_decision_run_still_supersedes_only_previous_same_day_recommendations(
    monkeypatch, stored_recommendations, step4_inputs
):
    store = stored_recommendations
    previous = deepcopy(store.rows)
    options, context = step4_inputs
    hold = DecisionItem(
        code="000001",
        name="Stop breached",
        action="HOLD",
        entry_zone_min=None,
        entry_zone_max=None,
        stop_loss=None,
        trim_ratio=None,
        tape_condition="",
        invalidate_condition="",
        is_add_on=False,
        reason="Model hold recommendation",
        confidence=0.8,
    )
    model_result = Step4DecisionResult(market_view="Full run", decisions=[hold])
    monkeypatch.setattr(step4, "call_step4_decision_model", lambda *_args: (True, "ok", model_result))

    result = step4._run_step4_decision_flow(options=options, context=context, report_progress=lambda *_args: None)

    assert result == (True, "ok")
    assert store.rows[:3] == [{**row, "status": "CANCELLED"} for row in previous[:3]]
    assert store.rows[3 : len(previous)] == previous[3:]
    current = store.rows[len(previous) :]
    assert len(current) == 2
    assert {(row["code"], row["action"]) for row in current} == {("000001", "EXIT"), ("000002", "HOLD")}
    assert all(row["run_id"] == "new-run" and row["status"] == "APPROVED" for row in current)
