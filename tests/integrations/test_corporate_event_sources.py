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
    titles = [item["title"] for item in items]
    assert XINGSHUAIER_TELEGRAPH in titles
    assert "筹划购买-row" in titles
    assert "extra" in titles


def test_collect_survives_source_failures() -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("upstream down")

    items = collect_corporate_event_items(fetch_news=boom, fetch_cls=boom, extra_items=[{"title": "kept"}])
    assert items == [{"title": "kept"}]


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
