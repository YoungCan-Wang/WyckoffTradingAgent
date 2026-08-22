"""Agent-facing stock price and Wyckoff diagnosis tools."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from agents.stock_data_helpers import (
    code_to_name,
    collect_tickflow_limit_hints_from_df,
    hist_metadata,
    latest_hist_date,
)
from agents.tool_context import ToolContext, ensure_tushare_token, get_credential
from utils.safe import drop_empty as _drop_empty
from utils.safe import safe_float as _safe_float

logger = logging.getLogger(__name__)


def analyze_stock(
    code: str,
    mode: str = "diagnose",
    cost: float = 0.0,
    days: int = 30,
    tool_context: ToolContext | None = None,
    buy_dt: str = "",
) -> dict:
    """分析单只 A 股股票：Wyckoff 健康诊断、近期行情或基本面质量查询。

    传入 buy_dt 时，退出信号只看建仓后的价格路径；缺失时结构退出 fail-closed，
    避免建仓前的暴跌被误判为破位。
    """
    try:
        mode = (mode or "diagnose").strip().lower()
        if mode not in ("diagnose", "price", "fundamental"):
            return {"error": f"mode 参数无效: '{mode}'，可选值: diagnose, price, fundamental"}
        if mode == "fundamental":
            return _fundamental_result(code, tool_context)
        ensure_tushare_token(tool_context)
        end_date = date.today()
        if mode == "price":
            return _price_result(code, days, end_date)
        result = _diagnosis_result(code, cost, end_date, buy_dt)
        remember_stock_diagnosis(tool_context, result)
        return result
    except Exception as e:
        logger.exception("analyze_stock error")
        return {"error": str(e)}


def _fundamental_result(code: str, tool_context: ToolContext | None) -> dict:
    from core.fundamental_overlay import evaluate_fundamental_overlay
    from integrations.tickflow_client import TickFlowClient

    api_key = get_credential(tool_context, "tickflow_api_key", "TICKFLOW_API_KEY")
    if not api_key:
        return {"error": "基本面查询需要 TickFlow API Key，请先配置 TICKFLOW_API_KEY"}
    records = TickFlowClient(api_key=api_key).get_financial_metrics([code], latest=True)
    latest = next(iter(records.values()), []) if records else []
    metrics = latest[0] if latest else None
    overlay = evaluate_fundamental_overlay(metrics, signal_date=date.today())
    return {"code": code, "name": code_to_name(code), "data_status": "ok" if metrics else "no_data", **overlay}


def _price_result(code: str, days: int, end_date: date) -> dict:
    from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

    days = min(max(days, 1), 250)
    start_date = end_date - timedelta(days=int(days * 1.6))
    df = get_stock_hist(code, start_date, end_date)
    if df is None or df.empty:
        return {"error": f"无法获取 {code} 的行情数据"}
    hist_hints = collect_tickflow_limit_hints_from_df(df)
    hist_meta = hist_metadata(df)
    df = normalize_hist_df(df).tail(days)
    latest = df.iloc[-1] if len(df) > 0 else {}
    payload = {
        "code": code,
        "days": len(df),
        "latest_close": _round_number(latest.get("close")),
        "latest_date": str(latest.get("date", "")),
        "data_status": "ok",
        **hist_meta,
        "data": _price_records(df),
    }
    if hist_hints:
        payload["tickflow_limit_hint"] = hist_hints[0]
    return payload


def _price_records(df) -> list[dict]:
    return [
        {
            "date": str(row.get("date", "")),
            "open": _round_number(row.get("open")),
            "high": _round_number(row.get("high")),
            "low": _round_number(row.get("low")),
            "close": _round_number(row.get("close")),
            "volume": _safe_int(row.get("volume")),
            "pct_chg": _round_number(row.get("pct_chg")),
        }
        for _, row in df.iterrows()
    ]


def _diagnosis_result(code: str, cost: float, end_date: date, buy_dt: str = "") -> dict:
    from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text
    from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

    df = get_stock_hist(code, end_date - timedelta(days=500), end_date)
    if df is None or df.empty:
        return {"error": f"无法获取 {code} 的行情数据"}
    hist_hints = collect_tickflow_limit_hints_from_df(df)
    hist_meta = hist_metadata(df)
    latest_date = latest_hist_date(df, "日期")
    df = normalize_hist_df(df)
    diagnostic = diagnose_one_stock(code, code_to_name(code), cost, df, buy_dt=buy_dt)
    payload = _diagnostic_payload(
        diagnostic,
        format_diagnostic_text(diagnostic),
        latest_date or latest_hist_date(df),
        hist_meta,
    )
    if hist_hints:
        payload["tickflow_limit_hint"] = hist_hints[0]
    return _attach_chart_news_events(payload, code, df)


def _attach_chart_news_events(payload: dict[str, Any], code: str, df) -> dict[str, Any]:
    try:
        from integrations.stock_news_events import load_news_chart_events

        dates = [str(value)[:10] for value in df["date"].tolist()] if "date" in df.columns else []
        events = (
            load_news_chart_events(code, dates[0], dates[-1], dates, name=str(payload.get("name") or ""))
            if len(dates) >= 2
            else []
        )
    except Exception:
        logger.debug("chart news overlay failed for %s", code, exc_info=True)
        return payload
    if events:
        payload["chart_news_events"] = events
        payload["chart_news_note"] = "读盘叠加层，不改变漏斗候选"
    return payload


def _diagnostic_payload(d, text: str, latest_date: str, metadata: dict) -> dict:
    brief = diagnosis_brief_from_diagnostic(d)
    return {
        "code": d.code,
        "name": d.name,
        "health": d.health,
        "pnl_pct": _round_number(d.pnl_pct),
        "latest_close": _round_number(d.latest_close),
        "ma_pattern": d.ma_pattern,
        "l2_channel": d.l2_channel,
        "track": d.track,
        "accum_stage": d.accum_stage,
        "l4_triggers": d.l4_triggers,
        "candidate_lane": d.candidate_lane,
        "candidate_entry_type": d.candidate_entry_type,
        "candidate_score": _round_number(d.candidate_score),
        "exit_signal": d.exit_signal,
        "stop_loss_status": d.stop_loss_status,
        "vol_ratio_20_60": _round_number(d.vol_ratio_20_60),
        "range_60d_pct": _round_number(d.range_60d_pct, 1),
        "ret_10d_pct": _round_number(d.ret_10d_pct, 1),
        "ret_20d_pct": _round_number(d.ret_20d_pct, 1),
        "from_year_high_pct": _round_number(d.from_year_high_pct, 1),
        "from_year_low_pct": _round_number(d.from_year_low_pct, 1),
        "health_reasons": d.health_reasons,
        "diagnosis_brief": brief,
        "next_action": brief.get("next_step"),
        "next_tool": _diagnosis_next_tool(d.code, brief),
        "formatted_text": text,
        "data_status": "ok",
        "latest_date": latest_date,
        **metadata,
    }


def remember_stock_diagnosis(tool_context: ToolContext | None, result: dict[str, Any]) -> None:
    if tool_context is None or not isinstance(result, dict) or result.get("error"):
        return
    row = _compact_diagnosis_handoff_row(result)
    if not row:
        return
    # analyze_stock 是并发安全工具，多只股票可能在线程池中同时诊断；
    # 用锁保护"读旧列表 -> 合并 -> 写回"这一复合操作，避免并发覆盖丢记录。
    with tool_context.state_lock:
        previous = tool_context.state.get("last_stock_diagnosis")
        rows = []
        if isinstance(previous, dict):
            rows.extend(item for item in previous.get("diagnosed_symbols", []) if isinstance(item, dict))
        rows = [row, *[item for item in rows if item.get("code") != row.get("code")]][:6]
        tool_context.state["last_stock_diagnosis"] = {
            "latest": row,
            "diagnosed_symbols": rows,
            "next_action": "诊断已完成，可结合筛股结果、市场水温和攻防决策形成候选排序",
        }


def _compact_diagnosis_handoff_row(result: dict[str, Any]) -> dict[str, Any]:
    brief = result.get("diagnosis_brief") if isinstance(result.get("diagnosis_brief"), dict) else {}
    return _drop_empty(
        {
            "code": result.get("code"),
            "name": result.get("name"),
            "health": result.get("health"),
            "track": result.get("track"),
            "stage": result.get("accum_stage"),
            "candidate_lane": result.get("candidate_lane"),
            "candidate_score": result.get("candidate_score"),
            "latest_close": result.get("latest_close"),
            "latest_date": result.get("latest_date"),
            "action_status": brief.get("status"),
            "status_label": brief.get("label"),
            "headline": brief.get("headline"),
            "quality_factors": brief.get("strengths"),
            "risk_factors": _diagnosis_handoff_risks(result, brief),
            "new_buy_allowed": brief.get("direct_buy_allowed"),
            "next_step": brief.get("next_step"),
            "data_status": result.get("data_status"),
        }
    )


def _diagnosis_handoff_risks(result: dict[str, Any], brief: dict[str, Any]) -> list[str]:
    if "risks" in brief:
        value = brief.get("risks")
        return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []
    return [
        str(item)
        for item in (result.get("health_reasons") or [])
        if str(item).strip() and not _positive_health_reason(str(item))
    ]


def diagnosis_brief_from_diagnostic(d) -> dict[str, Any]:
    status = _diagnosis_status(d)
    risks = _diagnosis_risks(d)
    strengths = _diagnosis_strengths(d)
    return {
        "status": status,
        "label": _diagnosis_status_label(status),
        "headline": _diagnosis_headline(d, status),
        "strengths": strengths,
        "risks": risks,
        "direct_buy_allowed": False,
        "next_step": _diagnosis_next_step(status, risks),
    }


def _diagnosis_next_tool(code: str, brief: dict[str, Any]) -> dict[str, Any]:
    status = str(brief.get("status") or "").strip()
    if status not in {"priority_watch", "trigger_watch"}:
        return {}
    reason = "个股诊断进入重点/触发观察，可生成 AI 研报复核；不直接触发买入"
    return {"tool": "generate_ai_report", "args": {"stock_codes": [code]}, "reason": reason}


def _diagnosis_status(d) -> str:
    health = str(d.health or "")
    if d.exit_signal in {"stop_loss", "upthrust_warning"} or "危险" in health:
        return "avoid"
    if "警戒" in health:
        return "caution_watch"
    if d.track == "Trend" and float(d.candidate_score or 0.0) >= 80:
        return "priority_watch"
    if d.l4_triggers:
        return "trigger_watch"
    return "watch"


def _diagnosis_status_label(status: str) -> str:
    return {
        "avoid": "回避",
        "caution_watch": "警戒观察",
        "priority_watch": "重点观察",
        "trigger_watch": "触发观察",
        "watch": "观察",
    }.get(status, "观察")


def _diagnosis_headline(d, status: str) -> str:
    return f"{_diagnosis_status_label(status)}: {d.code} {d.name}"


def _diagnosis_strengths(d) -> list[str]:
    out: list[str] = []
    if str(d.ma_pattern or "") in {"多头排列", "MA50>MA200(偏强)"}:
        out.append(str(d.ma_pattern))
    if d.l2_channel and d.l2_channel != "未入选":
        out.append(f"L2通道: {d.l2_channel}")
    if d.l4_triggers:
        out.append(f"L4触发: {'+'.join(d.l4_triggers)}")
    if d.candidate_lane:
        out.append(f"候选车道: {d.candidate_entry_type or d.candidate_lane}({float(d.candidate_score or 0.0):.1f})")
    return out


def _diagnosis_risks(d) -> list[str]:
    risks = [str(item) for item in (d.health_reasons or []) if str(item) and not _positive_health_reason(str(item))]
    if d.exit_signal and not any("退出信号" in item for item in risks):
        risks.append(f"退出信号: {d.exit_signal}")
    if str(d.ma_pattern or "") == "MA50<MA200(偏弱)" and not risks:
        risks.append("均线中长期仍偏弱")
    return list(dict.fromkeys(risks))


def _positive_health_reason(reason: str) -> bool:
    text = reason.strip()
    return text in {"多头排列", "MA50>MA200(偏强)"} or text.startswith(("L2通道:", "L4信号:"))


def _diagnosis_next_step(status: str, risks: list[str]) -> str:
    if status == "avoid":
        return "回避新增，等待结构止损解除或重新站回强势结构"
    if status == "caution_watch":
        return "只观察，等待风险收敛后再复核"
    if status == "priority_watch":
        return "加入重点观察，等待市场闸门打开和回踩/触发确认"
    if status == "trigger_watch":
        return "观察触发是否延续，仍需结合大盘水温和攻防决策"
    if risks:
        return "观察，不直接买入，先处理风险项"
    return "观察，不直接买入；如需交易计划，继续生成攻防决策"


def _round_number(value: Any, digits: int = 2) -> float | None:
    out = _safe_float(value, None)
    return round(out, digits) if out is not None else None


def _safe_int(value: Any) -> int:
    rounded = _round_number(value, 0)
    return int(rounded) if rounded is not None else 0
