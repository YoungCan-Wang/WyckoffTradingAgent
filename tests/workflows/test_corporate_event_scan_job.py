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
