"""Shared A-share execution playbook text for daily reports."""

from __future__ import annotations

from core.market_trade_mode import MarketTradeMode, resolve_market_trade_mode


def funnel_playbook_lines(regime: str | None, selected_count: int = 0) -> list[str]:
    """Top-of-funnel card: what the operator should do tomorrow."""
    mode = resolve_market_trade_mode(regime)
    lines = [
        "**【🧭 今日执行纪律】**",
        f"1. 闸门：{mode.label}（{mode.mode}）— {mode.reason}",
        f"2. 新开仓：{_new_buy_rule(mode, selected_count)}",
        "3. 状态：DETECTED → SURVIVED（未失效）→ VALIDATED（需求确认）→ OMS_APPROVED。",
        "4. 风控：结构止损超过 -12% 的候选不送审；非主线默认 5 日兑现；禁止新仓日不从观察名单下单。",
        "",
    ]
    return lines


def oms_playbook_lines() -> list[str]:
    """Step4 OMS ticket header rules."""
    lines = [
        "**【🧭 执行纪律】**",
        "- 优先级：EXIT/TRIM（处理风险）> HOLD > PROBE/ATTACK（新开仓）。",
        "- 新开仓仅 APPROVED 的 PROBE/ATTACK；被拦截 NO_TRADE 不补单。",
        "- 持仓看「时间管理」：非主线 5 日、主线约 15 日；灾难止损约 -12%。",
        "- 明日开盘价不在工单允许买入区间：放弃买入，不追价也不抄低。",
    ]
    lines.append("")
    return lines


def step3_playbook_lines(regime: str | None = None) -> list[str]:
    """Compact rules for AI report package header."""
    mode = resolve_market_trade_mode(regime)
    return [
        "【执行纪律】",
        f"- 闸门：{mode.label} — {_gate_short(mode)}",
        "- 只把「起跳板」当可操作；储备营地等待，逻辑破产不碰。",
        "- 主线回踩 MA5/MA10/MA20 或平台再突破优先；不追鱼尾。",
        "- 实盘需先达到 VALIDATED；次日开盘价须位于 OMS 唯一允许区间，非主线默认 5 日兑现。",
        "",
    ]


def _new_buy_rule(mode: MarketTradeMode, selected_count: int) -> str:
    if not mode.allow_ai_review:
        return "禁止。只管理旧仓，不从本报告选新买入。"
    if mode.mode == "overheat_shadow":
        return "禁止正式新开；可做 AI/shadow 对照，不写推荐、不执行买入。"
    if not mode.allow_recommendation_write:
        return "仅研究复核；不写正式推荐，市场闸门重新开放前不新开仓。"
    if selected_count <= 0:
        return "允许但今日无送审候选 → 空仓或只管旧仓，不硬找票。"
    return f"允许（约 {selected_count} 只送审）→ VALIDATED 后由 OMS 给出唯一允许区间，次日开盘执行。"


def _gate_short(mode: MarketTradeMode) -> str:
    if not mode.allow_ai_review:
        return "禁止新仓"
    if not mode.allow_recommendation_write:
        return "仅研究复核，禁止新仓"
    return "可执行买入（主线优先）"
