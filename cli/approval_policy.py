"""高风险写操作的风险分级 — 决定 daemon 能否无人放行。"""

from __future__ import annotations

from typing import Any

AUTO = "auto"
REVIEW = "review"
CONFIRM = "confirm"

# 无人时可自动执行的工具。安全性来自工具本身能做的事很窄——set_stop_loss
# 不接受股数、成本、现金参数——而不是来自这里检查了参数字段。
AUTO_TOOLS = frozenset({"set_stop_loss"})

# 名义金额超过净值这个比例，要二次确认而不是普通审批。
CONFIRM_NAV_RATIO = 0.05

# 清仓、批量删记录这类不可逆动作，一律二次确认。
DESTRUCTIVE_ACTIONS = frozenset({"sell", "remove", "delete", "clear", "delete_records"})


def notional(args: dict[str, Any]) -> float:
    """估算这笔操作的名义金额；缺价缺量时返回 0（交由字段规则判定）。"""
    shares = _as_float(args.get("shares"))
    price = _as_float(args.get("cost_price")) or _as_float(args.get("price"))
    if shares is None or price is None:
        return 0.0
    return abs(shares * price)


def explain(tool_name: str, args: dict[str, Any], nav: float = 0.0) -> str:
    """为什么是这个档位 —— 给人看的一句话，界面上代替心算。

    与 classify 分开：档位决定放行，理由只用于展示。理由缺失或算错都不该
    影响闸门，所以这里只读同一批规则，不参与判定。
    """
    if tool_name in AUTO_TOOLS:
        return "reason.auto_narrow_tool"
    if str(args.get("action") or "").strip().lower() in DESTRUCTIVE_ACTIONS:
        return "reason.destructive_action"
    if tool_name == "record_trade_fill" and str(args.get("side") or "").strip().lower() == "sell":
        return "reason.destructive_action"

    items = args.get("items")
    if isinstance(items, list) and items:
        if any(not isinstance(item, dict) for item in items):
            return "reason.batch_malformed"
        if any(str(i.get("action") or "").strip().lower() in DESTRUCTIVE_ACTIONS for i in items):
            return "reason.destructive_action"
        if any(_over_nav_threshold(i, nav) for i in items):
            return "reason.over_nav"
        if nav > 0 and sum(notional(i) for i in items) > nav * CONFIRM_NAV_RATIO:
            return "reason.batch_over_nav"
        return "reason.write_tool"

    if _over_nav_threshold(args, nav):
        return "reason.over_nav"
    if notional(args) > 0 and nav <= 0:
        # 拿不到净值就无法判断占比，只能按普通审批处理 —— 说清楚，
        # 否则用户会以为系统认定这笔金额不大。
        return "reason.nav_unknown"
    return "reason.write_tool"


def nav_ratio(args: dict[str, Any], nav: float) -> float:
    """这笔操作占净值的比例；批量取合计。拿不到净值或金额时返回 0。"""
    if nav <= 0:
        return 0.0
    items = args.get("items")
    if isinstance(items, list) and items:
        total = sum(notional(i) for i in items if isinstance(i, dict))
    else:
        total = notional(args)
    return total / nav if total else 0.0


def classify(tool_name: str, args: dict[str, Any], nav: float = 0.0) -> str:
    """返回 auto / review / confirm。daemon 只放行 auto。"""
    if tool_name in AUTO_TOOLS:
        return AUTO
    if tool_name == "record_trade_fill":
        if str(args.get("side") or "").strip().lower() == "sell":
            return CONFIRM
        return CONFIRM if _over_nav_threshold(args, nav) else REVIEW
    if tool_name != "update_portfolio":
        # exec_command / write_file 等一律要人看。
        return REVIEW

    if str(args.get("action") or "").strip().lower() in DESTRUCTIVE_ACTIONS:
        return CONFIRM

    # 批量 items 把真实动作藏在数组里，顶层字段检查看不见——逐项判定，取最高档。
    items = args.get("items")
    if isinstance(items, list) and items:
        return _classify_batch(items, nav)

    if _over_nav_threshold(args, nav):
        return CONFIRM
    return REVIEW


def _classify_batch(items: list[Any], nav: float) -> str:
    for item in items:
        if not isinstance(item, dict):
            return CONFIRM
        if str(item.get("action") or "").strip().lower() in DESTRUCTIVE_ACTIONS:
            return CONFIRM
        if _over_nav_threshold(item, nav):
            return CONFIRM
    if nav > 0 and sum(notional(i) for i in items if isinstance(i, dict)) > nav * CONFIRM_NAV_RATIO:
        return CONFIRM
    return REVIEW


def _over_nav_threshold(args: dict[str, Any], nav: float) -> bool:
    return nav > 0 and notional(args) > nav * CONFIRM_NAV_RATIO


def is_auto_tool(tool_name: str) -> bool:
    return tool_name in AUTO_TOOLS


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
