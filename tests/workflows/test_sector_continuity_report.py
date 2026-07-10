from __future__ import annotations

from datetime import date


def test_resolve_trade_date_skips_non_trading_day(monkeypatch):
    import workflows.sector_continuity_runtime as runtime

    monkeypatch.setattr(runtime, "resolve_end_calendar_day", lambda: date(2026, 6, 14))
    monkeypatch.setattr(runtime, "is_a_share_trading_day", lambda _day: False)

    assert runtime.resolve_sector_trade_date() is None


def test_update_history_uses_resolved_trade_date():
    from workflows.sector_continuity_report import update_history_with_trade_date

    history = update_history_with_trade_date(
        {},
        [{"name": "半导体", "pct": 2.5, "net_inflow": 300_000_000}],
        date(2026, 6, 15),
    )

    assert history == {"2026-06-15": {"半导体": {"pct": 2.5, "inflow": 300_000_000}}}


def test_notify_report_sends_feishu(monkeypatch):
    import workflows.sector_continuity_runtime as runtime

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        runtime,
        "send_feishu_notification",
        lambda webhook, title, content: (
            captured.update({"webhook": webhook, "title": title, "content": content}) or True
        ),
    )

    result = runtime.notify_sector_continuity_report(
        "# report",
        date(2026, 6, 15),
        webhook="https://example.invalid/webhook",
    )

    assert result.ok is True
    assert captured == {
        "webhook": "https://example.invalid/webhook",
        "title": "板块延续性报告 2026-06-15",
        "content": "# report",
    }
