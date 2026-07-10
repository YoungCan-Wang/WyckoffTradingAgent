from __future__ import annotations

from collections import Counter
from datetime import date

import pandas as pd

from core.funnel_taxonomy import (
    REVIEW_STAGE_BASE_REJECT,
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_TRIGGER_MISS,
)
from core.wyckoff_engine import FunnelConfig
from workflows.review_big_gainers import find_big_gainers, load_today_review_codes
from workflows.review_list_replay import (
    ReplayContext,
    build_candidate_entry_map,
    classify_review_code,
)
from workflows.review_recommendation_lookup import format_recommendation_history, normalize_code6
from workflows.review_report_render import (
    build_focus_lines,
    build_report_lines,
    short_code_list,
)


def _row(code: str, name: str, stage: str) -> dict[str, str]:
    return {"code": code, "name": name, "stage": stage, "reason": ""}


def _ctx() -> ReplayContext:
    return ReplayContext(
        cfg=FunnelConfig(),
        all_symbol_set={"000001"},
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={},
        df_map={"000001": pd.DataFrame({"close": [1.0, 1.1]})},
        l1_set={"000001"},
        l2_set={"000001"},
        l3_set={"000001"},
        end_trade_date="2026-04-30",
        l2_ctx={},
        hit_map={"000001": ["SOS（量价点火）"]},
        blocked_exit_map={},
        candidate_entry_map={},
    )


def test_short_code_list_limits_output():
    rows = [
        _row("000001", "平安银行", REVIEW_STAGE_STRENGTH_MISS),
        _row("000002", "万科A", REVIEW_STAGE_STRENGTH_MISS),
        _row("000003", "国农科技", REVIEW_STAGE_STRENGTH_MISS),
    ]

    assert short_code_list(rows, limit=2) == "000001平安银行、000002万科A、等3只"


def test_classify_review_code_reports_pool_and_l4_hit():
    name, stage, reason = classify_review_code("999999", _ctx())
    assert (name, stage) == ("999999", "池外")
    assert "全市场" in reason

    name, stage, reason = classify_review_code("000001", _ctx())
    assert name == "平安银行"
    assert stage == REVIEW_STAGE_TRIGGER_HIT
    assert reason == "SOS（量价点火）"


def test_classify_review_code_reports_new_candidate_before_old_l2_gate():
    ctx = ReplayContext(
        cfg=FunnelConfig(),
        all_symbol_set={"000001"},
        name_map={"000001": "平安银行"},
        market_cap_map={},
        sector_map={"000001": "共封装光学(CPO)"},
        df_map={"000001": pd.DataFrame({"close": [1.0, 1.1]})},
        l1_set={"000001"},
        l2_set=set(),
        l3_set=set(),
        end_trade_date="2026-06-24",
        l2_ctx={},
        hit_map={},
        blocked_exit_map={},
        candidate_entry_map=build_candidate_entry_map(
            [
                {
                    "code": "000001",
                    "entry_type": "trend_breakout",
                    "score": 82.5,
                    "opportunity": "强趋势平台突破: 共封装光学(CPO)",
                }
            ]
        ),
    )

    name, stage, reason = classify_review_code("000001", ctx)

    assert name == "平安银行"
    assert stage == REVIEW_STAGE_CANDIDATE_HIT
    assert "趋势突破" in reason
    assert "强趋势平台突破" in reason


def test_build_candidate_entry_map_keeps_highest_scored_duplicate() -> None:
    entry_map = build_candidate_entry_map(
        [
            {"code": "000001", "entry_type": "launchpad", "score": 80.0},
            {"code": "000001", "entry_type": "spring", "score": 100.0},
        ]
    )

    assert entry_map["000001"]["entry_type"] == "spring"
    assert entry_map["000001"]["score"] == 100.0


def test_find_big_gainers_derives_pct_from_close():
    df = pd.DataFrame(
        {
            "date": ["2026-05-11", "2026-05-12", "2026-05-13"],
            "close": [10.0, 10.5, 11.4],
            "pct_chg": [0.0, 0.0, 0.0],
        }
    )

    codes = find_big_gainers({"000001": df}, {"000001": "平安银行"})

    assert codes == ["000001"]


def test_find_big_gainers_falls_back_to_pct_chg():
    df = pd.DataFrame({"date": ["2026-05-12", "2026-05-13"], "close": [10.0, 10.9], "pct_chg": [5.9, 8.2]})

    codes = find_big_gainers({"000001": df}, {"000001": "平安银行"})

    assert codes == ["000001"]


def test_find_big_gainers_excludes_hot_previous_day():
    df = pd.DataFrame(
        {
            "date": ["2026-05-11", "2026-05-12", "2026-05-13"],
            "close": [10.0, 10.7, 11.6],
            "pct_chg": [0.0, 0.0, 0.0],
        }
    )

    codes = find_big_gainers({"000001": df}, {"000001": "平安银行"})

    assert codes == []


def test_load_today_review_codes_falls_back_when_spot_candidates_empty(monkeypatch):
    from integrations import spot_snapshot

    monkeypatch.setattr(
        spot_snapshot,
        "load_spot_snapshot_map",
        lambda force_refresh: {"000001": {"pct_chg": 0.0}, "000002": {"pct_chg": 0.0}},
    )
    calls = []

    def fake_fetch(codes, name_map, window, log=None):
        calls.append(list(codes))
        return ["000001"]

    monkeypatch.setattr("workflows.review_big_gainers.fetch_and_filter_review_codes", fake_fetch)

    codes = load_today_review_codes(["000001", "000002"], {"000001": "平安银行", "000002": "万科A"}, object())

    assert codes == ["000001"]
    assert calls == [["000001", "000002"]]


def test_build_focus_lines_highlights_actionable_buckets():
    rows = [
        _row("000000", "候选A", REVIEW_STAGE_CANDIDATE_HIT),
        _row("000001", "平安银行", REVIEW_STAGE_STRENGTH_MISS),
        _row("000002", "万科A", REVIEW_STAGE_STRENGTH_MISS),
        _row("000003", "国农科技", REVIEW_STAGE_RISK_BLOCK),
        _row("000004", "长江证券", REVIEW_STAGE_TRIGGER_MISS),
        _row("000005", "世纪星源", REVIEW_STAGE_THEME_MISS),
        _row("000006", "深振业A", REVIEW_STAGE_BASE_REJECT),
        _row("000007", "全新好", REVIEW_STAGE_TRIGGER_HIT),
    ]

    lines = build_focus_lines(rows, today=date(2026, 5, 6), previous_trade_date=date(2026, 4, 30))
    text = "\n".join(lines)

    assert lines[0] == "**重点归因**"
    assert "日期间隔" in text
    assert "候选池已捕获" in text
    assert "结构强度不足" in text
    assert "风控拦截优先复盘" in text
    assert "000003国农科技" in text
    assert "买点未确认" in text
    assert "题材共振不足" in text
    assert "基础准入淘汰" in text
    assert "买点已确认" in text


def test_format_recommendation_history_reports_missing_and_hits():
    assert normalize_code6(1) == "000001"
    assert format_recommendation_history("000001", {}) == "推荐记录: 此股没被推荐过"

    lookup = {
        "000001": [
            {"code": 1, "recommend_date": 20260430, "recommend_count": 3},
            {"code": 1, "recommend_date": 20260429, "recommend_count": 2},
        ]
    }

    note = format_recommendation_history("000001", lookup)

    assert "2026-04-30、2026-04-29 被推荐过" in note
    assert "累计推荐3次" in note


def test_build_report_lines_appends_recommendation_note():
    rows = [
        {
            "code": "000001",
            "name": "平安银行",
            "stage": REVIEW_STAGE_STRENGTH_MISS,
            "reason": "六通道均未通过",
            "recommendation": "推荐记录: 2026-04-30 被推荐过；累计推荐1次",
        }
    ]

    lines = build_report_lines(
        rows,
        Counter({REVIEW_STAGE_STRENGTH_MISS: 1}),
        today=date(2026, 5, 6),
        previous_trade_date=date(2026, 4, 30),
        end_trade_date="2026-04-30",
    )

    assert "推荐记录: 2026-04-30 被推荐过；累计推荐1次" in "\n".join(lines)
    assert "**推荐表交叉检查**: 命中1只 | 未推荐0只" in "\n".join(lines)
