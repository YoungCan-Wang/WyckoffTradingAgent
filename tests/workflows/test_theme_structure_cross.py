from __future__ import annotations

from core.theme_structure_cross import COHORT_BROAD, COHORT_PRIMARY, CROSS_EMPTY_DAY, CROSS_MISSING_CREDS
from integrations.radar_supabase import RadarCredentialsMissing, RadarDaySnapshot
from workflows.theme_structure_cross import compute_theme_structure_cross, persist_daily_theme_structure_cross


def _longping_metrics() -> dict:
    return {
        "end_trade_date": "2026-09-01",
        "mainline_candidates": [
            {"code": 998, "name": "隆平高科", "status": "主线买点候选", "score": 88},
        ],
        "candidate_entries": [
            {"code": "600519", "entry_type": "sos", "score": 70, "name": "贵州茅台"},
        ],
    }


def _radar_day() -> RadarDaySnapshot:
    return RadarDaySnapshot(
        trade_date="2026-09-01",
        themes=[{"theme": "农业种植", "rank": 1, "status": "diffusing"}],
        symbols=[{"symbol": "000998.SZ", "primary_theme": "农业种植", "roles": ["leader"]}],
        plan_codes=["000998.SZ"],
    )


def test_compute_classifies_historical_primary_and_keeps_broad_off_display(monkeypatch) -> None:
    monkeypatch.setattr("workflows.theme_structure_cross.load_radar_day", lambda _date: _radar_day())
    payload = compute_theme_structure_cross(_longping_metrics())
    assert payload["status"] == "ready"
    assert payload["primary_count"] == 1
    primary = payload["rows"][0]
    assert primary["ts_code"] == "000998"
    assert primary["radar_theme"] == "农业种植"
    assert primary["theme_rank"] == 1
    assert primary["cohort"] == COHORT_PRIMARY
    assert all(row["cohort"] != COHORT_BROAD or row["ts_code"] != "000998" for row in payload["rows"])


def test_missing_radar_creds_are_not_empty_day(monkeypatch) -> None:
    def _raise(_date):
        raise RadarCredentialsMissing("需要 RADAR_SUPABASE_URL 与 RADAR_SUPABASE_SERVICE_ROLE_KEY")

    monkeypatch.setattr("workflows.theme_structure_cross.load_radar_day", _raise)
    logs: list[str] = []
    payload = persist_daily_theme_structure_cross(
        trade_date="2026-09-01",
        metrics=_longping_metrics(),
        dry_run=False,
        log_fn=lambda msg, _path: logs.append(msg),
        logs_path=None,
    )
    assert payload["status"] == "missing_creds"
    assert payload["rows"] == []
    assert CROSS_EMPTY_DAY not in str(payload)
    assert "missing_creds" in logs[0]


def test_empty_ready_day_does_not_invent_buys(monkeypatch) -> None:
    monkeypatch.setattr(
        "workflows.theme_structure_cross.load_radar_day",
        lambda _date: RadarDaySnapshot(trade_date="2026-09-01"),
    )
    saved: list[list] = []
    monkeypatch.setattr(
        "integrations.supabase_theme_structure_cross.save_theme_structure_cross_rows",
        lambda rows: saved.append(rows) or 0,
    )
    payload = persist_daily_theme_structure_cross(
        trade_date="2026-09-01",
        metrics=_longping_metrics(),
        dry_run=False,
        log_fn=lambda *_args: None,
        logs_path=None,
    )
    assert payload["status"] == "ready"
    assert payload["rows"] == []
    assert saved == [[]]
    from core.theme_structure_cross import render_cross_section_lines

    text = "\n".join(render_cross_section_lines(payload))
    assert CROSS_EMPTY_DAY in text
    assert CROSS_MISSING_CREDS not in text


def test_persist_step2_calls_cross_after_recommendations() -> None:
    from pathlib import Path

    source = Path("workflows/daily_job_step2.py").read_text(encoding="utf-8")
    rec = source.index("persist_recommendations")
    hook = source.index("_persist_theme_structure_cross")
    assert rec < hook
    assert "persist_daily_theme_structure_cross" in source
