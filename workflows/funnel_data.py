"""Data preparation workflow for the A-share funnel job."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from core.market_breadth import calc_market_breadth
from core.wyckoff_engine import FunnelConfig
from integrations.fetch_a_share_csv import resolve_trading_window
from integrations.funnel_snapshot import dump_full_fetch_snapshot
from integrations.index_data_source import fetch_index_hist
from integrations.market_metadata import (
    CONCEPT_HEAT_HISTORY,
    detect_theme_lines,
    fetch_concept_heat,
    fetch_concept_map,
    fetch_float_share_map,
    fetch_historical_market_cap_map,
    fetch_market_cap_map,
    fetch_sector_map,
    stale_json_cache,
    update_concept_heat_history,
)
from integrations.ths_hot_concept import (
    fetch_ths_hot_events,
    merge_concept_heat,
    summarize_ths_hot_events,
    ths_hot_events_to_concept_heat,
)
from tools.data_fetcher import fetch_all_ohlcv
from tools.external_seeds import (
    ExternalSeedConfig,
    append_external_symbols,
    load_external_seed_config,
)
from tools.market_liquidity import calc_amount_distribution_health, calc_market_money_flow
from tools.market_regime import analyze_benchmark_and_tune_cfg
from tools.symbol_pool import load_stock_name_map, resolve_symbol_pool, resolve_symbol_pool_from_env
from utils.env import env_bool, env_flag, parse_int_env
from utils.progress import report_progress as _report_progress
from utils.trading_clock import resolve_end_calendar_day
from workflows.fetch_runtime_config import fetch_runtime_config_from_env
from workflows.funnel_config_overrides import apply_funnel_cfg_overrides
from workflows.funnel_data_quality import assert_funnel_data_freshness
from workflows.funnel_etf import run_etf_enhancement
from workflows.funnel_settings import (
    BATCH_SIZE,
    BATCH_SLEEP,
    BATCH_TIMEOUT,
    BREADTH_MA_WINDOW,
    EXECUTOR_MODE,
    FUNNEL_EXPORT_DIR,
    FUNNEL_EXPORT_FULL_FETCH,
    MAX_WORKERS,
    SMALLCAP_BENCH_CODE,
    TRADING_DAYS,
)
from workflows.market_liquidity_config import amount_distribution_config_from_env, market_money_flow_config_from_env
from workflows.market_regime_config import market_regime_config_from_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FunnelSymbolPool:
    symbols: list[str]
    pool_name_map: dict[str, str]
    stats: dict
    external_seed_cfg: ExternalSeedConfig
    external_added_to_pool: int
    main_count: int
    chinext_count: int
    star_count: int
    bse_count: int
    merged_count: int
    st_excluded_count: int
    total_batches: int


@dataclass(frozen=True)
class FunnelReferenceData:
    sector_map: dict[str, str]
    concept_map: dict[str, list[str]]
    concept_heat: list[dict]
    ths_hot_events: dict[str, Any]
    event_concept_heat: list[dict]
    concept_heat_history: dict[str, dict]
    hot_concepts: list
    market_cap_map: dict[str, float]
    financial_map: dict[str, dict]
    name_map: dict[str, str]


@dataclass(frozen=True)
class FunnelJobData:
    cfg: FunnelConfig
    window: Any
    pool: FunnelSymbolPool
    ref_data: FunnelReferenceData
    bench_df: pd.DataFrame | None
    smallcap_df: pd.DataFrame | None
    all_df_map: dict[str, pd.DataFrame]
    fetch_stats: dict
    snapshot_dir: str
    etf_symbols: list[str]
    etf_sector_map: dict[str, str]
    etf_df_map: dict[str, pd.DataFrame]
    etf_l2_passed: list[str]
    etf_candidates: list[dict]
    benchmark_context: dict


def prepare_funnel_job_data(
    direct_source: bool,
    *,
    enforce_target_trade_date: bool = False,
    pool_board: str | None = None,
    pool_limit_count: int | None = None,
    executor_mode: str | None = None,
    include_financial_metrics: bool = True,
) -> FunnelJobData:
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    apply_funnel_cfg_overrides(cfg)
    window = resolve_trading_window(
        end_calendar_day=_resolve_funnel_end_calendar_day(),
        trading_days=TRADING_DAYS,
    )
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")
    pool = _resolve_funnel_symbol_pool(pool_board, pool_limit_count=pool_limit_count)
    ref_data = _load_reference_data(pool.symbols, window, cfg, include_financial_metrics=include_financial_metrics)
    bench_df, smallcap_df = _load_benchmark_indices(start_s, end_s)
    all_df_map, fetch_stats = _fetch_funnel_ohlcv(
        pool,
        window,
        enforce_target_trade_date=enforce_target_trade_date,
        direct_source=direct_source,
        executor_mode=executor_mode,
    )
    if env_bool("FUNNEL_DATA_FRESHNESS_HARD_FAIL", True):
        assert_funnel_data_freshness(
            pool.symbols,
            all_df_map,
            [bench_df, smallcap_df],
            window.end_trade_date,
        )
    snapshot_dir = dump_full_fetch_snapshot(
        enabled=FUNNEL_EXPORT_FULL_FETCH,
        export_dir=FUNNEL_EXPORT_DIR,
        df_map=all_df_map,
        all_symbols=pool.symbols,
        window=window,
        fetch_stats=fetch_stats,
        bench_df=bench_df,
        smallcap_df=smallcap_df,
    )
    etf = run_etf_enhancement(cfg, window, bench_df, direct_source=direct_source)
    benchmark_context = _build_benchmark_context(all_df_map, bench_df, smallcap_df, cfg)
    benchmark_context["trade_date"] = window.end_trade_date.isoformat()
    return FunnelJobData(
        cfg=cfg,
        window=window,
        pool=pool,
        ref_data=ref_data,
        bench_df=bench_df,
        smallcap_df=smallcap_df,
        all_df_map=all_df_map,
        fetch_stats=fetch_stats,
        snapshot_dir=snapshot_dir,
        etf_symbols=etf[0],
        etf_sector_map=etf[1],
        etf_df_map=etf[2],
        etf_l2_passed=etf[3],
        etf_candidates=etf[4],
        benchmark_context=benchmark_context,
    )


def _fetch_funnel_ohlcv(
    pool: FunnelSymbolPool,
    window,
    *,
    enforce_target_trade_date: bool,
    direct_source: bool,
    executor_mode: str | None,
) -> tuple[dict[str, pd.DataFrame], dict]:
    _report_progress("日线拉取", f"共{len(pool.symbols)}只/{pool.total_batches}批", 0.40)
    all_df_map, fetch_stats = fetch_all_ohlcv(
        symbols=pool.symbols,
        window=window,
        enforce_target_trade_date=enforce_target_trade_date,
        batch_size=BATCH_SIZE,
        max_workers=MAX_WORKERS,
        batch_timeout=BATCH_TIMEOUT,
        batch_sleep=BATCH_SLEEP,
        executor_mode=_resolve_executor_mode(executor_mode),
        direct_source=direct_source,
        runtime_config=fetch_runtime_config_from_env(),
    )
    _report_progress("日线拉取", _fetch_progress_summary(fetch_stats, all_df_map), 0.75)
    fetch_stats["turnover_coverage"] = _attach_turnover(all_df_map)
    return all_df_map, fetch_stats


def _attach_turnover(all_df_map: dict[str, pd.DataFrame]) -> float:
    """用流通股本把成交量折算成换手率(%)，返回覆盖率。

    TickFlow/Tushare/Baostock 的日线都不带换手率，缺了这一列时下游的换手率门槛
    会因为 NaN 而恒真、静默失效，所以在数据装配阶段一次性补齐。
    """
    try:
        float_share_map = fetch_float_share_map()
    except Exception as exc:
        logger.warning("流通股本映射加载失败，换手率门槛将失效: %s", exc)
        float_share_map = {}
    covered = 0
    for symbol, df in all_df_map.items():
        float_share = float_share_map.get(symbol, 0.0)
        if df is None or df.empty or "volume" not in df.columns or float_share <= 0:
            continue
        df["turnover"] = pd.to_numeric(df["volume"], errors="coerce") / float_share * 100.0
        covered += 1
    coverage = covered / len(all_df_map) if all_df_map else 0.0
    print(f"[funnel] 换手率折算覆盖: {covered}/{len(all_df_map)} ({coverage:.1%})")
    return coverage


def _fetch_progress_summary(fetch_stats: dict, all_df_map: dict[str, pd.DataFrame]) -> str:
    ok = int(fetch_stats.get("fetch_ok", len(all_df_map)) or 0)
    fail = int(fetch_stats.get("fetch_fail", 0) or 0)
    mismatch = int(fetch_stats.get("fetch_date_mismatch", 0) or 0)
    parts = [f"成功={ok}", f"失败={fail}"]
    if mismatch:
        parts.append(f"日期不匹配={mismatch}")
    return "，".join(parts)


def _resolve_funnel_end_calendar_day() -> date:
    raw = os.getenv("END_CALENDAR_DAY", "").strip()
    if raw:
        try:
            return pd.to_datetime(raw).date()
        except Exception as e:
            logger.warning("END_CALENDAR_DAY=%r 解析失败，回退自动日期: %s", raw, e)
    return resolve_end_calendar_day()


def _load_benchmark_indices(start_s: str, end_s: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    _report_progress("指数加载", "加载大盘/小盘基准", 0.30)
    bench_df = smallcap_df = None
    try:
        bench_df = fetch_index_hist("000001", start_s, end_s)
        print("[funnel] 大盘基准加载成功")
    except Exception as e:
        logger.error("大盘基准加载失败: %s", e, exc_info=True)
    try:
        smallcap_df = fetch_index_hist(SMALLCAP_BENCH_CODE, start_s, end_s)
        print(f"[funnel] 小盘基准加载成功: {SMALLCAP_BENCH_CODE}")
    except Exception as e:
        logger.error("小盘基准加载失败 %s: %s", SMALLCAP_BENCH_CODE, e, exc_info=True)
    _report_progress("指数加载", "基准加载完成", 0.35)
    return bench_df, smallcap_df


def _resolve_external_seed_pool(all_symbols: list[str]) -> tuple[ExternalSeedConfig, list[str], int]:
    seed_cfg = load_external_seed_config()
    merged, added = append_external_symbols(all_symbols, seed_cfg)
    if seed_cfg.enabled:
        print(
            "[funnel] 外部观察名单: "
            f"source={seed_cfg.source}, seeds={len(seed_cfg.symbols)}, "
            f"added_to_pool={added}, mode=shadow_only"
        )
    return seed_cfg, merged, added


def _resolve_pool_limit_count(pool_limit_count: int | None) -> int:
    if pool_limit_count is None:
        return parse_int_env("FUNNEL_POOL_LIMIT_COUNT", 0)
    return max(int(pool_limit_count or 0), 0)


def _resolve_funnel_symbol_pool(
    pool_board: str | None = None,
    *,
    pool_limit_count: int | None = None,
) -> FunnelSymbolPool:
    limit_count = _resolve_pool_limit_count(pool_limit_count)
    if pool_board:
        all_symbols, pool_name_map, pool_stats = resolve_symbol_pool(
            pool_mode="board",
            board_name=pool_board,
            limit_count=limit_count,
        )
    elif pool_limit_count is not None:
        all_symbols, pool_name_map, pool_stats = resolve_symbol_pool(
            pool_mode=os.getenv("FUNNEL_POOL_MODE", ""),
            board_name=os.getenv("FUNNEL_POOL_BOARD", ""),
            manual_symbols=os.getenv("FUNNEL_POOL_MANUAL_SYMBOLS", ""),
            limit_count=limit_count,
        )
    else:
        all_symbols, pool_name_map, pool_stats = resolve_symbol_pool_from_env()
    seed_cfg, all_symbols, external_added = _resolve_external_seed_pool(all_symbols)
    main_count = int(pool_stats.get("pool_main", 0) or 0)
    chinext_count = int(pool_stats.get("pool_chinext", 0) or 0)
    star_count = int(pool_stats.get("pool_star", 0) or 0)
    bse_count = int(pool_stats.get("pool_bse", 0) or 0)
    st_excluded_count = int(pool_stats.get("pool_st_excluded", 0) or 0)
    total_batches = (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE if all_symbols else 0
    print(
        "[funnel] 股票池统计: "
        f"mode={pool_stats.get('pool_mode')}, main={main_count}, chinext={chinext_count}, "
        f"star={star_count}, bse={bse_count}, merged={len(pool_name_map)}, st_excluded={st_excluded_count}, "
        f"final={len(all_symbols)}, limit={pool_stats.get('pool_limit', 0)}, "
        f"batches={total_batches} (batch_size={BATCH_SIZE})"
    )
    _report_progress("股票池加载", f"共{len(all_symbols)}只", 0.05)
    return FunnelSymbolPool(
        symbols=all_symbols,
        pool_name_map=pool_name_map,
        stats=pool_stats,
        external_seed_cfg=seed_cfg,
        external_added_to_pool=external_added,
        main_count=main_count,
        chinext_count=chinext_count,
        star_count=star_count,
        bse_count=bse_count,
        merged_count=len(pool_name_map),
        st_excluded_count=st_excluded_count,
        total_batches=total_batches,
    )


def _resolve_executor_mode(raw: str | None) -> str:
    mode = str(raw or EXECUTOR_MODE or "process").strip().lower()
    return mode if mode in {"thread", "process"} else "process"


def _load_market_metadata(
    window, cfg: FunnelConfig
) -> tuple[dict, dict, list[dict], dict[str, Any], list[dict], dict[str, dict], list, dict[str, float]]:
    _report_progress("元数据加载", "行业/概念/热度", 0.08)
    print("[funnel] 加载行业映射...")
    try:
        sector_map = fetch_sector_map()
    except Exception as e:
        logger.warning("行业映射加载失败，降级为空映射: %s", e)
        sector_map = {}
    print("[funnel] 加载概念映射...")
    try:
        concept_map = fetch_concept_map()
    except Exception as e:
        logger.warning("概念映射加载失败，降级为空映射: %s", e)
        concept_map = {}

    as_of_date = window.end_trade_date.isoformat()
    is_historical = (window.end_trade_date < date.today()) or bool((os.getenv("END_CALENDAR_DAY") or "").strip())

    if is_historical:
        concept_heat, ths_events, event_heat, concept_history, hot_concepts = _load_historical_metadata(as_of_date, cfg)
    else:
        concept_heat, ths_events, event_heat, concept_history, hot_concepts = _load_live_metadata(as_of_date, cfg)

    _report_progress("元数据加载", "市值数据", 0.14)
    if is_historical:
        print(f"[funnel] 回放模式：加载历史市值数据 (as_of_date={as_of_date})...")
        try:
            market_cap_map = fetch_historical_market_cap_map(as_of_date)
        except Exception as e:
            logger.warning("历史市值数据加载失败，降级为空映射: %s", e)
            market_cap_map = {}
        if not market_cap_map:
            print(
                "[funnel] ⚠️ 历史市值数据加载为空（可能API频控或权限限制），回放模式下将临时跳过市值过滤以防误杀，存在数据质量降级风险"
            )
    else:
        print("[funnel] 加载当前实时市值数据...")
        try:
            market_cap_map = fetch_market_cap_map()
        except Exception as e:
            logger.warning("市值数据加载失败，降级为空映射: %s", e)
            market_cap_map = {}
        if not market_cap_map:
            print("[funnel] ⚠️ 市值数据为空（TUSHARE_TOKEN 可能缺失/失效），Layer1 将跳过市值过滤")
    _report_progress("元数据加载", "元数据加载完成", 0.18)
    return sector_map, concept_map, concept_heat, ths_events, event_heat, concept_history, hot_concepts, market_cap_map


def _load_historical_metadata(as_of_date: str, cfg: FunnelConfig) -> tuple[list[dict], dict, list[dict], dict, list]:
    print(f"[funnel] 历史/回放模式运行中 (as_of_date={as_of_date})，跳过实时热度及事件抓取")
    try:
        from integrations.supabase_concept_heat import load_concept_heat_history_from_supabase

        concept_history = load_concept_heat_history_from_supabase(limit_days=30, as_of_date=as_of_date)
    except Exception as e:
        logger.warning("从 Supabase 加载历史概念热度失败，降级本地缓存: %s", e)
        concept_history = {}

    if not concept_history:
        concept_history = stale_json_cache(CONCEPT_HEAT_HISTORY, {})
        if not isinstance(concept_history, dict):
            concept_history = {}
    concept_history = {d: v for d, v in concept_history.items() if d <= as_of_date}

    day_heat = concept_history.get(as_of_date, {})
    concept_heat = []
    for name, info in day_heat.items():
        concept_heat.append(
            {
                "name": name,
                "pct": float(info.get("pct", 0.0) or 0.0),
                "net_inflow": float(info.get("inflow", 0.0) or 0.0),
            }
        )
    ths_events = {"trade_date": as_of_date, "events": []}
    event_heat = []
    hot_concepts = detect_theme_lines(min_days=cfg.theme_line_min_days, as_of_date=as_of_date)
    return concept_heat, ths_events, event_heat, concept_history, hot_concepts


def _load_live_metadata(as_of_date: str, cfg: FunnelConfig) -> tuple[list[dict], dict, list[dict], dict, list]:
    print("[funnel] 加载概念热度...")
    try:
        concept_heat = fetch_concept_heat()
    except Exception as e:
        logger.warning("概念热度加载失败: %s", e)
        concept_heat = []
    ths_events, event_heat = _load_ths_hot_events()
    heat_for_history = merge_concept_heat(concept_heat, event_heat)
    if heat_for_history:
        update_concept_heat_history(as_of_date, heat_for_history, top_n=cfg.theme_line_top_n)
    concept_history = stale_json_cache(CONCEPT_HEAT_HISTORY, {})
    if not isinstance(concept_history, dict):
        concept_history = {}
    concept_history = {d: v for d, v in concept_history.items() if d <= as_of_date}
    hot_concepts = detect_theme_lines(min_days=cfg.theme_line_min_days, as_of_date=as_of_date)
    return concept_heat, ths_events, event_heat, concept_history, hot_concepts


def _load_ths_hot_events() -> tuple[dict[str, Any], list[dict]]:
    _report_progress("元数据加载", "同花顺事件主线", 0.11)
    print("[funnel] 加载同花顺事件主线...")
    try:
        snapshot = fetch_ths_hot_events()
        rows = ths_hot_events_to_concept_heat(snapshot)
        summary = summarize_ths_hot_events(snapshot, limit=5)
        print(
            f"[funnel] 同花顺事件主线: events={len(snapshot.get('events') or [])}, heat_rows={len(rows)}, {summary or '无'}"
        )
        return snapshot, rows
    except Exception as e:
        logger.warning("同花顺事件主线加载失败，降级为空: %s", e)
        return {}, []


def _load_financial_metrics(all_symbols: list[str]) -> dict[str, dict]:
    if env_flag("FUNNEL_SKIP_FINANCIAL_METRICS"):
        print("[funnel] TickFlow 财务指标已按环境开关跳过")
        _report_progress("财务指标", "已按环境开关跳过", 0.20)
        return {}
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        _report_progress("财务指标", "未配置 TickFlow，跳过", 0.20)
        return {}
    try:
        from integrations.tickflow_client import TickFlowClient

        client = TickFlowClient(api_key=api_key)
        print(f"[funnel] TickFlow 财务指标请求: symbols={len(all_symbols)}")
        _report_progress("财务指标", f"请求{len(all_symbols)}只", 0.20)
        raw_fin = client.get_financial_metrics(all_symbols, latest=True)
        financial_map = {sym: records[0] for sym, records in raw_fin.items() if records}
        missing = max(len(all_symbols) - len(financial_map), 0)
        sample_missing = ",".join(sorted([s for s in all_symbols if s not in financial_map])[:8])
        print(
            f"[funnel] TickFlow 财务指标加载成功: {len(financial_map)}/{len(all_symbols)}, "
            f"missing={missing}, sample_missing={sample_missing or '-'}"
        )
        _report_progress("财务指标", f"成功{len(financial_map)}/{len(all_symbols)}", 0.24)
        return financial_map
    except Exception as e:
        logger.warning("TickFlow 财务指标加载失败，跳过财务过滤: %s", e)
        _report_progress("财务指标", "加载失败，跳过", 0.24)
        return {}


def _skipped_financial_metrics(reason: str) -> dict[str, dict]:
    print(f"[funnel] TickFlow 财务指标跳过: {reason}")
    _report_progress("财务指标", reason, 0.20)
    return {}


def _load_stock_names() -> dict[str, str]:
    _report_progress("股票名称", "加载代码名称映射", 0.25)
    print("[funnel] 加载股票名称...")
    try:
        out = load_stock_name_map()
        _report_progress("股票名称", f"加载{len(out)}条", 0.28)
        return out
    except Exception as e:
        logger.warning("股票名称加载失败，降级为代码展示: %s", e)
        _report_progress("股票名称", "加载失败，降级为代码", 0.28)
        return {}


def _load_reference_data(
    all_symbols: list[str],
    window,
    cfg: FunnelConfig,
    *,
    include_financial_metrics: bool = True,
) -> FunnelReferenceData:
    (
        sector_map,
        concept_map,
        concept_heat,
        ths_hot_events,
        event_concept_heat,
        concept_history,
        hot_concepts,
        market_cap_map,
    ) = _load_market_metadata(window, cfg)
    return FunnelReferenceData(
        sector_map=sector_map,
        concept_map=concept_map,
        concept_heat=concept_heat,
        ths_hot_events=ths_hot_events,
        event_concept_heat=event_concept_heat,
        concept_heat_history=concept_history,
        hot_concepts=hot_concepts,
        market_cap_map=market_cap_map,
        financial_map=(
            _load_financial_metrics(all_symbols)
            if include_financial_metrics
            else _skipped_financial_metrics("聊天快扫已跳过")
        ),
        name_map=_load_stock_names(),
    )


def _build_benchmark_context(
    all_df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    smallcap_df: pd.DataFrame | None,
    cfg: FunnelConfig,
) -> dict:
    _report_progress("大盘水温", "计算广度/资金/总闸", 0.78)
    breadth_context = calc_market_breadth(all_df_map, BREADTH_MA_WINDOW)
    money_flow_context = calc_market_money_flow(
        all_df_map,
        breadth_context,
        config=market_money_flow_config_from_env(),
    )
    amount_distribution_context = calc_amount_distribution_health(
        all_df_map,
        cfg.min_avg_amount_wan,
        cfg.amount_avg_window,
        config=amount_distribution_config_from_env(),
    )
    benchmark_context = analyze_benchmark_and_tune_cfg(
        bench_df,
        smallcap_df,
        cfg,
        breadth=breadth_context,
        money_flow=money_flow_context,
        amount_distribution=amount_distribution_context,
        regime_config=market_regime_config_from_env(),
    )
    _print_benchmark_gate(benchmark_context)
    _report_progress("大盘水温", f"regime={benchmark_context.get('regime')}", 0.82)
    return benchmark_context


def _print_benchmark_gate(benchmark_context: dict) -> None:
    print(
        "[funnel] 大盘总闸: "
        f"regime={benchmark_context['regime']}, "
        f"close={benchmark_context['close']}, ma50={benchmark_context['ma50']}, ma200={benchmark_context['ma200']}, "
        f"ma50_slope_5d={benchmark_context['ma50_slope_5d']}, main_today={benchmark_context.get('main_today_pct')}, "
        f"recent3={benchmark_context['recent3_pct']}, recent3_cum={benchmark_context['recent3_cum_pct']}, "
        f"smallcap_code={benchmark_context.get('smallcap_code')}, "
        f"smallcap_today={benchmark_context.get('smallcap_today_pct')}, "
        f"breadth={benchmark_context.get('breadth')}, money_flow={benchmark_context.get('money_flow')}, "
        f"amount_distribution={benchmark_context.get('amount_distribution')}, "
        f"holiday_grace={benchmark_context.get('holiday_grace_dynamic')}, "
        f"pv_policy_shadow={benchmark_context.get('market_pv_policy_shadow')}, "
        f"panic_triggered={benchmark_context.get('panic_triggered')}, "
        f"panic_reasons={benchmark_context.get('panic_reasons')}, "
        f"repair_triggered={benchmark_context.get('repair_triggered')}, "
        f"repair_reasons={benchmark_context.get('repair_reasons')}, tuned={benchmark_context['tuned']}"
    )
