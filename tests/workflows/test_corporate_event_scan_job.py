from core.corporate_event_scan import XINGSHUAIER_TELEGRAPH
from workflows.corporate_event_scan_runtime import notify_corporate_event_scan, run_corporate_event_scan


def test_job_surfaces_xingshuaier_without_live_or_suspend(tmp_path) -> None:
    result = run_corporate_event_scan(
        as_of="2026-09-21 19:40",
        extra_items=[{"title": XINGSHUAIER_TELEGRAPH, "source": "财联社", "published_at": "2026-09-21 19:20:00"}],
        fetch_news=lambda *_args, **_kwargs: [],
        fetch_cls=lambda: [],
        output=str(tmp_path / "scan.md"),
        json_output=str(tmp_path / "scan.json"),
    )
    assert [hit.code for hit in result.hits] == ["002860"]
    assert "星帅尔" in result.report
    assert "不是实盘" in result.report
    assert "002860" in result.markdown_path.read_text(encoding="utf-8")
    assert "002860" in result.json_path.read_text(encoding="utf-8")


def test_notify_dry_run_does_not_send(monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "workflows.corporate_event_scan_runtime.send_feishu_notification",
        lambda *_args, **_kwargs: sent.append("sent") or True,
    )
    result = run_corporate_event_scan(
        as_of="2026-09-21",
        extra_items=[],
        fetch_news=lambda *_args, **_kwargs: [],
        fetch_cls=lambda: [],
        persist=False,
    )
    notice = notify_corporate_event_scan(result, webhook="https://example.invalid/hook", dry_run=True)
    assert notice.reason == "dry_run"
    assert sent == []


import json
from types import SimpleNamespace

import pytest


def fail_source(*_args, **_kwargs):
    raise TimeoutError("source unavailable")


@pytest.mark.parametrize("mode", ["ok", "partial", "unavailable"])
def test_source_health_survives_through_report_artifact_and_notification(monkeypatch, tmp_path, mode):
    result = run_corporate_event_scan(
        as_of="2026-09-21 08:15",
        fetch_news=(lambda *_a, **_k: []) if mode == "ok" else fail_source,
        fetch_cls=fail_source if mode == "unavailable" else lambda: [],
        output=str(tmp_path / "report.md"),
        json_output=str(tmp_path / "report.json"),
    )
    assert result.source_status == mode
    assert result.source_ok is (mode == "ok")
    payload = json.loads(result.json_path.read_text())
    assert payload["source_status"] == mode and len(payload["sources"]) == 5
    sent = []
    monkeypatch.setattr(
        "workflows.corporate_event_scan_runtime.send_feishu_notification", lambda *args: sent.append(args) or True
    )
    notice = notify_corporate_event_scan(result, webhook="https://example.invalid/webhook")
    assert notice.ok
    if mode != "ok":
        assert "数据源异常" in sent[0][1]
        assert "不可用" in sent[0][2]
    if mode == "unavailable":
        assert "本次检索窗口未命中" not in result.report and "未扫到" not in result.report


def test_undated_results_are_separate_from_recent_observations():
    result = run_corporate_event_scan(
        as_of="2026-09-21 08:15",
        fetch_news=lambda *_a, **_k: [],
        fetch_cls=lambda: [],
        extra_items=[{"title": XINGSHUAIER_TELEGRAPH}],
        persist=False,
    )
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
        spec = importlib.util.spec_from_file_location(
            "scan_job", Path(__file__).resolve().parents[2] / "scripts/corporate_event_scan_job.py"
        )
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(
        job, "parse_args", lambda: SimpleNamespace(extra_json="", as_of="", output="", json_output="", dry_run=True)
    )
    result = run_corporate_event_scan(
        as_of="2026-09-21",
        fetch_news=(lambda *_a, **_k: []) if mode == "ok" else fail_source,
        fetch_cls=fail_source if mode == "unavailable" else lambda: [],
        persist=False,
    )
    monkeypatch.setattr(job, "run_corporate_event_scan", lambda **_: result)
    assert job.main() == expected


def test_page_failure_and_retained_items_reach_report_and_json(monkeypatch, tmp_path):
    from integrations import stock_news_events

    def page(_query, number):
        if number == 2:
            raise TimeoutError("private URL")
        return {"result": {"cmsArticleWebOld": [{"title": XINGSHUAIER_TELEGRAPH, "date": "2026-09-21 19:20:00"}] * 20}}

    monkeypatch.setattr(stock_news_events, "_request_news_page", page)
    result = run_corporate_event_scan(
        as_of="2026-09-21 19:40",
        fetch_cls=lambda: [],
        output=str(tmp_path / "scan.md"),
        json_output=str(tmp_path / "scan.json"),
    )
    assert result.source_status == "partial" and len(result.hits) == 1
    assert "失败页 2，保留 20 条" in result.report
    payload = json.loads(result.json_path.read_text())
    assert payload["sources"][0]["failed_pages"] == [{"page": 2, "error": "TimeoutError"}]
    assert payload["hits"][0]["related_codes"] == ["002860"]
