"""nav_snapshot_job 目标组合解析：必须对齐 Step4 的 USER_LIVE:<uuid>。"""

from __future__ import annotations

import importlib

import pytest

nav_job = importlib.import_module("scripts.nav_snapshot_job")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MY_PORTFOLIO_ID": "USER_LIVE:explicit"}, "USER_LIVE:explicit"),
        ({"PORTFOLIO_ID": "USER_LIVE:port"}, "USER_LIVE:port"),
        (
            {"MY_PORTFOLIO_ID": "USER_LIVE:explicit", "SUPABASE_USER_ID": "uuid-a"},
            "USER_LIVE:explicit",
        ),
        (
            {"SUPABASE_USER_ID": "e66942b7-be66-46fe-95ed-ebc7f3b47928"},
            "USER_LIVE:e66942b7-be66-46fe-95ed-ebc7f3b47928",
        ),
        ({}, "USER_LIVE"),
    ],
)
def test_default_portfolio_id_maps_supabase_user(monkeypatch, env: dict[str, str], expected: str) -> None:
    for key in ("MY_PORTFOLIO_ID", "PORTFOLIO_ID", "SUPABASE_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert nav_job._default_portfolio_id() == expected
