"""
TickFlow 港股/美股 Wyckoff 漏斗工作流。

流程：标的池实时行情 -> 流动性预筛 -> 批量历史日 K -> Wyckoff 漏斗。
结果写入本地 artifact / GitHub Summary；US/HK 生产任务会写推荐表供 Web 复盘页读取。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from core.candidate_policy import candidate_score_value
from core.candidate_ranker import TRIGGER_LABELS
from core.market_funnel_config import apply_funnel_config_overrides, funnel_config_for_market
from core.signal_confirmation import compute_support_level, score_springboard_abc
from core.wyckoff_engine import (
    FunnelConfig,
    detect_leader_radar,
    dollar_volume_series,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer4_triggers,
)
from integrations.tickflow_client import TickFlowClient
from integrations.tickflow_notice import TICKFLOW_UPGRADE_URL
from workflows.hk_risk_gate import apply_hk_risk_gate
from workflows.market_funnel_data import fetch_market_inputs, load_market_symbols
from workflows.market_funnel_report import (
    market_funnel_report_path,
    send_market_funnel_notification,
    write_market_funnel_output,
    write_market_funnel_report,
)
from workflows.market_funnel_runtime import MARKET_SPECS, RuntimeConfig, runtime_config_from_env
from workflows.market_funnel_tracking import write_tracking_candidates_if_enabled
from workflows.step3_entry_quality import annotate_entry_quality

MARKET_CHOICES = tuple(sorted(MARKET_SPECS))


def _market_regime_context(bench_df: pd.DataFrame | None, cfg: FunnelConfig, *, is_hk: bool = False) -> dict[str, Any]:
    context: dict[str, Any] = {"available": False, "regime": "UNKNOWN", "candidate_cap": None}
    if bench_df is None or bench_df.empty or len(bench_df) < 60:
        return context
    work = bench_df.sort_values("date").copy()
    close = pd.to_numeric(work.get("close"), errors="coerce")
    pct = pd.to_numeric(work.get("pct_chg"), errors="coerce")
    if close.dropna().empty:
        return context
    ma50 = close.rolling(50).mean().iloc[-1]
    ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")
    recent3 = pct.dropna().tail(3)
    today_pct = float(recent3.iloc[-1]) if not recent3.empty else None
    recent3_cum = float(((recent3 / 100.0 + 1.0).prod() - 1.0) * 100.0) if not recent3.empty else None
    regime = _classify_benchmark_regime(float(close.iloc[-1]), ma50, ma200, today_pct, recent3_cum, is_hk=is_hk)
    _tune_funnel_for_regime(cfg, regime, is_hk=is_hk)
    context.update(
        {
            "available": True,
            "regime": regime,
            "close": round(float(close.iloc[-1]), 4),
            "ma50": None if pd.isna(ma50) else round(float(ma50), 4),
            "ma200": None if pd.isna(ma200) else round(float(ma200), 4),
            "today_pct": today_pct,
            "recent3_cum_pct": recent3_cum,
            "candidate_cap": 0 if regime == "CRASH" else 30 if regime == "RISK_OFF" else None,
        }
    )
    return context


def _classify_benchmark_regime(
    close: float,
    ma50: float,
    ma200: float,
    today_pct: float | None,
    recent3_cum: float | None,
    *,
    is_hk: bool = False,
) -> str:
    # 港股恒指单日波幅大于 A 股，CRASH 阈值需放宽
    crash_today = -5.0 if is_hk else -3.5
    crash_cum = -9.0 if is_hk else -6.0
    risk_off_cum = -4.0 if is_hk else -2.0
    if today_pct is not None and today_pct <= crash_today:
        return "CRASH"
    if recent3_cum is not None and recent3_cum <= crash_cum:
        return "CRASH"
    if pd.notna(ma200) and pd.notna(ma50) and close < ma200 and ma50 < ma200 and (recent3_cum or 0.0) <= risk_off_cum:
        return "RISK_OFF"
    if pd.notna(ma200) and pd.notna(ma50) and close > ma50 > ma200 and (recent3_cum or 0.0) >= 0.0:
        return "RISK_ON"
    return "NEUTRAL"


def _tune_funnel_for_regime(cfg: FunnelConfig, regime: str, *, is_hk: bool = False) -> None:
    # 港股 CRASH 场景下 RPS 提升幅度更温和：港股本身 RPS 门槛已降低，
    # 空头回补反弹可能很快推高 RPS 但不意味着趋势真正好转。
    crash_rps_fast = 75.0 if is_hk else 85.0
    crash_rps_slow = 72.0 if is_hk else 80.0
    crash_rs_long = 3.0 if is_hk else 4.0
    riskoff_rps_fast = 68.0 if is_hk else 80.0
    riskoff_rps_slow = 65.0 if is_hk else 75.0
    riskoff_rs_long = 2.5 if is_hk else 3.0
    if regime == "CRASH":
        cfg.rps_fast_min = max(cfg.rps_fast_min, crash_rps_fast)
        cfg.rps_slow_min = max(cfg.rps_slow_min, crash_rps_slow)
        cfg.rs_min_long = max(cfg.rs_min_long, crash_rs_long)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
    elif regime == "RISK_OFF":
        cfg.rps_fast_min = max(cfg.rps_fast_min, riskoff_rps_fast)
        cfg.rps_slow_min = max(cfg.rps_slow_min, riskoff_rps_slow)
        cfg.rs_min_long = max(cfg.rs_min_long, riskoff_rs_long)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)


def _funnel_config(cfg: RuntimeConfig, overrides: Mapping[str, object] | None = None) -> FunnelConfig:
    funnel_cfg = funnel_config_for_market(
        cfg.spec.key,
        trading_days=cfg.kline_count,
        min_avg_amount=cfg.min_avg_amount,
    )
    # 只有回测的研究变体会传覆盖；先覆盖再按水温调参，与回测里 base_cfg 的叠加顺序一致。
    apply_funnel_config_overrides(funnel_cfg, overrides or {})
    return funnel_cfg


def _run_layers(
    symbols: list[str],
    name_map: dict[str, str],
    df_map: dict[str, pd.DataFrame],
    cfg: RuntimeConfig,
    bench_df: pd.DataFrame | None = None,
    sector_map: dict[str, str] | None = None,
    cfg_overrides: Mapping[str, object] | None = None,
) -> tuple[dict[str, list[tuple[str, float]]], dict[str, Any]]:
    funnel_cfg = _funnel_config(cfg, cfg_overrides)
    benchmark_context = _market_regime_context(bench_df, funnel_cfg, is_hk=cfg.spec.key == "hk")
    layer1 = layer1_filter(symbols, name_map, {}, df_map, funnel_cfg)
    layer2, channel_map, _ = layer2_strength_detailed(layer1, df_map, bench_df, funnel_cfg, rps_universe=symbols)
    sector_map = sector_map or {}
    layer3, top_sectors = layer3_sector_resonance(layer2, sector_map, funnel_cfg, base_symbols=layer1, df_map=df_map)
    triggers = layer4_triggers(layer3, df_map, funnel_cfg, channel_map=channel_map)
    trend_watch_rows = detect_leader_radar(layer1, df_map, sector_map, channel_map, funnel_cfg)
    metrics = {
        "layer1": len(layer1),
        "layer2": len(layer2),
        "layer3": len(layer3),
        "total_hits": sum(len(items) for items in triggers.values()),
        "by_trigger": {key: len(items) for key, items in triggers.items()},
        "trend_watch": len(trend_watch_rows),
        "trend_watch_rows": trend_watch_rows,
        "trend_watch_symbols": [str(row["code"]) for row in trend_watch_rows],
        "top_sectors": top_sectors,
        "layer2_channel_map": channel_map,
        "benchmark": benchmark_context,
        "sector_coverage": sum(1 for symbol in layer2 if sector_map.get(symbol)),
    }
    return triggers, metrics


def _latest_history_snapshot(df: pd.DataFrame | None) -> tuple[float | None, int | None]:
    if df is None or df.empty or "close" not in df.columns:
        return (None, None)
    if "date" in df.columns:
        work = df[["date", "close"]].copy()
        work["date"] = pd.to_datetime(work["date"], errors="coerce")
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        work = work.dropna(subset=["date", "close"])
        work = work[work["close"] > 0].sort_values("date")
        if not work.empty:
            latest = work.iloc[-1]
            return (float(latest["close"]), int(latest["date"].strftime("%Y%m%d")))
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    return (float(close.iloc[-1]), None) if not close.empty else (None, None)


# step3_entry_quality._liquidity_points 的阈值（1 / 3）按 A 股「亿元人民币」(1e8 CNY) 标定；
# 将美股美元 (USD ≈ 7.2 CNY) 与港股港币 (HKD ≈ 0.92 CNY) 的成交额折算为等值亿元人民币。
_CURRENCY_TO_CNY_YI: dict[str, float] = {
    "us": 7.2 / 1e8,
    "hk": 0.92 / 1e8,
}


def _candidate_rows(
    triggers: dict[str, list[tuple[str, float]]],
    *,
    name_map: dict[str, str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None = None,
    market: str = "us",
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for trigger, hits in triggers.items():
        for symbol, score in hits:
            item = rows.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": name_map.get(symbol, symbol),
                    "score": 0.0,
                    "triggers": [],
                    "raw_triggers": [],
                },
            )
            item["score"] = candidate_score_value(item["score"]) + candidate_score_value(score)
            item["triggers"].append(TRIGGER_LABELS.get(trigger, trigger))
            item["raw_triggers"].append(str(trigger).strip().lower())
    out = list(rows.values())
    for item in out:
        latest_close, latest_trade_date = _latest_history_snapshot(df_map.get(str(item["symbol"])))
        item["latest_close"] = latest_close
        if latest_trade_date is not None:
            item["latest_trade_date"] = latest_trade_date
    out.sort(key=lambda item: candidate_score_value(item.get("score")), reverse=True)
    _enrich_candidates_entry_context(out, df_map, bench_df, market=market)
    return out


def _enrich_candidates_entry_context(
    candidates: list[dict[str, Any]],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    *,
    market: str = "us",
) -> None:
    bench_closes = _sorted_close_series(bench_df)
    enriched_indices: list[int] = []
    metric_rows: list[dict[str, Any]] = []
    for idx, item in enumerate(candidates):
        symbol = str(item.get("symbol") or "")
        df = df_map.get(symbol)
        if df is None or len(df) < 20 or not {"high", "low", "close", "volume"}.issubset(df.columns):
            continue
        raw_trigs = [str(t) for t in (item.get("raw_triggers") or []) if str(t)]
        metrics = _candidate_entry_metrics(
            df,
            raw_trigs,
            float(item.get("score") or 0.0),
            bench_closes,
            market=market,
            symbol=symbol,
        )
        item.update(metrics)
        enriched_indices.append(idx)
        metric_rows.append(metrics)
    if not metric_rows:
        return
    annotated = annotate_entry_quality(pd.DataFrame(metric_rows)).to_dict("records")
    for idx, eq in zip(enriched_indices, annotated, strict=False):
        item = candidates[idx]
        for key in ("entry_quality_score", "entry_quality_grade", "entry_quality_tag", "entry_risk_flags"):
            item[key] = eq.get(key)
        right_side = bool(item.get("springboard_b")) or any(
            t in ("spring", "trend_pullback", "sos") for t in (item.get("raw_triggers") or [])
        )
        bias_200 = item.get("bias_200")
        bias_20 = item.get("bias_20")
        actionable = (
            str(item.get("entry_quality_grade") or "") in ("S", "A", "B")
            and bias_200 is not None
            and float(bias_200) >= 0.0
            and bias_20 is not None
            and 0.0 <= float(bias_20) <= 10.0
            and right_side
        )
        item["actionable"] = actionable
        item["action_label"] = "优先建仓" if actionable else "观察池"


def _sorted_close_series(df: pd.DataFrame | None) -> pd.Series | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    work = df.sort_values("date") if "date" in df.columns else df
    closes = pd.to_numeric(work["close"], errors="coerce").dropna()
    return closes if len(closes) >= 11 else None


def _candidate_entry_metrics(
    df: pd.DataFrame,
    raw_trigs: list[str],
    score: float,
    bench_closes: pd.Series | None,
    *,
    market: str = "us",
    symbol: str = "",
) -> dict[str, Any]:
    work = (df.sort_values("date") if "date" in df.columns else df).reset_index(drop=True)
    closes = pd.to_numeric(work["close"], errors="coerce")
    vols = pd.to_numeric(work["volume"], errors="coerce")
    amts = dollar_volume_series(work)
    close_px = float(closes.iloc[-1])
    ma20 = float(closes.rolling(20).mean().iloc[-1])
    ma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else 0.0
    vol_ma20 = vols.rolling(20).mean()
    vr = vols / vol_ma20.where(vol_ma20 > 0)
    stock_10 = (close_px / float(closes.iloc[-11]) - 1.0) * 100.0 if len(closes) >= 11 else 0.0
    bench_10 = (
        (float(bench_closes.iloc[-1]) / float(bench_closes.iloc[-11]) - 1.0) * 100.0
        if bench_closes is not None and len(bench_closes) >= 11
        else 0.0
    )
    trig_keys = raw_trigs or ["lps"]
    best_trig, sb = max(
        ((t, score_springboard_abc(work, t)) for t in trig_keys),
        key=lambda pair: (
            int(pair[1].get("met_count") or 0),
            bool(pair[1].get("b")),
            bool(pair[1].get("a")),
            bool(pair[1].get("c")),
            int(pair[1].get("touch_count") or 0),
        ),
    )
    grade = str(sb.get("grade") or "none")
    mkt_key = "hk" if market.lower() == "hk" or symbol.upper().endswith(".HK") else "us"
    avg_amt = float(amts.tail(20).mean()) if not amts.empty else 0.0
    return {
        "support_level": round(compute_support_level(work, best_trig), 4),
        "bias_20": round((close_px / ma20 - 1.0) * 100.0, 2) if ma20 > 0 else None,
        "bias_200": round((close_px / ma200 - 1.0) * 100.0, 2) if ma200 > 0 else None,
        "rs_10": round(stock_10 - bench_10, 2),
        "min_vol_ratio_5d": round(float(vr.tail(5).min()), 4) if len(vr) >= 5 else 1.0,
        "avg_amount_20_yi": round(avg_amt * _CURRENCY_TO_CNY_YI[mkt_key], 4),
        "priority_score": score,
        "springboard_a": bool(sb.get("a")),
        "springboard_b": bool(sb.get("b")),
        "springboard_c": bool(sb.get("c")),
        "springboard_combo": grade,
        "springboard_grade": grade,
        "springboard_met_count": int(sb.get("met_count") or 0),
        "springboard_support": sb.get("support"),
        "springboard_touch_count": int(sb.get("touch_count") or 0),
        "springboard_evidence": sb.get("evidence") or {},
        "springboard_scored": True,
    }


def _sector_map_from_ranked(ranked: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item.get("symbol")): str(item.get("sector")).strip()
        for item in ranked
        if str(item.get("symbol") or "").strip() and str(item.get("sector") or "").strip()
    }


def _cap_candidates_for_regime(candidates: list[dict[str, Any]], metrics: dict[str, Any]) -> list[dict[str, Any]]:
    benchmark = metrics.get("benchmark") if isinstance(metrics.get("benchmark"), dict) else {}
    cap = benchmark.get("candidate_cap")
    if cap is None:
        return candidates
    limit = max(int(cap), 0)
    return candidates[:limit]


def _require_tickflow_client() -> TickFlowClient:
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"market_funnel_job 需要实时行情数据，请购买 TickFlow：{TICKFLOW_UPGRADE_URL}")
    return TickFlowClient(api_key=api_key)


def _build_funnel_result(
    runtime: RuntimeConfig,
    universe_symbols: list[str],
    quotes: dict[str, dict[str, Any]],
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    fetch_stats: dict[str, Any],
    metrics: dict[str, Any],
    candidates: list[dict[str, Any]],
    report_path: Path | None,
) -> dict[str, Any]:
    return {
        "ok": bool(quotes and df_map),
        "market": runtime.spec.key,
        "label": runtime.spec.label,
        "universe": runtime.spec.universe,
        "symbol_file": str(runtime.symbol_path),
        "report_path": str(report_path) if report_path else "",
        "universe_symbol_count": len(universe_symbols),
        "quote_count": len(quotes),
        "selected_count": len(symbols),
        "fetched_count": len(df_map),
        "fetch_stats": fetch_stats,
        "metrics": metrics,
        "top_candidates": candidates[:100],
        "top_trend_watch": (metrics.get("trend_watch_rows") or [])[:100],
        "limits": {
            "max_symbols": runtime.max_symbols,
            "quote_batch_size": runtime.quote_batch_size,
            "quote_batch_sleep": runtime.quote_batch_sleep,
            "kline_batch_size": runtime.kline_batch_size,
            "kline_batch_sleep": runtime.kline_batch_sleep,
            "min_quote_amount": runtime.min_quote_amount,
            "min_quote_price": runtime.min_quote_price,
        },
    }


def run_funnel_for_ranked(
    ranked: list[dict[str, Any]],
    df_map: dict[str, pd.DataFrame],
    runtime: RuntimeConfig,
    bench_df: pd.DataFrame | None,
    bench_symbol: str,
    *,
    cfg_overrides: Mapping[str, object] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    symbols = [str(item["symbol"]) for item in ranked]
    fetched_symbols = [symbol for symbol in symbols if symbol in df_map]
    hk_risk_blocked: dict[str, str] = {}
    if runtime.spec.key == "hk":
        fetched_symbols, hk_risk_blocked = apply_hk_risk_gate(fetched_symbols, df_map)
    name_map = {str(item["symbol"]): str(item["name"]) for item in ranked}
    sector_map = _sector_map_from_ranked(ranked)
    print(f"[market-funnel] {runtime.spec.label} 漏斗筛选 L1~L4 symbols={len(fetched_symbols)}")
    triggers, metrics = (
        _run_layers(
            fetched_symbols,
            name_map,
            df_map,
            runtime,
            bench_df=bench_df,
            sector_map=sector_map,
            cfg_overrides=cfg_overrides,
        )
        if df_map
        else ({}, {})
    )
    if metrics and bench_symbol:
        metrics["benchmark_symbol"] = bench_symbol
    if hk_risk_blocked:
        metrics["hk_risk_blocked"] = hk_risk_blocked
    raw_candidates = _candidate_rows(
        triggers,
        name_map=name_map,
        df_map=df_map,
        bench_df=bench_df,
        market=runtime.spec.key,
    )
    candidates = _cap_candidates_for_regime(raw_candidates, metrics)
    if metrics:
        metrics["actionable_count"] = sum(1 for item in candidates if item.get("actionable"))
    if len(candidates) < len(raw_candidates):
        print(f"[market-funnel] regime cap: kept={len(candidates)}/{len(raw_candidates)}")
    return metrics, candidates


def run_market_funnel(
    market: str,
    *,
    output: str | None = None,
    client: TickFlowClient | None = None,
) -> dict[str, Any]:
    runtime = runtime_config_from_env(market, output)
    tf = client or _require_tickflow_client()
    universe_symbols = load_market_symbols(runtime.symbol_path)
    print(
        f"[market-funnel] start market={runtime.spec.key} universe={runtime.spec.universe} "
        f"symbols={len(universe_symbols)} max_symbols={runtime.max_symbols} "
        f"quote_batch={runtime.quote_batch_size} quote_sleep={runtime.quote_batch_sleep} "
        f"kline_batch={runtime.kline_batch_size} "
        f"symbol_file={runtime.symbol_path}"
    )
    quotes, ranked, bench_df, bench_symbol, df_map, fetch_stats = fetch_market_inputs(tf, universe_symbols, runtime)
    symbols = [str(item["symbol"]) for item in ranked]
    report_path = market_funnel_report_path(runtime.output_path)
    metrics, candidates = run_funnel_for_ranked(ranked, df_map, runtime, bench_df, bench_symbol)
    result = _build_funnel_result(
        runtime,
        universe_symbols,
        quotes,
        symbols,
        df_map,
        fetch_stats,
        metrics,
        candidates,
        report_path,
    )
    write_market_funnel_output(runtime.output_path, result)
    write_market_funnel_report(report_path, result)
    write_tracking_candidates_if_enabled(candidates, runtime.spec.key)
    # 港美一视同仁推送。通知内容本身与市场无关（标题取 result.label、正文走
    # render_market_funnel_report），此前只给港股发是历史遗留，不是设计取舍。
    # webhook 未配置时 send_market_funnel_notification 自己会跳过并打印。
    send_market_funnel_notification(os.getenv("FEISHU_WEBHOOK_URL", ""), result)
    print(
        f"[market-funnel] done ok={result['ok']} market={runtime.spec.key} "
        f"quotes={len(quotes)} selected={len(symbols)} fetched={len(df_map)} "
        f"hits={metrics.get('total_hits', 0) if metrics else 0}"
    )
    return result
