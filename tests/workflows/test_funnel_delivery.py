from __future__ import annotations

import workflows.funnel_delivery as delivery
from workflows.funnel_render import FunnelRenderedCard


def test_deliver_funnel_card_skips_notification_when_disabled(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(delivery, "send_feishu_notification", lambda *args: calls.append(args) or True)
    card = FunnelRenderedCard(
        title="标题",
        content="内容",
        symbols=[{"code": "000001"}],
        benchmark_context={"regime": "NEUTRAL"},
    )

    result = delivery.deliver_funnel_card(card, webhook_url="https://feishu.example", notify=False)

    assert result == (True, [{"code": "000001"}], {"regime": "NEUTRAL"})
    assert calls == []


def test_deliver_funnel_card_sends_notification_and_keeps_details_shape(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(delivery, "send_feishu_notification", lambda *args: calls.append(args) or False)
    card = FunnelRenderedCard(
        title="标题",
        content="内容",
        symbols=[{"code": "000001"}],
        benchmark_context={"regime": "NEUTRAL"},
        details={"selected_for_ai": ["000001"]},
    )

    result = delivery.deliver_funnel_card(card, webhook_url="https://feishu.example", notify=True)

    assert result == (
        False,
        [{"code": "000001"}],
        {"regime": "NEUTRAL"},
        {"selected_for_ai": ["000001"]},
    )
    assert calls == [("https://feishu.example", "标题", "内容")]
