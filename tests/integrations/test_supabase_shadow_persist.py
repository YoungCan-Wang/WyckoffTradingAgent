"""影子账本落库：事件键用 action；计划状态先于账本写入。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from core.constants import TABLE_SHADOW_ACCOUNT, TABLE_SHADOW_TRADE_PLANS
from core.shadow_ledger import ShadowBook, ShadowPlan, plan_key
from integrations import supabase_shadow as ss


class _FakeTable:
    def __init__(self, name: str, log: list[tuple[str, str, Any]], *, existing: list[dict] | None = None):
        self.name = name
        self.log = log
        self.existing = list(existing or [])
        self._pending_delete_code: str | None = None

    def upsert(self, rows, on_conflict: str = ""):
        self.log.append((self.name, "upsert", rows))
        return self

    def select(self, *_a, **_k):
        self.log.append((self.name, "select", list(self.existing)))
        return self

    def delete(self):
        self.log.append((self.name, "delete", None))
        self._pending_delete_code = None
        return self

    def eq(self, column: str, value, **_k):
        if column == "code":
            self._pending_delete_code = str(value)
        return self

    def insert(self, rows):
        self.log.append((self.name, "insert", rows))
        return self

    def execute(self):
        if self._pending_delete_code is not None:
            self.existing = [row for row in self.existing if str(row.get("code")) != self._pending_delete_code]
            self._pending_delete_code = None
        return type("R", (), {"data": list(self.existing)})()


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
    from core.constants import TABLE_SHADOW_POSITIONS
    from core.shadow_ledger import ShadowPosition

    book = ShadowBook(
        cash=50_000.0,
        positions={"000001": ShadowPosition(code="000001", shares=100, sellable_shares=100, avg_cost=10.0)},
    )
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
    position_upsert = names_ops.index((TABLE_SHADOW_POSITIONS, "upsert"))
    assert plan_upsert < account_upsert < position_upsert
    assert (TABLE_SHADOW_POSITIONS, "insert") not in names_ops


def test_replace_positions_upserts_before_deleting_stale(monkeypatch):
    """upsert 失败时不得先删光；多余代码在 upsert 成功后再删。"""
    from core.constants import TABLE_SHADOW_POSITIONS
    from core.shadow_ledger import ShadowPosition

    log: list[tuple[str, str, Any]] = []
    tables = {
        TABLE_SHADOW_POSITIONS: _FakeTable(
            TABLE_SHADOW_POSITIONS,
            log,
            existing=[{"code": "000001"}, {"code": "600519"}],
        )
    }
    monkeypatch.setattr(ss, "_table", lambda name: tables[name])
    book = ShadowBook(
        cash=1.0,
        positions={"000001": ShadowPosition(code="000001", shares=100, sellable_shares=100, avg_cost=10.0)},
    )

    ss._replace_positions("USER_SHADOW:test", book)

    names_ops = [(n, o) for n, o, _ in log]
    assert names_ops[0] == (TABLE_SHADOW_POSITIONS, "upsert")
    assert names_ops[1] == (TABLE_SHADOW_POSITIONS, "select")
    assert (TABLE_SHADOW_POSITIONS, "delete") in names_ops
    assert names_ops.index((TABLE_SHADOW_POSITIONS, "upsert")) < names_ops.index((TABLE_SHADOW_POSITIONS, "delete"))
    assert [row["code"] for row in tables[TABLE_SHADOW_POSITIONS].existing] == ["000001"]


def test_money_or_default_keeps_zero_cash() -> None:
    """买满后 cash=0 必须原样读回，否则次日会话会凭空灌回 INITIAL_CAPITAL。"""
    assert ss._money_or_default(0, ss.INITIAL_CAPITAL) == 0.0
    assert ss._money_or_default(0.0, ss.INITIAL_CAPITAL) == 0.0
    assert ss._money_or_default(None, ss.INITIAL_CAPITAL) == ss.INITIAL_CAPITAL
    assert ss._money_or_default("", ss.INITIAL_CAPITAL) == ss.INITIAL_CAPITAL


def test_load_shadow_book_preserves_zero_cash(monkeypatch):
    """成交把现金耗尽后落库 cash=0；次日 load 不得再当成缺字段灌回 10 万。"""

    class _Result:
        def __init__(self, data):
            self.data = data

    class _Query:
        def __init__(self, data):
            self._data = data

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return _Result(self._data)

    account = {
        "account_id": "USER_SHADOW:test",
        "cash": 0.0,
        "initial_capital": 100_000.0,
    }
    monkeypatch.setattr(ss, "_configured", lambda: True)
    monkeypatch.setattr(ss, "seed_shadow_account", lambda _aid: None)
    monkeypatch.setattr(
        ss,
        "_table",
        lambda name: _Query([account] if name == TABLE_SHADOW_ACCOUNT else []),
    )

    book = ss.load_shadow_book("USER_SHADOW:test")
    assert book.cash == 0.0
    assert book.initial_capital == 100_000.0
    assert book.positions == {}
