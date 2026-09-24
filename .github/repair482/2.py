import json

replace('integrations/public_mcp/runtime.py', 'def __init__(self, backend: Callable[[ToolSpec, dict[str, Any]], Any] | None = None) -> None:', 'def __init__(self, backend: Callable[[ToolSpec, dict[str, Any]], Any] | None = None, *, backend_factory: Callable | None = None) -> None:')
replace('integrations/public_mcp/runtime.py', '        self._backend = backend\n', '        self._backend = backend\n        self._backend_factory = backend_factory\n')
replace('integrations/public_mcp/runtime.py', '                    from integrations.public_mcp.backend import DomainBackend\n\n                    self._backend = DomainBackend()', '                    if self._backend_factory is None:\n                        return failure("BACKEND_NOT_CONFIGURED", "Start the composed server with wyckoff-mcp.")\n                    self._backend = self._backend_factory()')
replace('integrations/public_mcp/server.py', 'async def serve_stdio() -> None:', 'async def serve_stdio(runtime: Runtime | None = None) -> None:')
replace('integrations/public_mcp/server.py', '    server = create_server()', '    server = create_server(runtime)')
replace('integrations/public_mcp/server.py', 'def main() -> None:', 'def main(runtime: Runtime | None = None) -> None:')
replace('integrations/public_mcp/server.py', 'anyio.run(serve_stdio)', 'anyio.run(serve_stdio, runtime)')
write('mcp_server.py', '''"""Composition root for wyckoff-mcp and python mcp_server.py."""


def build_backend():
    from agents.public_mcp_backend import DomainBackend

    return DomainBackend()


def main() -> None:
    from integrations.public_mcp.runtime import Runtime
    from integrations.public_mcp.server import main as serve

    serve(Runtime(backend_factory=build_backend))


if __name__ == "__main__":
    main()
''')
replace('integrations/public_mcp/contracts.py', 'CONTRACT_VERSION = "1.0"', 'CONTRACT_VERSION = "1.1"')
replace('integrations/public_mcp/contracts.py', '_LOCAL = "integrations.public_mcp.handlers:"', '_LOCAL = "agents.public_mcp_handlers:"')
replace('integrations/public_mcp/contracts.py', 'TOOLS = (\n', '''TOOLS = (
    ToolSpec(
        "scan_corporate_events",
        "agents.corporate_event_tools:scan_corporate_events",
        "检索最近48小时重组/停复牌消息；返回来源状态，否认/终止与未知时间单独标识。不是全量公告核对，不构成买卖许可。",
        {"limit": param("integer", 20, minimum=1, maximum=50)},
        read_only=True,
        timeout=60.0,
    ),
''')
for path in ('tests/test_mcp_server.py', 'tests/test_public_mcp_routing.py'):
    p = r/path
    text = p.read_text().replace('from integrations.public_mcp.backend import', 'from agents.public_mcp_backend import').replace('from integrations.public_mcp.handlers import', 'from agents.public_mcp_handlers import').replace('from integrations.public_mcp import backend as module', 'from agents import public_mcp_backend as module')
    p.write_text(text)
for path in ('tests/test_public_mcp_contracts.py', 'tests/test_public_mcp_stdio.py'):
    p = r/path
    p.write_text(p.read_text().replace('== 19', '== 20').replace('from integrations.public_mcp.server import main', 'from mcp_server import main'))
replace('tests/test_public_mcp_contracts.py', 'NAMES = {', 'NAMES = {\n    "scan_corporate_events",')
with (r/'tests/test_public_mcp_contracts.py').open('a') as f:
    f.write('''

def test_factory_is_lazy_and_only_initialized_once(monkeypatch):
    initialized = []
    runtime = Runtime(backend_factory=lambda: initialized.append(True) or (lambda *_: {"hits": []}))
    assert runtime.call("scan_corporate_events", {"limit": 0}).is_error
    assert runtime.call("update_portfolio", {"action": "remove", "code": "000001"}).is_error
    assert initialized == []
    assert not runtime.call("scan_corporate_events", {"limit": 1}).is_error
    assert not runtime.call("scan_corporate_events", {"limit": 50}).is_error
    assert initialized == [True]


def test_protocol_without_business_backend_reports_configuration_error():
    assert Runtime().call("portfolio").data["code"] == "BACKEND_NOT_CONFIGURED"
''')
replace('scripts/corporate_event_scan_job.py', '    return 0 if not notification.attempted or notification.ok else 1', '    if not result.source_ok:\n        return 2\n    if not args.dry_run and not notification.ok:\n        return 1\n    return 0')
cases = [
 {"title":"星帅尔002860：筹划购买PCB刀具设备公司湘鹰新材料等100%股权，股票停牌","code":"002860","event_status":"announced","halt_status":"announced","reason":"halt_and_restructure"},
 {"title":"新华传媒600825：筹划重大资产重组，股票停牌","code":"600825","event_status":"announced","halt_status":"announced","reason":"halt_and_restructure"},
 {"title":"测试公司002860：未筹划重大资产重组，股票不停牌","code":"002860","event_status":"denied","halt_status":"not_halted","reason":"denied"},
 {"title":"测试公司002860：终止筹划重大资产重组，明日起复牌","code":"002860","event_status":"terminated","halt_status":"resumed","reason":"terminated"},
 {"title":"测试公司（600825）：重大资产重组进展，股票复牌","code":"600825","event_status":"resumed","halt_status":"resumed","reason":"resumed"},
 {"title":"测试公司002860：否认借壳传闻","content":"此前媒体称该股拟筹划重大资产重组并停牌。","code":"002860","event_status":"denied","halt_status":"unknown","reason":"denied"},
 {"title":"公告编号20260921001，关于筹划重大资产重组","code":"","event_status":"announced","halt_status":"unknown","reason":"restructure"},
 {"title":"重大资产重组公告编号：600825","code":"","event_status":"unknown","halt_status":"unknown","reason":"unknown"},
 {"title":"000001与600825重大资产重组讨论","code":"","event_status":"unknown","halt_status":"unknown","reason":"unknown"},
 {"title":"测试公司002860：筹划重大资产重组，尚未复牌，继续停牌","code":"002860","event_status":"announced","halt_status":"announced","reason":"halt_and_restructure"},
]
write('tests/fixtures/corporate_event_cases.json', json.dumps(cases, ensure_ascii=False, indent=2)+'\n')
replace('tests/core/test_corporate_event_scan.py', 'test_filter_recent_keeps_same_evening_and_missing_timestamp', 'test_filter_recent_keeps_same_evening_but_quarantines_missing_timestamp')
replace('tests/core/test_corporate_event_scan.py', '{"002860", "000002"}', '{"002860"}')
with (r/'tests/core/test_corporate_event_scan.py').open('a') as f:
    f.write('''

import json
from pathlib import Path

import pytest

from core.corporate_event_scan import normalize_stock_code
from core.corporate_event_time import event_time

CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/corporate_event_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["title"])
def test_shared_classification_cases(case):
    [hit] = scan_corporate_events([{key: case[key] for key in ("title", "content") if key in case}])
    for key in ("code", "event_status", "halt_status", "reason"):
        assert getattr(hit, key) == case[key]


@pytest.mark.parametrize("code", ["20260921001", "16008251", "id600825", "6008259", "123456", "600825abc"])
def test_numeric_identifiers_are_not_stock_codes(code):
    assert normalize_stock_code(code) == ""
    assert parse_telegraph_symbol(f"公告编号{code}，筹划重大资产重组")[0] == ""


def test_full_title_and_time_distinguish_updates():
    prefix = "测试公司002860：关于筹划重大资产重组进展事项及股票停牌的公告"
    hits = scan_corporate_events([
        {"title": prefix + "（继续推进）", "published_at": "2026-09-21 08:00:00"},
        {"title": prefix + "（终止事项）", "published_at": "2026-09-21 08:00:00"},
    ])
    assert len(hits) == 2


@pytest.mark.parametrize("as_of", ["2026-09-21 08:15", "2026-09-21T00:15:00Z", "2026-09-20T20:15:00-04:00"])
def test_window_enforces_time_of_day_offsets_and_unknown_dates(as_of):
    dates = ["2026-09-21 08:15:00", "2026-09-21 19:20:00", "2026-09-19 08:14:59", "2026-09-19 08:15:00", "", "not-a-time", "2026-09-21"]
    hits = scan_corporate_events([{"title": XINGSHUAIER_TELEGRAPH, "published_at": value} for value in dates])
    kept = filter_recent_hits(hits, as_of=as_of)
    assert {hit.published_at for hit in kept} == {dates[0], dates[3]}


def test_date_only_cutoff_is_day_end_and_invalid_cutoff_is_rejected():
    hits = scan_corporate_events([{"title": XINGSHUAIER_TELEGRAPH, "published_at": "2026-09-21"}])
    assert filter_recent_hits(hits, as_of="2026-09-21") == hits
    with pytest.raises(ValueError):
        filter_recent_hits(hits, as_of="bad date")
    assert event_time("2026-02-30 08:00") is None
''')
replace('tests/integrations/test_corporate_event_sources.py', 'titles = [item["title"] for item in items]', 'titles = [item["title"] for item in items.items]\n    assert items.status == "ok"\n    assert len(items.sources) == 5')
replace('tests/integrations/test_corporate_event_sources.py', 'assert items == [{"title": "kept"}]', 'assert items.items == [{"title": "kept"}]\n    assert items.status == "unavailable"\n    assert all(not source.ok for source in items.sources)')
with (r/'tests/integrations/test_corporate_event_sources.py').open('a') as f:
    f.write('''

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
''')
with (r/'tests/workflows/test_corporate_event_scan_job.py').open('a') as f:
    f.write('''

import json
from types import SimpleNamespace

import pytest


def fail_source(*_args, **_kwargs):
    raise TimeoutError("source unavailable")


@pytest.mark.parametrize("mode", ["ok", "partial", "unavailable"])
def test_source_health_survives_through_report_artifact_and_notification(monkeypatch, tmp_path, mode):
    result = run_corporate_event_scan(
        as_of="2026-09-21 08:15", fetch_news=(lambda *_a, **_k: []) if mode == "ok" else fail_source,
        fetch_cls=fail_source if mode == "unavailable" else lambda: [],
        output=str(tmp_path / "report.md"), json_output=str(tmp_path / "report.json"),
    )
    assert result.source_status == mode
    assert result.source_ok is (mode == "ok")
    payload = json.loads(result.json_path.read_text())
    assert payload["source_status"] == mode and len(payload["sources"]) == 5
    sent = []
    monkeypatch.setattr("workflows.corporate_event_scan_runtime.send_feishu_notification", lambda *args: sent.append(args) or True)
    notice = notify_corporate_event_scan(result, webhook="https://example.invalid/webhook")
    assert notice.ok
    if mode != "ok":
        assert "数据源异常" in sent[0][1]
        assert "不可用" in sent[0][2]
    if mode == "unavailable":
        assert "本次检索窗口未命中" not in result.report and "未扫到" not in result.report


def test_undated_results_are_separate_from_recent_observations():
    result = run_corporate_event_scan(as_of="2026-09-21 08:15", fetch_news=lambda *_a, **_k: [], fetch_cls=lambda: [],
        extra_items=[{"title": XINGSHUAIER_TELEGRAPH}], persist=False)
    assert result.hits == []
    assert len(result.undated_hits) == 1
    assert "未计入近期结果" in result.report


@pytest.mark.parametrize("mode,expected", [("ok", 0), ("partial", 2), ("unavailable", 2)])
def test_job_exit_code_reflects_sources_not_just_webhook(monkeypatch, mode, expected):
    import importlib.util
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("scan_job", Path(__file__).resolve().parents[2] / "scripts/corporate_event_scan_job.py")
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(job, "parse_args", lambda: SimpleNamespace(extra_json="", as_of="", output="", json_output="", dry_run=True))
    result = run_corporate_event_scan(as_of="2026-09-21", fetch_news=(lambda *_a, **_k: []) if mode == "ok" else fail_source,
        fetch_cls=fail_source if mode == "unavailable" else lambda: [], persist=False)
    monkeypatch.setattr(job, "run_corporate_event_scan", lambda **_: result)
    assert job.main() == expected
''')
write('tests/agents/test_corporate_event_tools.py', '''from core.corporate_event_scan import XINGSHUAIER_TELEGRAPH
from integrations.public_mcp.contracts import TOOL_BY_NAME
from integrations.public_mcp.runtime import Runtime
from workflows.corporate_event_scan_runtime import run_corporate_event_scan


def test_agent_scan_is_observation_only(monkeypatch):
    from agents.corporate_event_tools import scan_corporate_events

    result = run_corporate_event_scan(as_of="2026-09-21 19:40", extra_items=[{"title": XINGSHUAIER_TELEGRAPH, "published_at": "2026-09-21 19:20:00"}],
        fetch_news=lambda *_a, **_k: [], fetch_cls=lambda: [], persist=False)
    def fake_run(**kwargs):
        assert kwargs["persist"] is False
        return result
    monkeypatch.setattr("workflows.corporate_event_scan_runtime.run_corporate_event_scan", fake_run)
    payload = scan_corporate_events(limit=5)
    assert payload["hits"][0]["code"] == "002860"
    assert payload["source_ok"] is True
    assert "不是实盘" in payload["note"]
    assert TOOL_BY_NAME["scan_corporate_events"].read_only


def test_all_sources_failed_is_also_an_mcp_tool_error(monkeypatch):
    from agents.corporate_event_tools import scan_corporate_events

    def boom(*_a, **_k):
        raise TimeoutError("private URL")
    result = run_corporate_event_scan(fetch_news=boom, fetch_cls=boom, persist=False)
    monkeypatch.setattr("workflows.corporate_event_scan_runtime.run_corporate_event_scan", lambda **_: result)
    outcome = Runtime(lambda _spec, args: scan_corporate_events(**args)).call("scan_corporate_events")
    assert outcome.is_error
    assert outcome.data["source_status"] == "unavailable"
    assert "本次检索窗口未命中" not in outcome.data["report"]
    assert "private URL" not in str(outcome.data)


def test_invalid_limit_is_rejected_before_fetching(monkeypatch):
    from agents.corporate_event_tools import scan_corporate_events

    def forbidden(**_):
        raise AssertionError("must not fetch")
    monkeypatch.setattr("workflows.corporate_event_scan_runtime.run_corporate_event_scan", forbidden)
    for value in (0, 51, -1, True, "20", 1.5):
        assert "error" in scan_corporate_events(limit=value)
''')
