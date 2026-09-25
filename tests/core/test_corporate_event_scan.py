from core.corporate_event_scan import (
    XINGSHUAIER_TELEGRAPH,
    XINHUA_MEDIA_TELEGRAPH,
    filter_recent_hits,
    is_material_restructure_or_halt,
    parse_telegraph_symbol,
    render_corporate_event_report,
    scan_corporate_events,
)


def test_xingshuaier_cls_telegraph_is_a_required_hit() -> None:
    assert is_material_restructure_or_halt(XINGSHUAIER_TELEGRAPH)
    assert parse_telegraph_symbol(XINGSHUAIER_TELEGRAPH) == ("002860", "星帅尔")
    hits = scan_corporate_events(
        [{"title": XINGSHUAIER_TELEGRAPH, "source": "财联社", "published_at": "2026-09-21 19:20:00"}]
    )
    assert [hit.code for hit in hits] == ["002860"]
    assert hits[0].name == "星帅尔"
    assert hits[0].reason == "halt_and_restructure"
    report = render_corporate_event_report(hits, as_of="2026-09-21")
    assert "002860" in report and "星帅尔" in report
    assert "不是实盘" in report


def test_xinhua_media_halt_regression_still_hits() -> None:
    assert is_material_restructure_or_halt(XINHUA_MEDIA_TELEGRAPH)
    hits = scan_corporate_events([{"title": XINHUA_MEDIA_TELEGRAPH, "source": "财联社"}])
    assert hits[0].code == "600825"
    assert hits[0].name == "新华传媒"


def test_scan_ignores_plain_limit_up_and_keeps_official_restructure() -> None:
    hits = scan_corporate_events(
        [
            {"title": "某股20cm涨停，龙虎榜净买入", "published_at": "2026-09-21 15:10:00"},
            {"title": "公司发布重大资产重组预案", "code": "000001", "name": "平安银行"},
        ]
    )
    assert [hit.code for hit in hits] == ["000001"]


def test_scan_parses_code_from_body_and_ignores_article_id() -> None:
    hits = scan_corporate_events(
        [
            {
                "title": "筹划购买湘鹰新材料等100%股权，股票停牌",
                "content": "星帅尔002860：晚间公告",
                "code": "20260921201234",
                "source": "公告",
            }
        ]
    )
    assert hits[0].code == "002860"
    assert hits[0].name == "星帅尔"


def test_filter_recent_keeps_same_evening_but_quarantines_missing_timestamp() -> None:
    hits = scan_corporate_events(
        [
            {"title": XINGSHUAIER_TELEGRAPH, "published_at": "2026-09-21 19:20:00"},
            {"title": XINHUA_MEDIA_TELEGRAPH, "published_at": "2026-09-10 09:00:00"},
            {"title": "某公司发布重大资产重组预案", "code": "000002"},
        ]
    )
    kept = filter_recent_hits(hits, as_of="2026-09-21 23:00", lookback_days=2)
    assert {hit.code for hit in kept} == {"002860"}


import json
from pathlib import Path

import pytest

from core.corporate_event_scan import normalize_stock_code
from core.corporate_event_time import event_time

CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/corporate_event_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["title"])
def test_shared_classification_cases(case):
    item = {key: case[key] for key in ("title", "content", "published_at") if key in case}
    item.update(code=case.get("source_code", ""), related_codes=case.get("source_related_codes", []))
    [hit] = scan_corporate_events([item])
    for key in ("code", "event_status", "halt_status", "reason", "subject_status", "related_codes", "effective_date"):
        if key in case:
            assert hit.as_dict()[key] == case[key]


@pytest.mark.parametrize("code", ["20260921001", "16008251", "id600825", "6008259", "123456", "600825abc"])
def test_numeric_identifiers_are_not_stock_codes(code):
    assert normalize_stock_code(code) == ""
    assert parse_telegraph_symbol(f"公告编号{code}，筹划重大资产重组")[0] == ""


def test_full_title_and_time_distinguish_updates():
    prefix = "测试公司002860：关于筹划重大资产重组进展事项及股票停牌的公告"
    hits = scan_corporate_events(
        [
            {"title": prefix + "（继续推进）", "published_at": "2026-09-21 08:00:00"},
            {"title": prefix + "（终止事项）", "published_at": "2026-09-21 08:00:00"},
        ]
    )
    assert len(hits) == 2


@pytest.mark.parametrize("as_of", ["2026-09-21 08:15", "2026-09-21T00:15:00Z", "2026-09-20T20:15:00-04:00"])
def test_window_enforces_time_of_day_offsets_and_unknown_dates(as_of):
    dates = [
        "2026-09-21 08:15:00",
        "2026-09-21 19:20:00",
        "2026-09-19 08:14:59",
        "2026-09-19 08:15:00",
        "",
        "not-a-time",
        "2026-09-21",
    ]
    hits = scan_corporate_events([{"title": XINGSHUAIER_TELEGRAPH, "published_at": value} for value in dates])
    kept = filter_recent_hits(hits, as_of=as_of)
    assert {hit.published_at for hit in kept} == {dates[0], dates[3]}


def test_date_only_cutoff_is_day_end_and_invalid_cutoff_is_rejected():
    hits = scan_corporate_events([{"title": XINGSHUAIER_TELEGRAPH, "published_at": "2026-09-21"}])
    assert filter_recent_hits(hits, as_of="2026-09-21") == hits
    with pytest.raises(ValueError):
        filter_recent_hits(hits, as_of="bad date")
    assert event_time("2026-02-30 08:00") is None


def test_report_does_not_claim_announced_resumption_is_tradeable():
    hits = scan_corporate_events(
        [
            {
                "title": "测试公司002860：终止重大资产重组，明日起复牌",
                "published_at": "2026-09-21 19:20:00",
            }
        ]
    )
    report = render_corporate_event_report(hits, as_of="2026-09-21 19:40")
    assert "2026-09-22" in report
    assert "不代表当前已复牌或可交易" in report


def test_symbol_report_exposes_ambiguous_related_codes():
    hits = scan_corporate_events([{"title": "甲公司（600001）与乙公司（600002）重大资产重组事项对比"}])
    report = render_corporate_event_report(hits, as_of="2026-09-21")
    assert "主体未确认" in report
    assert "600001, 600002" in report
