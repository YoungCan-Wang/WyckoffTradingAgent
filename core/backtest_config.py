"""Validated backtest runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from core.a_share_entry_research import AShareEntryResearchPolicy
from core.ai_candidate_allocation import AiCandidateAllocationConfig
from core.backtest_execution import ExitSimulationConfig, IntradayPriceFetcher
from core.backtest_performance import BacktestPerformanceConfig
from core.backtest_replay import BacktestReplayConfig, MarketBreadthCalculator, MarketRegimeAnalyzer
from core.candidate_policy import CandidatePolicyConfig
from core.cash_portfolio import CashPortfolioConfig, expand_portfolio_styles
from core.mainline_engine import MainlineEngineConfig


@dataclass(frozen=True)
class BacktestRunInput:
    start_dt: date
    end_dt: date
    hold_days: int
    board: str
    top_n: int
    trading_days: int
    snapshot_dir: Path | None
    exit_config: ExitSimulationConfig
    trailing_activate_pct: float
    buy_friction_pct: float
    sell_friction_pct: float
    regime_filter: bool
    execution_regime_gate: str
    pending_mode: str
    pending_merge_order: str
    metrics_engine: str
    wbt_fee_rate: float
    wbt_n_jobs: int
    abc_filter: bool
    entry_price_mode: str
    entry_price_time: str
    entry_price_fallback: str
    cash_portfolio: bool
    cash_config: CashPortfolioConfig
    portfolio_styles: str | list[str]
    full_formal_l4_max: int
    selection_mode: str
    max_atr_hold_days: int
    strategy_variant: str = "live"
    a_share_entry_research: AShareEntryResearchPolicy = field(default_factory=AShareEntryResearchPolicy)
    intraday_entry_price_fetcher: IntradayPriceFetcher | None = None
    funnel_config_overrides: dict[str, object] = field(default_factory=dict)
    market_breadth_calculator: MarketBreadthCalculator | None = None
    market_regime_analyzer: MarketRegimeAnalyzer | None = None
    candidate_policy: CandidatePolicyConfig = field(default_factory=CandidatePolicyConfig)
    ai_allocation: AiCandidateAllocationConfig = field(default_factory=AiCandidateAllocationConfig)
    concept_map: dict[str, list[str]] = field(default_factory=dict)
    concept_heat: list[dict] = field(default_factory=list)
    theme_radar: dict = field(default_factory=dict)
    financial_map: dict[str, dict] = field(default_factory=dict)
    mainline_config: MainlineEngineConfig | None = None
    signal_weight_map: dict[str, float] = field(default_factory=dict)
    signal_weight_meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestRunConfig:
    metrics_engine: str
    entry_price_mode: str
    entry_price_fallback: str
    pending_mode: str
    pending_merge_order: str
    strategy_variant: str
    snapshot_dir: Path | None
    funnel_config_overrides: dict[str, object]
    portfolio_style_list: list[str]
    replay: BacktestReplayConfig
    performance: BacktestPerformanceConfig


def build_backtest_run_config(params: BacktestRunInput) -> BacktestRunConfig:
    metrics_engine, entry_price_mode, entry_price_fallback, pending_mode, pending_merge_order = _normalized_modes(
        params
    )
    _validate_run_input(
        params, metrics_engine, entry_price_mode, entry_price_fallback, pending_mode, pending_merge_order
    )
    style_list = expand_portfolio_styles(params.portfolio_styles)
    snapshot = Path(params.snapshot_dir).resolve() if params.snapshot_dir is not None else None
    replay = _replay_config(
        params,
        pending_mode=pending_mode,
        pending_merge_order=pending_merge_order,
        entry_price_mode=entry_price_mode,
        entry_price_fallback=entry_price_fallback,
    )
    performance = _performance_config(
        params.hold_days,
        params.buy_friction_pct,
        params.sell_friction_pct,
        metrics_engine,
        params.wbt_fee_rate,
        params.wbt_n_jobs,
        params.cash_portfolio,
        params.cash_config,
        style_list,
    )
    return BacktestRunConfig(
        metrics_engine=metrics_engine,
        entry_price_mode=entry_price_mode,
        entry_price_fallback=entry_price_fallback,
        pending_mode=pending_mode,
        pending_merge_order=pending_merge_order,
        strategy_variant=params.strategy_variant,
        snapshot_dir=snapshot,
        funnel_config_overrides=dict(params.funnel_config_overrides),
        portfolio_style_list=style_list,
        replay=replay,
        performance=performance,
    )


def _validate_run_input(
    params: BacktestRunInput,
    metrics_engine: str,
    entry_price_mode: str,
    entry_price_fallback: str,
    pending_mode: str,
    pending_merge_order: str,
) -> None:
    _validate_modes(
        metrics_engine,
        entry_price_mode,
        entry_price_fallback,
        pending_mode,
        pending_merge_order,
        params.exit_config,
    )
    if str(params.execution_regime_gate or "live").strip().lower() not in {"live", "off", "neutral_only"}:
        raise ValueError("execution_regime_gate 必须是 live / off / neutral_only")
    _validate_dates_and_trade_params(
        params.start_dt, params.end_dt, params.hold_days, params.exit_config, params.trailing_activate_pct
    )
    _validate_costs_and_cash(
        params.buy_friction_pct,
        params.sell_friction_pct,
        params.wbt_fee_rate,
        params.wbt_n_jobs,
        params.cash_config,
    )


def _normalized_modes(params: BacktestRunInput) -> tuple[str, str, str, str, str]:
    return (
        str(params.metrics_engine or "legacy").strip().lower(),
        str(params.entry_price_mode or "open").strip().lower(),
        str(params.entry_price_fallback or "close").strip().lower(),
        str(params.pending_mode or "off").strip().lower(),
        str(params.pending_merge_order or "funnel_first").strip().lower(),
    )


def _validate_modes(
    metrics_engine: str,
    entry_price_mode: str,
    entry_price_fallback: str,
    pending_mode: str,
    pending_merge_order: str,
    exit_config: ExitSimulationConfig,
) -> None:
    if metrics_engine not in {"legacy", "auto", "both", "wbt"}:
        raise ValueError("metrics_engine 必须是 legacy / auto / both / wbt")
    if entry_price_mode not in {"open", "close", "tail_1455"}:
        raise ValueError("entry_price_mode 必须是 open / close / tail_1455")
    if entry_price_fallback not in {"close", "skip", "error"}:
        raise ValueError("entry_price_fallback 必须是 close / skip / error")
    if pending_mode not in {"off", "only", "both"}:
        raise ValueError("pending_mode 必须是 off / only / both")
    if pending_merge_order not in {"funnel_first", "confirmed_first"}:
        raise ValueError("pending_merge_order 必须是 funnel_first 或 confirmed_first")
    if exit_config.exit_mode not in {"close_only", "sltp", "atr"}:
        raise ValueError("exit_mode 必须是 close_only、sltp 或 atr")
    if exit_config.sltp_priority not in {"stop_first", "take_first"}:
        raise ValueError("sltp_priority 必须是 stop_first 或 take_first")


def _validate_dates_and_trade_params(
    start_dt: date,
    end_dt: date,
    hold_days: int,
    exit_config: ExitSimulationConfig,
    trailing_activate_pct: float,
) -> None:
    if end_dt <= start_dt:
        raise ValueError("end 必须晚于 start")
    if hold_days < 1:
        raise ValueError("hold_days 必须 >= 1")
    if exit_config.trailing_stop_pct > 0:
        raise ValueError("trailing_stop_pct 必须 <= 0（如 -5.0 表示从最高点回撤 5%），0 表示不启用")
    if trailing_activate_pct < 0:
        raise ValueError("trailing_activate_pct 必须 >= 0（如 10.0 表示浮盈 10% 后激活），0 表示立即启用")
    if exit_config.stop_loss_pct > 0:
        raise ValueError("stop_loss_pct 必须 <= 0，0 表示不设止损")
    if exit_config.take_profit_pct < 0:
        raise ValueError("take_profit_pct 必须 >= 0，0 表示不设止盈")


def _validate_costs_and_cash(
    buy_friction_pct: float,
    sell_friction_pct: float,
    wbt_fee_rate: float,
    wbt_n_jobs: int,
    cash_config: CashPortfolioConfig,
) -> None:
    if buy_friction_pct < 0 or sell_friction_pct < 0:
        raise ValueError("buy_friction_pct / sell_friction_pct 必须 >= 0")
    if buy_friction_pct >= 100 or sell_friction_pct >= 100:
        raise ValueError("buy_friction_pct / sell_friction_pct 必须 < 100")
    if wbt_fee_rate < 0:
        raise ValueError("wbt_fee_rate 必须 >= 0")
    if wbt_n_jobs < 1:
        raise ValueError("wbt_n_jobs 必须 >= 1")
    if cash_config.initial_cash <= 0:
        raise ValueError("initial_cash 必须 > 0")
    if cash_config.max_positions < 1:
        raise ValueError("max_positions 必须 >= 1")
    if cash_config.commission_rate < 0 or cash_config.small_trade_threshold < 0 or cash_config.small_trade_fee < 0:
        raise ValueError("commission_rate / small_trade_threshold / small_trade_fee 必须 >= 0")
    if cash_config.lot_size < 1:
        raise ValueError("lot_size 必须 >= 1")


def _replay_config(
    params: BacktestRunInput,
    pending_mode: str,
    pending_merge_order: str,
    entry_price_mode: str,
    entry_price_fallback: str,
) -> BacktestReplayConfig:
    return BacktestReplayConfig(
        trading_days=params.trading_days,
        hold_days=params.hold_days,
        board=params.board,
        top_n=int(params.top_n),
        selection_mode=params.selection_mode,
        full_formal_l4_max=params.full_formal_l4_max,
        regime_filter=False,
        execution_regime_gate=str(params.execution_regime_gate or "live").strip().lower(),
        pending_mode=pending_mode,
        pending_merge_order=pending_merge_order,
        abc_filter=bool(params.abc_filter),
        entry_price_mode=entry_price_mode,
        entry_price_time=params.entry_price_time,
        entry_price_fallback=entry_price_fallback,
        buy_friction_pct=params.buy_friction_pct,
        sell_friction_pct=params.sell_friction_pct,
        max_atr_hold_days=params.max_atr_hold_days,
        intraday_entry_price_fetcher=params.intraday_entry_price_fetcher,
        market_breadth_calculator=params.market_breadth_calculator,
        market_regime_analyzer=params.market_regime_analyzer,
        exit=params.exit_config,
        candidate_policy=params.candidate_policy,
        ai_allocation=params.ai_allocation,
        concept_map=dict(params.concept_map),
        concept_heat=list(params.concept_heat),
        theme_radar=dict(params.theme_radar),
        financial_map=dict(params.financial_map),
        mainline_config=params.mainline_config,
        signal_weight_map=dict(params.signal_weight_map),
        signal_weight_meta=dict(params.signal_weight_meta),
        a_share_entry_research=params.a_share_entry_research,
    )


def _performance_config(
    hold_days: int,
    buy_friction_pct: float,
    sell_friction_pct: float,
    metrics_engine: str,
    wbt_fee_rate: float,
    wbt_n_jobs: int,
    cash_portfolio: bool,
    cash_config: CashPortfolioConfig,
    style_list: list[str],
) -> BacktestPerformanceConfig:
    cash_configs = [
        replace(
            cash_config,
            portfolio_style=style,
            buy_friction_pct=buy_friction_pct,
            sell_friction_pct=sell_friction_pct,
        )
        for style in style_list
    ]
    return BacktestPerformanceConfig(
        hold_days=hold_days,
        buy_friction_pct=buy_friction_pct,
        metrics_engine=metrics_engine,
        wbt_fee_rate=wbt_fee_rate,
        wbt_n_jobs=wbt_n_jobs,
        cash_portfolio=bool(cash_portfolio),
        cash_config_by_style=cash_configs,
    )
