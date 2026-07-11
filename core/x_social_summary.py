"""X-ready market summary for Step3 daily reports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from core.compliance_report import ComplianceLLMConfig, fmt_pct
from utils.safe import finite_float

logger = logging.getLogger(__name__)

XSummaryLLMCaller = Callable[..., str]


def generate_x_social_summary(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str] | None = None,
    code_name: dict[str, str] | None = None,
    report_text: str = "",
    llm_config: ComplianceLLMConfig | None = None,
    llm_caller: XSummaryLLMCaller | None = None,
) -> str:
    payload = build_x_summary_payload(
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        code_name=code_name,
        report_text=report_text,
    )
    fallback = render_x_summary_fallback(payload)
    if llm_config is None or llm_caller is None:
        return fallback
    try:
        text = llm_caller(
            provider=llm_config.provider,
            model=llm_config.model,
            api_key=llm_config.api_key,
            system_prompt=_x_summary_system_prompt(),
            user_message=_x_summary_user_message(payload),
            base_url=llm_config.base_url,
            timeout=llm_config.timeout_seconds,
            max_output_tokens=min(max(int(llm_config.max_output_tokens), 512), 1600),
        ).strip()
    except Exception as exc:
        logger.warning("[step3][x-summary] 生成失败: %s", exc)
        return fallback
    return _normalize_x_summary(text) or fallback


def build_x_summary_payload(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str] | None = None,
    code_name: dict[str, str] | None = None,
    report_text: str = "",
) -> dict[str, Any]:
    df = selected_df.copy() if isinstance(selected_df, pd.DataFrame) else pd.DataFrame()
    ops_set = {str(code).strip() for code in (ops_codes or []) if str(code).strip()}
    return {
        "trade_date": str(benchmark_context.get("trade_date") or benchmark_context.get("end_trade_date") or ""),
        "market": _market_payload(benchmark_context or {}),
        "rotation": _rotation_payload(benchmark_context or {}),
        "candidates": _candidate_payload(df, ops_set, code_name or {}),
        "report_digest": _report_digest(report_text),
    }


def render_x_summary_fallback(payload: dict[str, Any]) -> str:
    market = payload.get("market") or {}
    candidates = payload.get("candidates") or []
    names = "、".join(_candidate_label(item) for item in candidates[:4]) or "无明确个股"
    action = "先观察，不硬买" if not candidates else "重点看尾盘/次日承接，不追高"
    return (
        "## 🧵 X直白版总结\n"
        f"今天大盘处在{market.get('regime', '未知')}，收盘{market.get('close', '-')}，"
        f"当日{market.get('main_today_pct', '待更新')}，近3日{market.get('recent3_cum_pct', '待更新')}。"
        f"当前最该看的不是热闹，而是谁能在修复里走出持续性。"
        f"个股上我会盯：{names}。{action}。"
    )


def _candidate_payload(df: pd.DataFrame, ops_set: set[str], code_name: dict[str, str]) -> list[dict[str, Any]]:
    if df.empty:
        return []
    ranked = df.assign(_rank_score=_score_series(df)).sort_values("_rank_score", ascending=False)
    return [_candidate_row(row, ops_set, code_name) for _, row in ranked.head(8).iterrows()]


def _candidate_row(row: pd.Series, ops_set: set[str], code_name: dict[str, str]) -> dict[str, Any]:
    code = str(row.get("code", "") or "").strip()
    return {
        "code": code,
        "name": str(row.get("name") or code_name.get(code) or code),
        "industry": str(row.get("industry") or ""),
        "tag": str(row.get("tag") or ""),
        "track": str(row.get("track") or ""),
        "stage": str(row.get("stage") or ""),
        "score": finite_float(row.get("_rank_score")),
        "confirmed": code in ops_set,
        "confirm_reason": str(row.get("confirm_reason") or ""),
    }


def _score_series(df: pd.DataFrame) -> pd.Series:
    priority = _numeric_column(df, "priority_score")
    funnel = _numeric_column(df, "funnel_score")
    return priority.where(priority.notna(), funnel).fillna(0.0)


def _numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="Float64")
    return pd.to_numeric(df[column], errors="coerce")


def _market_payload(ctx: dict[str, Any]) -> dict[str, str]:
    return {
        "regime": str(ctx.get("regime") or "UNKNOWN"),
        "close": _fmt_number(ctx.get("close")),
        "ma50": _fmt_number(ctx.get("ma50")),
        "ma200": _fmt_number(ctx.get("ma200")),
        "main_today_pct": fmt_pct(ctx.get("main_today_pct")),
        "recent3_cum_pct": fmt_pct(ctx.get("recent3_cum_pct")),
        "money_flow": str((ctx.get("money_flow") or {}).get("summary") or ""),
        "pv_outlook": str(ctx.get("market_pv_outlook") or ctx.get("market_pv_summary") or ""),
    }


def _rotation_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    rotation = ctx.get("sector_rotation") or {}
    return {
        "headline": str(rotation.get("headline") or ""),
        "overview": [str(x) for x in (rotation.get("overview_lines") or [])[:6]],
    }


def _report_digest(report_text: str) -> str:
    lines = [line.strip() for line in str(report_text or "").splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith(("## 🧾", "- A：", "- B：", "- C："))]
    return "\n".join(useful[:45])[:2600]


def _x_summary_system_prompt() -> str:
    return (
        "你是A股盘后复盘作者。请基于输入事实写一段适合发到X的中文总结。"
        "可以提股票代码和名称，但不能编造未提供的信息，不能承诺上涨，不能喊单。"
        "输入中的主线、阶段、角色属于程序确定字段，只能原样引用；不要把 WATCH 或起跳板写成买入建议。"
        "语言要直白、短、有判断，重点回答今天市场强弱、主线方向、明天盯哪些股票。"
    )


def _x_summary_user_message(payload: dict[str, Any]) -> str:
    return (
        "请输出 Markdown，标题固定为 `## 🧵 X直白版总结`。正文 120-260 字，最多 3 个短要点，可以提个股。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _normalize_x_summary(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    if "X直白版总结" not in clean:
        clean = "## 🧵 X直白版总结\n" + clean
    return clean.rstrip() + "\n"


def _candidate_label(item: dict[str, Any]) -> str:
    code = str(item.get("code") or "").strip()
    name = str(item.get("name") or code).strip()
    return f"{code} {name}".strip()


def _fmt_number(value: Any) -> str:
    num = finite_float(value)
    return "待更新" if num is None else f"{num:.2f}"
