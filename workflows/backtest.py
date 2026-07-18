"""Backtest workflow orchestration over data adapters and core replay engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from pathlib import Path

import pandas as pd

from core.backtest_config import BacktestRunConfig, BacktestRunInput, build_backtest_run_config
from core.backtest_execution import ExitSimulationConfig
from core.backtest_run import (
    BacktestPreparedData,
    BacktestRunContext,
    build_backtest_signal_ledger,
    execute_backtest_run,
)
from core.cash_portfolio import CashPortfolioConfig
from core.dynamic_policy import dynamic_policy_mode
from core.market_breadth import calc_market_breadth
from core.theme_activity import build_theme_member_index
from tools.mainline_config import load_mainline_engine_config
from tools.market_regime import analyze_benchmark_and_tune_cfg
from workflows.ai_candidate_allocation_config import ai_candidate_allocation_config_from_env
from workflows.backtest_data import (
    BacktestUniverse,
    ProgressReporter,
    load_backtest_history,
    load_backtest_metadata,
    normalize_backtest_board,
    resolve_backtest_universe,
)
from workflows.backtest_defaults import (
    DEFAULT_ATR_HARD_STOP_PCT,
    DEFAULT_ATR_MAX_HOLD_DAYS,
    DEFAULT_ATR_MULTIPLIER,
    DEFAULT_ATR_PERIOD,
    DEFAULT_BUY_FRICTION_PCT,
    DEFAULT_CASH_PORTFOLIO_COMMISSION_RATE,
    DEFAULT_CASH_PORTFOLIO_INITIAL_CASH,
    DEFAULT_CASH_PORTFOLIO_LOT_SIZE,
    DEFAULT_CASH_PORTFOLIO_MAX_POSITIONS,
    DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_FEE,
    DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_THRESHOLD,
    DEFAULT_CASH_PORTFOLIO_STYLES,
    DEFAULT_ENTRY_PRICE_FALLBACK,
    DEFAULT_ENTRY_PRICE_TIME,
    DEFAULT_EXIT_MODE,
    DEFAULT_METRICS_ENGINE,
    DEFAULT_SELL_FRICTION_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_TRAILING_ACTIVATE_PCT,
    DEFAULT_TRAILING_STOP_PCT,
    DEFAULT_USE_CURRENT_META,
    DEFAULT_WBT_FEE_RATE,
    DEFAULT_WBT_N_JOBS,
    FUNNEL_AI_SELECTION_MODE,
    full_formal_l4_max,
)
from workflows.backtest_intraday import tickflow_entry_price_fetcher_from_env
from workflows.backtest_strategy_variants import (
    normalize_strategy_variant,
    strategy_variant_entry_policy,
    strategy_variant_overrides,
)
from workflows.candidate_policy_config import candidate_policy_config_from_env
from workflows.dynamic_policy_config import dynamic_policy_config_from_env
from workflows.funnel_config_overrides import funnel_cfg_overrides_from_env
from workflows.market_regime_config import market_regime_config_from_env
from workflows.strategy_attribution_policy import attribution_weights_for_funnel, load_attribution_policy_snapshot

logger = logging.getLogger(__name__)
BACKTEST_FULL_FORMAL_L4_MAX = full_formal_l4_max()


@dataclass(frozen=True)
class BacktestWorkflowRequest:
    start_dt: date
    end_dt: date
    hold_days: int
    top_n: int
    board: str
    sample_size: int
    trading_days: int
    max_workers: int
    snapshot_dir: Path | None = None
    benchmark: str = "000001"
    exit_mode: str = DEFAULT_EXIT_MODE
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT
    trailing_stop_pct: float = DEFAULT_TRAILING_STOP_PCT
    trailing_activate_pct: float = DEFAULT_TRAILING_ACTIVATE_PCT
    sltp_priority: str = "stop_first"
    use_current_meta: bool = DEFAULT_USE_CURRENT_META
    buy_friction_pct: float = DEFAULT_BUY_FRICTION_PCT
    sell_friction_pct: float = DEFAULT_SELL_FRICTION_PCT
    regime_filter: bool = False
    execution_regime_gate: str = "live"
    strategy_variant: str = "live"
    pending_mode: str = "only"
    pending_merge_order: str = "confirmed_first"
    atr_period: int = DEFAULT_ATR_PERIOD
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER
    atr_hard_stop_pct: float = DEFAULT_ATR_HARD_STOP_PCT
    metrics_engine: str = DEFAULT_METRICS_ENGINE
    wbt_fee_rate: float = DEFAULT_WBT_FEE_RATE
    wbt_n_jobs: int = DEFAULT_WBT_N_JOBS
    abc_filter: bool = False
    entry_price_mode: str = "open"
    entry_price_time: str = DEFAULT_ENTRY_PRICE_TIME
    entry_price_fallback: str = DEFAULT_ENTRY_PRICE_FALLBACK
    cash_portfolio: bool = False
    initial_cash: float = DEFAULT_CASH_PORTFOLIO_INITIAL_CASH
    max_positions: int = DEFAULT_CASH_PORTFOLIO_MAX_POSITIONS
    commission_rate: float = DEFAULT_CASH_PORTFOLIO_COMMISSION_RATE
    small_trade_threshold: float = DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_THRESHOLD
    small_trade_fee: float = DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_FEE
    lot_size: int = DEFAULT_CASH_PORTFOLIO_LOT_SIZE
    portfolio_styles: str | list[str] = DEFAULT_CASH_PORTFOLIO_STYLES


def run_backtest_request(
    request: BacktestWorkflowRequest,
    progress: ProgressReporter | None = None,
) -> tuple[pd.DataFrame, dict]:
    progress = progress or _noop_progress
    config = _build_run_config(request)
    universe = resolve_backtest_universe(request.board, request.sample_size, config.snapshot_dir)
    _report_universe(universe, request, progress)
    data = _load_prepared_data(request, config, universe, progress)
    context = BacktestRunContext(
        start_dt=request.start_dt,
        end_dt=request.end_dt,
        board=request.board,
        sample_size=request.sample_size,
        use_current_meta=request.use_current_meta,
    )
    return execute_backtest_run(context=context, data=data, config=config, progress=progress)


def run_backtest_request_suite(
    requests: list[BacktestWorkflowRequest],
    progress: ProgressReporter | None = None,
) -> list[tuple[pd.DataFrame, dict]]:
    if not requests:
        raise ValueError("回测参数组合不能为空")
    progress = progress or _noop_progress
    configs = [_build_run_config(request) for request in requests]
    _validate_shared_signal_suite(requests)
    first = requests[0]
    universe = resolve_backtest_universe(first.board, first.sample_size, configs[0].snapshot_dir)
    _report_universe(universe, first, progress)
    load_request = max(requests, key=lambda item: item.hold_days)
    data = _load_prepared_data(load_request, configs[0], universe, progress)
    context = BacktestRunContext(first.start_dt, first.end_dt, first.board, first.sample_size, first.use_current_meta)
    ledger = build_backtest_signal_ledger(
        context=context,
        data=data,
        config=configs[0],
        signal_hold_days=min(request.hold_days for request in requests),
        progress=progress,
    )
    return [execute_backtest_run(context=context, data=data, config=config, signal_ledger=ledger) for config in configs]


def _validate_shared_signal_suite(requests: list[BacktestWorkflowRequest]) -> None:
    first_request = requests[0]
    request_key = _shared_request_key(first_request)
    if any(_shared_request_key(request) != request_key for request in requests[1:]):
        raise ValueError("复用信号台账的回测组合只能改变持有期和退出参数")


def _shared_request_key(request: BacktestWorkflowRequest) -> tuple:
    ignored = {
        "hold_days",
        "exit_mode",
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "trailing_activate_pct",
        "sltp_priority",
        "atr_period",
        "atr_multiplier",
        "atr_hard_stop_pct",
    }
    return tuple((name, value) for name, value in vars(request).items() if name not in ignored)


def _build_run_config(request: BacktestWorkflowRequest) -> BacktestRunConfig:
    signal_weight_map, signal_weight_meta = _signal_policy_from_env()
    strategy_variant = normalize_strategy_variant(request.strategy_variant)
    funnel_overrides = {**funnel_cfg_overrides_from_env(), **strategy_variant_overrides(strategy_variant)}
    return build_backtest_run_config(
        BacktestRunInput(
            start_dt=request.start_dt,
            end_dt=request.end_dt,
            hold_days=request.hold_days,
            board=normalize_backtest_board(request.board),
            top_n=request.top_n,
            trading_days=request.trading_days,
            snapshot_dir=request.snapshot_dir,
            exit_config=_exit_config(request),
            trailing_activate_pct=request.trailing_activate_pct,
            buy_friction_pct=request.buy_friction_pct,
            sell_friction_pct=request.sell_friction_pct,
            regime_filter=request.regime_filter,
            execution_regime_gate=request.execution_regime_gate,
            strategy_variant=strategy_variant,
            a_share_entry_research=strategy_variant_entry_policy(strategy_variant),
            pending_mode=request.pending_mode,
            pending_merge_order=request.pending_merge_order,
            metrics_engine=request.metrics_engine,
            wbt_fee_rate=request.wbt_fee_rate,
            wbt_n_jobs=request.wbt_n_jobs,
            abc_filter=request.abc_filter,
            entry_price_mode=request.entry_price_mode,
            entry_price_time=request.entry_price_time,
            entry_price_fallback=request.entry_price_fallback or DEFAULT_ENTRY_PRICE_FALLBACK,
            cash_portfolio=request.cash_portfolio,
            cash_config=_cash_config(request),
            portfolio_styles=request.portfolio_styles,
            full_formal_l4_max=BACKTEST_FULL_FORMAL_L4_MAX,
            selection_mode=FUNNEL_AI_SELECTION_MODE,
            max_atr_hold_days=DEFAULT_ATR_MAX_HOLD_DAYS,
            intraday_entry_price_fetcher=_intraday_entry_price_fetcher(request),
            funnel_config_overrides=funnel_overrides,
            market_breadth_calculator=calc_market_breadth,
            market_regime_analyzer=_market_regime_analyzer_from_env(),
            candidate_policy=candidate_policy_config_from_env(),
            ai_allocation=ai_candidate_allocation_config_from_env(),
            mainline_config=load_mainline_engine_config(),
            signal_weight_map=signal_weight_map,
            signal_weight_meta=signal_weight_meta,
        )
    )


def _market_regime_analyzer_from_env():
    return partial(analyze_benchmark_and_tune_cfg, regime_config=market_regime_config_from_env())


def _signal_weight_map_from_env() -> dict[str, float]:
    return _signal_policy_from_env()[0]


def _signal_policy_from_env() -> tuple[dict[str, float], dict[str, object]]:
    config = dynamic_policy_config_from_env()
    mode = dynamic_policy_mode(config)
    if mode == "off":
        return {}, {}
    snapshot = load_attribution_policy_snapshot(market="cn", log_fn=lambda message: logger.info(message))
    weights = attribution_weights_for_funnel(snapshot, mode=mode, log_fn=lambda message: logger.info(message))
    return weights, snapshot.as_dict()


def _intraday_entry_price_fetcher(request: BacktestWorkflowRequest):
    if str(request.entry_price_mode or "open").strip().lower() != "tail_1455":
        return None
    return tickflow_entry_price_fetcher_from_env()


def _load_prepared_data(
    request: BacktestWorkflowRequest,
    config: BacktestRunConfig,
    universe: BacktestUniverse,
    progress: ProgressReporter,
) -> BacktestPreparedData:
    prefetch_start = request.start_dt - timedelta(days=request.trading_days * 3)
    prefetch_end = request.end_dt + timedelta(days=request.hold_days * 3 + 30)
    history = load_backtest_history(
        symbols=universe.symbols,
        snapshot_dir=config.snapshot_dir,
        benchmark=request.benchmark,
        start_dt=prefetch_start,
        end_dt=prefetch_end,
        max_workers=request.max_workers,
        progress=progress,
    )
    metadata = load_backtest_metadata(request.use_current_meta, config.snapshot_dir)
    return BacktestPreparedData(
        all_df_map=history.all_df_map,
        bench_df=history.bench_df,
        name_map=universe.name_map,
        market_cap_map=metadata.market_cap_map,
        sector_map=metadata.sector_map,
        concept_map=metadata.concept_map,
        concept_heat=metadata.concept_heat,
        financial_map=metadata.financial_map,
        failures=history.failures,
        theme_member_index=build_theme_member_index(metadata.concept_map, metadata.sector_map),
        snapshot_rows_total=history.snapshot_rows_total,
        snapshot_used=history.snapshot_used,
        metadata_source=metadata.source,
    )


def _exit_config(request: BacktestWorkflowRequest) -> ExitSimulationConfig:
    return ExitSimulationConfig(
        exit_mode=request.exit_mode,
        stop_loss_pct=request.stop_loss_pct,
        take_profit_pct=request.take_profit_pct,
        trailing_stop_pct=request.trailing_stop_pct,
        trailing_activate_pct=request.trailing_activate_pct,
        sltp_priority=request.sltp_priority,
        atr_period=request.atr_period,
        atr_multiplier=request.atr_multiplier,
        atr_hard_stop_pct=request.atr_hard_stop_pct,
    )


def _cash_config(request: BacktestWorkflowRequest) -> CashPortfolioConfig:
    return CashPortfolioConfig(
        initial_cash=request.initial_cash,
        max_positions=request.max_positions,
        commission_rate=request.commission_rate,
        small_trade_threshold=request.small_trade_threshold,
        small_trade_fee=request.small_trade_fee,
        lot_size=request.lot_size,
        buy_friction_pct=request.buy_friction_pct,
        sell_friction_pct=request.sell_friction_pct,
    )


def _report_universe(universe: BacktestUniverse, request: BacktestWorkflowRequest, progress: ProgressReporter) -> None:
    logger.info(
        "股票池=%d (%s, board=%s, sample_size=%s)",
        len(universe.symbols),
        universe.source,
        request.board,
        request.sample_size,
    )
    progress("股票池建立", f"共{len(universe.symbols)}只", 0.0)
    if not universe.symbols:
        raise RuntimeError("股票池为空")


def _noop_progress(_stage: str, _detail: str, _ratio: float) -> None:
    return None
