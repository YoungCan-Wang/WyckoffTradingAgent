"""Funnel notification delivery."""

from __future__ import annotations

from utils.feishu import send_feishu_notification
from workflows.funnel_ai_selection import FunnelAiSelection
from workflows.funnel_render import FunnelRenderedCard, render_legacy_funnel_card, render_modern_funnel_card


def deliver_funnel_selection(
    ctx,
    selection: FunnelAiSelection,
    *,
    legacy_card: bool,
    webhook_url: str,
    notify: bool,
    return_details: bool,
) -> tuple[bool, list[dict], dict] | tuple[bool, list[dict], dict, dict]:
    card = (
        render_legacy_funnel_card(ctx, selection, return_details=return_details)
        if legacy_card
        else render_modern_funnel_card(ctx, selection, return_details=return_details)
    )
    return deliver_funnel_card(card, webhook_url=webhook_url, notify=notify)


def deliver_funnel_card(
    card: FunnelRenderedCard,
    *,
    webhook_url: str,
    notify: bool,
) -> tuple[bool, list[dict], dict] | tuple[bool, list[dict], dict, dict]:
    # 全量名单会超过飞书单卡约 2800 字；send_feishu_notification 走 split_lark_md 多卡，禁止在此再截断。
    ok = True if not notify else send_feishu_notification(webhook_url, card.title, card.content)
    if card.details is not None:
        return ok, card.symbols, card.benchmark_context, card.details
    return ok, card.symbols, card.benchmark_context
