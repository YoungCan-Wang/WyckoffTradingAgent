from core.news_chart_events import classify_headline, select_news_chart_events, snap_to_session


def test_classify_keeps_shareholding_deal_and_drops_limit_up_noise() -> None:
    classified = classify_headline("中际旭创拟超17亿元入股中石科技")
    assert classified is not None
    assert classified[0] == "deal"
    assert classified[1] == "bullish"
    assert classify_headline("300684，20cm三连板！") is None
    assert classify_headline("20%涨停！万亿巨头17亿重仓杀入", "拟入股中石科技") is None
    assert classify_headline("万亿龙头入股！300684，20cm两连板！") is None
    assert classify_headline("净利同比增长10.92倍！盘后公告集锦") is None


def test_select_news_chart_events_snaps_weekend_and_keeps_one_per_day() -> None:
    events = select_news_chart_events(
        [
            {
                "title": "中际旭创拟超17亿元入股中石科技",
                "content": "战略入股",
                "published_at": "2026-08-15 22:38:00",
            },
            {
                "title": "中际旭创继续增资入股中石科技",
                "content": "入股细节",
                "published_at": "2026-08-17 09:00:00",
            },
            {
                "title": "公司收到证监会立案调查通知",
                "published_at": "2026-08-17 10:00:00",
            },
        ],
        start="2026-08-01",
        end="2026-08-20",
        session_dates=["2026-08-14", "2026-08-17", "2026-08-18"],
        limit=8,
    )
    assert [item.date for item in events] == ["2026-08-17"]
    assert events[0].kind == "regulatory"


def test_snap_to_session_uses_next_bar() -> None:
    assert snap_to_session("2026-08-16", ["2026-08-14", "2026-08-17"]) == "2026-08-17"


def test_select_requires_symbol_or_name_mention() -> None:
    events = select_news_chart_events(
        [
            {
                "title": "某机构上调行业评级",
                "content": "光纤连接器需求回暖",
                "published_at": "2026-08-13 10:00:00",
            },
            {
                "title": "中石科技回购42万股",
                "content": "回购金额1999万元",
                "published_at": "2026-08-05 16:00:00",
            },
        ],
        start="2026-08-01",
        end="2026-08-20",
        session_dates=["2026-08-05", "2026-08-13"],
        symbol="300684",
        name="中石科技",
    )
    assert [item.kind for item in events] == ["holder"]
