from __future__ import annotations

from core.theme_structure_cross import (
    COHORT_BROAD,
    COHORT_PRIMARY,
    CROSS_EMPTY_DAY,
    CROSS_MISSING_CREDS,
    CROSS_OBSERVATION_NOTE,
    classify_cohort,
    collect_wyckoff_views,
    display_cross_rows,
    intersect_cross_rows,
    is_gate_blocked,
    normalize_symbol_code,
    render_cross_section_lines,
    render_cross_ticket_lines,
    wyckoff_view,
)


def test_normalizer_accepts_int_zero_pad_and_radar_symbol() -> None:
    assert normalize_symbol_code(998) == "000998"
    assert normalize_symbol_code("998") == "000998"
    assert normalize_symbol_code("000998.SZ") == "000998"
    assert normalize_symbol_code("001872.SZ") == "001872"


def test_primary_includes_market_blocked_source() -> None:
    row = {
        "code": 998,
        "name": "隆平高科",
        "selection_source": "signal_confirmed:market_blocked",
        "candidate_lane": "mainline",
        "candidate_status": "主线买点候选",
        "score": 81.2,
    }
    view = wyckoff_view(row)
    assert view is not None
    assert view["ts_code"] == "000998"
    assert view["cohort"] == COHORT_PRIMARY
    assert view["gate_blocked"] is True
    assert is_gate_blocked(row) is True


def test_broad_lane_is_not_default_display() -> None:
    row = {"code": "000001", "candidate_lane": "sos", "score": 70}
    assert classify_cohort(row) == COHORT_BROAD
    assert classify_cohort({"code": "000001", "candidate_status": "formal_l4", "candidate_lane": "sos"}) == (
        COHORT_PRIMARY
    )


def test_historical_longping_shape_is_primary_agriculture_rank_one() -> None:
    """2026-09-01 000998 隆平高科：PRIMARY × 农业种植 rank 1。"""
    wy_rows = collect_wyckoff_views(
        [
            {
                "code": 998,
                "name": "隆平高科",
                "selection_source": "mainline",
                "candidate_status": "主线买点候选",
                "score": 88,
            }
        ]
    )
    rows = intersect_cross_rows(
        trade_date="2026-09-01",
        wyckoff_rows=wy_rows,
        radar_symbols=[
            {
                "symbol": "000998.SZ",
                "primary_theme": "农业种植",
                "roles": ["leader"],
                "action_state": "watch",
            }
        ],
        radar_themes=[{"theme": "农业种植", "rank": 1, "status": "diffusing", "lifecycle_stage": "diffusion"}],
        plan_codes=["000998.SZ"],
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["ts_code"] == "000998"
    assert row["name"] == "隆平高科"
    assert row["cohort"] == COHORT_PRIMARY
    assert row["radar_theme"] == "农业种植"
    assert row["theme_rank"] == 1
    assert row["gate_blocked"] is False
    assert row["has_radar_plan"] is True
    assert display_cross_rows(rows) == rows


def test_gate_blocked_cross_is_still_stored() -> None:
    wy_rows = collect_wyckoff_views(
        [{"code": "000998", "selection_source": "signal_confirmed:market_blocked", "name": "隆平高科"}]
    )
    rows = intersect_cross_rows(
        trade_date="2026-09-01",
        wyckoff_rows=wy_rows,
        radar_symbols=[{"symbol": "000998.SZ", "primary_theme": "农业种植"}],
        radar_themes=[{"theme": "农业种植", "rank": 1}],
    )
    assert rows[0]["gate_blocked"] is True
    assert rows[0]["cohort"] == COHORT_PRIMARY


def test_rank_above_five_is_not_mainline() -> None:
    rows = intersect_cross_rows(
        trade_date="2026-09-01",
        wyckoff_rows=[{"ts_code": "000998", "cohort": COHORT_PRIMARY, "name": "隆平高科"}],
        radar_symbols=[{"symbol": "000998.SZ", "primary_theme": "农业种植"}],
        radar_themes=[{"theme": "农业种植", "rank": 6}],
    )
    assert rows == []


def test_empty_day_copy_is_explicit_observation() -> None:
    text = "\n".join(render_cross_section_lines({"status": "ready", "rows": []}))
    assert CROSS_EMPTY_DAY in text
    assert CROSS_OBSERVATION_NOTE in text
    assert "next_buy" not in text
    assert "开盘带" in text


def test_missing_radar_creds_copy_is_not_empty_day() -> None:
    text = "\n".join(render_cross_section_lines({"status": "missing_creds", "rows": []}))
    assert CROSS_MISSING_CREDS in text
    assert CROSS_EMPTY_DAY not in text


def test_ticket_section_stays_observation_only() -> None:
    lines = render_cross_ticket_lines(
        {
            "status": "ready",
            "rows": [
                {
                    "ts_code": "000998",
                    "name": "隆平高科",
                    "radar_theme": "农业种植",
                    "theme_rank": 1,
                    "wy_status": "主线买点候选",
                    "wy_lane": "mainline",
                    "gate_blocked": True,
                    "cohort": COHORT_PRIMARY,
                }
            ],
        }
    )
    joined = "\n".join(lines)
    assert "筛选观察" in joined
    assert "农业种植#1" in joined
    assert "闸门拦截=是" in joined
    assert "不是买点" in joined
