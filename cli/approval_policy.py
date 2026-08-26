"""高风险写操作的风险分级 — 决定 daemon 能否无人放行。"""

from __future__ import annotations

from typing import Any

AUTO = "auto"
REVIEW = "review"
CONFIRM = "confirm"

# 无人时可自动执行的工具。安全性原本来自「工具本身能做的事很窄」——
# set_stop_loss 不接受股数、成本、现金参数。
#
# 但这个前提已经不完整了：set_stop_loss 现在支持 stop_loss=None **清除**止损。
# 「设置一道保护」和「移除一道保护」的风险完全不同方向，不该共享档位 ——
# 否则 daemon 可以在无人值守时自动执行 agent 提出的「把止损撤掉」。
# 所以 AUTO 现在要看参数，见 _is_auto_safe()。
AUTO_TOOLS = frozenset({"set_stop_loss"})

# 名义金额超过净值这个比例，要二次确认而不是普通审批。
CONFIRM_NAV_RATIO = 0.05

# 清仓、批量删记录这类不可逆动作，一律二次确认。
DESTRUCTIVE_ACTIONS = frozenset({"sell", "remove", "delete", "clear", "delete_records"})


def _entry_clears_stop_loss(entry: dict[str, Any]) -> bool:
    """单条参数是不是在清除止损。

    工具约定 stop_loss=None 表示清除。注意不能只用 entry.get("stop_loss") is None
    来判断 —— 那样「压根没传这个字段」也会被当成清除。没传是参数不全，交给下游
    报错，不该在这里升档。
    """
    if "stop_loss" not in entry:
        return False
    raw = entry.get("stop_loss")
    if raw is None:
        return True
    # 空串同样被下游当成「没有值」，一并视为清除。
    return isinstance(raw, str) and not raw.strip()


def _clears_stop_loss(args: dict[str, Any]) -> bool:
    """这次 set_stop_loss 调用里是否**有任何一条**在清除止损。

    必须逐条看 items：set_stop_loss 支持批量，而批量把真实动作藏在数组里，
    只查顶层字段的话 {"items": [{"code": "A", "stop_loss": None}]} 会被判成
    低风险 —— 一次撤掉多只票的止损，反而比单只更容易溜过去。
    （同一个坑 update_portfolio 那边已经用 _classify_batch 处理过了。）
    """
    items = args.get("items")
    if isinstance(items, list) and items:
        return any(not isinstance(entry, dict) or _entry_clears_stop_loss(entry) for entry in items)
    return _entry_clears_stop_loss(args)


def _is_auto_safe(tool_name: str, args: dict[str, Any]) -> bool:
    """工具在 AUTO 名单里，且这组参数确实是低风险的那一半。"""
    if tool_name not in AUTO_TOOLS:
        return False
    if tool_name == "set_stop_loss" and _clears_stop_loss(args):
        return False
    return True


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
    if _is_auto_safe(tool_name, args):
        return "reason.auto_narrow_tool"
    if tool_name in AUTO_TOOLS:
        return "reason.clears_stop_loss"
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
    if _is_auto_safe(tool_name, args):
        return AUTO
    if tool_name in AUTO_TOOLS:
        # 在 AUTO 名单里却没通过参数检查 —— 目前只有「清除止损」这一种：
        # 移除风控保护要人过一眼，但它不动仓位也不花钱，REVIEW 足够。
        return REVIEW
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


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
