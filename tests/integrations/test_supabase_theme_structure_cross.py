from __future__ import annotations

import re
from pathlib import Path

from core.theme_structure_cross import COHORT_PRIMARY, intersect_cross_rows
from core.theme_structure_cross_schema import UNIQUE_KEY, build_ddl, payload_keys
from integrations.supabase_theme_structure_cross import build_persist_rows


def test_payload_keys_match_schema_columns() -> None:
    rows = intersect_cross_rows(
        trade_date="2026-09-01",
        wyckoff_rows=[
            {
                "ts_code": "000998",
                "name": "隆平高科",
                "wy_lane": "mainline",
                "wy_status": "主线买点候选",
                "wy_source": "mainline",
                "wy_score": 88,
                "gate_blocked": False,
                "cohort": COHORT_PRIMARY,
            }
        ],
        radar_symbols=[{"symbol": "000998.SZ", "primary_theme": "农业种植", "roles": ["leader"]}],
        radar_themes=[{"theme": "农业种植", "rank": 1, "status": "diffusing"}],
        plan_codes=["000998.SZ"],
    )
    written = set(build_persist_rows(rows)[0])
    assert written == set(payload_keys()), sorted(written ^ set(payload_keys()))


def test_unique_key_matches_upsert_conflict_target() -> None:
    source = Path("integrations/supabase_theme_structure_cross.py").read_text(encoding="utf-8")
    assert f'_CONFLICT_KEY = "{",".join(UNIQUE_KEY)}"' in source


def test_ddl_is_idempotent_and_indexes_date_and_code() -> None:
    ddl = build_ddl()
    assert "create table if not exists public.theme_structure_cross_daily" in ddl
    assert re.search(r"create index if not exists \w+_date_cohort_idx", ddl)
    assert re.search(r"create index if not exists \w+_code_idx", ddl)
    assert f"unique ({', '.join(UNIQUE_KEY)})" in ddl
    assert "gate_blocked" in ddl
    assert "cohort" in ddl
