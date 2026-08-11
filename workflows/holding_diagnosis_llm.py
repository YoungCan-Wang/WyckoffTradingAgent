"""LLM overlay and report rendering for holding diagnosis jobs."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.llm_docs import build_llm_doc_context
from integrations.llm_client import call_llm, provider_fallbacks, provider_route_chain, resolve_provider_name
from integrations.supabase_portfolio import load_portfolio_state

TZ = ZoneInfo("Asia/Shanghai")
HOLDING_ACTIONS = ("ADD", "HOLD", "TRIM", "EXIT")
SIGNAL_SEVERITIES = {"NONE", "WARNING", "CONFIRMED_BREAK", "HARD_RISK"}
ACTION_TIMINGS = {"NOW", "CLOSE_CONFIRM", "NEXT_SESSION_IF", "ON_REBOUND", "WAIT"}
SYSTEM_PROMPT = (
    "你是证据约束型A股持仓诊断助手。先独立判断股票质量、趋势和风险，再判断当前账户内角色。"
    "给出最终操作结论。你只能在 ADD/HOLD/TRIM/EXIT 中选择一个，必须返回 JSON。\n"
    "ADD=加仓, HOLD=不动, TRIM=减仓, EXIT=清仓。\n"
    "若规则理由包含疑似洗盘、回踩测试、未确认破位，默认保持 HOLD；"
    "只有硬风控跌破、放量收低、确认破位或派发特征明确时，才输出 TRIM/EXIT。\n"
    "WARNING 只能 HOLD；TRIM/EXIT 必须标记 CONFIRMED_BREAK 或 HARD_RISK，并给出可执行 action_timing。\n"
    "接近日内低点且尚未收盘确认时不得立即卖出，应使用 CLOSE_CONFIRM、ON_REBOUND 或 WAIT。\n"
    "账户输入默认 capital_scope=account_only，不得只因本账户现金比例、单股权重或集中度卖出。\n"
    "若输入包含主线、阶段或角色，它们只能用于解释持有优先级，不能覆盖硬止损、确认破位或派发证据。\n"
    "不得自行改写或编造主线字段，不得因为属于主线核心就自动 ADD。\n"
    "禁止输出投资建议免责声明，禁止输出 markdown。"
)


@dataclass
class HoldingLLMResult:
    code: str
    name: str
    rule_action: str
    llm_action: str = ""
    llm_reason: str = ""
    llm_confidence: float | None = None
    signal_severity: str = "NONE"
    action_timing: str = "WAIT"
    error: str = ""


def run_holding_llm_report(
    *,
    holdings: list[Any],
    rule_section: str,
    portfolio_id: str,
    deadline_at: datetime,
    started_at: float,
    log: Callable[[str], None] | None = None,
) -> str:
    logger = log or (lambda _msg: None)
    free_cash, total_equity = _portfolio_cash_and_equity(portfolio_id, holdings)
    llm_routes = _build_llm_routes()
    logger(f"[holding-diag] LLM routes: {[r['name'] for r in llm_routes]}")
    llm_results = _run_holdings_llm(holdings, free_cash, total_equity, llm_routes, deadline_at)
    llm_ok = sum(1 for result in llm_results if result.llm_action)
    logger(f"[holding-diag] LLM: {llm_ok}/{len(llm_results)} success")
    return _build_report(llm_results, holdings, free_cash, total_equity, rule_section, time.time() - started_at)


def _portfolio_cash_and_equity(portfolio_id: str, holdings: list[Any]) -> tuple[float, float]:
    state = load_portfolio_state(portfolio_id)
    free_cash = float(state.get("free_cash", 0)) if isinstance(state, dict) else 0
    total_equity = float(state.get("total_equity") or 0) if isinstance(state, dict) else 0
    if total_equity <= 0:
        total_equity = free_cash + sum(holding.current_price * holding.shares for holding in holdings)
    return free_cash, total_equity


def _build_llm_routes() -> list[dict[str, str]]:
    provider = resolve_provider_name("HOLDING_DIAG_LLM_PROVIDER", "efficiency")
    return provider_route_chain(
        provider,
        provider_fallbacks("HOLDING_DIAG_LLM_FALLBACK_PROVIDERS"),
    )


def _run_holdings_llm(
    holdings: list[Any],
    free_cash: float,
    total_equity: float,
    llm_routes: list[dict[str, str]],
    deadline_at: datetime,
) -> list[HoldingLLMResult]:
    if not llm_routes:
        return [
            HoldingLLMResult(code=h.code, name=h.name, rule_action=h.action, error="no_llm_routes") for h in holdings
        ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(
            executor.map(
                lambda holding: _judge_holding(holding, free_cash, total_equity, llm_routes, deadline_at), holdings
            )
        )


def _judge_holding(
    holding: Any,
    free_cash: float,
    total_equity: float,
    llm_routes: list[dict[str, str]],
    deadline_at: datetime,
) -> HoldingLLMResult:
    prompt = _build_holding_llm_prompt(holding, free_cash, total_equity)
    for route in llm_routes:
        left = (deadline_at - datetime.now(TZ)).total_seconds()
        if left <= 5:
            return HoldingLLMResult(code=holding.code, name=holding.name, rule_action=holding.action, error="deadline")
        result = _try_holding_route(holding, prompt, route, int(left))
        if result.llm_action:
            return result
    return HoldingLLMResult(code=holding.code, name=holding.name, rule_action=holding.action, error="all_routes_failed")


def _try_holding_route(holding: Any, prompt: str, route: dict[str, str], seconds_left: int) -> HoldingLLMResult:
    try:
        text = call_llm(
            provider=route["provider"],
            model=route["model"],
            api_key=route["api_key"],
            system_prompt=SYSTEM_PROMPT,
            user_message=prompt,
            base_url=route.get("base_url") or None,
            timeout=min(30, max(10, seconds_left - 3)),
            max_output_tokens=256,
            allow_truncated_text=True,
        )
    except Exception:
        return HoldingLLMResult(code=holding.code, name=holding.name, rule_action=holding.action)
    parsed = _parse_holding_llm(text)
    if not parsed:
        return HoldingLLMResult(code=holding.code, name=holding.name, rule_action=holding.action)
    return HoldingLLMResult(
        code=holding.code,
        name=holding.name,
        rule_action=holding.action,
        llm_action=parsed["action"],
        llm_reason=parsed["reason"],
        llm_confidence=parsed["confidence"],
        signal_severity=parsed["signal_severity"],
        action_timing=parsed["action_timing"],
    )


def _build_holding_llm_prompt(advice: Any, free_cash: float, total_equity: float) -> str:
    features = advice.features or {}
    cash_pct = (free_cash / total_equity * 100) if total_equity > 0 else 0
    llm_doc_context = build_llm_doc_context("holding_diagnosis", symbols=[advice.code], as_of=datetime.now(TZ).date())
    return (
        "analysis_mode=portfolio_rebalance\n"
        "capital_scope=account_only\n"
        "evidence.price_volume=available_daily_snapshot\n"
        "evidence.fundamentals=only_if_explicitly_present\n"
        "evidence.news=only_if_explicitly_present\n"
        f"股票: {advice.code} {advice.name}\n"
        f"持仓: {advice.shares}股, 成本={advice.cost:.2f}, 现价={advice.current_price:.2f}, 浮盈={advice.pnl_pct:+.1f}%\n"
        f"账户: 可用现金={free_cash:.0f} ({cash_pct:.1f}%), 总权益={total_equity:.0f}\n"
        f"规则一判: action={advice.action}, rule_score={advice.rule_score:.1f}\n"
        f"规则理由: {'；'.join(advice.reasons[:3])}\n"
        f"主线语义: phase={features.get('candidate_phase') or '-'}, role={features.get('candidate_role') or '-'}\n"
        f"日线特征:\n"
        f"- ma_pattern={features.get('ma_pattern') or '-'}\n"
        f"- l4_triggers={features.get('l4_triggers') or '-'}\n"
        f"- intraday_path={features.get('intraday_path') or '-'}\n"
        f"- vol_ratio_20_60={_sf(features.get('vol_ratio_20_60')):.3f}\n"
        f"- ret_10d_pct={_sf(features.get('ret_10d_pct')):.3f}\n"
        f"- risk_tag={getattr(advice, 'risk_tag', '')}\n"
        + (f"\n{llm_doc_context}\n" if llm_doc_context else "")
        + '\n请输出严格 JSON：{"action":"ADD|HOLD|TRIM|EXIT",'
        '"signal_severity":"NONE|WARNING|CONFIRMED_BREAK|HARD_RISK",'
        '"action_timing":"NOW|CLOSE_CONFIRM|NEXT_SESSION_IF|ON_REBOUND|WAIT",'
        '"reason":"<=80字","confidence":0.0}'
    )


def _parse_holding_llm(text: str) -> dict[str, Any] | None:
    parsed = _parse_json_object(text)
    if not isinstance(parsed, dict):
        return None
    action = str(parsed.get("action", "")).strip().upper()
    if action not in HOLDING_ACTIONS:
        return None
    severity_default = "WARNING" if action in {"TRIM", "EXIT"} else "NONE"
    if action in {"TRIM", "EXIT"}:
        timing_default = "CLOSE_CONFIRM"
    elif action == "ADD":
        timing_default = "NEXT_SESSION_IF"
    else:
        timing_default = "WAIT"
    severity = _enum_value(parsed.get("signal_severity"), SIGNAL_SEVERITIES, severity_default)
    timing = _enum_value(parsed.get("action_timing"), ACTION_TIMINGS, timing_default)
    reason = str(parsed.get("reason", "")).strip()
    if action in {"TRIM", "EXIT"} and (
        severity not in {"CONFIRMED_BREAK", "HARD_RISK"} or timing not in {"NOW", "NEXT_SESSION_IF", "ON_REBOUND"}
    ):
        reason = f"卖出条件尚未确认（{severity}/{timing}），降级为 HOLD" + (f"；原判断: {reason}" if reason else "")
        action = "HOLD"
    return {
        "action": action,
        "reason": reason,
        "confidence": _clamped_confidence(parsed.get("confidence")),
        "signal_severity": severity,
        "action_timing": timing,
    }


def _enum_value(raw: object, allowed: set[str], default: str) -> str:
    value = str(raw or "").strip().upper()
    return value if value in allowed else default


def _parse_json_object(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None


def _clamped_confidence(raw: object) -> float | None:
    try:
        return max(0.0, min(1.0, float(raw)))
    except Exception:
        return None


def _build_report(
    llm_results: list[HoldingLLMResult],
    holdings: list[Any],
    free_cash: float,
    total_equity: float,
    rule_section: str,
    elapsed: float,
) -> str:
    lines = _report_header(holdings, free_cash, total_equity, elapsed)
    action_map = _group_results_by_action(llm_results)
    for action, label in [("ADD", "加仓"), ("HOLD", "持有/洗盘观察"), ("TRIM", "确认破位减仓"), ("EXIT", "清仓")]:
        lines.extend(_action_section(action, label, action_map.get(action, [])))
    lines.append("---")
    lines.append(rule_section)
    return "\n".join(lines)


def _report_header(holdings: list[Any], free_cash: float, total_equity: float, elapsed: float) -> list[str]:
    cash_pct = (free_cash / total_equity * 100) if total_equity > 0 else 0
    return [
        f"📊 持仓诊断 {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 持仓数: {len(holdings)}",
        f"- 可用现金: {free_cash:.0f} ({cash_pct:.1f}%)",
        f"- 总权益: {total_equity:.0f}",
        f"- 耗时: {elapsed:.1f}s",
        "",
    ]


def _group_results_by_action(llm_results: list[HoldingLLMResult]) -> dict[str, list[HoldingLLMResult]]:
    action_map: dict[str, list[HoldingLLMResult]] = {"ADD": [], "HOLD": [], "TRIM": [], "EXIT": []}
    for result in llm_results:
        action_map.setdefault(result.llm_action or result.rule_action, []).append(result)
    return action_map


def _action_section(action: str, label: str, items: list[HoldingLLMResult]) -> list[str]:
    lines = [f"## {action}（{label}）"]
    if not items:
        lines.append("- 无")
    else:
        lines.extend(_action_item_line(item) for item in items)
    lines.append("")
    return lines


def _action_item_line(result: HoldingLLMResult) -> str:
    reason = result.llm_reason or "(规则判断)"
    confidence = f" conf={result.llm_confidence:.0%}" if result.llm_confidence is not None else ""
    final_action = result.llm_action or result.rule_action
    rule_tag = f" [规则:{result.rule_action}]" if result.rule_action != final_action else ""
    contract = f" [{result.signal_severity}/{result.action_timing}]" if result.llm_action else ""
    return f"- {result.code} {result.name}{rule_tag}{confidence}{contract} | {reason}"


def _sf(raw: Any) -> float:
    try:
        return float(raw or 0)
    except Exception:
        return 0.0
