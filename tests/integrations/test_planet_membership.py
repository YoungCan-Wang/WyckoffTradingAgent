from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from integrations.planet_membership import (
    PLANET_MEMBERSHIP_URL,
    is_active_planet_member,
    is_membership_active,
    membership_required_message,
)

TODAY = date(2026, 10, 12)


@pytest.mark.parametrize(
    ("expires_on", "expected"),
    [
        (None, True),
        ("", True),
        ("2026-10-12", True),
        ("2026-12-31", True),
        ("2026-10-11", False),
        ("20261231", False),
        ("not-a-date", False),
        ("2026-02-30", False),
    ],
)
def test_membership_expiry_matches_shared_ts_semantics(expires_on, expected) -> None:
    assert is_membership_active(expires_on, today=TODAY) is expected


class _Client:
    def __init__(self, data=None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.filters: list[tuple] = []

    def table(self, name: str):
        self.filters.append(("table", name))
        return self

    def select(self, *args):
        return self

    def eq(self, column: str, value: str):
        self.filters.append((column, value))
        return self

    def limit(self, _n: int):
        return self

    def execute(self):
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.data)


def test_lookup_filters_by_user_so_an_admin_client_cannot_return_someone_else() -> None:
    client = _Client(data=[{"expires_on": None}])

    assert is_active_planet_member(client, "u-1") is True
    assert ("user_id", "u-1") in client.filters


def test_no_row_means_not_a_member() -> None:
    assert is_active_planet_member(_Client(data=[]), "u-1") is False


def test_expired_row_means_not_active() -> None:
    assert is_active_planet_member(_Client(data=[{"expires_on": "2000-01-01"}]), "u-1") is False


def test_lookup_failure_is_unknown_not_false() -> None:
    """查询失败不能被当成「不是会员」：否则网络抖动会让会员看到「请开通会员」。"""
    assert is_active_planet_member(_Client(error=RuntimeError("boom")), "u-1") is None
    assert is_active_planet_member(_Client(data=None), "u-1") is None
    assert is_active_planet_member(_Client(data=[{"expires_on": None}]), "") is None


def test_message_names_the_feature_and_carries_the_entry() -> None:
    message = membership_required_message("策略归因报告")

    assert "策略归因报告" in message
    assert PLANET_MEMBERSHIP_URL in message
    assert "不是暂无数据" in message
