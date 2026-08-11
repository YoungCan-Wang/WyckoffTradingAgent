"""引擎直连工具 — 纯计算、不经 LLM，MCP 与 Agent 共用同一实现。"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date, timedelta
from typing import Any

from agents.tool_context import ToolContext, get_credential

logger = logging.getLogger(__name__)


def market_regime() -> dict:
    """A 股市况判定与动态阈值。get_market_overview 只给原始行情，不含这个判定。"""
    try:
        from core.wyckoff_engine import FunnelConfig
        from integrations.data_source import fetch_index_hist
        from tools.market_regime import analyze_benchmark_and_tune_cfg

        end = date.today()
        start = end - timedelta(days=400)
        bench_df = fetch_index_hist("000001", start, end)
        smallcap_df = fetch_index_hist("399006", start, end)
        return analyze_benchmark_and_tune_cfg(bench_df, smallcap_df, FunnelConfig(), breadth=None)
    except Exception as e:
        logger.exception("market_regime error")
        return {"error": str(e)}


def wyckoff_diagnose(code: str) -> dict:
    """单股 Wyckoff 结构诊断：交易区间、触发信号、阶段与事件分类。"""
    try:
        from core.wyckoff_engine import FunnelConfig
        from core.wyckoff_events import classify_wyckoff_event
        from core.wyckoff_structure import detect_structure_triggers, identify_trading_range
        from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

        end = date.today()
        start = end - timedelta(days=500)
        raw = get_stock_hist(code, start, end)
        if raw is None or raw.empty:
            return {"error": f"无法获取 {code} 的行情数据"}

        df = normalize_hist_df(raw)
        cfg = FunnelConfig()
        tr = identify_trading_range(df, cfg)
        result = detect_structure_triggers([code], {code: df}, cfg)

        triggers = [
            trig_type
            for trig_type in ("spring", "sos", "lps", "evr")
            for sym, _score in result.triggers.get(trig_type, [])
            if sym == code
        ]
        stage = result.stage_map.get(code, "")
        return {
            "code": code,
            "trading_range": dataclasses.asdict(tr) if tr else None,
            "triggers": triggers,
            "stage": stage,
            # 阶段仅供诊断解读，不等于可执行决策。
            "stage_semantics": "diagnostic_only",
            "event": dataclasses.asdict(classify_wyckoff_event(triggers, stage=stage)),
        }
    except Exception as e:
        logger.exception("wyckoff_diagnose error")
        return {"error": str(e)}


def intraday_analysis(code: str, tool_context: ToolContext | None = None) -> dict:
    """盘中多周期分析：VWAP 位置、5m/15m 趋势、量能分布、强度分。"""
    try:
        from core.intraday_analysis import analyze_intraday

        client, error = _tickflow_client(tool_context)
        if error:
            return error
        df_1m = client.get_intraday(code, period="1m", count=500)
        if df_1m is None or df_1m.empty:
            return {"error": f"{code} 无法获取分钟线数据，可能非交易时段"}
        df_5m = client.get_intraday(code, period="5m", count=100)
        df_15m = client.get_intraday(code, period="15m", count=50)
        return {"code": code, **analyze_intraday(df_1m, df_5m, df_15m).to_dict()}
    except Exception as e:
        logger.exception("intraday_analysis error")
        return {"error": str(e)}


def intraday_rescue_check(code: str, tool_context: ToolContext | None = None) -> dict:
    """60m 结构救援评估：平台突破、VWAP 收复、趋势确立。"""
    try:
        from core.intraday_analysis import analyze_rescue_structure

        client, error = _tickflow_client(tool_context)
        if error:
            return error
        df_60m = client.get_klines(code, period="60m", count=100)
        if df_60m is None or df_60m.empty:
            return {"error": f"{code} 无法获取 60m 数据，可能非交易时段"}
        try:
            df_30m = client.get_klines(code, period="30m", count=100)
        except Exception:
            logger.debug("30m klines unavailable for %s", code, exc_info=True)
            df_30m = None
        return {"code": code, **analyze_rescue_structure(df_60m, df_30m).to_dict()}
    except Exception as e:
        logger.exception("intraday_rescue_check error")
        return {"error": str(e)}


def _tickflow_client(tool_context: ToolContext | None) -> tuple[Any, dict | None]:
    from integrations.tickflow_client import TickFlowClient

    api_key = get_credential(tool_context, "tickflow_api_key", "TICKFLOW_API_KEY")
    if not api_key:
        return None, {"error": "未配置 TICKFLOW_API_KEY，无法获取分钟线数据"}
    return TickFlowClient(api_key=api_key), None
