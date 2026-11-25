"""Post-market workflow and Markdown formatter for the medium-term Slow-Bull engine."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from core.asof_cut import cut_financial_map, cut_ohlcv_map
from core.candidate_metadata import code6
from core.slow_bull_engine import (
    SlowBullConfig,
    detect_tech_rotation_warning,
    diagnose_slow_bull_symbol,
    scan_slow_bull_candidates,
)
from integrations.fetch_a_share_csv import resolve_trading_window
from integrations.index_data_source import fetch_index_hist
from integrations.market_metadata import fetch_sector_map, fetch_slow_bull_daily_basic_map
from integrations.tickflow_client import fetch_financial_metric_map
from tools.data_fetcher import fetch_all_ohlcv
from tools.symbol_pool import load_stock_name_map, resolve_symbol_pool
from utils.trading_clock import resolve_end_calendar_day

logger = logging.getLogger(__name__)

_EXECUTION_BOUNDARY_NOTE = (
    "慢牛引擎信号（slow_bull_emerge / slow_bull_pullback / slow_bull_watch）仅属"
    "20~60日中期底仓研究发现（research_only=true, new_buy_allowed=false），"
    "与5日短线Step4/OMS自动调仓及盘中雷达完全解耦。"
)


@dataclass(frozen=True)
class SlowBullScanRequest:
    """Parameters for targeted diagnosis or cross-sectional market scan."""

    codes: tuple[str, ...] = ()
    board: str = "main_chinext_star"
    limit: int = 0
    as_of_date: date | None = None
    trading_days: int = 160
    include_financial_metrics: bool = True
    allow_cycle_cashflow_bypass: bool = False
    top_n: int = 15


def _load_benchmarks(start_s: str, end_s: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    csi300_df = chinext_df = None
    for idx_code in ("000300", "000001"):
        try:
            csi300_df = fetch_index_hist(idx_code, start_s, end_s)
            if csi300_df is not None and not csi300_df.empty:
                break
        except Exception as exc:
            logger.warning("慢牛扫描基准 %s 加载失败: %s", idx_code, exc)
    try:
        chinext_df = fetch_index_hist("399006", start_s, end_s)
    except Exception as exc:
        logger.warning("慢牛扫描创业板指 399006 加载失败: %s", exc)
    return csi300_df, chinext_df


def _load_financial_map(codes: list[str], *, enabled: bool, as_of_date: date | None) -> dict[str, dict]:
    if not enabled or not codes:
        return {}
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        logger.info("TICKFLOW_API_KEY 未配置，跳过慢牛财务护城河过滤")
        return {}
    try:
        fin_map = fetch_financial_metric_map(api_key, codes)
        return cut_financial_map(fin_map, as_of_date) if as_of_date is not None else fin_map
    except Exception as exc:
        logger.warning("慢牛扫描财务指标加载失败，将仅做量价/市值校验: %s", exc)
        return {}


def _split_basic_maps(
    basic_map: dict[str, dict[str, float]],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    mv_map = {k: float(v["market_cap_yi"]) for k, v in basic_map.items() if "market_cap_yi" in v}
    dv_map = {k: float(v["dv_ttm_pct"]) for k, v in basic_map.items() if "dv_ttm_pct" in v}
    turn_map = {k: float(v["turnover_pct"]) for k, v in basic_map.items() if "turnover_pct" in v}
    return mv_map, dv_map, turn_map


def _build_config(request: SlowBullScanRequest, mv_map: dict[str, float]) -> SlowBullConfig:
    return SlowBullConfig(
        require_market_cap=bool(mv_map),
        allow_cycle_cashflow_bypass=bool(request.allow_cycle_cashflow_bypass),
    )


def _format_candidate_rows(rows: list[dict[str, Any]], empty_text: str) -> list[str]:
    if not rows:
        return [f"- {empty_text}"]
    lines = [
        "| 代码 | 名称 | 行业 | 信号 | R²(40d) | 40日涨幅 | 40日回撤 | 最大单日 | 阳线率 | 量比 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        m = row.get("metrics") or {}
        lines.append(
            f"| {row['code']} | {row.get('name') or '-'} | {row.get('sector') or '-'} | `{row['signal_type']}` "
            f"| {m.get('log_r2_40', '-')} | {m.get('ret_40d_pct', '-')}% | -{m.get('mdd_40_pct', '-')}% "
            f"| {m.get('max_1d_40_pct', '-')}% | {round(float(m.get('yang_ratio_40') or 0) * 100, 1)}% "
            f"| {m.get('vol_ratio_1_20', '-')} |"
        )
    return lines


def format_slow_bull_markdown(payload: dict[str, Any]) -> str:
    """Format scan or diagnosis payload into concise Markdown report."""
    climate = payload.get("climate") or {}
    counts = payload.get("counts") or {}
    lines = [
        f"## 低波机构慢牛扫描 ({payload.get('trade_date') or 'latest'})",
        f"> {payload.get('execution_boundary_note') or _EXECUTION_BOUNDARY_NOTE}",
        "",
        f"- **市场气候预警**: `tech_rotation_warning={climate.get('tech_rotation_warning')}`"
        f" | 创业板20日={climate.get('chinext_ret_20d_pct')}% vs 沪深300={climate.get('csi300_ret_20d_pct')}%"
        f" | 建议：{climate.get('advisory', '')}",
        f"- **股票池覆盖**: 输入={counts.get('input_symbols', 0)} | 合格={counts.get('pit_eligible', 0)}"
        f" | 机构护城河={counts.get('moat_passed', 0)} | 摩擦成本={payload.get('friction_round_trip_pct', 0.202)}%",
        "",
        "### 阶段 A｜刚启动埋伏 (`slow_bull_emerge`)",
        *_format_candidate_rows(payload.get("emerge_candidates") or [], "当日无阶段 A 命中"),
        "",
        "### 阶段 B｜在途慢牛缩量回踩 (`slow_bull_pullback`)",
        *_format_candidate_rows(payload.get("pullback_candidates") or [], "当日无阶段 B 回踩买点"),
        "",
        "### 阶段 B｜在途慢牛观察池 (`slow_bull_watch`)",
        *_format_candidate_rows(payload.get("watch_candidates") or [], "当日无阶段 B 观察标的"),
    ]
    return "\n".join(lines)


def _compact_summary(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    climate = payload.get("climate") or {}
    emerge_codes = [row["code"] for row in (payload.get("emerge_candidates") or [])[:5]]
    pullback_codes = [row["code"] for row in (payload.get("pullback_candidates") or [])[:5]]
    return (
        f"慢牛扫描({payload.get('trade_date', '')}, 模式={payload.get('mode')}): "
        f"护城河={counts.get('moat_passed', 0)}/{counts.get('pit_eligible', 0)}, "
        f"阶段A={counts.get('emerge_count', 0)}({','.join(emerge_codes) or '无'}), "
        f"阶段B回踩={counts.get('pullback_count', 0)}({','.join(pullback_codes) or '无'}), "
        f"tech_rotation_warning={climate.get('tech_rotation_warning')}"
    )


def _run_targeted_diagnosis(
    request: SlowBullScanRequest,
    window,
    csi300_df: pd.DataFrame | None,
    chinext_df: pd.DataFrame | None,
    name_map: dict[str, str],
    sector_map: dict[str, str],
    basic_map: dict[str, dict[str, float]],
) -> dict[str, Any]:
    codes = [code6(code) for code in request.codes if code6(code)]
    mv_map, dv_map, turn_map = _split_basic_maps(basic_map)
    cfg = _build_config(request, mv_map)
    df_map, _ = fetch_all_ohlcv(codes, window, direct_source=False)
    if request.as_of_date is not None:
        df_map = cut_ohlcv_map(df_map, window.end_trade_date)
    fin_map = _load_financial_map(codes, enabled=request.include_financial_metrics, as_of_date=request.as_of_date)
    climate = detect_tech_rotation_warning(csi300_df, chinext_df, all_df_map=None, cfg=cfg)
    diagnoses = [
        diagnose_slow_bull_symbol(
            code,
            df_map.get(code),
            name=name_map.get(code, ""),
            csi300_df=csi300_df,
            market_cap_yi=mv_map.get(code),
            dv_ttm_pct=dv_map.get(code),
            turnover_pct=turn_map.get(code),
            sector=sector_map.get(code, ""),
            financial=fin_map.get(code),
            cfg=cfg,
        )
        for code in codes
    ]
    payload = {
        "ok": True,
        "job_kind": "slow_bull_scan",
        "mode": "targeted_diagnosis",
        "trade_date": window.end_trade_date.isoformat(),
        "research_only": True,
        "new_buy_allowed": False,
        "execution_boundary_note": _EXECUTION_BOUNDARY_NOTE,
        "climate": climate,
        "counts": {
            "input_symbols": len(codes),
            "pit_eligible": sum(1 for row in diagnoses if row.get("eligible")),
            "moat_passed": sum(1 for row in diagnoses if row.get("moat_passed")),
            "emerge_count": sum(1 for row in diagnoses if "slow_bull_emerge" in row.get("matched_stages", ())),
            "pullback_count": sum(1 for row in diagnoses if "slow_bull_pullback" in row.get("matched_stages", ())),
            "watch_count": sum(1 for row in diagnoses if "slow_bull_watch" in row.get("matched_stages", ())),
        },
        "diagnoses": diagnoses,
        "emerge_candidates": [row for row in diagnoses if "slow_bull_emerge" in row.get("matched_stages", ())],
        "pullback_candidates": [row for row in diagnoses if "slow_bull_pullback" in row.get("matched_stages", ())],
        "watch_candidates": [row for row in diagnoses if "slow_bull_watch" in row.get("matched_stages", ())],
    }
    payload["result_summary"] = _compact_summary(payload)
    payload["markdown_report"] = format_slow_bull_markdown(payload)
    return payload


def run_slow_bull_scan(request: SlowBullScanRequest) -> dict[str, Any]:
    """Execute post-market Slow-Bull scan or single/multi-symbol diagnosis."""
    end_day = request.as_of_date or resolve_end_calendar_day()
    window = resolve_trading_window(end_calendar_day=end_day, trading_days=max(int(request.trading_days), 130))
    start_s, end_s = window.start_trade_date.strftime("%Y%m%d"), window.end_trade_date.strftime("%Y%m%d")
    csi300_df, chinext_df = _load_benchmarks(start_s, end_s)
    name_map = load_stock_name_map()
    sector_map = fetch_sector_map()
    basic_map = fetch_slow_bull_daily_basic_map(as_of_date=window.end_trade_date if request.as_of_date else None)
    if request.codes:
        return _run_targeted_diagnosis(request, window, csi300_df, chinext_df, name_map, sector_map, basic_map)

    board = "all" if request.board == "all" else request.board
    symbols, pool_names, _ = resolve_symbol_pool(
        pool_mode="board", board_name=board, limit_count=max(int(request.limit), 0)
    )
    name_map = {**name_map, **pool_names}
    mv_map, dv_map, turn_map = _split_basic_maps(basic_map)
    cfg = _build_config(request, mv_map)
    df_map, _ = fetch_all_ohlcv(symbols, window, direct_source=False)
    if request.as_of_date is not None:
        df_map = cut_ohlcv_map(df_map, window.end_trade_date)
    fin_targets = [s for s in df_map if not mv_map or mv_map.get(s, 0.0) >= cfg.min_market_cap_yi]
    fin_map = _load_financial_map(fin_targets, enabled=request.include_financial_metrics, as_of_date=request.as_of_date)
    scan = scan_slow_bull_candidates(
        df_map,
        csi300_df=csi300_df,
        chinext_df=chinext_df,
        name_map=name_map,
        market_cap_map=mv_map,
        dv_ttm_map=dv_map,
        turnover_map=turn_map,
        sector_map=sector_map,
        financial_map=fin_map,
        cfg=cfg,
        top_n=max(int(request.top_n), 1),
    )
    payload = {
        "ok": True,
        "job_kind": "slow_bull_scan",
        "mode": "market_scan",
        "trade_date": scan.trade_date or window.end_trade_date.isoformat(),
        "board": request.board,
        "research_only": True,
        "new_buy_allowed": False,
        "execution_boundary_note": _EXECUTION_BOUNDARY_NOTE,
        **scan.to_dict(),
    }
    payload["result_summary"] = _compact_summary(payload)
    payload["markdown_report"] = format_slow_bull_markdown(payload)
    return payload
