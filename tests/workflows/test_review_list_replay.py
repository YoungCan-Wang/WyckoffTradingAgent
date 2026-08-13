from __future__ import annotations

from collections import Counter
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

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
from workflows.review_big_gainers import (
    ReviewPool,
    execution_snapshot,
    find_big_gainers,
    find_big_gainers_from_spot,
    load_today_review_codes,
    load_today_review_pool,
)
from workflows.review_list_replay import (
    ReplayContext,
    ReviewDates,
    _write_review_outputs,
    build_candidate_entry_map,
    classify_review_code,
    load_previous_context,
    replay_context_from_trace,
)
from workflows.review_recommendation_lookup import format_recommendation_history, normalize_code6, recommendation_state
from workflows.review_report_render import (
    build_focus_lines,
    build_report_lines,
    short_code_list,
)
from workflows.review_trace import build_review_trace, load_review_trace_artifact, write_review_trace_artifact


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
            "close": [10.0, 10.2, 11.0],
            "pct_chg": [0.0, 0.0, 0.0],
        }
    )

    codes = find_big_gainers({"000001": df}, {"000001": "平安银行"})

    assert codes == ["000001"]


def test_find_big_gainers_falls_back_to_pct_chg():
    df = pd.DataFrame({"date": ["2026-05-12", "2026-05-13"], "close": [10.0, 10.8], "pct_chg": [2.9, 7.2]})

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


def test_find_big_gainers_uses_strict_close_boundaries() -> None:
    exactly_seven_today = pd.DataFrame(
        {"date": ["2026-05-11", "2026-05-12", "2026-05-13"], "close": [10.0, 10.2, 10.914]}
    )
    exactly_three_previous = pd.DataFrame(
        {"date": ["2026-05-11", "2026-05-12", "2026-05-13"], "close": [10.0, 10.3, 11.1]}
    )

    codes = find_big_gainers(
        {"000001": exactly_seven_today, "000002": exactly_three_previous},
        {"000001": "平安银行", "000002": "万科A"},
    )

    assert codes == []


def test_find_big_gainers_does_not_filter_gap_up_open() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2026-05-11", "2026-05-12", "2026-05-13"],
            "open": [10.0, 10.1, 11.0],
            "close": [10.0, 10.2, 11.1],
        }
    )

    assert find_big_gainers({"000001": frame}, {"000001": "平安银行"}) == ["000001"]


def test_execution_snapshot_separates_raw_review_from_open_tradeability() -> None:
    tradable = pd.DataFrame(
        {
            "date": ["2026-05-12", "2026-05-13"],
            "open": [10.0, 10.3],
            "high": [10.2, 11.0],
            "low": [9.9, 10.2],
            "close": [10.0, 10.9],
        }
    )
    gap_up = tradable.copy()
    gap_up.loc[1, "open"] = 10.5
    one_price = tradable.copy()
    one_price.loc[1, ["open", "high", "low", "close"]] = 11.0

    assert execution_snapshot(tradable)["executable"] is True
    assert execution_snapshot(gap_up)["executable"] is False
    assert execution_snapshot(gap_up)["reason"] == "开盘跳空超过4%"
    assert execution_snapshot(one_price)["executable"] is False
    assert execution_snapshot(one_price)["reason"] == "一字板不可成交"


def test_execution_snapshot_reports_intraday_tradeability() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2026-05-12", "2026-05-13"],
            "open": [10.0, 10.6],
            "high": [10.2, 11.0],
            "low": [9.9, 10.3],
            "close": [10.0, 10.9],
        }
    )

    snapshot = execution_snapshot(frame)

    assert snapshot["executable"] is False
    assert snapshot["intraday_executable"] is True
    assert snapshot["low_gap_pct"] == pytest.approx(3.0)


def test_spot_prefilter_uses_strict_today_close_threshold() -> None:
    codes, usable = find_big_gainers_from_spot(
        {
            "000001": {"pct_chg": 7.0, "open": 10.8, "close": 10.7},
            "000002": {"pct_chg": 7.01, "open": 11.0, "close": 10.8},
        },
        {"000001": "平安银行", "000002": "万科A"},
    )

    assert usable == 2
    assert codes == ["000002"]


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
        return ReviewPool(["000001"], {})

    monkeypatch.setattr("workflows.review_big_gainers.fetch_review_pool", fake_fetch)

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


def test_recommendation_state_separates_tracking_from_ai_recommendation() -> None:
    records = [
        {
            "recommend_date": "2026-05-12",
            "is_ai_recommended": False,
            "candidate_status": "shadow",
            "selection_source": "step2_selected_for_ai",
        }
    ]

    state = recommendation_state(records, "2026-05-12")

    assert state == {
        "tracked": True,
        "ai_recommended": False,
        "statuses": ["shadow"],
        "sources": ["step2_selected_for_ai"],
    }


def test_build_report_lines_appends_recommendation_note():
    rows = [
        {
            "code": "000001",
            "name": "平安银行",
            "stage": REVIEW_STAGE_STRENGTH_MISS,
            "reason": "八通道均未通过",
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


def test_review_outputs_write_structured_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_REPORT_OUTPUT_DIR", str(tmp_path))
    dates = ReviewDates(date(2026, 5, 13), date(2026, 5, 12), object())

    _write_review_outputs([_row("000001", "平安银行", REVIEW_STAGE_TRIGGER_MISS)], {"total": 1}, dates, "报告")

    assert (tmp_path / "review_list_20260513.md").read_text(encoding="utf-8") == "报告\n"
    payload = (tmp_path / "review_list_20260513.json").read_text(encoding="utf-8")
    assert '"previous_trade_date": "2026-05-12"' in payload


def test_build_report_lines_separates_raw_and_executable_capture_rates() -> None:
    rows = [_row("000001", "平安银行", REVIEW_STAGE_CANDIDATE_HIT)]
    lines = build_report_lines(
        rows,
        Counter({REVIEW_STAGE_CANDIDATE_HIT: 1}),
        today=date(2026, 5, 13),
        previous_trade_date=date(2026, 5, 12),
        end_trade_date="2026-05-12",
        stats={
            "candidate": 1,
            "recommended": 0,
            "total": 3,
            "l1_eligible": 2,
            "open_executable": 1,
            "candidate_open_executable": 1,
            "execution_available": 3,
        },
    )

    text = "\n".join(lines)
    assert "前日基础准入 2/3" in text
    assert "次日开盘≤+4%且非一字板 1/2" in text
    assert "可交易样本前日候选 1/1" in text


def test_tushare_cross_sections_avoid_full_market_history_fetch(monkeypatch):
    from integrations import tushare_client

    class FakePro:
        def __init__(self):
            self.calls: list[str] = []

        def daily(self, *, trade_date: str):
            self.calls.append(trade_date)
            if trade_date == "20260513":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "pct_chg": 8.0,
                            "pre_close": 10.0,
                            "open": 10.3,
                            "high": 11.0,
                            "low": 10.2,
                            "close": 10.8,
                        },
                        {
                            "ts_code": "000002.SZ",
                            "pct_chg": 9.0,
                            "pre_close": 8.0,
                            "open": 8.1,
                            "high": 8.8,
                            "low": 8.0,
                            "close": 8.7,
                        },
                    ]
                )
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "pct_chg": 2.0, "close": 10.0},
                    {"ts_code": "000002.SZ", "pct_chg": 4.0, "close": 8.0},
                ]
            )

    pro = FakePro()
    monkeypatch.setattr(tushare_client, "has_tushare_token", lambda: True)
    monkeypatch.setattr(tushare_client, "get_pro", lambda: pro)
    monkeypatch.setattr(
        "workflows.review_big_gainers.fetch_review_pool",
        lambda *_args, **_kwargs: pytest.fail("full OHLCV fallback should not run"),
    )

    pool = load_today_review_pool(
        ["000001", "000002"],
        {"000001": "平安银行", "000002": "万科A"},
        SimpleNamespace(end_trade_date=date(2026, 5, 13)),
        previous_trade_date=date(2026, 5, 12),
    )

    assert pro.calls == ["20260513", "20260512"]
    assert pool.codes == ["000001"]
    assert execution_snapshot(pool.frames["000001"])["executable"] is True


def test_review_trace_records_as_run_stages_without_ohlcv(tmp_path):
    cfg = FunnelConfig()
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range("2025-08-01", periods=220),
            "close": [10.0] * 220,
            "amount": [100_000_000.0] * 220,
        }
    )
    inputs = SimpleNamespace(
        cfg=cfg,
        window=SimpleNamespace(end_trade_date=date(2026, 5, 12)),
        pool=SimpleNamespace(symbols=["000001", "000002", "000003"]),
        ref_data=SimpleNamespace(
            name_map={"000001": "平安银行", "000002": "万科A", "000003": "低市值"},
            sector_map={"000001": "银行", "000002": "地产", "000003": "其它"},
            market_cap_map={"000001": 100.0, "000002": 100.0, "000003": 5.0},
            financial_map={},
        ),
        all_df_map={"000001": frame, "000002": frame, "000003": frame},
        layers=SimpleNamespace(
            l1_passed=["000001", "000002"],
            l2_passed=["000001"],
            l3_passed=["000001"],
            l2_channel_map={"000001": "点火破局"},
            l2_rejections={"000002": "最接近趋势延续(缺口5.0%)"},
        ),
        candidates=SimpleNamespace(
            candidate_entries=[
                {
                    "code": "000001",
                    "entry_type": "trend_breakout",
                    "score": 82.0,
                    "opportunity": "平台突破",
                }
            ],
            exit_signals={},
        ),
    )
    payload = build_review_trace(inputs, {"sos": [("000001", 5.0)]}, {"data_quality": {"status": "normal"}})

    assert payload["symbols"]["000001"]["stage"] == REVIEW_STAGE_CANDIDATE_HIT
    assert payload["symbols"]["000002"]["stage"] == REVIEW_STAGE_STRENGTH_MISS
    assert payload["symbols"]["000003"]["stage"] == REVIEW_STAGE_BASE_REJECT
    assert "all_df_map" not in payload
    assert "close" not in payload["symbols"]["000001"]
    assert payload["symbols"]["000002"]["shadow_lane"] == "near_l2"

    path = write_review_trace_artifact(inputs, {"sos": [("000001", 5.0)]}, {}, str(tmp_path))
    loaded = load_review_trace_artifact(path, date(2026, 5, 12))
    assert loaded["config_digest"] == payload["config_digest"]
    with pytest.raises(ValueError, match="date mismatch"):
        load_review_trace_artifact(path, date(2026, 5, 11))


def test_replay_context_from_trace_uses_recorded_decision_reason():
    payload = {
        "trade_date": "2026-05-12",
        "run": {"git_sha": "abcdef1234567890"},
        "symbols": {
            "000001": {
                "name": "平安银行",
                "sector": "银行",
                "stage": REVIEW_STAGE_STRENGTH_MISS,
                "reason": "生产时八通道未通过",
                "l1_eligible": True,
                "l2_eligible": False,
                "l3_eligible": False,
            }
        },
    }

    ctx = replay_context_from_trace(payload)

    assert classify_review_code("000001", ctx) == (
        "平安银行",
        REVIEW_STAGE_STRENGTH_MISS,
        "生产时八通道未通过",
    )
    assert ctx.source == "production_artifact:abcdef123456"


def test_previous_context_does_not_full_replay_without_explicit_fallback(monkeypatch):
    monkeypatch.delenv("REVIEW_TRACE_PATH", raising=False)
    monkeypatch.delenv("REVIEW_ALLOW_FULL_FUNNEL_FALLBACK", raising=False)
    monkeypatch.setattr(
        "workflows.review_list_replay.run_previous_funnel",
        lambda *_args, **_kwargs: pytest.fail("full replay must be explicit"),
    )

    assert load_previous_context(date(2026, 5, 12), log=lambda _line: None) is None
