"""Read-disk news overlay: classify headlines onto a price chart.

This is not a funnel gate. It only ranks already-published articles so the
analysis chart can show a few fundamental/sentiment turning points.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

KIND_WEIGHTS = {
    "regulatory": 5,
    "risk": 4,
    "earnings": 4,
    "holder": 3,
    "deal": 3,
}
KIND_KEYWORDS = {
    "regulatory": ("立案", "调查", "处罚", "问询函", "证监会", "监管函"),
    "risk": ("风险提示", "停牌", "退市", "债务违约", "暴雷"),
    "earnings": ("业绩预增", "业绩预减", "业绩预亏", "扭亏", "年报", "中报", "一季报", "三季报"),
    "holder": ("减持", "增持", "回购", "股权激励"),
    "deal": ("中标", "签订合同", "战略投资", "入股", "定增", "收购"),
}
BULLISH_KEYWORDS = ("预增", "扭亏", "增持", "回购", "中标", "入股", "超预期", "增长")
BEARISH_KEYWORDS = ("预减", "预亏", "减持", "立案", "调查", "处罚", "问询", "下滑", "违约")
NOISE_ONLY = ("涨停", "跌停", "连板", "龙虎榜", "20cm", "一字")
ROUNDUP_TITLES = ("集锦", "一览", "股今日获", "龙虎榜")


@dataclass(frozen=True)
class NewsChartEvent:
    date: str
    kind: str
    sentiment: str
    title: str
    summary: str
    source: str
    url: str
    score: int


def select_news_chart_events(
    items: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    session_dates: list[str],
    limit: int = 8,
    symbol: str = "",
    name: str = "",
) -> list[NewsChartEvent]:
    start_day, end_day = _parse_day(start), _parse_day(end)
    if start_day is None or end_day is None or end_day < start_day:
        return []
    sessions = [day for day in session_dates if _parse_day(day)]
    scored: list[NewsChartEvent] = []
    seen: set[str] = set()
    for item in items:
        event = _event_from_item(item, start_day, end_day, sessions, seen, symbol, name)
        if event:
            scored.append(event)
    scored.sort(key=lambda event: (-event.score, event.date, event.title))
    return _one_per_day(scored)[: max(int(limit), 0)]


def events_as_dicts(events: list[NewsChartEvent]) -> list[dict[str, Any]]:
    return [asdict(event) for event in events]


def classify_headline(title: str, content: str = "") -> tuple[str, str, int] | None:
    text = f"{title} {content}"
    if any(word in title for word in (*ROUNDUP_TITLES, *NOISE_ONLY)):
        return None
    kind = next((name for name, words in KIND_KEYWORDS.items() if any(word in text for word in words)), "")
    if not kind:
        return None
    bullish = any(word in text for word in BULLISH_KEYWORDS)
    bearish = any(word in text for word in BEARISH_KEYWORDS)
    sentiment = "mixed" if bullish and bearish else "bullish" if bullish else "bearish" if bearish else "unknown"
    extra = sum(1 for word in (*BULLISH_KEYWORDS, *BEARISH_KEYWORDS) if word in text)
    return kind, sentiment, KIND_WEIGHTS[kind] + extra


def snap_to_session(raw_date: str, session_dates: list[str]) -> str:
    day = _parse_day(raw_date)
    if day is None:
        return ""
    needle = day.isoformat()
    for session in session_dates:
        if session >= needle:
            return session
    return session_dates[-1] if session_dates else ""


def _event_from_item(
    item: dict[str, Any],
    start_day: date,
    end_day: date,
    sessions: list[str],
    seen: set[str],
    symbol: str,
    name: str,
) -> NewsChartEvent | None:
    title = str(item.get("title") or "").strip()
    published = _parse_day(str(item.get("published_at") or item.get("date") or ""))
    if not title or published is None or published < start_day or published > end_day:
        return None
    if not _mentions_symbol(title, str(item.get("content") or ""), symbol, name):
        return None
    classified = classify_headline(title, str(item.get("content") or ""))
    if classified is None:
        return None
    kind, sentiment, score = classified
    session = snap_to_session(published.isoformat(), sessions)
    if not session:
        return None
    fingerprint = f"{session}:{_title_key(title)}"
    if fingerprint in seen:
        return None
    seen.add(fingerprint)
    summary = str(item.get("content") or title).strip().replace("\n", " ")
    return NewsChartEvent(
        date=session,
        kind=kind,
        sentiment=sentiment,
        title=title,
        summary=summary[:80],
        source=str(item.get("source") or "eastmoney"),
        url=str(item.get("url") or ""),
        score=score,
    )


def _one_per_day(events: list[NewsChartEvent]) -> list[NewsChartEvent]:
    kept: list[NewsChartEvent] = []
    used_days: set[str] = set()
    for event in events:
        if event.date in used_days:
            continue
        used_days.add(event.date)
        kept.append(event)
    return kept


def _mentions_symbol(title: str, content: str, symbol: str, name: str) -> bool:
    if not symbol and not name:
        return True
    text = f"{title} {content[:80]}"
    return bool(symbol and symbol in text) or bool(name and name in text)


def _title_key(title: str) -> str:
    return "".join(ch for ch in title.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")[:24]


def _parse_day(raw: str) -> date | None:
    text = str(raw or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
