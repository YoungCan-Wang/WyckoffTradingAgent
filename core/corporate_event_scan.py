"""公司大事 / 停牌扫描：只观察已公告与媒体电报，不改实盘。

漏斗、新闻打点原先只在「已经打开某只股票」时按个股检索；盘后财联社电报
（如星帅尔 002860 筹划购买 + 股票停牌）没有全市场入口，就会整条漏掉。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

XINGSHUAIER_TELEGRAPH = "星帅尔002860：筹划购买PCB刀具设备公司湘鹰新材料等100%股权，股票停牌"
XINHUA_MEDIA_TELEGRAPH = "新华传媒600825：筹划重大资产重组，股票停牌"

MATERIAL_PHRASES = (
    "重大资产重组",
    "资产重组",
    "并购重组",
    "借壳",
    "筹划购买",
)
EQUITY_PHRASES = ("100%股权", "全部股权", "控股权")
HALT_WORDS = ("股票停牌", "停牌")
HALT_CONTEXT = ("重组", "并购", "借壳", "筹划", "收购", "购买", "股权")
SEARCH_KEYWORDS = (
    "重大资产重组",
    "股票停牌",
    "筹划购买",
    "借壳",
)
_TELEGRAPH = re.compile(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9*＊]{2,12})?(?P<code>\d{6})[：:](?P<body>.+)")
_PAREN_CODE = re.compile(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9*＊]{2,12})[（(](?P<code>\d{6})[)）]")
_LOOSE_CODE = re.compile(r"(?P<name>[\u4e00-\u9fff]{2,8})?(?P<code>\d{6})")


@dataclass(frozen=True)
class CorporateEventHit:
    code: str
    name: str
    title: str
    reason: str
    source: str
    published_at: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def is_material_restructure_or_halt(title: str, content: str = "") -> bool:
    text = f"{title} {content}"
    if any(phrase in text for phrase in MATERIAL_PHRASES):
        return True
    if any(phrase in text for phrase in EQUITY_PHRASES):
        return True
    return any(word in text for word in HALT_WORDS) and any(word in text for word in HALT_CONTEXT)


def classify_corporate_event_reason(title: str, content: str = "") -> str:
    text = f"{title} {content}"
    halted = any(word in text for word in HALT_WORDS)
    restructure = any(phrase in text for phrase in (*MATERIAL_PHRASES, *EQUITY_PHRASES, "重组", "并购", "借壳"))
    if halted and restructure:
        return "halt_and_restructure"
    if restructure:
        return "restructure"
    if halted:
        return "halt_with_deal"
    return "material_deal"


def parse_telegraph_symbol(title: str) -> tuple[str, str]:
    text = title.strip()
    match = _TELEGRAPH.match(text) or _PAREN_CODE.search(text) or _LOOSE_CODE.search(text)
    if not match:
        return "", ""
    return str(match.group("code") or ""), str(match.group("name") or "")


def filter_recent_hits(
    hits: list[CorporateEventHit],
    *,
    as_of: str,
    lookback_days: int = 2,
) -> list[CorporateEventHit]:
    end = _parse_day(as_of)
    if end is None:
        return list(hits)
    start = end - timedelta(days=max(int(lookback_days), 0))
    kept: list[CorporateEventHit] = []
    for hit in hits:
        day = _parse_day(hit.published_at)
        if day is None or start <= day <= end:
            kept.append(hit)
    return kept


def scan_corporate_events(items: list[dict[str, Any]]) -> list[CorporateEventHit]:
    hits: list[CorporateEventHit] = []
    seen: set[str] = set()
    for item in items:
        hit = _hit_from_item(item)
        if hit is None:
            continue
        key = f"{hit.code}:{_title_key(hit.title)}"
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
    hits.sort(key=lambda row: (row.published_at, row.code, row.title), reverse=True)
    return hits


def render_corporate_event_report(hits: list[CorporateEventHit], *, as_of: str) -> str:
    lines = [
        f"公司大事 / 停牌扫描 {as_of}",
        "已公告与媒体电报观察，不是实盘，也不是漏斗买许可。",
        "",
    ]
    if not hits:
        lines.append("当日未扫到重大资产重组 / 停牌电报。")
        return "\n".join(lines)
    for hit in hits:
        label = f"{hit.code} {hit.name}".strip() or hit.code or "未解析代码"
        lines.append(f"- {label} | {hit.reason} | {hit.source} {hit.published_at} | {hit.title}")
    return "\n".join(lines)


def _hit_from_item(item: dict[str, Any]) -> CorporateEventHit | None:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "")
    if not title or not is_material_restructure_or_halt(title, content):
        return None
    parsed_code, parsed_name = parse_telegraph_symbol(title)
    if not parsed_code:
        parsed_code, parsed_name = parse_telegraph_symbol(content)
    return CorporateEventHit(
        code=_six_digit(item.get("code")) or parsed_code,
        name=str(item.get("name") or "").strip() or parsed_name,
        title=title,
        reason=classify_corporate_event_reason(title, content),
        source=str(item.get("source") or "media"),
        published_at=str(item.get("published_at") or item.get("date") or ""),
    )


def _six_digit(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits if len(digits) == 6 else ""


def _parse_day(raw: str) -> date | None:
    text = str(raw or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _title_key(title: str) -> str:
    return "".join(ch for ch in title.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")[:32]
