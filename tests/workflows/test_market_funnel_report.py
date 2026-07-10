from __future__ import annotations

from workflows.market_funnel_report import render_market_funnel_report, send_market_funnel_notification


def _result(**metrics_overrides) -> dict:
    metrics = {"layer1": 10, "layer2": 5, "layer3": 3, "total_hits": 2, "by_trigger": {"sos": 2}}
    metrics.update(metrics_overrides)
    return {
        "label": "港股",
        "market": "hk",
        "universe_symbol_count": 100,
        "quote_count": 90,
        "selected_count": 50,
        "fetched_count": 45,
        "metrics": metrics,
        "top_candidates": [],
        "limits": {},
    }


class TestRenderMarketFunnelReport:
    def test_report_has_no_risk_block_when_nothing_blocked(self):
        report = render_market_funnel_report(_result())
        assert "港股风险剔除" not in report

    def test_report_includes_hk_risk_blocked_block(self):
        report = render_market_funnel_report(_result(hk_risk_blocked={"00099.HK": "疑似仙股"}))
        assert "## 港股风险剔除" in report
        assert "00099.HK" in report
        assert "疑似仙股" in report


class TestSendMarketFunnelNotification:
    def test_missing_webhook_returns_false(self):
        assert send_market_funnel_notification("", _result()) is False

    def test_sends_with_success_icon(self, monkeypatch):
        captured = {}

        def fake_send(webhook_url, title, content):
            captured["webhook_url"] = webhook_url
            captured["title"] = title
            captured["content"] = content
            return True

        monkeypatch.setattr("workflows.market_funnel_report.send_feishu_notification", fake_send)
        ok = send_market_funnel_notification("https://example.invalid/webhook", _result())

        assert ok is True
        assert captured["webhook_url"] == "https://example.invalid/webhook"
        assert "✅" in captured["title"]
        assert "港股" in captured["title"]

    def test_sends_with_neutral_icon_when_no_hits(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "workflows.market_funnel_report.send_feishu_notification",
            lambda webhook_url, title, content: captured.setdefault("title", title) or True,
        )
        send_market_funnel_notification("https://example.invalid/webhook", _result(total_hits=0))

        assert "⚪" in captured["title"]
