from integrations.stock_news_events import _normalize_article, _parse_jsonp, load_news_chart_events


def test_parse_jsonp_and_normalize_article() -> None:
    payload = _parse_jsonp(
        'jQuery3510({"result":{"cmsArticleWebOld":[{"date":"2026-08-13 22:38:00","title":"入股","code":"abc","mediaName":"财联社"}]}})'
    )
    article = _normalize_article(payload["result"]["cmsArticleWebOld"][0])
    assert article["title"] == "入股"
    assert article["url"].endswith("/a/abc.html")


def test_load_news_chart_events_skips_non_ashare_codes(monkeypatch) -> None:
    monkeypatch.setattr("integrations.stock_news_events.fetch_eastmoney_stock_news", lambda _code: [_article()])
    assert load_news_chart_events("AAPL", "2026-08-01", "2026-08-20", ["2026-08-13"]) == []


def test_load_news_chart_events_filters_with_local_classifier(monkeypatch) -> None:
    monkeypatch.setattr(
        "integrations.stock_news_events.fetch_eastmoney_stock_news",
        lambda _code: [_article(), {"title": "300684三连板", "published_at": "2026-08-18 11:00:00"}],
    )
    events = load_news_chart_events("300684", "2026-08-01", "2026-08-20", ["2026-08-13", "2026-08-18"], name="中石科技")
    assert len(events) == 1
    assert events[0]["kind"] == "deal"
    assert events[0]["date"] == "2026-08-13"


def _article() -> dict[str, str]:
    return {
        "title": "中际旭创拟超17亿元入股中石科技",
        "content": "战略入股",
        "published_at": "2026-08-13 22:38:00",
        "source": "财联社",
        "url": "https://finance.eastmoney.com/a/x.html",
    }
