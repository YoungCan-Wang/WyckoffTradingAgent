"""影子账本落库：事件键用 action；计划状态先于账本写入。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from core.constants import TABLE_SHADOW_ACCOUNT, TABLE_SHADOW_TRADE_PLANS
from core.shadow_ledger import ShadowBook, ShadowPlan, plan_key
from integrations import supabase_shadow as ss


class _FakeTable:
    def __init__(self, name: str, log: list[tuple[str, str, Any]]):
        self.name = name
        self.log = log

    def upsert(self, rows, on_conflict: str = ""):
        self.log.append((self.name, "upsert", rows))
        return self

    def delete(self):
        self.log.append((self.name, "delete", None))
        return self

    def eq(self, *_a, **_k):
        return self

    def insert(self, rows):
        self.log.append((self.name, "insert", rows))
        return self

    def execute(self):
        return type("R", (), {"data": []})()


@pytest.fixture
def persist_log(monkeypatch):
    log: list[tuple[str, str, Any]] = []
    monkeypatch.setattr(ss, "_configured", lambda: True)
    monkeypatch.setattr(ss, "seed_shadow_account", lambda _aid: None)
    monkeypatch.setattr(ss, "_table", lambda name: _FakeTable(name, log))
    return log


def _filled(action: str, code: str, qty: int) -> ShadowPlan:
    as_of = date(2026, 8, 20)
    return ShadowPlan(
        plan_key=plan_key("USER_SHADOW:test", as_of, action, code),
        code=code,
        name=code,
        action=action,
        signal_date=as_of,
        status="filled",
        fill_reason="next_open:10.0000",
        entry_date=date(2026, 8, 21),
        entry_price=10.0,
        qty=qty,
        fees={"fee": 1.0},
    )


def test_insert_events_uses_action_not_status(persist_log):
    buy = _filled("buy", "600519", 100)
    sell = _filled("sell", "600519", 100)
    ss._insert_events("USER_SHADOW:test", date(2026, 8, 21), [buy, sell])

    assert len(persist_log) == 1
    _table, op, rows = persist_log[0]
    assert op == "upsert"
    assert [r["event_type"] for r in rows] == ["buy", "sell"]
    assert rows[0]["event_key"] != rows[1]["event_key"]
    assert ":buy:" in rows[0]["event_key"] and ":filled" in rows[0]["event_key"]
    assert rows[0]["payload"]["status"] == "filled"


def test_persist_upserts_plans_before_account_and_positions(persist_log):
    """计划状态是成交幂等闸，必须先于 cash/positions 落库。"""
    book = ShadowBook(cash=50_000.0)
    fill = _filled("buy", "000001", 100)
    nav = {"cash": 50_000.0, "market_value": 0.0, "equity": 50_000.0, "pnl_total": 0.0, "pnl_day": 0.0}

    ss.persist_shadow_session(
        account_id="USER_SHADOW:test",
        as_of=date(2026, 8, 21),
        book=book,
        fills=[fill],
        new_plans=[],
        nav=nav,
    )

    names_ops = [(n, o) for n, o, _ in persist_log]
    plan_upsert = names_ops.index((TABLE_SHADOW_TRADE_PLANS, "upsert"))
    account_upsert = names_ops.index((TABLE_SHADOW_ACCOUNT, "upsert"))
    assert plan_upsert < account_upsert
