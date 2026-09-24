replace('docs/ARCHITECTURE.md', 'MCP（19）', 'MCP（20）')
replace('docs/ARCHITECTURE.md', '所有 19 个工具', '所有 20 个工具')
replace('docs/ARCHITECTURE.md', '两档 cron 各自完整拉源', '两档 cron 各自独立检索（非全量公告核对）')
with (r/'docs/ARCHITECTURE.md').open('a', encoding='utf-8') as f:
    f.write('''
### 公司大事观察的数据契约

CLI/MCP 与 Web 均使用 48 小时精确截止窗口和上海时区，采集东财四词各最多两页及财联社最新批次。
来源错误逐项传播，不以空列表掩盖失败；否认/终止/复牌与时间未知项不会伪装成新发生的停牌。
扫描脚本在来源降级时返回非零退出码，并发送明确标注数据源异常的报告。详见 [扫描契约](CORPORATE_EVENT_SCAN.md)。
MCP 的业务 backend/handlers 位于 `agents/public_mcp_*`，由顶层 `mcp_server.py` 注入；
`integrations/public_mcp/` 只保留协议、契约与参数/权限边界，不向上导入业务实现。
''')
replace('docs/A_SHARE_FUNNEL_FLOW.md', '所以盘后电报必须走这条全市场入口。', '该入口仅检索有限媒体来源，不能据零命中断言无事件；来源状态与精确48小时窗口见 [扫描契约](CORPORATE_EVENT_SCAN.md)。')
replace('docs/PUBLIC_MCP.md', 'the 19 existing tool names', '20 public tool names (including `scan_corporate_events`)')
replace('docs/PUBLIC_MCP.md', '- `backend.py`: lazy principal/context initialization and the shared production `ToolSurface`.', '- `agents/public_mcp_backend.py`: lazy principal/context initialization and the shared production `ToolSurface`.')
replace('docs/PUBLIC_MCP.md', '- `handlers.py`: the existing funnel parameter adaptation, not a replacement trading algorithm.', '- `agents/public_mcp_handlers.py`: the existing funnel parameter adaptation, not a replacement trading algorithm.')
with (r/'docs/PUBLIC_MCP.md').open('a', encoding='utf-8') as f:
    f.write('''
The executable `mcp_server.py` injects the lazy backend factory into the protocol runtime.
The `integrations` layer never imports business modules. Calling an uncomposed `Runtime()`
returns `BACKEND_NOT_CONFIGURED`; discovery remains side-effect-free. Public launch commands are unchanged.
`scan_corporate_events` is read-only, accepts an integer limit of 1–50, and returns an MCP tool
error when all sources fail. Partial coverage is returned with explicit source warnings, never as a clean scan.
See [the observation contract](CORPORATE_EVENT_SCAN.md).
''')
replace('docs/README_EN.md', 'Market-wide announced restructure / halt scan from media telegrams; observation only, not a live or funnel buy gate', 'Bounded 48-hour restructure/halt news observations; explicit source health, denied/terminated/resumed states, no trading writes')
with (r/'docs/README_EN.md').open('a', encoding='utf-8') as f:
    f.write('''
Corporate-event observations use the same bounded East Money keyword searches and latest CLS batch in
Python and Web. `source_status` is `ok`, `partial`, or `unavailable`; a source failure never means no
corporate events. Undated results are excluded from recent hits. Shanghai-local cutoffs include time of day.
See [the corporate-event contract](CORPORATE_EVENT_SCAN.md) for coverage limits and job exit codes.
''')
write('docs/CORPORATE_EVENT_SCAN.md', '''# 公司大事 / 停复牌观察契约

## 来源与覆盖

CLI、MCP、Web 同名工具都请求东方财富四个关键词：重大资产重组、股票停牌、筹划购买、借壳，
每词最多两页（每页20条）；另取财联社最新电报批次。不遍历全部上市公司，不核对全量交易所公告，
也不保证补齐调度间已滚出最新电报列表的记录。两次 cron 是独立检索，不是接续消费的可靠队列。
东财请求每页12秒超时，财联社8秒超时；各来源独立失败。源码之外的网络可用性必须单独验收。

`source_status` 为 `ok`（所有请求成功）、`partial`（部分失败）或 `unavailable`（全部失败）。
`source_ok` 仅在 `ok` 时为真；它不承诺市场覆盖完整。JSON 返回每个来源的成功状态、条数、
已见最早/最晚发布时间和脱敏错误类别。额外注入的回归样例不把失效的数据源变成健康来源。
合法空数组是零条数据；HTTP/网络失败、无效 JSON 或响应结构不能当成正常零条。

全部失败时报告明确说明无法判断，MCP 返回 `isError`；部分失败仍保留已获得的观察结果并展示告警。
正常零命中也只能说明本次检索未命中，不等于没有停牌或重组。

## 时间

窗口为 `[as_of - 48小时, as_of]`，包含两端。`as_of` 在发起采集前确定，比较到具体时间而非只比较日期。
无时区时间按 `Asia/Shanghai` 解释；带 `Z` 或 UTC offset 的时间按绝对时间比较。
日期形式的截止值按上海当日 `23:59:59.999` 解释；只有日期的发布值也保守放到当日末尾，
不能提前出现在当天08:15的重放里。缺失或非法的发布时间放入 `undated_hits`，不算近期命中。
CLI/MCP JSON 中有 `as_of`、`timezone`、`lookback_hours`；Web 文本保留上海时区与来源异常提示。

## 事件与代码

`event_status` 区分 `announced`、`denied`、`terminated`、`resumed`、`unknown`。
`halt_status` 区分 `announced`、`not_halted`、`resumed`、`unknown`。分类首先考虑当前标题，
避免正文引用的历史停牌覆盖当前否认/终止更新。规则不能替代原文核证；模糊消息标记未知。
正文含重组关键词不等于存在当前重组，更不等于可执行买单。

代码必须满足独立六位及支持的代码格式，不从长公告编号截出六位。单条新闻有多个未消歧代码，
或财联社关联多只股票时，不直接拿第一只当事件主体。此处格式校验不是上市证券主表认证。
原文 URL 随命中保留，完整标题及时间参与去重，避免把相同前缀的不同更新吞掉。
Python/TypeScript 共用 `tests/fixtures/corporate_event_cases.json` 的回归样例。

## 调度与副作用

`corporate_event_scan.yml` 计划在北京时间19:40、08:15触发；实际执行可能延迟，cron间隔不构成顺序。
脚本只写 Markdown/JSON 观察产物和飞书通知，不改持仓、交易信号、候选池或 OMS。
飞书通知标题在来源降级时加“数据源异常”。退出码0代表来源健康且所要求通知成功，
1代表通知失败或未配置，2代表来源部分或全部不可用；来源异常优先。`--dry-run` 不通知，
但仍会采集并写产物，也仍会因来源异常非零退出；不是离线模式。

运行入口为 `python scripts/corporate_event_scan_job.py`。CLI/MCP/Web 工具不写产物，
`limit` 为1–50的整数，默认20。它限制返回条数，不扩大上游覆盖。
''')
replace('integrations/stock_news_events.py', 'from urllib.parse import urlencode', 'from urllib.parse import quote, urlencode')
replace('integrations/stock_news_events.py', 'def fetch_eastmoney_news(keyword: str, *, pages: int = _MAX_PAGES)', 'def fetch_eastmoney_news(keyword: str, *, pages: int = _MAX_PAGES, strict: bool = False)')
replace('integrations/stock_news_events.py', '        if not isinstance(batch, list) or not batch:\n            break', '        if not isinstance(batch, list):\n            if strict:\n                raise ValueError("Invalid East Money news payload")\n            break\n        if strict and any(not isinstance(item, dict) for item in batch):\n            raise ValueError("Invalid East Money article")\n        if not batch:\n            break')
replace('integrations/stock_news_events.py', 'keyword={keyword}', "keyword={quote(keyword, safe='')}")
replace('integrations/stock_news_events.py', 'str(item.get("date") or "")[:19]', 'str(item.get("date") or "")')
write('integrations/cls_telegraph.py', '''"""财联社最新电报批次；来源失败交给采集层记录，不伪装成空结果。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.corporate_event_scan import normalize_stock_code
from core.corporate_event_time import SHANGHAI

CLS_TELEGRAPH_URL = "https://www.cls.cn/nodeapi/updateTelegraphList"
_TIMEOUT_SECONDS = 8


def fetch_cls_telegraphs(*, timeout: int = _TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    payload = _request_cls(timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("roll_data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Invalid CLS telegraph payload")
    if any(not isinstance(item, dict) for item in rows):
        raise ValueError("Invalid CLS telegraph row")
    return [_normalize_cls(item) for item in rows]


def _request_cls(timeout: int) -> dict[str, Any]:
    params = urlencode({"app": "CailianpressWeb", "os": "web", "sv": "8.4.6"})
    request = Request(
        f"{CLS_TELEGRAPH_URL}?{params}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/telegraph"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, dict) else {}


def _normalize_cls(item: dict[str, Any]) -> dict[str, Any]:
    first = _first_stock(item)
    title = str(item.get("title") or item.get("brief") or "").strip()
    content = str(item.get("content") or item.get("descr") or "").strip()
    return {
        "title": title or content[:80],
        "content": content,
        "code": _stock_code(first),
        "name": str(first.get("name") or first.get("secu_name") or "").strip(),
        "source": "财联社",
        "published_at": _cls_time(item.get("ctime") or item.get("time")),
        "url": str(item.get("shareurl") or item.get("url") or ""),
    }


def _first_stock(item: dict[str, Any]) -> dict[str, Any]:
    rows = item.get("stock_list") or item.get("stocks") or []
    return rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}


def _stock_code(stock: dict[str, Any]) -> str:
    return normalize_stock_code(stock.get("StockID") or stock.get("code"))


def _cls_time(raw: Any) -> str:
    try:
        stamp = int(raw)
    except (TypeError, ValueError):
        return str(raw or "")
    if stamp > 10**12:
        stamp //= 1000
    try:
        return datetime.fromtimestamp(stamp, tz=UTC).astimezone(SHANGHAI).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""
''')
write('integrations/corporate_event_sources.py', '''"""Bounded news collection with explicit per-source failure provenance."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any

from core.corporate_event_scan import SEARCH_KEYWORDS
from core.corporate_event_time import event_time

logger = logging.getLogger(__name__)
NewsFetcher = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class SourceResult:
    source: str
    ok: bool
    item_count: int
    error: str = ""
    oldest_at: str = ""
    newest_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CorporateEventCollection:
    items: list[dict[str, Any]]
    sources: list[SourceResult]

    @property
    def status(self) -> str:
        successes = sum(source.ok for source in self.sources)
        return "unavailable" if not successes else "ok" if successes == len(self.sources) else "partial"


def collect_corporate_event_items(
    *,
    extra_items: list[dict[str, Any]] | None = None,
    fetch_news: NewsFetcher | None = None,
    fetch_cls: NewsFetcher | None = None,
    pages: int = 2,
) -> CorporateEventCollection:
    if fetch_news is None:
        from integrations.stock_news_events import fetch_eastmoney_news

        fetch_news = partial(fetch_eastmoney_news, strict=True)
    if fetch_cls is None:
        from integrations.cls_telegraph import fetch_cls_telegraphs

        fetch_cls = fetch_cls_telegraphs
    jobs = [(f"eastmoney:{keyword}", partial(fetch_news, keyword, pages=pages)) for keyword in SEARCH_KEYWORDS]
    jobs.append(("cls", fetch_cls))
    # Each source has its own network deadline. Concurrent collection keeps a slow
    # keyword from consuming the entire interactive tool deadline.
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_collect_source, jobs))
    items = [item for batch, _ in results for item in batch]
    return CorporateEventCollection(items + list(extra_items or []), [source for _, source in results])


def _collect_source(job: tuple[str, NewsFetcher]) -> tuple[list[dict[str, Any]], SourceResult]:
    source, fetcher = job
    try:
        items = fetcher()
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Invalid news item list")
        stamps = sorted(
            stamp
            for item in items
            if (stamp := event_time(str(item.get("published_at") or item.get("date") or ""))) is not None
        )
        return items, SourceResult(
            source,
            True,
            len(items),
            oldest_at=stamps[0].isoformat() if stamps else "",
            newest_at=stamps[-1].isoformat() if stamps else "",
        )
    except Exception as exc:
        logger.warning("corporate event source %s unavailable (%s)", source, type(exc).__name__)
        return [], SourceResult(source, False, 0, error=type(exc).__name__)
''')
write('workflows/corporate_event_scan_runtime.py', '''"""Corporate news observations and notifications, isolated from trading state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.corporate_event_scan import SOURCE_NOTE, CorporateEventHit, filter_recent_hits, render_corporate_event_report, scan_corporate_events
from core.corporate_event_time import SHANGHAI, event_time, observation_cutoff
from integrations.corporate_event_sources import SourceResult, collect_corporate_event_items
from utils.feishu import send_feishu_notification


@dataclass(frozen=True)
class CorporateEventScanResult:
    as_of: str
    hits: list[CorporateEventHit]
    report: str
    markdown_path: Path
    json_path: Path
    sources: list[SourceResult]
    source_status: str
    undated_hits: list[CorporateEventHit]

    @property
    def source_ok(self) -> bool:
        return self.source_status == "ok"

    def payload(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "timezone": "Asia/Shanghai", "lookback_hours": 48,
                "note": "已公告与媒体电报观察，不是实盘，也不是漏斗买许可。", "coverage": SOURCE_NOTE,
                "source_ok": self.source_ok, "source_status": self.source_status,
                "sources": [source.as_dict() for source in self.sources],
                "hits": [hit.as_dict() for hit in self.hits], "undated_hits": [hit.as_dict() for hit in self.undated_hits]}


@dataclass(frozen=True)
class CorporateEventNotification:
    attempted: bool
    ok: bool
    title: str
    reason: str


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def run_corporate_event_scan(
    *, as_of: str | None = None, extra_items: list[dict[str, Any]] | None = None, fetch_news=None, fetch_cls=None,
    output: str = "logs/corporate_event_scan.md", json_output: str = "logs/corporate_event_scan.json", persist: bool = True,
) -> CorporateEventScanResult:
    as_of_text = observation_cutoff(as_of or shanghai_now().isoformat()).isoformat(timespec="milliseconds")
    collection = collect_corporate_event_items(extra_items=extra_items, fetch_news=fetch_news, fetch_cls=fetch_cls)
    classified = scan_corporate_events(collection.items)
    hits = filter_recent_hits(classified, as_of=as_of_text)
    undated = [hit for hit in classified if event_time(hit.published_at) is None]
    report = render_corporate_event_report(hits, as_of=as_of_text, source_status=collection.status,
        failed_sources=tuple(source.source for source in collection.sources if not source.ok), undated_count=len(undated))
    result = CorporateEventScanResult(as_of_text, hits, report, Path(output), Path(json_output), collection.sources, collection.status, undated)
    if persist:
        _write_text(result.markdown_path, report)
        _write_text(result.json_path, json.dumps(result.payload(), ensure_ascii=False, indent=2))
    return result


def notify_corporate_event_scan(result: CorporateEventScanResult, *, webhook: str | None = None, dry_run: bool = False) -> CorporateEventNotification:
    suffix = "" if result.source_ok else "（数据源异常）"
    title = f"公司大事/停牌扫描 {result.as_of}{suffix}"
    if dry_run:
        return CorporateEventNotification(False, True, title, "dry_run")
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip() if webhook is None else webhook.strip()
    if not webhook:
        return CorporateEventNotification(False, False, title, "FEISHU_WEBHOOK_URL 未配置")
    ok = bool(send_feishu_notification(webhook, title, result.report))
    return CorporateEventNotification(True, ok, title, "ok" if ok else "failed")


def _write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
''')
