from core.corporate_event_scan import XINGSHUAIER_TELEGRAPH
from integrations.cls_telegraph import _normalize_cls
from integrations.corporate_event_sources import collect_corporate_event_items


def test_collect_merges_keyword_news_and_cls(monkeypatch) -> None:
    del monkeypatch
    items = collect_corporate_event_items(
        extra_items=[{"title": "extra"}],
        fetch_news=lambda keyword, pages=2: [{"title": f"{keyword}-row", "pages": pages}],
        fetch_cls=lambda: [{"title": XINGSHUAIER_TELEGRAPH, "source": "财联社"}],
    )
    titles = [item["title"] for item in items.items]
    assert items.status == "ok"
    assert len(items.sources) == 5
    assert XINGSHUAIER_TELEGRAPH in titles
    assert "筹划购买-row" in titles
    assert "extra" in titles


def test_collect_survives_source_failures() -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("upstream down")

    items = collect_corporate_event_items(fetch_news=boom, fetch_cls=boom, extra_items=[{"title": "kept"}])
    assert items.items == [{"title": "kept"}]
    assert items.status == "unavailable"
    assert all(not source.ok for source in items.sources)


def test_normalize_cls_telegraph_extracts_stock() -> None:
    item = _normalize_cls(
        {
            "title": "",
            "content": XINGSHUAIER_TELEGRAPH,
            "ctime": 1758472800,
            "stock_list": [{"StockID": "sz002860", "name": "星帅尔"}],
            "shareurl": "https://www.cls.cn/detail/1",
        }
    )
    assert item["code"] == "002860"
    assert item["name"] == "星帅尔"
    assert item["source"] == "财联社"
    assert item["title"].startswith("星帅尔002860")


import http.client
import json
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest

from core.corporate_event_scan import SEARCH_KEYWORDS
from integrations import cls_telegraph, stock_news_events


@pytest.mark.parametrize("keyword", [*SEARCH_KEYWORDS, "002860"])
def test_eastmoney_real_request_headers_encode_without_network(monkeypatch, keyword):
    def local_response(request, timeout):
        assert timeout > 0
        params = parse_qs(urlparse(request.full_url).query)
        assert json.loads(params["param"][0])["keyword"] == keyword
        # putheader uses the actual HTTP/1 header encoder without opening a socket.
        conn = http.client.HTTPConnection("unused.invalid")
        conn.putrequest("GET", "/")
        for key, value in request.header_items():
            conn.putheader(key, value)
        assert parse_qs(urlparse(request.get_header("Referer")).query)["keyword"] == [keyword]
        return BytesIO(b'jQuery3510({"result":{"cmsArticleWebOld":[]}})')

    monkeypatch.setattr(stock_news_events, "urlopen", local_response)
    assert stock_news_events.fetch_eastmoney_news(keyword, strict=True) == []


@pytest.mark.parametrize("payload", [{}, {"result": {}}, {"result": {"cmsArticleWebOld": None}}])
def test_bad_eastmoney_payload_is_unavailable_not_empty(monkeypatch, payload):
    monkeypatch.setattr(stock_news_events, "_request_news_page", lambda *_: payload)
    collection = collect_corporate_event_items(fetch_cls=lambda: [])
    assert collection.status == "partial"
    assert [source.ok for source in collection.sources] == [False, False, False, False, True]


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {"roll_data": None}}])
def test_bad_cls_payload_is_unavailable_not_empty(monkeypatch, payload):
    monkeypatch.setattr(cls_telegraph, "_request_cls", lambda *_: payload)
    with pytest.raises(ValueError):
        cls_telegraph.fetch_cls_telegraphs()


def test_cls_time_uses_shanghai_even_on_utc_host():
    import os
    import time
    from datetime import datetime
    from unittest.mock import patch

    epoch = int(datetime.fromisoformat("2026-09-21T19:20:00+08:00").timestamp())
    with patch.dict(os.environ, {"TZ": "UTC"}):
        time.tzset()
        assert cls_telegraph._cls_time(epoch) == "2026-09-21T19:20:00+08:00"
    time.tzset()


def test_partial_source_failure_is_not_hidden_or_secret_leaking():
    def fetch(keyword, **_):
        if keyword == "借壳":
            raise RuntimeError("private credential in upstream URL")
        return [{"title": keyword, "published_at": "2026-09-21 08:00:00"}]

    result = collect_corporate_event_items(fetch_news=fetch, fetch_cls=lambda: [])
    assert result.status == "partial"
    assert len(result.items) == 3
    assert result.sources[0].newest_at == "2026-09-21T08:00:00+08:00"
    assert "private credential" not in repr(result)


def test_cls_multiple_stocks_are_not_blindly_attributed_to_first():
    row = _normalize_cls({"content": "重组市场观察", "stock_list": [{"code": "000001"}, {"code": "600825"}]})
    assert row["code"] == ""
