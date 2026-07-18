"""Core orchestration for replaying a prepared daily backtest dataset."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime

import pandas as pd

from core.backtest_config import BacktestRunConfig
from core.backtest_performance import enrich_backtest_summary
from core.backtest_replay import (
    BacktestReplayResult,
    BacktestSignalLedger,
    build_signal_ledger,
    replay_backtest,
    replay_signal_ledger,
)
from core.hk_boards import apply_hk_funnel_cfg
from core.theme_activity import build_theme_member_index
from core.wyckoff_engine import FunnelConfig

ProgressReporter = Callable[[str, str, float], None]


@dataclass(frozen=True)
class BacktestRunContext:
    start_dt: date
    end_dt: date
    board: str
    sample_size: int
    use_current_meta: bool


@dataclass(frozen=True)
class BacktestPreparedData:
    all_df_map: dict[str, pd.DataFrame]
    bench_df: pd.DataFrame
    name_map: dict[str, str]
    market_cap_map: dict[str, float]
    sector_map: dict[str, str]
    concept_map: dict[str, list[str]]
    concept_heat: list[dict]
    financial_map: dict[str, dict]
    failures: list[str]
    theme_member_index: dict[str, list[str]] | None = None
    snapshot_rows_total: int = 0
    snapshot_used: bool = False
    metadata_source: str = "disabled"


def parse_date(v: str) -> date:
    s = str(v).strip().replace("/", "-")
    if "-" in s:
        return datetime.strptime(s, "%Y-%m-%d").date()
    return datetime.strptime(s, "%Y%m%d").date()


def execute_backtest_run(
    *,
    context: BacktestRunContext,
    data: BacktestPreparedData,
    config: BacktestRunConfig,
    progress: ProgressReporter | None = None,
    signal_ledger: BacktestSignalLedger | None = None,
) -> tuple[pd.DataFrame, dict]:
    trade_dates = _trade_dates(data.bench_df, context.start_dt, context.end_dt)
    _validate_trade_dates(trade_dates, config.replay.hold_days)
    replay_config = _replay_config_with_metadata(config, data)
    if signal_ledger is None:
        replay = replay_backtest(
            all_df_map=data.all_df_map,
            bench_df=data.bench_df,
            trade_dates=trade_dates,
            name_map=data.name_map,
            market_cap_map=data.market_cap_map,
            sector_map=data.sector_map,
            base_cfg=_base_funnel_config(config),
            config=replay_config,
            progress=progress,
        )
    else:
        replay = replay_signal_ledger(
            ledger=signal_ledger,
            all_df_map=data.all_df_map,
            trade_dates=trade_dates,
            name_map=data.name_map,
            config=replay_config,
        )
    trades_df = pd.DataFrame([record.__dict__ for record in replay.records])
    summary = _build_run_summary(context, data, config, replay, trades_df)
    summary = enrich_backtest_summary(
        summary,
        trades_df=trades_df,
        records=replay.records,
        all_df_map=data.all_df_map,
        ohlc_lookup_cache=replay.ohlc_lookup_cache,
        trade_dates=trade_dates,
        start_dt=context.start_dt,
        end_dt=context.end_dt,
        config=config.performance,
    )
    return trades_df, summary


def build_backtest_signal_ledger(
    *,
    context: BacktestRunContext,
    data: BacktestPreparedData,
    config: BacktestRunConfig,
    signal_hold_days: int,
    progress: ProgressReporter | None = None,
) -> BacktestSignalLedger:
    trade_dates = _trade_dates(data.bench_df, context.start_dt, context.end_dt)
    _validate_trade_dates(trade_dates, signal_hold_days)
    return build_signal_ledger(
        all_df_map=data.all_df_map,
        bench_df=data.bench_df,
        trade_dates=trade_dates,
        name_map=data.name_map,
        market_cap_map=data.market_cap_map,
        sector_map=data.sector_map,
        base_cfg=_base_funnel_config(config),
        config=_replay_config_with_metadata(config, data),
        max_idx=len(trade_dates) - signal_hold_days - 1,
        progress=progress,
    )


def _trade_dates(bench_df: pd.DataFrame, start_dt: date, end_dt: date) -> list[date]:
    return [d for d in bench_df["date"].tolist() if start_dt <= d <= end_dt]


def _validate_trade_dates(trade_dates: list[date], hold_days: int) -> None:
    if len(trade_dates) <= hold_days + 1:
        raise RuntimeError(
            f"回测区间交易日过少({len(trade_dates)})，无法计算 forward return (hold_days={hold_days}，需至少 {hold_days + 2} 个交易日)"
        )


def _base_funnel_config(config: BacktestRunConfig) -> FunnelConfig:
    base_cfg = FunnelConfig(trading_days=config.replay.trading_days)
    if config.replay.board == "us":
        _apply_us_cfg(base_cfg)
    elif config.replay.board == "hk":
        apply_hk_funnel_cfg(base_cfg)
    _apply_funnel_config_overrides(base_cfg, config.funnel_config_overrides)
    # Backtest selection never consumes display-only radar rows; skip their full-universe scan.
    base_cfg.enable_leader_radar = False
    return base_cfg


def _replay_config_with_metadata(config: BacktestRunConfig, data: BacktestPreparedData):
    return replace(
        config.replay,
        concept_map=data.concept_map,
        concept_heat=data.concept_heat,
        financial_map=data.financial_map,
        theme_member_index=data.theme_member_index
        if data.theme_member_index is not None
        else build_theme_member_index(data.concept_map, data.sector_map),
    )


def _apply_funnel_config_overrides(cfg: FunnelConfig, overrides: dict[str, object]) -> None:
    for name, value in overrides.items():
        if name == "enable_evr_trigger":
            continue
        if not hasattr(cfg, name):
            raise ValueError(f"未知 FunnelConfig 覆盖字段: {name}")
        setattr(cfg, name, value)


def _apply_us_cfg(cfg: FunnelConfig) -> None:
    cfg.require_cn_main_or_chinext = False
    cfg.enable_rs_filter = False
    cfg.enable_rs_divergence_channel = False
    cfg.require_bench_latest_alignment = False
    cfg.sos_pct_min = 7.0
    cfg.sos_vol_ratio = 3.0
    cfg.spring_vol_ratio = 1.3
    cfg.evr_max_rise = 3.0


def _build_run_summary(
    context: BacktestRunContext,
    data: BacktestPreparedData,
    config: BacktestRunConfig,
    replay: BacktestReplayResult,
    trades_df: pd.DataFrame,
) -> dict:
    summary = _base_summary(context, data, config, replay, trades_df)
    summary.update(_exit_summary(config))
    summary.update(_execution_summary(config, replay, trades_df))
    summary.update(_cash_summary(config))
    summary.update(_wbt_summary(config))
    return summary


def _base_summary(
    context: BacktestRunContext,
    data: BacktestPreparedData,
    config: BacktestRunConfig,
    replay: BacktestReplayResult,
    trades_df: pd.DataFrame,
) -> dict:
    return {
        "start": context.start_dt.isoformat(),
        "end": context.end_dt.isoformat(),
        "hold_days": config.replay.hold_days,
        "top_n": config.replay.top_n,
        "ai_selection_mode": config.replay.selection_mode,
        "strategy_variant": config.strategy_variant,
        "a_share_entry_research": asdict(config.replay.a_share_entry_research),
        "ai_top_n_cap": None if config.replay.top_n <= 0 else config.replay.top_n,
        "board": context.board,
        "sample_size": context.sample_size,
        "trading_days": config.replay.trading_days,
        "universe_ok": len(data.all_df_map),
        "universe_fail": len(data.failures),
        "snapshot_used": data.snapshot_used,
        "snapshot_rows_total": data.snapshot_rows_total,
        "concept_map_loaded": len(data.concept_map),
        "concept_heat_loaded": len(data.concept_heat),
        "financial_map_loaded": len(data.financial_map),
        "metadata_source": data.metadata_source,
        "mainline_engine_enabled": bool(config.replay.mainline_config and config.replay.mainline_config.enabled),
        "signal_weight_count": len(config.replay.signal_weight_map),
        "signal_weight_map": dict(config.replay.signal_weight_map),
        "signal_weight_meta": dict(config.replay.signal_weight_meta),
        "eval_days": replay.eval_days,
        "signal_days": replay.signal_days,
        "trades": len(trades_df),
        "use_current_meta": bool(context.use_current_meta),
    }


def _exit_summary(config: BacktestRunConfig) -> dict:
    exit_cfg = config.replay.exit
    return {
        "exit_mode": exit_cfg.exit_mode,
        "stop_loss_pct": exit_cfg.stop_loss_pct,
        "take_profit_pct": exit_cfg.take_profit_pct,
        "trailing_stop_pct": exit_cfg.trailing_stop_pct,
        "trailing_activate_pct": exit_cfg.trailing_activate_pct,
        "atr_period": exit_cfg.atr_period if exit_cfg.exit_mode == "atr" else None,
        "atr_multiplier": exit_cfg.atr_multiplier if exit_cfg.exit_mode == "atr" else None,
        "atr_hard_stop_pct": exit_cfg.atr_hard_stop_pct if exit_cfg.exit_mode == "atr" else None,
        "atr_max_hold_days": config.replay.max_atr_hold_days if exit_cfg.exit_mode == "atr" else None,
        "sltp_priority": exit_cfg.sltp_priority,
    }


def _execution_summary(config: BacktestRunConfig, replay: BacktestReplayResult, trades_df: pd.DataFrame) -> dict:
    summary = {
        "buy_friction_pct": float(config.replay.buy_friction_pct),
        "sell_friction_pct": float(config.replay.sell_friction_pct),
        "regime_filter": False,
        "regime_filter_note": "deprecated_live_aligned_noop",
        "execution_regime_gate": config.replay.execution_regime_gate,
        "regime_day_counts": dict(replay.regime_day_counts),
        "regime_blocked_signal_days": replay.regime_blocked_signal_days,
        "regime_blocked_candidates": replay.regime_blocked_candidates,
        "pending_mode": config.pending_mode,
        "pending_merge_order": config.pending_merge_order,
        "pending_confirmed_total": replay.pending_confirmed_total,
        "entry_price_mode": config.entry_price_mode,
        "entry_price_time": config.replay.entry_price_time if config.entry_price_mode == "tail_1455" else "",
        "entry_price_fallback": config.entry_price_fallback if config.entry_price_mode == "tail_1455" else "",
        "entry_price_missing_skipped": replay.entry_price_missing_skipped,
        "entry_price_source_counts": _entry_price_source_counts(trades_df),
    }
    summary.update({f"crash_probe_{key}": value for key, value in replay.crash_probe_stats.items()})
    return summary


def _cash_summary(config: BacktestRunConfig) -> dict:
    cash_cfg = config.performance.cash_config_by_style[0]
    return {
        "cash_portfolio_enabled": bool(config.performance.cash_portfolio),
        "cash_portfolio_styles_requested": ",".join(config.portfolio_style_list),
        "cash_portfolio_commission_rate": float(cash_cfg.commission_rate),
        "cash_portfolio_small_trade_threshold": float(cash_cfg.small_trade_threshold),
        "cash_portfolio_small_trade_fee": float(cash_cfg.small_trade_fee),
        "cash_portfolio_lot_size": int(cash_cfg.lot_size),
    }


def _wbt_summary(config: BacktestRunConfig) -> dict:
    return {
        "metrics_engine": config.metrics_engine,
        "wbt_fee_rate": float(config.performance.wbt_fee_rate),
        "wbt_n_jobs": int(config.performance.wbt_n_jobs),
        "wbt_requested": config.metrics_engine in {"auto", "both", "wbt"},
        "wbt_available": None,
        "wbt_error": "",
    }


def _entry_price_source_counts(trades_df: pd.DataFrame) -> dict[str, int]:
    if trades_df.empty or "entry_price_source" not in trades_df.columns:
        return {}
    counts = trades_df["entry_price_source"].value_counts(dropna=False).to_dict()
    return {str(k): int(v) for k, v in counts.items()}
