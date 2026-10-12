"""非会员写云端持仓被 RLS 拒绝后的回报。

RLS 对 insert 抛 42501；对 update/delete 不报错，只是命中 0 行，和「行不存在」同形，
update_free_cash 以前还会把它回报成「已更新」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.tool_context import _is_auth_error
from integrations import supabase_portfolio as sp

PORTFOLIO_ID = "USER_LIVE:u-1"
RLS_ERROR = 'new row violates row-level security policy for table "portfolio_positions"'
POSITION = {"code": "600000", "name": "浦发银行", "shares": 100, "cost_price": 10.0, "buy_dt": "20260101"}


class _Chain:
    def __init__(self, result) -> None:
        self.result = result

    def __getattr__(self, _name):
        return lambda *_a, **_k: self

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(data=self.result)


class _Client:
    """results: {(table, op): rows | Exception}；没列出的组合一律返回空行。"""

    def __init__(self, results: dict) -> None:
        self.results = results

    def table(self, name: str):
        return _Table(self.results, name)


class _Table:
    def __init__(self, results: dict, name: str) -> None:
        self.results, self.name = results, name

    def _op(self, op: str) -> _Chain:
        return _Chain(self.results.get((self.name, op), []))

    def select(self, *_a, **_k):
        return self._op("select")

    def update(self, *_a, **_k):
        return self._op("update")

    def delete(self, *_a, **_k):
        return self._op("delete")

    def insert(self, *_a, **_k):
        return self._op("insert")


def _client(overrides: dict | None = None) -> _Client:
    base = {
        ("portfolios", "select"): [{"portfolio_id": PORTFOLIO_ID}],
        ("portfolio_positions", "select"): [{"code": "600000", "shares": 100, "cost": 10.0}],
        ("planet_members", "select"): [],
    }
    base.update(overrides or {})
    return _Client(base)


def test_non_member_update_hitting_zero_rows_reports_membership_not_missing() -> None:
    client = _client({("portfolio_positions", "update"): []})

    ok, message = sp.update_position(PORTFOLIO_ID, POSITION, client=client, refresh_equity=False)

    assert (ok, message) == (False, sp.MEMBERSHIP_WRITE_DENIED_MSG)


def test_member_update_hitting_zero_rows_is_still_a_missing_row() -> None:
    """判别性用例：不能把所有 0 行都说成会员问题。"""
    client = _client({("portfolio_positions", "update"): [], ("planet_members", "select"): [{"expires_on": None}]})

    ok, message = sp.update_position(PORTFOLIO_ID, POSITION, client=client, refresh_equity=False)

    assert (ok, message) == (False, sp.POSITION_MISSING_ERROR)


def test_membership_lookup_failure_keeps_the_original_missing_message() -> None:
    client = _client({("portfolio_positions", "update"): [], ("planet_members", "select"): RuntimeError("down")})

    ok, message = sp.update_position(PORTFOLIO_ID, POSITION, client=client, refresh_equity=False)

    assert (ok, message) == (False, sp.POSITION_MISSING_ERROR)


def test_insert_rls_violation_is_translated() -> None:
    client = _client(
        {("portfolio_positions", "select"): [], ("portfolio_positions", "insert"): RuntimeError(RLS_ERROR)}
    )

    ok, message = sp.insert_position(PORTFOLIO_ID, POSITION, client=client, refresh_equity=False)

    assert (ok, message) == (False, sp.MEMBERSHIP_WRITE_DENIED_MSG)


def test_insert_other_errors_are_not_relabelled() -> None:
    client = _client({("portfolio_positions", "select"): [], ("portfolio_positions", "insert"): RuntimeError("boom")})

    ok, message = sp.insert_position(PORTFOLIO_ID, POSITION, client=client, refresh_equity=False)

    assert (ok, message) == (False, "boom")


def test_non_member_delete_hitting_zero_rows_reports_membership() -> None:
    client = _client({("portfolio_positions", "delete"): []})

    ok, message = sp.delete_position(PORTFOLIO_ID, "600000", client=client, refresh_equity=False)

    assert (ok, message) == (False, sp.MEMBERSHIP_WRITE_DENIED_MSG)


def test_cash_update_no_longer_claims_success_when_rls_denied_it() -> None:
    client = _client({("portfolios", "update"): []})

    ok, message = sp.update_free_cash(PORTFOLIO_ID, 5000.0, client=client, refresh_equity=False)

    assert (ok, message) == (False, sp.MEMBERSHIP_WRITE_DENIED_MSG)


def test_cash_update_for_a_member_still_succeeds() -> None:
    client = _client({("portfolios", "update"): [{"free_cash": 5000.0}]})

    ok, message = sp.update_free_cash(PORTFOLIO_ID, 5000.0, client=client, refresh_equity=False)

    assert ok is True
    assert "5,000.00" in message


@pytest.mark.parametrize("text", [sp.MEMBERSHIP_WRITE_DENIED_MSG])
def test_message_does_not_trigger_auth_retry(text: str) -> None:
    """命中重登关键字会让 with_auth_retry 把同一笔写入再执行一遍。"""
    assert _is_auth_error(Exception(text)) is False
