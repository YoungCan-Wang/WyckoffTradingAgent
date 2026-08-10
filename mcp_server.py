"""Wyckoff MCP Server — 将 Wyckoff 分析能力通过 MCP 协议对外暴露。"""

from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from tools.funnel_public import public_funnel_details

mcp = FastMCP("wyckoff")
_MAX_SCAN_LIMIT = 3000


# ---------------------------------------------------------------------------
# 全局 ToolContext — 从环境变量构建凭证
# ---------------------------------------------------------------------------


def _build_ctx():
    from agents.tool_context import ToolContext

    state = {
        "user_id": os.getenv("SUPABASE_USER_ID", ""),
        "access_token": os.getenv("SUPABASE_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("SUPABASE_REFRESH_TOKEN", ""),
    }

    # 如果没有注入环境变量，尝试读取本地的 CLI 登录态
    if not state["access_token"]:
        from contextlib import suppress

        with suppress(Exception):
            from integrations.local_auth import load_session

            sess = load_session()
            if sess:
                state["user_id"] = sess.get("user_id") or sess.get("user", {}).get("id", "")
                state["access_token"] = sess.get("access_token", "")
                state["refresh_token"] = sess.get("refresh_token", "")

    return ToolContext(state=state)


_ctx = _build_ctx()


# ---------------------------------------------------------------------------
# ToolSurface Registry & execution helper for MCP Server
# ---------------------------------------------------------------------------
from collections.abc import Callable

from tools.tool_surface import ToolSurface

_surface = ToolSurface()


def _execute_mcp_tool(name: str, handler: Callable, arguments: dict[str, Any]) -> dict[str, Any]:
    from tools.tool_surface import ToolAccessContext, from_handler

    existing = _surface.resolve(name)
    if existing is None or existing.handler is not handler:
        _surface.register(from_handler(handler, name=name))

    ctx = ToolAccessContext(
        timeout_seconds=250.0 if "screen" in name or "backtest" in name else 60.0,
        session_id=_ctx.state.get("user_id", ""),
    )
    tool_def = _surface.resolve(name)
    if tool_def:
        import inspect

        sig = inspect.signature(tool_def.handler)
        if "tool_context" in sig.parameters:
            arguments["tool_context"] = _ctx

    res = _surface.execute_tool(name, arguments, ctx)
    if not res["ok"]:
        return {"status": "error", "error": res["error"]["message"]}
    return res["result"]


def _normalize_scan_limit(limit: int | None) -> int | None:
    if limit in (None, ""):
        return None
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是正整数，或留空表示全量扫描") from None
    if value < 0:
        raise ValueError("limit 必须是正整数，或留空表示全量扫描")
    if value > _MAX_SCAN_LIMIT:
        raise ValueError(f"limit 最大支持 {_MAX_SCAN_LIMIT}；全量扫描请不要传 limit")
    return value


# ---------------------------------------------------------------------------
# Tier 1: 无需凭证 — 纯本地 SQLite 读取
# ---------------------------------------------------------------------------

from agents.history_tools import query_history as _query_history
from agents.research_tools import research_hypothesis as _research_hypothesis


@mcp.tool()
def query_history(
    source: Literal["recommendation", "signal", "attribution"],
    status: str = "all",
    limit: int = 20,
) -> dict:
    """查询历史记录。

    **调用时机**：用户问"最近推荐了什么"、"信号池有哪些"、"策略归因/降权"时调用。
    source 决定查哪张表：recommendation(形态复盘)、signal(信号确认池)、
    attribution(策略归因治理器，返回 latest_source/remote_error、latest_operator_summary、
    latest_policy_display/latest_execution_summary、promotion_checklist/latest_operations)。
    """
    return _execute_mcp_tool(
        "query_history",
        _query_history,
        {
            "source": source,
            "status": status,
            "limit": limit,
        },
    )


@mcp.tool()
def research_hypothesis(
    action: Literal["create", "list", "detail", "update", "link_evidence", "evaluate", "transition"],
    hypothesis_id: str = "",
    title: str = "",
    thesis: str = "",
    status: str = "",
    universe: str = "",
    signal_definition: str = "",
    invalidation_criteria: str = "",
    evidence_type: str = "",
    artifact_ref: str = "",
    verdict: str = "review",
    summary: str = "",
    metrics: dict | None = None,
    target_status: str = "",
    reason: str = "",
    limit: int = 50,
) -> dict:
    """登记策略研究假设，并把回测、归因和 shadow 产物作为晋级证据关联起来。"""
    return _execute_mcp_tool(
        "research_hypothesis",
        _research_hypothesis,
        {
            "action": action,
            "hypothesis_id": hypothesis_id,
            "title": title,
            "thesis": thesis,
            "status": status,
            "universe": universe,
            "signal_definition": signal_definition,
            "invalidation_criteria": invalidation_criteria,
            "evidence_type": evidence_type,
            "artifact_ref": artifact_ref,
            "verdict": verdict,
            "summary": summary,
            "metrics": metrics,
            "target_status": target_status,
            "reason": reason,
            "limit": limit,
        },
    )


# ---------------------------------------------------------------------------
# Tier 2: 需 TUSHARE_TOKEN（env 注入）
# ---------------------------------------------------------------------------

from agents.backtest_tools import run_backtest as _run_backtest
from agents.diagnosis_tools import analyze_stock as _analyze_stock
from agents.market_tools import (
    get_market_overview as _get_market_overview,
)
from agents.screen_tools import screen_stocks as _screen_stocks
from agents.search_tools import search_stock_by_name as _search_stock_by_name


@mcp.tool()
def search_stock_by_name(keyword: str) -> list[dict]:
    """根据关键词搜索 A 股股票，支持名称、代码、拼音首字母模糊搜索。"""
    return _execute_mcp_tool("search_stock_by_name", _search_stock_by_name, {"keyword": keyword})


@mcp.tool()
def analyze_stock(
    code: str, mode: Literal["diagnose", "price", "fundamental"] = "diagnose", cost: float = 0.0, days: int = 30
) -> dict:
    """分析单只 A 股。

    **调用时机**：用户问某只股票怎么样、做个诊断、查价格或基本面时调用。
    - mode='diagnose'：Wyckoff 结构诊断（阶段、支撑压力、趋势强度、操作建议）
    - mode='price'：返回近 N 天 OHLCV 数据
    - mode='fundamental'：最新财报的基本面质量评级（研究性质，不改变正式漏斗或买卖信号）
    **结果处理**：诊断结果较专业，请用通俗语言解释给用户。
    """
    return _execute_mcp_tool(
        "analyze_stock",
        _analyze_stock,
        {"code": code, "mode": mode, "cost": cost, "days": days},
    )


@mcp.tool()
def get_market_overview(trade_date: str = "", include_breadth: bool = False) -> dict:
    """获取最新或指定日期的 A 股市场截面。

    **调用时机**：用户问大盘、历史某日市场或上涨/下跌家数时调用。
    trade_date 支持 YYYY-MM-DD/ YYYYMMDD；查涨跌家数时设 include_breadth=true。
    """
    return _execute_mcp_tool(
        "get_market_overview",
        _get_market_overview,
        {"trade_date": trade_date, "include_breadth": include_breadth},
    )


@mcp.tool()
def screen_stocks(
    board: Literal["all", "main_chinext", "main", "chinext", "star", "bse"] = "all",
    limit: int | None = None,
    financial_metrics: bool | None = None,
) -> dict:
    """运行 Wyckoff 五层漏斗筛选，从全市场筛选结构性机会股票。

    **调用时机**：用户说"帮我选股"、"今天有什么机会"、"跑一下漏斗"时调用。
    **注意**：耗时 2-3 分钟，请提前告知用户需要等待。
    **快速试扫**：limit 可限制扫描股票池前 N 只；聊天态留空默认快扫，全量扫描传 limit=0。
    **财务过滤**：聊天快扫默认跳过 TickFlow 财务指标；明确需要完整财务过滤时传 financial_metrics=true.
    **结果处理**：返回候选股票列表和分数，请用专业但易懂的方式呈现。
    """
    return _execute_mcp_tool(
        "screen_stocks",
        _screen_stocks,
        {"board": board, "limit": limit, "financial_metrics": financial_metrics},
    )


@mcp.tool()
def run_backtest(
    start: str = "",
    end: str = "",
    hold_days: int = 10,
    top_n: int = 3,
    board: str = "all",
    stop_loss_pct: float = -7.0,
    take_profit_pct: float = 18.0,
    entry_price_mode: str = "open",
) -> dict:
    """回测威科夫五层漏斗策略的历史表现。

    **调用时机**：用户说"回测一下"、"看看历史表现"时调用。
    **注意**：耗时 3-10 分钟，请提前告知用户。
    **结果处理**：返回胜率、收益率、最大回撤等指标，请对比基准解读。
    **entry_price_mode**：open=信号次日开盘价买入（默认）；close=信号次日收盘价买入；tail_1455=次日14:55分钟线价。
    """
    return _execute_mcp_tool(
        "run_backtest",
        _run_backtest,
        {
            "start": start,
            "end": end,
            "hold_days": hold_days,
            "top_n": top_n,
            "board": board,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "entry_price_mode": entry_price_mode,
        },
    )


# ---------------------------------------------------------------------------
# Tier 2+: 引擎直连工具（无需 LLM，返回纯结构数据）
# ---------------------------------------------------------------------------


@mcp.tool()
def market_regime() -> dict:
    """获取 A 股市场水温和动态阈值。纯引擎计算，不经过 LLM。

    **调用时机**：需要量化判断当前是牛市还是熊市、是否适合加仓时调用。
    返回 regime 枚举（含 CRASH、PANIC_REPAIR 候选、PANIC_REPAIR_CONFIRMED 确认）及斜率、3日收益等指标。
    **结果处理**：regime 是核心字段，请据此给出仓位建议。
    """
    from datetime import date as _date
    from datetime import timedelta

    from core.wyckoff_engine import FunnelConfig
    from integrations.data_source import fetch_index_hist
    from tools.market_regime import analyze_benchmark_and_tune_cfg

    end = _date.today()
    start = end - timedelta(days=400)
    bench_df = fetch_index_hist("000001", start, end)
    smallcap_df = fetch_index_hist("399006", start, end)
    return analyze_benchmark_and_tune_cfg(bench_df, smallcap_df, FunnelConfig(), breadth=None)


@mcp.tool()
def wyckoff_diagnose(code: str) -> dict:
    """单股 Wyckoff 结构诊断。纯引擎计算，不经过 LLM，返回结构化数据。

    **调用时机**：需要精确的 Wyckoff 阶段判定和触发信号检测时调用（比 analyze_stock 更底层）。
    返回交易区间(TR)、触发信号(Spring/SOS/LPS/EVR)、阶段和事件分类。
    **结果处理**：trading_range 和 triggers 是核心，请结合阶段判断当前是吸筹/派发/标记。
    """
    import dataclasses
    from datetime import date as _date
    from datetime import timedelta

    from core.wyckoff_engine import FunnelConfig
    from core.wyckoff_events import classify_wyckoff_event
    from core.wyckoff_structure import detect_structure_triggers, identify_trading_range
    from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

    end = _date.today()
    start = end - timedelta(days=500)
    raw = get_stock_hist(code, start, end)
    if raw is None or raw.empty:
        return {"error": f"无法获取 {code} 的行情数据"}

    df = normalize_hist_df(raw)
    cfg = FunnelConfig()
    tr = identify_trading_range(df, cfg)
    result = detect_structure_triggers([code], {code: df}, cfg)

    stock_triggers = []
    for trig_type in ("spring", "sos", "lps", "evr"):
        for sym, _score in result.triggers.get(trig_type, []):
            if sym == code:
                stock_triggers.append(trig_type)

    stage = result.stage_map.get(code, "")
    event = classify_wyckoff_event(stock_triggers, stage=stage)

    return {
        "code": code,
        "trading_range": dataclasses.asdict(tr) if tr else None,
        "triggers": stock_triggers,
        "stage": stage,
        "stage_semantics": "diagnostic_only",
        "event": dataclasses.asdict(event),
    }


@mcp.tool()
def intraday_analysis(code: str) -> dict:
    """单股盘中多周期分析。纯引擎计算，返回分钟线结构化特征。

    **调用时机**：用户问"盘中表现如何"、"现在能买吗"、"今天走势怎样"时调用。
    返回 VWAP 位置、5m/15m 趋势方向、动量、量能分布、综合强度分等。
    **结果处理**：strength_score 是核心（0-100），配合趋势和VWAP位置给出通俗建议。
    """
    import os

    from core.intraday_analysis import analyze_intraday
    from integrations.tickflow_client import TickFlowClient

    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        return {"error": "未配置 TICKFLOW_API_KEY，无法获取分钟线数据"}
    client = TickFlowClient(api_key=api_key)
    df_1m = client.get_intraday(code, period="1m", count=500)
    df_5m = client.get_intraday(code, period="5m", count=100)
    df_15m = client.get_intraday(code, period="15m", count=50)
    if df_1m.empty:
        return {"error": f"{code} 无法获取分钟线数据，可能非交易时段"}
    profile = analyze_intraday(df_1m, df_5m, df_15m)
    return {"code": code, **profile.to_dict()}


@mcp.tool()
def intraday_rescue_check(code: str) -> dict:
    """单股60m结构救援评估：检测平台突破、VWAP收复、趋势确立等中期结构信号。

    **调用时机**：用户问"这票中周期结构怎么样"、"60分钟线能不能救回来"、"主线票日线不行但想看看中期"时调用。
    """
    import os

    from core.intraday_analysis import analyze_rescue_structure
    from integrations.tickflow_client import TickFlowClient

    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        return {"error": "未配置 TICKFLOW_API_KEY"}
    client = TickFlowClient(api_key=api_key)
    df_60m = client.get_klines(code, period="60m", count=100)
    if df_60m is None or df_60m.empty:
        return {"error": f"{code} 无法获取60m数据，可能非交易时段"}
    try:
        df_30m = client.get_klines(code, period="30m", count=100)
    except Exception:
        df_30m = None
    result = analyze_rescue_structure(df_60m, df_30m)
    return {"code": code, **result.to_dict()}


@mcp.tool()
def run_funnel_simulation(
    board: Literal["all", "main_chinext", "main", "chinext", "star", "bse"] = "all",
    limit: int | None = None,
) -> dict:
    """运行 Wyckoff 五层漏斗仿真，返回原始结构数据。

    **调用时机**：用户说"今天有什么机会"、"帮我复盘并推荐"时调用。与 screen_stocks 类似但返回更底层的原始数据。
    **注意**：耗时 30-60 秒，请耐心等待。
    **快速试扫**：limit 可限制扫描股票池前 N 只；全量扫描请留空。
    **结果处理**：candidates 是最终候选列表，details 含每层的筛选计数和触发信号明细。
    请用专业研报格式输出，不要直接扔原始 JSON。
    """
    board_name = str(board or "all").strip().lower()
    if board_name == "main_chinext":
        board_name = "main_chinext_star"
    if board_name not in {"all", "main_chinext_star", "main", "chinext", "star", "bse"}:
        return {"error": f"不支持的 board 值 '{board}'，可选: all / main / chinext / star / bse"}
    try:
        pool_limit = _normalize_scan_limit(limit)
    except ValueError as exc:
        return {"error": str(exc)}

    from workflows.wyckoff_funnel import run as run_funnel

    ok, symbols, bench_ctx, details = run_funnel(
        "",
        notify=False,
        return_details=True,
        pool_board=board_name,
        pool_limit_count=pool_limit,
        executor_mode="thread",
    )

    if not ok:
        return {"error": "漏斗运行失败", "details": details}
    return {
        "success": True,
        "candidates": symbols,
        "regime": bench_ctx,
        "details": public_funnel_details(details),
    }


# ---------------------------------------------------------------------------
# Tier 3: 需 Supabase 用户认证
# ---------------------------------------------------------------------------

from agents.portfolio_tools import portfolio as _portfolio
from agents.portfolio_tools import record_trade_fill as _record_trade_fill
from agents.portfolio_tools import update_portfolio as _update_portfolio
from agents.report_tools import generate_ai_report as _generate_ai_report
from agents.strategy_tools import generate_strategy_decision as _generate_strategy_decision


@mcp.tool()
def portfolio(mode: Literal["view", "diagnose"] = "view") -> dict:
    """查看或诊断用户持仓。

    **调用时机**：用户问"我的持仓"、"帮我体检一下"时调用。
    - mode='view'：仅列出持仓明细和盈亏
    - mode='diagnose'：对每只持仓股做 Wyckoff 结构诊断
    """
    return _portfolio(mode=mode, tool_context=_ctx)


@mcp.tool()
def update_portfolio(
    action: Literal["add", "remove", "update", "set_cash", "delete_records"],
    code: str = "",
    name: str = "",
    shares: int = 0,
    cost_price: float = 0,
    buy_dt: str = "",
    free_cash: float = 0,
    table: str = "",
    codes: list[str] | None = None,
    items: list[dict] | None = None,
) -> dict:
    """管理用户持仓或删除追踪记录。

    **调用时机**：用户说"买入/卖出/调仓"、"设置现金"、"删除记录"时调用。
    **批量**：一次改多只用 items，不要拆成多次调用。
    **危险操作**：会修改用户数据，请确认用户意图后再调用。
    """
    return _update_portfolio(
        action=action,
        code=code,
        name=name,
        shares=shares,
        cost_price=cost_price,
        buy_dt=buy_dt,
        free_cash=free_cash,
        table=table,
        codes=codes,
        items=items,
        tool_context=_ctx,
    )


@mcp.tool()
def record_trade_fill(
    code: str,
    side: Literal["buy", "sell"],
    shares: int,
    price: float,
    trade_date: str = "",
    name: str = "",
) -> dict:
    """回填一笔真实成交，按增量更新持仓与现金。

    **调用时机**：用户说"我今天卖了 X 股 Y"、"刚买入 Z"等已经发生的成交时调用。
    **与 update_portfolio 的区别**：这个按成交算账（摊薄成本、扣费用、算已实现盈亏），
    update_portfolio 是直接覆盖持仓快照。用户描述的是「成交」就用这个。
    **危险操作**：会修改持仓与现金，确认股数、价格、方向后再调用。
    """
    return _record_trade_fill(
        code=code,
        side=side,
        shares=shares,
        price=price,
        trade_date=trade_date,
        name=name,
        tool_context=_ctx,
    )


@mcp.tool()
def generate_ai_report(stock_codes: list[str]) -> dict:
    """对指定股票列表生成威科夫三阵营 AI 深度研报。

    **调用时机**：用户说"出个研报"、"深度分析这几只"时调用。
    **注意**：耗时约 1 分钟，需要 LLM API 配额。
    **结果处理**：返回三阵营（进攻/防守/观察）分类和详细分析，可直接呈现。
    """
    return _generate_ai_report(stock_codes=stock_codes, tool_context=_ctx)


@mcp.tool()
def generate_strategy_decision() -> dict:
    """生成持仓去留决策和新标的买入策略。

    **调用时机**：用户说"该怎么操作"、"给个策略"、"持仓怎么调"时调用。
    **注意**：需要 LLM API 配额。
    **结果处理**：返回每只持仓的操作建议（持有/减仓/清仓）和新标的买入建议。
    """
    return _generate_strategy_decision(tool_context=_ctx)


@mcp.tool()
def reassess_profile(
    report_text: str,
    profile: Literal["conservative", "balanced", "aggressive"] = "balanced",
) -> dict:
    """基于已有的 AI 研报文本，重新评估并预览保守 (conservative)、均衡 (balanced) 或激进 (aggressive) 决策风格下的交易信号与参数调整。不写入数据库。"""
    from workflows.reassess_profile import reassess_decision_profile

    return _execute_mcp_tool(
        "reassess_profile",
        reassess_decision_profile,
        {"report_text": report_text, "profile": profile},
    )


@mcp.tool()
def diagnose_backend() -> dict:
    """运行后端大模型诊疗（Doctor），全面检查所有 LLM 接口和数据源凭证（如 Tushare Token）的配置、连通性及延迟，输出诊断报告。"""
    from tools.backend_doctor import diagnose_backend as _diagnose

    return _execute_mcp_tool("diagnose_backend", _diagnose, {})


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    from integrations.local_db import init_db

    init_db()
    mcp.run()


if __name__ == "__main__":
    main()
