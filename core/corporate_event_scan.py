"""Observation-only classification of corporate news; never an execution signal."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from core.corporate_event_time import event_time, observation_cutoff, resume_effective_date

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
_PLANNED_RESUME = re.compile(
    r"(?:将|拟|计划|预计|申请|安排|定于|自|于|明日|明天|后日|后天|次日|下一交易日|下个交易日)[^，；。]{0,32}复牌"
)
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
    related_codes: tuple[str, ...] = ()
    subject_status: str = "unknown"
    effective_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "related_codes": list(self.related_codes)}


def is_material_restructure_or_halt(title: str, content: str = "") -> bool:
    text = f"{title} {content}"
    return any(word in text for word in (*MATERIAL_PHRASES, *EQUITY_PHRASES)) or (
        any(word in text for word in ("停牌", "复牌")) and any(word in text for word in HALT_CONTEXT)
    )


def corporate_event_status(title: str, content: str = "") -> tuple[str, str]:
    # The headline expresses the current update; the body may quote an earlier
    # halt/restructure that has since been denied or cancelled.
    text = _status_text(title, content)
    resumed = "复牌" in text and not _NOT_RESUMED.search(text)
    resume_announced = (
        resumed and bool(_PLANNED_RESUME.search(text)) and not bool(re.search(r"已(?:于[^，；。]{0,20})?复牌", text))
    )
    halt = (
        "not_halted"
        if _NOT_HALTED.search(text)
        else "resume_announced"
        if resume_announced
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
        return "resumption_announced" if resume_announced else "resumed", halt
    if any(word in text for word in ("筹划", "预案", "拟购买", "拟收购", "审议通过", "股票停牌")):
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


def _status_text(title: str, content: str) -> str:
    current = is_material_restructure_or_halt(title) or _DENIED.search(title) or _TERMINATED.search(title)
    return title if current or "复牌" in title else f"{title} {content}"


def _mentioned_codes(text: str) -> set[str]:
    return {
        match.group("code")
        for match in _LOOSE_CODE.finditer(text)
        if not re.search(r"(?:编号|金额|订单|日期)[：: ]*$", text[: match.start()])
    }


def parse_telegraph_symbol(title: str) -> tuple[str, str]:
    text = title.strip()
    codes = _mentioned_codes(text)
    if len(codes) != 1:
        return "", ""
    code = next(iter(codes))
    match = _TELEGRAPH.match(text) or _PAREN_CODE.search(text)
    return code, match.group("name") if match and match.group("code") == code else ""


def _event_subject(item: dict[str, Any], title: str, content: str) -> tuple[str, str, tuple[str, ...], str]:
    declared = normalize_stock_code(item.get("code"))
    mentioned = _mentioned_codes(title) | _mentioned_codes(content)
    raw_related = item.get("related_codes") or []
    related = (
        {normalize_stock_code(value) for value in raw_related} if isinstance(raw_related, (list, tuple)) else set()
    )
    codes = tuple(sorted((mentioned | related | {declared}) - {""}))
    if declared and mentioned and declared not in mentioned:
        return "", "", codes, "conflict"
    if len(codes) != 1:
        return "", "", codes, "ambiguous" if codes else "unknown"
    code = codes[0]
    parsed_code, name = parse_telegraph_symbol(title)
    if not parsed_code:
        _, name = parse_telegraph_symbol(content)
    source_name = str(item.get("name") or "").strip() if declared == code else ""
    return code, source_name or name, codes, "single_candidate"


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
    source_details: list[dict[str, Any]] | None = None,
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
    for source in source_details or []:
        failed = [str(page["page"]) for page in source.get("failed_pages", [])]
        if failed or source.get("rejected_items", 0):
            lines.append(
                f"来源 {source['source']}：失败页 {','.join(failed) or '无'}，保留 {source['item_count']} 条，隔离 {source.get('rejected_items', 0)} 条无效记录。"
            )
        if source.get("coverage_status") == "possibly_truncated":
            lines.append(f"来源 {source['source']} 为有界批次，可能截断；不代表48小时覆盖完整。")
    if undated_count:
        lines.append(f"另有 {undated_count} 条发布时间缺失或无效，未计入近期结果，需核实时间。")
    if not hits and source_status != "unavailable":
        lines.append("本次检索窗口未命中；不代表没有停牌或重组。")
    for hit in hits:
        label = f"{hit.code} {hit.name}".strip() or "未解析代码"
        lines.append(f"- {label} | {hit.reason} / {hit.halt_status} | {hit.source} {hit.published_at} | {hit.title}")
        if hit.subject_status in {"ambiguous", "conflict"}:
            lines.append(f"  主体未确认（{hit.subject_status}），相关代码：{', '.join(hit.related_codes)}")
        if hit.halt_status == "resume_announced":
            lines.append(f"  复牌安排：{hit.effective_date or '生效日期待核实'}；不代表当前已复牌或可交易。")
        if hit.url.startswith(("https://", "http://")):
            lines.append(f"  原文：{hit.url}")
    return "\n".join(lines)


def _hit_from_item(item: dict[str, Any]) -> CorporateEventHit | None:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "")
    if not title or not is_material_restructure_or_halt(title, content):
        return None
    code, name, related, subject_status = _event_subject(item, title, content)
    status, halt = corporate_event_status(title, content)
    return CorporateEventHit(
        code=code,
        name=name,
        title=title,
        reason=classify_corporate_event_reason(title, content),
        source=str(item.get("source") or "media"),
        published_at=str(item.get("published_at") or item.get("date") or ""),
        event_status=status,
        halt_status=halt,
        url=str(item.get("url") or ""),
        related_codes=related,
        subject_status=subject_status,
        effective_date=resume_effective_date(
            _status_text(title, content), str(item.get("published_at") or item.get("date") or "")
        )
        if halt == "resume_announced"
        else "",
    )
