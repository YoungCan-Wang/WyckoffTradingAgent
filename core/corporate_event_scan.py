"""Observation-only classification of corporate news; never an execution signal."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from core.corporate_event_time import event_time, observation_cutoff

XINGSHUAIER_TELEGRAPH = "星帅尔002860：筹划购买PCB刀具设备公司湘鹰新材料等100%股权，股票停牌"
XINHUA_MEDIA_TELEGRAPH = "新华传媒600825：筹划重大资产重组，股票停牌"
MATERIAL_PHRASES = ("重大资产重组", "资产重组", "并购重组", "借壳", "筹划购买")
EQUITY_PHRASES = ("100%股权", "全部股权", "控股权")
HALT_CONTEXT = ("重组", "并购", "借壳", "筹划", "收购", "购买", "股权")
SEARCH_KEYWORDS = ("重大资产重组", "股票停牌", "筹划购买", "借壳")
_CODE = r"(?P<code>[034689]\d{5})(?!\d)"
_NAME = r"(?P<name>[\u4e00-\u9fffA-Za-z*＊]{2,12})"
_TELEGRAPH = re.compile(r"^" + _NAME + r"\s*" + _CODE + r"[：:]")
_PAREN_CODE = re.compile(_NAME + r"[（(]" + _CODE + r"[)）]")
_LOOSE_CODE = re.compile(r"(?<![\dA-Za-z])" + _CODE + r"(?![A-Za-z])")
_DENIED = re.compile(
    r"(?:未|没有|不存在|不)(?:正在)?(?:筹划|涉及|进行)[^，；。]{0,16}(?:重组|并购|借壳|购买)|否认[^，；。]{0,20}(?:重组|借壳)|(?:重组|借壳)[^，；。]{0,12}(?:不实|无依据)"
)
_TERMINATED = re.compile(r"(?:终止|取消|不再推进)[^，；。]{0,16}(?:重组|并购|购买|收购|交易)")
_NOT_HALTED = re.compile(r"(?:不|无需|不会|未|不涉及)(?:申请)?停牌")
_NOT_RESUMED = re.compile(r"(?:未|不|暂不|尚未)复牌")
SOURCE_NOTE = "来源为东财关键词检索与财联社最新电报，不是全量公告核对；未命中不等于无事件。"


@dataclass(frozen=True)
class CorporateEventHit:
    code: str
    name: str
    title: str
    reason: str
    source: str
    published_at: str
    event_status: str = "unknown"
    halt_status: str = "unknown"
    url: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def is_material_restructure_or_halt(title: str, content: str = "") -> bool:
    text = f"{title} {content}"
    return any(word in text for word in (*MATERIAL_PHRASES, *EQUITY_PHRASES)) or (
        any(word in text for word in ("停牌", "复牌")) and any(word in text for word in HALT_CONTEXT)
    )


def corporate_event_status(title: str, content: str = "") -> tuple[str, str]:
    # The headline expresses the current update; the body may quote an earlier
    # halt/restructure that has since been denied or cancelled.
    text = title if is_material_restructure_or_halt(title) else f"{title} {content}"
    resumed = "复牌" in text and not _NOT_RESUMED.search(text)
    halt = (
        "not_halted"
        if _NOT_HALTED.search(text)
        else "resumed"
        if resumed
        else "announced"
        if "停牌" in text
        else "unknown"
    )
    if _DENIED.search(text):
        return "denied", halt
    if _TERMINATED.search(text):
        return "terminated", halt
    if resumed:
        return "resumed", halt
    if (
        any(word in text for word in ("筹划", "预案", "拟购买", "拟收购", "审议通过", "股票停牌"))
        and halt != "not_halted"
    ):
        return "announced", halt
    return "unknown", halt


def classify_corporate_event_reason(title: str, content: str = "") -> str:
    status, halt = corporate_event_status(title, content)
    if status != "announced":
        return status
    text = f"{title} {content}"
    restructure = any(word in text for word in (*MATERIAL_PHRASES, *EQUITY_PHRASES, "重组", "并购", "借壳"))
    if halt == "announced":
        return "halt_and_restructure" if restructure else "halt_with_deal"
    return "restructure" if restructure else "material_deal"


def normalize_stock_code(raw: Any) -> str:
    match = re.fullmatch(r"(?i)(?:sh|sz|bj)?([034689]\d{5})(?:\.(?:sh|sz|bj))?", str(raw or "").strip())
    return match.group(1) if match else ""


def parse_telegraph_symbol(title: str) -> tuple[str, str]:
    text = title.strip()
    match = _TELEGRAPH.match(text) or _PAREN_CODE.search(text)
    if match:
        return match.group("code"), match.group("name")
    # Multiple codes and numeric identifiers must not be attributed to the first
    # apparent six digits. Unknown is preferable to inventing a traded symbol.
    matches = list(_LOOSE_CODE.finditer(text))
    if len(matches) != 1 or re.search(r"(?:编号|金额|订单|日期)[：: ]*$", text[: matches[0].start()]):
        return "", ""
    return matches[0].group("code"), ""


def filter_recent_hits(hits: list[CorporateEventHit], *, as_of: str, lookback_days: int = 2) -> list[CorporateEventHit]:
    end = observation_cutoff(as_of)
    start = end - timedelta(days=max(int(lookback_days), 0))
    return [hit for hit in hits if (stamp := event_time(hit.published_at)) is not None and start <= stamp <= end]


def scan_corporate_events(items: list[dict[str, Any]]) -> list[CorporateEventHit]:
    hits: list[CorporateEventHit] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        hit = _hit_from_item(item)
        if hit is None:
            continue
        key = (hit.code, re.sub(r"\W", "", hit.title.lower()), hit.published_at)
        if key not in seen:
            seen.add(key)
            hits.append(hit)
    hits.sort(
        key=lambda row: (
            event_time(row.published_at).timestamp() if event_time(row.published_at) else float("-inf"),
            row.code,
        ),
        reverse=True,
    )
    return hits


def render_corporate_event_report(
    hits: list[CorporateEventHit],
    *,
    as_of: str,
    source_status: str = "ok",
    failed_sources: tuple[str, ...] = (),
    undated_count: int = 0,
) -> str:
    lines = [
        f"公司大事 / 停牌扫描 {as_of}（Asia/Shanghai）",
        "已公告与媒体电报观察，不是实盘，也不是漏斗买许可。",
        SOURCE_NOTE,
    ]
    if source_status != "ok":
        lines.append(
            "全部消息源不可用，无法判断是否有事件。"
            if source_status == "unavailable"
            else "部分消息源不可用，以下仅为已获取的观察结果。"
        )
        lines.append("不可用来源：" + "、".join(failed_sources))
    if undated_count:
        lines.append(f"另有 {undated_count} 条发布时间缺失或无效，未计入近期结果，需核实时间。")
    if not hits and source_status != "unavailable":
        lines.append("本次检索窗口未命中；不代表没有停牌或重组。")
    for hit in hits:
        label = f"{hit.code} {hit.name}".strip() or "未解析代码"
        lines.append(f"- {label} | {hit.reason} / {hit.halt_status} | {hit.source} {hit.published_at} | {hit.title}")
        if hit.url.startswith(("https://", "http://")):
            lines.append(f"  原文：{hit.url}")
    return "\n".join(lines)


def _hit_from_item(item: dict[str, Any]) -> CorporateEventHit | None:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "")
    if not title or not is_material_restructure_or_halt(title, content):
        return None
    code, name = parse_telegraph_symbol(title)
    if not code:
        code, name = parse_telegraph_symbol(content)
    status, halt = corporate_event_status(title, content)
    return CorporateEventHit(
        code=normalize_stock_code(item.get("code")) or code,
        name=str(item.get("name") or "").strip() or name,
        title=title,
        reason=classify_corporate_event_reason(title, content),
        source=str(item.get("source") or "media"),
        published_at=str(item.get("published_at") or item.get("date") or ""),
        event_status=status,
        halt_status=halt,
        url=str(item.get("url") or ""),
    )
