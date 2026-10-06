"""Stop storage failures must propagate through Step4 persistence and rollback."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.constants import TABLE_PORTFOLIO_POSITIONS
from integrations import supabase_portfolio as portfolio_db
from integrations.fetch_a_share_csv import TradingWindow
from workflows import step4_rebalancer as step4
from workflows import step4_results
from workflows.step4_models import (
    DecisionItem,
    PortfolioState,
    PositionItem,
    Step4InputContext,
    Step4OrderConfig,
    Step4RunOptions,
    Step4RuntimeConfig,
)


class _StopClient:
    def __init__(self):
        self.stops = {"000001": 8.0, "600519": None}
        self.failed_reads = set()
        self.failed_writes = set()
        self.reads = 0
        self.writes = []
        self.snapshots = []

    def load(self, portfolio_id, *, client):
        assert portfolio_id == "P1" and client is self
        self.reads += 1
        if self.reads in self.failed_reads:
            return None
        return {"positions": [{"code": code, "stop_loss": stop} for code, stop in self.stops.items()]}

    def table(self, table_name):
        assert table_name == TABLE_PORTFOLIO_POSITIONS
        self.filters = {}
        return self

    def update(self, values):
        assert set(values) == {"stop_loss"}
        self.values = values
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        assert self.filters["portfolio_id"] == "P1"
        code = self.filters["code"]
        self.writes.append((code, self.values["stop_loss"]))
        if len(self.writes) in self.failed_writes:
            raise RuntimeError("stop write unavailable")
        self.stops[code] = self.values["stop_loss"]
        self.snapshots.append(dict(self.stops))
        return SimpleNamespace(data=[{"code": code, **self.values}])


def _context(stops):
    positions = [
        PositionItem(code=code, name=code, cost=10.0, buy_dt="2026-05-01", shares=100, stop_loss=stop)
        for code, stop in stops.items()
    ]
    return Step4InputContext(
        portfolio=PortfolioState(free_cash=10000.0, total_equity=12400.0, positions=positions),
        state_signature="abc123",
        window=TradingWindow(start_trade_date=date(2026, 5, 1), end_trade_date=date(2026, 5, 15)),
        trade_date="2026-05-15",
        total_equity=12400.0,
        latest_price_map=dict.fromkeys(stops, 12.0),
        atr_map=dict.fromkeys(stops, 0.5),
        allowed_codes=set(stops),
        candidate_meta_map={},
        name_map={},
        market_regime="NEUTRAL",
        system_market_view="neutral",
        user_message="",
    )


@pytest.fixture(params=[False, True], ids=["normal", "fallback"])
def persistence(monkeypatch, request):
    client = _StopClient()
    flow = SimpleNamespace(
        client=client,
        context=_context(client.stops),
        fallback=request.param,
        saved_orders=Mock(return_value=True),
        nav=Mock(return_value=True),
        cancelled=Mock(return_value=2),
        notification=Mock(return_value=True),
        progress=Mock(),
    )
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(portfolio_db, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio_db, "_get_supabase_admin_client", lambda: client)
    monkeypatch.setattr(portfolio_db, "load_portfolio_state", client.load)
    monkeypatch.setattr(step4_results, "save_ai_trade_orders", flow.saved_orders)
    monkeypatch.setattr(step4_results, "upsert_daily_nav", flow.nav)
    monkeypatch.setattr(step4_results, "cancel_trade_orders", flow.cancelled)
    monkeypatch.setattr(step4, "_send_trade_ticket", flow.notification)
    monkeypatch.setattr(step4, "_load_ticket_day_pnl", lambda *_args: None)
    return flow


def _run_persistence(flow):
    options = Step4RunOptions(
        provider="test",
        model="test-model",
        api_key="",
        llm_base_url="",
        portfolio_id="P1",
        tg_bot_token="token",
        tg_chat_id="chat",
        runtime_config=Step4RuntimeConfig(),
        order_config=Step4OrderConfig(),
    )
    if flow.fallback:
        return step4._run_stop_loss_only_fallback(options, flow.context, flow.progress, "llm_failed")
    decisions = [
        DecisionItem(
            code=position.code,
            name=position.name,
            action="HOLD",
            entry_zone_min=None,
            entry_zone_max=None,
            stop_loss=position.stop_loss,
            trim_ratio=None,
            tape_condition="",
            invalidate_condition="",
            is_add_on=False,
            reason="hold",
            confidence=None,
        )
        for position in flow.context.portfolio.positions
    ]
    tickets, free_cash_after = step4.execute_step4_decisions(flow.context, decisions, options.order_config)
    return step4._send_and_persist_step4_results(
        options=options,
        context=flow.context,
        decisions=decisions,
        tickets=tickets,
        free_cash_after=free_cash_after,
        rendered_market_view="neutral",
        stale_exits=[],
        report_progress=flow.progress,
    )


def _assert_failed_run(flow, result, status):
    expected_status = f"llm_failed_degraded_{status}" if flow.fallback else status
    assert result == (False, expected_status)
    flow.notification.assert_not_called()
    flow.saved_orders.assert_called_once()
    saved = flow.saved_orders.call_args.kwargs
    assert [order["action"] for order in saved["orders"]] == ["HOLD", "HOLD"]
    assert [order["stop_loss"] for order in saved["orders"]] == [11.0, 11.0]
    flow.cancelled.assert_called_once_with(
        portfolio_id="P1",
        trade_date="2026-05-15",
        only_run_id=saved["run_id"],
        raise_on_error=True,
    )
    assert all(call.args[0] != "决策完成" for call in flow.progress.call_args_list)


@pytest.mark.parametrize(
    ("failed_reads", "status"),
    [
        ({1}, "persistence_failed"),
        ({1, 2}, "persistence_failed_rollback_failed"),
    ],
    ids=["transient-read-failure", "persistent-read-failure"],
)
def test_stop_read_failure_propagates_through_persistence_and_rollback(persistence, failed_reads, status):
    persistence.client.failed_reads = failed_reads
    original_stops = dict(persistence.client.stops)

    result = _run_persistence(persistence)

    _assert_failed_run(persistence, result, status)
    assert persistence.client.reads == 2
    assert persistence.client.stops == original_stops
    expected_writes = list(original_stops.items()) if failed_reads == {1} else []
    assert persistence.client.writes == expected_writes


def test_partial_stop_update_is_restored_when_a_later_update_fails(persistence):
    persistence.client.failed_writes = {2}
    original_stops = dict(persistence.client.stops)

    result = _run_persistence(persistence)

    _assert_failed_run(persistence, result, "persistence_failed")
    assert persistence.client.snapshots[0] == {"000001": 11.0, "600519": None}
    assert persistence.client.stops == original_stops
    assert persistence.client.writes == [("000001", 11.0), ("600519", 11.0), *original_stops.items()]


def test_partial_stop_update_with_failed_restore_reports_rollback_failure(persistence, caplog):
    persistence.client.failed_writes = {2, 3}

    result = _run_persistence(persistence)

    _assert_failed_run(persistence, result, "persistence_failed_rollback_failed")
    assert persistence.client.stops == {"000001": 11.0, "600519": None}
    assert persistence.client.writes == [("000001", 11.0), ("600519", 11.0), ("000001", 8.0)]
    assert "Step4 止损回滚失败" in caplog.text
