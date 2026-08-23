from __future__ import annotations

from core.report_snapshot_cache import (
    build_step3_snapshot_payload,
    data_snapshot_hash,
    load_cached_report,
    store_cached_report,
)


def _payload(**overrides):
    base = {
        "trade_date": "2026-08-21",
        "regime": "NEUTRAL",
        "model": "gemini-x",
        "selected_rows": [{"code": "000001", "score": 1.2}],
        "rag_veto_lines": [],
    }
    base.update(overrides)
    return build_step3_snapshot_payload(**base)


def test_hash_changes_when_candidate_score_changes():
    first = data_snapshot_hash(_payload())
    second = data_snapshot_hash(_payload(selected_rows=[{"code": "000001", "score": 9.9}]))
    assert first != second
    assert first.startswith("step3_snapshot_v1:")


def test_cache_roundtrip_rejects_stale_hash(tmp_path):
    payload = _payload()
    snapshot = data_snapshot_hash(payload)
    store_cached_report(snapshot, {"report": "ok", "used_models": {"Trend": "m"}}, tmp_path)
    hit = load_cached_report(snapshot, tmp_path)
    assert hit is not None
    assert hit["report"] == "ok"
    assert load_cached_report("step3_snapshot_v1:deadbeef", tmp_path) is None
