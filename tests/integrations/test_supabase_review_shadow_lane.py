"""影子车道落库:行构建与 schema 防漂移。

schema 以 Python 常量版本化(本项目不留 .sql),写入侧和建表侧靠测试对齐,
不靠人记。
"""

from __future__ import annotations

import re
from pathlib import Path

from core.funnel_taxonomy import (
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_MISS,
)
from core.review_shadow_lane_schema import UNIQUE_KEY, payload_keys
from core.review_shadow_lanes import SHADOW_POLICY_VERSION
from integrations.supabase_review_shadow_lane import build_lane_rows


def _trace(symbols: dict) -> dict:
    return {
        "trade_date": "2026-09-01",
        "policy": {"shadow_near_l2_max_gap_pct": 10.0},
        "symbols": symbols,
    }


def test_only_lane_eligible_symbols_become_rows() -> None:
    """全市场 5000 只里只有落在车道上的才写:过候选/数据失败的不是观测对象。"""
    payload = _trace(
        {
            "000001": {"stage": REVIEW_STAGE_TRIGGER_MISS, "l3_eligible": True, "layer3_quality_score": 0.5},
            "000002": {
                "stage": REVIEW_STAGE_CANDIDATE_HIT,
                "l1_eligible": True,
                "l2_eligible": True,
                "l3_eligible": True,
            },
            "000003": {"stage": "数据失败"},
        }
    )

    rows = build_lane_rows(payload)

    assert [row["ts_code"] for row in rows] == ["000001"]
    assert rows[0]["lane"] == "pre_breakout"
    assert rows[0]["policy_version"] == SHADOW_POLICY_VERSION


def test_row_carries_momentum_and_close_for_same_momentum_control() -> None:
    """同动量对照要的是信号日动量:缺了它只能拿全市场比,会把择时读成选股。"""
    payload = _trace(
        {
            "000001": {
                "stage": REVIEW_STAGE_TRIGGER_MISS,
                "l3_eligible": True,
                "layer3_quality_score": 0.42,
                "rps_fast": 88.5,
                "rps_slow": 91.25,
                "close": 12.34,
            }
        }
    )

    row = build_lane_rows(payload)[0]

    assert row["rps_fast"] == 88.5
    assert row["rps_slow"] == 91.25
    assert row["close"] == 12.34
    assert row["watch_score"] == 0.42
    assert row["ranked"] is True
    assert row["score"] is not None


def test_unranked_lane_stores_null_score_not_a_constant() -> None:
    """没有连续键就存 null。填常数会让「取前 N 只」看起来能做,实际全是同分。"""
    payload = _trace({"000001": {"stage": REVIEW_STAGE_THEME_MISS, "l2_eligible": True, "l2_channel": "主升通道"}})

    row = build_lane_rows(payload)[0]

    assert row["lane"] == "rotation_setup"
    assert row["score"] is None
    assert row["ranked"] is False


def test_missing_trade_date_yields_no_rows() -> None:
    symbols = {"000001": {"stage": REVIEW_STAGE_TRIGGER_MISS, "l3_eligible": True}}
    assert build_lane_rows({"symbols": symbols}) == []


def test_payload_keys_match_schema_columns() -> None:
    """写入字段集必须等于建表字段集(created_at 由 default 填)。"""
    source = Path("integrations/supabase_review_shadow_lane.py").read_text(encoding="utf-8")
    block = source.split("def _lane_row(")[1].split("\n\n")[0]
    written = set(re.findall(r'^\s+"(\w+)":', block, re.M))
    assert written == set(payload_keys()), sorted(written ^ set(payload_keys()))


def test_unique_key_matches_upsert_conflict_target() -> None:
    """键错位会让一行冲突回滚整批,而且是静默的(memory dedup-key-must-match-db-constraint)。"""
    source = Path("integrations/supabase_review_shadow_lane.py").read_text(encoding="utf-8")
    assert f'_CONFLICT_KEY = "{",".join(UNIQUE_KEY)}"' in source


def test_ddl_is_idempotent_and_indexes_momentum() -> None:
    from core.review_shadow_lane_schema import build_ddl

    ddl = build_ddl()
    assert "create table if not exists" in ddl
    assert ddl.count("create index if not exists") == 2
    assert "(trade_date, rps_slow)" in ddl
