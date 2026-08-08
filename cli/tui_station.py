"""Wyckoff Station TUI 视觉层：确认卡 / 欢迎简报 / 体检卡 / 状态短名。"""

from __future__ import annotations

import re
from typing import Any

_PORTFOLIO_ACTION_LABELS = {
    "add": "新增持仓",
    "update": "更新持仓",
    "remove": "删除持仓",
    "set_cash": "设置现金",
    "delete_records": "删除记录",
}


def short_model_label(provider: str, model: str) -> str:
    """底栏用短模型名，去掉 provider 前缀与过长路径。"""
    model_text = str(model or "").strip()
    if not model_text:
        return str(provider or "").strip() or "—"
    # nvidia/nemotron-3-ultra-558b-a55b:free → nemotron-3-ultra
    leaf = model_text.split("/")[-1]
    leaf = leaf.split(":")[0]
    if len(leaf) > 28:
        leaf = leaf[:25] + "…"
    return leaf


def currency_prefix(code: str) -> str:
    upper = str(code or "").strip().upper()
    if upper.endswith(".HK"):
        return "HK$"
    if upper.endswith(".US"):
        return "$"
    return "¥"


def portfolio_action_title(args: dict[str, Any], display_name: str = "调仓操作") -> str:
    action = str(args.get("action") or "").strip().lower()
    label = _PORTFOLIO_ACTION_LABELS.get(action, display_name)
    code = str(args.get("code") or "").strip()
    name = str(args.get("name") or "").strip()
    subject = " ".join(part for part in (name, code) if part).strip()
    return f"{label} · {subject}" if subject else label


def format_confirm_summary(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "exec_command":
        return f"命令\n  {args.get('command', '')}"
    if tool_name == "write_file":
        path = args.get("path", "")
        size = len(str(args.get("content", "") or ""))
        return f"路径\n  {path}\n内容\n  {size} 字符"
    if tool_name == "update_portfolio":
        return _portfolio_confirm_summary(args)
    import json

    return json.dumps(args, ensure_ascii=False, indent=2)


def _portfolio_confirm_summary(args: dict[str, Any]) -> str:
    lines: list[str] = []
    code = str(args.get("code") or "").strip()
    if shares := args.get("shares"):
        lines.append(f"股数  {shares}")
    if (cost := args.get("cost_price")) is not None and str(cost) != "":
        lines.append(f"成本  {currency_prefix(code)}{cost}")
    if buy_dt := str(args.get("buy_dt") or "").strip():
        lines.append(f"日期  {buy_dt}")
    if (cash := args.get("free_cash")) is not None and str(args.get("action") or "") == "set_cash":
        lines.append(f"现金  ¥{cash}")
    return "\n".join(lines) if lines else "（无额外参数）"


def confirm_option_labels() -> list[tuple[str, str]]:
    """(id, label) — 主操作带 Enter 提示。"""
    return [
        ("once", "Enter  允许一次"),
        ("always", "a      本会话总是允许"),
        ("edit", "e      改参数后再执行"),
        ("deny", "Esc    取消"),
    ]


def portfolio_edit_placeholder(args: dict[str, Any]) -> str:
    return "格式: 代码 股数 成本 [名称] [买入日]  例: 06881.HK 1000 7.68 中国银河 20260807"


def portfolio_edit_initial(args: dict[str, Any]) -> str:
    parts = [
        str(args.get("code") or "").strip(),
        str(args.get("shares") or "").strip(),
        str(args.get("cost_price") or "").strip(),
        str(args.get("name") or "").strip(),
        str(args.get("buy_dt") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def parse_portfolio_edit_line(text: str, base: dict[str, Any]) -> dict[str, Any]:
    """把「代码 股数 成本 [名称...] [买入日]」解析回 update_portfolio 参数。"""
    tokens = [tok for tok in str(text or "").strip().split() if tok]
    out = dict(base)
    if not tokens:
        return out
    out["code"] = tokens[0]
    if len(tokens) >= 2:
        out["shares"] = int(float(tokens[1]))
    if len(tokens) >= 3:
        out["cost_price"] = float(tokens[2])
    rest = tokens[3:]
    if not rest:
        return out
    if re.fullmatch(r"\d{8}", rest[-1]) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", rest[-1]):
        out["buy_dt"] = rest[-1].replace("-", "")
        if len(rest) > 1:
            out["name"] = " ".join(rest[:-1])
    else:
        out["name"] = " ".join(rest)
    return out


def is_confirm_timeout_error(message: str) -> bool:
    text = str(message or "")
    return "确认弹窗等待超时" in text or "确认超时" in text


def timeout_footnote() -> str:
    return "确认超时 · 未执行"


def thinking_preview_line(text: str, *, max_chars: int = 72) -> str:
    preview = str(text or "").strip().replace("\n", " ")
    if not preview:
        return ""
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "…"
    return f"思考中 · {preview}"


def user_echo_prefix() -> str:
    """对话区用户消息前缀（输入框仍用 ❯，与主区语法分离）。"""
    return "你"


def tool_running_line(display_name: str, *, pending_confirm: bool = False) -> str:
    label = str(display_name or "工具").strip() or "工具"
    if pending_confirm:
        return f"◆ {label} 待确认"
    return f"◇ {label}…"


def tool_done_header(display_name: str, elapsed_s: float, *, status: str = "ok") -> str:
    label = str(display_name or "工具").strip() or "工具"
    timing = f"{elapsed_s:.1f}s"
    if status == "error":
        return f"✗ {label}  {timing}"
    if status == "background":
        return f"↗ {label}  已提交后台"
    return f"◆ {label}  {timing}"


def tool_branch_lines(brief_lines: list[str]) -> list[str]:
    """把 brief 收成树状分支，最后一行用 └，中间用 │。"""
    cleaned = [str(line).rstrip() for line in brief_lines if str(line).strip()]
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return [f"  └ {cleaned[0]}"]
    lines = [f"  ├ {cleaned[0]}"]
    for mid in cleaned[1:-1]:
        lines.append(f"  │ {mid}")
    lines.append(f"  └ {cleaned[-1]}")
    return lines


def status_hotkey_legend() -> str:
    return "esc 中断 · ctrl+n 新会话 · ctrl+p 命令 · /help"


def welcome_brief_lines(
    *,
    version: str,
    position_count: int | None,
    free_cash: float | None,
    model_label: str,
) -> list[str]:
    holdings = "持仓 —" if position_count is None else f"持仓 {position_count} 只"
    if free_cash is None:
        cash = "现金 —"
    else:
        cash = f"现金 ¥{free_cash:,.0f}"
    model = model_label or "—"
    return [
        f"Wyckoff Station v{version}",
        f"今日  {holdings} · {cash} · 模型 {model}",
        "快捷  体检 / 调仓 / 扫描 /help",
        "例    06881.HK · 600519 · AAPL.US",
    ]


def portfolio_diagnosis_station_lines(result: dict[str, Any], *, max_positions: int = 12) -> list[str]:
    """持仓体检结构化卡；供 TUI 工具结果区直接渲染。"""
    from utils.tool_result_preview import _rank_portfolio_diagnostics

    diagnostics = [row for row in (result.get("diagnostics") or []) if isinstance(row, dict)]
    try:
        count = int(result.get("position_count") or len(diagnostics))
    except (TypeError, ValueError):
        count = len(diagnostics)

    headline_bits = [f"持仓体检 · {count} 只"]
    if assets := _positive_money(result.get("total_assets")):
        headline_bits.append(f"总资产 {assets}")
    if market := _positive_money(result.get("total_market_value")):
        headline_bits.append(f"市值 {market}")
    lines = [" · ".join(headline_bits)]

    danger = sum(1 for row in diagnostics if "危险" in str(row.get("health") or ""))
    warn = sum(1 for row in diagnostics if "警戒" in str(row.get("health") or ""))
    ok = max(count - danger - warn, 0)
    lines.append(f"危险 {danger} · 警戒 {warn} · 健康 {ok}")

    ranked = _rank_portfolio_diagnostics(diagnostics)
    for row in ranked[:max_positions]:
        lines.append(_diagnosis_station_row(row))
        if detail := _diagnosis_station_detail(row):
            lines.append(f"  {detail}")
    if len(ranked) > max_positions:
        lines.append(f"  …另有 {len(ranked) - max_positions} 只未展开")
    return lines


def _diagnosis_station_row(row: dict[str, Any]) -> str:
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip() or code
    health = str(row.get("health") or "—").strip()
    mark = "危险" if "危险" in health else "警戒" if "警戒" in health else "健康"
    pnl = _signed_pct(row.get("pnl_pct"))
    mv = _positive_money(row.get("market_value"))
    bits = [f"{code} {name}".strip(), mark]
    if mv:
        bits.append(f"市值 {mv}")
    if pnl:
        bits.append(pnl)
    return "  ".join(bits)


def _diagnosis_station_detail(row: dict[str, Any]) -> str:
    brief = row.get("diagnosis_brief") if isinstance(row.get("diagnosis_brief"), dict) else {}
    channel = str(row.get("l2_channel") or brief.get("structure") or "").strip()
    next_step = str(brief.get("next_step") or "").strip()
    error = str(row.get("error") or "").strip()
    parts = [part for part in (channel, next_step, error) if part]
    text = " · ".join(parts)
    return text[:72] + ("…" if len(text) > 72 else "")


def _positive_money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    return f"{amount:,.0f}"


def _signed_pct(value: Any) -> str:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{pct:+.1f}%"
