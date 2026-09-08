from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from integrations.recommendation_global import upsert_global_recommendation_tracking_updates
from integrations.recommendation_payload import upsert_recommendation_payload_rows
from integrations.recommendation_tracking_common import upsert_to_table
from integrations.supabase_base import read_only_write_context
from integrations.supabase_market_signal import _write_merged_row
from integrations.supabase_recommendation import (
    upsert_recommendation_tracking_price_updates,
    upsert_recommendation_tracking_updates,
)


class RecordingClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def upsert(self, rows, **kwargs):
        self.calls.append(("upsert", rows))
        return self

    def execute(self):
        self.calls.append(("execute", None))
        return SimpleNamespace(data=[])


def _writers(client):
    row = {"code": "300308", "recommend_date": 20260904, "id": 1}
    return [
        lambda: upsert_recommendation_tracking_updates(client, [row]),
        lambda: upsert_recommendation_tracking_price_updates(client, [row]),
        lambda: upsert_to_table(client, "recommendation_tracking", [row]),
        lambda: upsert_global_recommendation_tracking_updates(client, "hk", [row]),
        lambda: upsert_recommendation_payload_rows(client, [row]),
        lambda: _write_merged_row(client, {"trade_date": "2026-09-04"}),
    ]


def test_low_level_shared_adapters_reject_readonly_before_touching_client(monkeypatch):
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    client = RecordingClient()
    with read_only_write_context():
        with ThreadPoolExecutor(max_workers=1) as pool:
            for write in _writers(client):
                with pytest.raises(PermissionError, match="read-only"):
                    pool.submit(write).result()
        os.environ["WYCKOFF_SHARED_READ_ONLY"] = "0"
        for write in _writers(client):
            with pytest.raises(PermissionError, match="read-only"):
                write()
    assert client.calls == []


@pytest.mark.parametrize("identity", ["cli", "server_job", "custom-existing-context"])
def test_low_level_adapter_guard_does_not_change_normal_call_authority(monkeypatch, identity):
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", identity)
    monkeypatch.delenv("WYCKOFF_SHARED_READ_ONLY", raising=False)
    client = RecordingClient()
    for write in _writers(client):
        write()
    assert sum(kind == "execute" for kind, _ in client.calls) == 6


def test_factor_ic_write_guard_runs_before_admin_client_creation(monkeypatch):
    from integrations import supabase_factor_ic as factor

    client = RecordingClient()
    monkeypatch.setattr(factor, "create_admin_client", lambda: client)
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    with read_only_write_context(), pytest.raises(PermissionError, match="read-only"):
        factor.save_factor_ic_rows([{"name": "sample", "horizon": 5, "days": 10}])
    assert client.calls == []
    assert factor.save_factor_ic_rows([{"name": "sample", "horizon": 5, "days": 10}]) == 1


def test_replay_reachable_concept_and_shadow_writers_remain_blocked(monkeypatch):
    from integrations import supabase_concept_heat as concepts
    from integrations import supabase_review_shadow_lane as shadow

    client = RecordingClient()
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(concepts, "_configured", lambda: True)
    monkeypatch.setattr(concepts, "_admin", lambda: client)
    monkeypatch.setattr(shadow, "create_admin_client", lambda: client)
    with read_only_write_context():
        with pytest.raises(PermissionError):
            concepts.upsert_concept_heat_history("2026-09-04", [{"name": "CPO", "pct": 1.0}])
        with pytest.raises(PermissionError):
            shadow.save_review_shadow_lane_rows([{"code": "300308"}])
    assert client.calls == []


@pytest.mark.parametrize("original", [None, "", "unknown", "0", "true"])
def test_overlapping_scopes_keep_workers_blocked_until_last_exit(monkeypatch, original):
    from integrations import supabase_base as sb

    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    if original is None:
        monkeypatch.delenv("WYCKOFF_SHARED_READ_ONLY", raising=False)
    else:
        monkeypatch.setenv("WYCKOFF_SHARED_READ_ONLY", original)
    client = RecordingClient()
    entered = [Event(), Event()]
    release = [Event(), Event()]

    def hold_scope(index):
        with read_only_write_context():
            entered[index].set()
            assert release[index].wait(timeout=5)

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(hold_scope, 0)
        try:
            assert entered[0].wait(timeout=5)
            second = pool.submit(hold_scope, 1)
            assert entered[1].wait(timeout=5)
            release[0].set()
            first.result(timeout=5)
            assert os.environ["WYCKOFF_SHARED_READ_ONLY"] == "1"
            for value in ("1", "0"):
                os.environ["WYCKOFF_SHARED_READ_ONLY"] = value
                for write in _writers(client):
                    with pytest.raises(PermissionError, match="read-only"):
                        pool.submit(write).result(timeout=5)
        finally:
            release[0].set()
            release[1].set()
        second.result(timeout=5)
    assert client.calls == []
    assert os.environ.get("WYCKOFF_SHARED_READ_ONLY") == original
    assert sb.is_server_write_context() is (original != "true")
