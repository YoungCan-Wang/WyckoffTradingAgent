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


def test_filter_recent_keeps_same_evening_and_missing_timestamp() -> None:
    hits = scan_corporate_events(
        [
            {"title": XINGSHUAIER_TELEGRAPH, "published_at": "2026-09-21 19:20:00"},
            {"title": XINHUA_MEDIA_TELEGRAPH, "published_at": "2026-09-10 09:00:00"},
            {"title": "某公司发布重大资产重组预案", "code": "000002"},
        ]
    )
    kept = filter_recent_hits(hits, as_of="2026-09-21 23:00", lookback_days=2)
    assert {hit.code for hit in kept} == {"002860", "000002"}
