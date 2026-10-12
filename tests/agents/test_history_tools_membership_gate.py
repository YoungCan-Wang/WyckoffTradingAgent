"""RLS 对非会员返回空行，和「真的没有数据」同形：工具回报要把两者分开。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents import history_tools
from agents.tool_context import ToolContext
from integrations.planet_membership import PLANET_MEMBERSHIP_URL


class _MemberClient:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows, self.error = rows, error

    def table(self, _name: str):
        return self

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.rows)


def _ctx() -> ToolContext:
    return ToolContext(state={"user_id": "u-1"})


@pytest.fixture
def empty_remote(monkeypatch):
    monkeypatch.setattr(history_tools, "_load_attribution_rows", lambda *a, **k: [])
    monkeypatch.setattr(history_tools, "_load_local_recommendations", lambda *_a: [])
    monkeypatch.setattr(history_tools, "_load_remote_recommendations", lambda *a, **k: [])


def _as_user(monkeypatch, client) -> None:
    monkeypatch.setattr(history_tools, "get_user_client", lambda _ctx: client)


def test_attribution_empty_for_a_non_member_says_membership_not_no_data(empty_remote, monkeypatch) -> None:
    _as_user(monkeypatch, _MemberClient(rows=[]))

    result = history_tools._query_attribution(5, _ctx())

    assert PLANET_MEMBERSHIP_URL in result["message"]
    assert "策略归因报告" in result["message"]
    assert result["records"] == []


def test_attribution_empty_for_a_member_is_still_no_data(empty_remote, monkeypatch) -> None:
    _as_user(monkeypatch, _MemberClient(rows=[{"expires_on": None}]))

    assert history_tools._query_attribution(5, _ctx())["message"] == "暂无策略归因报告"


@pytest.mark.parametrize("client", [None, _MemberClient(error=RuntimeError("down"))])
def test_unknown_membership_keeps_the_original_message(empty_remote, monkeypatch, client) -> None:
    _as_user(monkeypatch, client)

    assert history_tools._query_attribution(5, _ctx())["message"] == "暂无策略归因报告"


@pytest.mark.parametrize("market", ["cn", "hk", "us"])
def test_every_market_tracking_is_member_only(empty_remote, monkeypatch, market) -> None:
    _as_user(monkeypatch, _MemberClient(rows=[]))

    result = history_tools._query_recommendation(5, _ctx(), market=market)

    assert PLANET_MEMBERSHIP_URL in result["message"]
    assert result["records"] == []


def test_member_with_no_tracking_rows_still_sees_no_data(empty_remote, monkeypatch) -> None:
    _as_user(monkeypatch, _MemberClient(rows=[{"expires_on": None}]))

    assert history_tools._query_recommendation(5, _ctx(), market="cn")["message"] == "暂无复盘记录"


def test_signal_empty_for_a_non_member_says_membership(monkeypatch) -> None:
    monkeypatch.setattr(history_tools, "_load_signal_rows", lambda *a, **k: [])
    _as_user(monkeypatch, _MemberClient(rows=[]))

    result = history_tools._query_signal("pending", 5, _ctx())

    assert PLANET_MEMBERSHIP_URL in result["message"]


def test_signal_empty_for_a_member_keeps_the_status_label(monkeypatch) -> None:
    monkeypatch.setattr(history_tools, "_load_signal_rows", lambda *a, **k: [])
    _as_user(monkeypatch, _MemberClient(rows=[{"expires_on": None}]))

    assert history_tools._query_signal("pending", 5, _ctx())["message"] == "暂无待确认信号记录"
