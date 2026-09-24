from pathlib import Path
import shutil

r = Path.cwd()
def write(path, text):
    (r / path).parent.mkdir(parents=True, exist_ok=True)
    (r / path).write_text(text, encoding='utf-8')
def replace(path, old, new):
    p = r / path
    value = p.read_text(encoding='utf-8')
    assert old in value, path
    p.write_text(value.replace(old, new), encoding='utf-8')

replace('GLOSSARY.md', '| **公司大事 / 停牌扫描** | 全市场观察：从已公告与媒体电报（东财关键词 + 财联社）抓重大资产重组、借壳、筹划购买和股票停牌。`scan_corporate_events` / `corporate_event_scan.yml` 对外可见；不读当日 `suspend_d`（停牌常次日才进交易日历）、不进漏斗、不改 OMS。回归样例：星帅尔 002860。 |', '| **公司大事 / 停复牌扫描** | `scan_corporate_events`：东财关键词与财联社最新批次的最近48小时观察，不是全量公告核对。`source_status` 区分 ok/partial/unavailable；否认、终止、复牌及未知状态独立标识；缺失时间不算近期。只写观察产物和通知，不改漏斗或 OMS。见 [扫描契约](docs/CORPORATE_EVENT_SCAN.md)。 |')
replace('README.md', '19 个公开工具', '20 个公开工具')
with (r/'README.md').open('a', encoding='utf-8') as f:
    f.write('''
### 公司大事 / 停复牌观察

`scan_corporate_events(limit=20)` 在 CLI、MCP 和读盘室检索最近 48 小时的重组与停复牌消息。
Python 和 Web 都查东财四个关键词（每词最多两页）与财联社最新电报批次；不是全量公告核对，
也不保证补齐两次调度间滚出电报列表的消息。来源失败不能解释为没有事件。
来源状态、时间窗口与事件状态见 [公司大事扫描契约](docs/CORPORATE_EVENT_SCAN.md)。
''')
write('agents/corporate_event_tools.py', '''"""Agent-facing corporate news observations, with no discovery-time I/O."""

from __future__ import annotations

import logging
from typing import Any

from agents.tool_context import ToolContext
from core.corporate_event_scan import render_corporate_event_report

logger = logging.getLogger(__name__)


def scan_corporate_events(limit: int = 20, tool_context: ToolContext | None = None) -> dict[str, Any]:
    from workflows.corporate_event_scan_runtime import run_corporate_event_scan

    del tool_context
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return {"error": "limit 必须是 1–50 的整数", "hits": []}
    try:
        result = run_corporate_event_scan(persist=False)
    except Exception as exc:
        logger.warning("scan_corporate_events failed (%s)", type(exc).__name__)
        return {
            "error": "消息源暂时不可用，不要据此断定没有停牌或重组。",
            "source_ok": False,
            "source_status": "unavailable",
            "hits": [],
        }
    payload = result.payload()
    payload.update(
        hits=payload["hits"][:limit], undated_hits=payload["undated_hits"][:limit], total_hits=len(result.hits)
    )
    payload["report"] = render_corporate_event_report(
        result.hits[:limit],
        as_of=result.as_of,
        source_status=result.source_status,
        failed_sources=tuple(source.source for source in result.sources if not source.ok),
        undated_count=len(result.undated_hits),
    )
    if result.source_status == "unavailable":
        payload["error"] = "全部消息源不可用；无法判断是否有事件。"
    return payload
''')
shutil.move(r/'integrations/public_mcp/backend.py', r/'agents/public_mcp_backend.py')
shutil.move(r/'integrations/public_mcp/handlers.py', r/'agents/public_mcp_handlers.py')
replace('cli/tools.py', '"扫描已公告与媒体电报中的重大资产重组、借壳、筹划购买与股票停牌。观察结果，不是实盘，也不改漏斗或买卖许可。"', '"检索最近48小时重组与停复牌消息；来源失败、否认/终止与未知时间单列。不保证全量覆盖，不改实盘或漏斗。"')
replace('cli/tools.py', '"description": "最多返回条数，默认 20，最大 50",', '"description": "最多返回条数，默认 20，范围 1–50",\n                    "minimum": 1,\n                    "maximum": 50,')
write('core/corporate_event_scan.py', r'''"""Observation-only classification of corporate news; never an execution signal."""

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
''')
write('core/corporate_event_time.py', '''"""Explicit Shanghai observation windows, independent of the host timezone."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def event_time(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A date-only publication has no known intraday time. Conservatively place
    # it at day end so an 08:15 replay cannot see later announcements.
    if len(text) == 10:
        value = datetime.combine(value.date(), time(23, 59, 59, 999000))
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def observation_cutoff(raw: str) -> datetime:
    value = event_time(raw)
    if value is None:
        raise ValueError("as_of must be an ISO date or datetime; naive values use Asia/Shanghai")
    return value
''')
