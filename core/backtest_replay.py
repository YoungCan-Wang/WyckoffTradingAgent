"""Daily backtest replay engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from time import monotonic

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from core._price_math import DATE_SORTED_ATTR
from core.a_share_entry_research import (
    AShareEntryResearchPolicy,
    calibrated_confirmation_score,
    confirmed_signal_allowed,
    market_context_allows_entry,
)
from core.ai_candidate_allocation import AiCandidateAllocationConfig
from core.backtest_crash_probe import (
    CrashProbeObservation,
    build_crash_probe_observations,
    summarize_crash_probe_replay,
)
from core.backtest_execution import (
    ExitSimulationConfig,
    IntradayPriceFetcher,
    TradeRecord,
    build_daily_ohlc_lookup,
    calc_trade_excursion_pct,
    entry_on_or_after,
    resolve_trade_exit,
)
from core.backtest_selection import combine_trigger_scores, select_ai_input_codes
from core.candidate_policy import CandidatePolicyConfig, candidate_score_value
from core.candidate_tracks import candidate_entry_track
from core.mainline_engine import MainlineEngineConfig
from core.market_breadth import calc_market_breadth
from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES, PROBE_ONLY_REGIMES, normalize_regime
from core.signal_confirmation import PendingPool, score_springboard_abc
from core.wyckoff_engine import FunnelConfig, FunnelResult, run_funnel

logger = logging.getLogger(__name__)
ProgressReporter = Callable[[str, str, float], None]
MarketBreadthCalculator = Callable[[dict[str, pd.DataFrame]], dict]
MarketRegimeAnalyzer = Callable[..., dict]
HistoryEndPositions = dict[str, NDArray[np.intp]]


def analyze_benchmark_and_tune_cfg(
    bench_df: pd.DataFrame | None,
    smallcap_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    *,
    breadth: dict | None = None,
    money_flow: dict | None = None,
    amount_distribution: dict | None = None,
) -> dict:
    return {"regime": "NEUTRAL"}


@dataclass(frozen=True)
class BacktestReplayConfig:
    trading_days: int
    hold_days: int
    board: str
    top_n: int
    selection_mode: str
    full_formal_l4_max: int
    regime_filter: bool
    execution_regime_gate: str
    pending_mode: str
    pending_merge_order: str
    abc_filter: bool
    entry_price_mode: str
    entry_price_time: str
    entry_price_fallback: str
    buy_friction_pct: float
    sell_friction_pct: float
    max_atr_hold_days: int
    exit: ExitSimulationConfig
    intraday_entry_price_fetcher: IntradayPriceFetcher | None = None
    candidate_policy: CandidatePolicyConfig = field(default_factory=CandidatePolicyConfig)
    ai_allocation: AiCandidateAllocationConfig = field(default_factory=AiCandidateAllocationConfig)
    concept_map: dict[str, list[str]] = field(default_factory=dict)
    concept_heat: list[dict] = field(default_factory=list)
    theme_radar: dict = field(default_factory=dict)
    financial_map: dict[str, dict] = field(default_factory=dict)
    theme_member_index: dict[str, list[str]] = field(default_factory=dict)
    mainline_config: MainlineEngineConfig | None = None
    signal_weight_map: dict[str, float] = field(default_factory=dict)
    signal_weight_meta: dict[str, object] = field(default_factory=dict)
    market_breadth_calculator: MarketBreadthCalculator | None = None
    market_regime_analyzer: MarketRegimeAnalyzer | None = None
    a_share_entry_research: AShareEntryResearchPolicy = field(default_factory=AShareEntryResearchPolicy)


@dataclass(frozen=True)
class BacktestReplayResult:
    records: list[TradeRecord]
    eval_days: int
    signal_days: int
    pending_confirmed_total: int
    entry_price_missing_skipped: int
    ohlc_lookup_cache: dict[str, dict[date, tuple[float, float, float, float]]]
    regime_day_counts: dict[str, int] = field(default_factory=dict)
    regime_blocked_signal_days: int = 0
    regime_blocked_candidates: int = 0
    crash_probe_stats: dict[str, float | int | str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestSignalLedger:
    days: list[_SignalDay]
    crash_probe_observations: list[CrashProbeObservation] = field(default_factory=list)


@dataclass(frozen=True)
class _DayContext:
    idx: int
    signal_date: date
    entry_target_date: date
    day_df_map: dict[str, pd.DataFrame]
    name_map: dict[str, str]
    day_cfg: FunnelConfig
    result: FunnelResult
    regime: str
    breadth: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _TradeContext:
    idx: int
    signal_date: date
    entry_target_date: date
    regime: str


@dataclass(frozen=True)
class _SignalDay:
    context: _TradeContext
    selected: _RankedSelection | None
    confirmed_count: int
    blocked_signal_day: bool
    blocked_candidates: int


@dataclass(frozen=True)
class _ConfirmedSignals:
    codes: list[str]
    score_map: dict[str, float]
    track_map: dict[str, str]
    trigger_map: dict[str, str]


@dataclass(frozen=True)
class _RankedSelection:
    codes: list[str]
    score_map: dict[str, float]
    track_map: dict[str, str]
    trigger_name_map: dict[str, tuple[float, str]]
    confirmed_codes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class _EntryPlan:
    entry_close: float
    actual_entry_date: date
    actual_entry_idx: int
    actual_exit_idx: int
    actual_exit_anchor: date
    entry_price_source: str


def apply_abc_filter(
    codes: list[str],
    day_df_map: dict[str, pd.DataFrame],
    triggers: dict[str, list],
) -> list[str]:
    passed: list[str] = []
    all_trigger_codes: dict[str, list[str]] = {}
    for trigger_type, hits in triggers.items():
        for code, _ in hits:
            all_trigger_codes.setdefault(str(code).strip(), []).append(trigger_type)
    for code in codes:
        df = day_df_map.get(code)
        if df is None or df.empty:
            continue
        best_count = max(
            score_springboard_abc(df, sig_type)["met_count"] for sig_type in all_trigger_codes.get(code, ["unknown"])
        )
        if best_count >= 2:
            passed.append(code)
    return passed


def replay_backtest(
    *,
    all_df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    trade_dates: list[date],
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    base_cfg: FunnelConfig,
    config: BacktestReplayConfig,
    progress: ProgressReporter | None = None,
) -> BacktestReplayResult:
    max_idx = len(trade_dates) - config.hold_days - 1
    ledger = build_signal_ledger(
        all_df_map=all_df_map,
        bench_df=bench_df,
        trade_dates=trade_dates,
        name_map=name_map,
        market_cap_map=market_cap_map,
        sector_map=sector_map,
        base_cfg=base_cfg,
        config=config,
        max_idx=max_idx,
        progress=progress,
    )
    return replay_signal_ledger(
        ledger=ledger,
        all_df_map=all_df_map,
        trade_dates=trade_dates,
        name_map=name_map,
        config=config,
    )


def build_signal_ledger(
    *,
    all_df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    trade_dates: list[date],
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    base_cfg: FunnelConfig,
    config: BacktestReplayConfig,
    max_idx: int | None = None,
    progress: ProgressReporter | None = None,
) -> BacktestSignalLedger:
    days: list[_SignalDay] = []
    crash_probe_observations: list[CrashProbeObservation] = []
    pending_pool = PendingPool() if config.pending_mode != "off" else None
    limit = max_idx if max_idx is not None else len(trade_dates) - 2
    started_at = monotonic()
    selected_total = 0
    history_end_positions = _build_history_end_positions(all_df_map, trade_dates)
    for idx in range(limit):
        ctx = _build_day_context(
            idx,
            all_df_map,
            bench_df,
            trade_dates,
            name_map,
            market_cap_map,
            sector_map,
            base_cfg,
            config,
            history_end_positions,
        )
        if ctx is None:
            continue
        if config.board not in {"us", "hk"}:
            crash_probe_observations.extend(
                build_crash_probe_observations(ctx.regime, ctx.signal_date, ctx.result.triggers, ctx.day_df_map)
            )
        selected, confirmed_count = _select_ranked_codes(ctx, pending_pool, sector_map, config)
        blocked_day, blocked_count = _apply_execution_gates(ctx, selected, config)
        selected = blocked_day.selected
        days.append(
            _SignalDay(
                _trade_context(ctx),
                selected,
                confirmed_count,
                blocked_day.regime_blocked,
                blocked_count,
            )
        )
        selected_total += len(selected.codes) if selected else 0
        _report_progress(idx, limit, selected_total, progress, started_at)
    return BacktestSignalLedger(days, crash_probe_observations)


@dataclass(frozen=True)
class _ExecutionGateResult:
    selected: _RankedSelection | None
    regime_blocked: bool


def _apply_execution_gates(
    ctx: _DayContext,
    selected: _RankedSelection | None,
    config: BacktestReplayConfig,
) -> tuple[_ExecutionGateResult, int]:
    if selected is not None and not _execution_regime_allows(ctx.regime, config.execution_regime_gate):
        return _ExecutionGateResult(None, True), len(selected.codes)
    if selected is None:
        return _ExecutionGateResult(None, False), 0
    limited_selection, limited = _limit_probe_only_selection(selected, ctx.regime, config.execution_regime_gate)
    return _ExecutionGateResult(limited_selection, False), limited


def replay_signal_ledger(
    *,
    ledger: BacktestSignalLedger,
    all_df_map: dict[str, pd.DataFrame],
    trade_dates: list[date],
    name_map: dict[str, str],
    config: BacktestReplayConfig,
) -> BacktestReplayResult:
    records: list[TradeRecord] = []
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]] = {}
    intraday_cache: dict = {}
    eval_days = signal_days = pending_total = missing_skipped = 0
    blocked_signal_days = blocked_candidates = 0
    regime_day_counts: dict[str, int] = {}
    max_idx = len(trade_dates) - config.hold_days - 1
    for day in ledger.days:
        if day.context.idx >= max_idx:
            break
        eval_days += 1
        regime = normalize_regime(day.context.regime)
        regime_day_counts[regime] = regime_day_counts.get(regime, 0) + 1
        pending_total += day.confirmed_count
        blocked_signal_days += int(day.blocked_signal_day)
        blocked_candidates += day.blocked_candidates
        if day.selected is not None:
            signal_days += 1
            missing_skipped += _append_trade_records(
                records,
                day.context,
                day.selected,
                all_df_map,
                trade_dates,
                name_map,
                ohlc_cache,
                intraday_cache,
                config,
            )
    return BacktestReplayResult(
        records,
        eval_days,
        signal_days,
        pending_total,
        missing_skipped,
        ohlc_cache,
        regime_day_counts,
        blocked_signal_days,
        blocked_candidates,
        summarize_crash_probe_replay(
            ledger.crash_probe_observations,
            all_df_map,
            trade_dates,
            hold_days=config.hold_days,
            buy_friction_pct=config.buy_friction_pct,
            sell_friction_pct=config.sell_friction_pct,
        ),
    )


def _trade_context(ctx: _DayContext) -> _TradeContext:
    return _TradeContext(ctx.idx, ctx.signal_date, ctx.entry_target_date, ctx.regime)


def _execution_regime_allows(regime: str, mode: str) -> bool:
    normalized = normalize_regime(regime)
    if mode == "off":
        return True
    if mode == "neutral_only":
        return normalized == "NEUTRAL"
    return normalized not in EXECUTE_BLOCK_NEW_BUY_REGIMES


def _limit_probe_only_selection(
    selected: _RankedSelection,
    regime: str,
    mode: str,
) -> tuple[_RankedSelection, int]:
    if mode != "live" or normalize_regime(regime) not in PROBE_ONLY_REGIMES or len(selected.codes) <= 1:
        return selected, 0
    kept = selected.codes[:1]
    return (
        _RankedSelection(
            kept,
            {code: selected.score_map[code] for code in kept if code in selected.score_map},
            {code: selected.track_map[code] for code in kept if code in selected.track_map},
            {code: selected.trigger_name_map[code] for code in kept if code in selected.trigger_name_map},
            frozenset(code for code in kept if code in selected.confirmed_codes),
        ),
        len(selected.codes) - 1,
    )


def _build_day_context(
    idx: int,
    all_df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    trade_dates: list[date],
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    base_cfg: FunnelConfig,
    config: BacktestReplayConfig,
    history_end_positions: HistoryEndPositions,
) -> _DayContext | None:
    signal_date = trade_dates[idx]
    day_df_map = _day_df_map(
        all_df_map,
        signal_date,
        config.trading_days,
        base_cfg.ma_long,
        date_index=idx,
        history_end_positions=history_end_positions,
    )
    bench_slice = _history_tail(bench_df, signal_date, config.trading_days)
    if not day_df_map or len(bench_slice) < base_cfg.ma_long:
        return None
    day_cfg = replace(base_cfg)
    breadth = _calculate_market_breadth(day_df_map, config)
    bench_context = _analyze_market_regime(bench_slice, day_cfg, breadth, config)
    result = run_funnel(
        all_symbols=list(day_df_map.keys()),
        df_map=day_df_map,
        bench_df=bench_slice,
        name_map=name_map,
        market_cap_map=market_cap_map,
        sector_map=sector_map,
        cfg=day_cfg,
        concept_map=config.concept_map,
        concept_heat=config.concept_heat,
        theme_radar=config.theme_radar,
        financial_map=config.financial_map,
        theme_member_index=config.theme_member_index,
        mainline_config=config.mainline_config,
    )
    regime = bench_context.get("regime", "NEUTRAL") if bench_context else "NEUTRAL"
    return _DayContext(
        idx,
        signal_date,
        trade_dates[idx + 1],
        day_df_map,
        name_map,
        day_cfg,
        result,
        str(regime),
        breadth,
    )


def _calculate_market_breadth(day_df_map: dict[str, pd.DataFrame], config: BacktestReplayConfig) -> dict:
    calculator = config.market_breadth_calculator or calc_market_breadth
    return calculator(day_df_map)


def _analyze_market_regime(
    bench_slice: pd.DataFrame,
    day_cfg: FunnelConfig,
    breadth: dict,
    config: BacktestReplayConfig,
) -> dict:
    analyzer = config.market_regime_analyzer or analyze_benchmark_and_tune_cfg
    return analyzer(bench_slice, None, day_cfg, breadth=breadth)


def _day_df_map(
    all_df_map: dict[str, pd.DataFrame],
    signal_date: date,
    trading_days: int,
    min_rows: int,
    *,
    date_index: int | None = None,
    history_end_positions: HistoryEndPositions | None = None,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for code, df in all_df_map.items():
        positions = (history_end_positions or {}).get(code)
        if positions is not None and date_index is not None:
            tail = _history_tail_at(df, int(positions[date_index]), trading_days)
        else:
            tail = _history_tail(df, signal_date, trading_days)
        if len(tail) >= min_rows:
            out[code] = tail
    return out


def _build_history_end_positions(all_df_map: dict[str, pd.DataFrame], trade_dates: list[date]) -> HistoryEndPositions:
    positions: HistoryEndPositions = {}
    for code, df in all_df_map.items():
        if df is None or df.empty or "date" not in df.columns or not df["date"].is_monotonic_increasing:
            continue
        positions[code] = df["date"].searchsorted(trade_dates, side="right")
    return positions


def _history_tail_at(df: pd.DataFrame, end: int, trading_days: int) -> pd.DataFrame:
    tail = df.iloc[max(0, end - trading_days) : end]
    tail.attrs[DATE_SORTED_ATTR] = True
    return tail


def _history_tail(df: pd.DataFrame, signal_date: date, trading_days: int) -> pd.DataFrame:
    dates = df["date"]
    if dates.is_monotonic_increasing:
        end = int(dates.searchsorted(signal_date, side="right"))
        return _history_tail_at(df, end, trading_days)
    return df[df["date"] <= signal_date].tail(trading_days)


def _select_ranked_codes(
    ctx: _DayContext,
    pending_pool: PendingPool | None,
    sector_map: dict[str, str],
    config: BacktestReplayConfig,
) -> tuple[_RankedSelection | None, int]:
    confirmed = _confirmed_signals(ctx, pending_pool, sector_map, config.a_share_entry_research)
    selected_codes, score_map, track_map = select_ai_input_codes(
        result=ctx.result,
        day_df_map=ctx.day_df_map,
        sector_map=sector_map,
        regime=ctx.regime,
        selection_mode=config.selection_mode,
        full_formal_l4_max=config.full_formal_l4_max,
        candidate_policy=config.candidate_policy,
        ai_allocation=config.ai_allocation,
        signal_weight_map=config.signal_weight_map,
    )
    ranked_codes = _merge_codes(selected_codes, confirmed.codes, config.pending_mode, config.pending_merge_order)
    _merge_confirmed_metadata(score_map, track_map, confirmed)
    ranked_codes = _apply_selection_guards(ranked_codes, ctx, config)
    if not ranked_codes:
        return None, len(confirmed.codes)
    return (
        _RankedSelection(
            ranked_codes,
            score_map,
            track_map,
            _name_score_map(ctx.result, confirmed, prefer_confirmed=config.pending_mode == "only"),
            frozenset(confirmed.codes),
        ),
        len(confirmed.codes),
    )


def _confirmed_signals(
    ctx: _DayContext,
    pending_pool: PendingPool | None,
    sector_map: dict[str, str],
    policy: AShareEntryResearchPolicy | None = None,
) -> _ConfirmedSignals:
    if pending_pool is None:
        return _ConfirmedSignals([], {}, {}, {})
    research = policy or AShareEntryResearchPolicy()
    signal_date_str = ctx.signal_date.isoformat()
    pending_pool.write(
        signal_date_str, ctx.result.triggers, ctx.day_df_map, ctx.regime, ctx.name_map, sector_map, ctx.day_cfg
    )
    confirmed_items = pending_pool.tick(ctx.day_df_map, signal_date_str)
    if not market_context_allows_entry(research, regime=ctx.regime, breadth=ctx.breadth):
        return _ConfirmedSignals([], {}, {}, {})
    codes: list[str] = []
    score_map: dict[str, float] = {}
    track_map: dict[str, str] = {}
    trigger_map: dict[str, str] = {}
    for item in confirmed_items:
        signal_type = str(item.get("signal_type", "confirmed"))
        if not confirmed_signal_allowed(research, signal_type):
            continue
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        score = calibrated_confirmation_score(research, signal_type, item.get("score"))
        if code not in score_map:
            codes.append(code)
        if code not in score_map or score > score_map[code]:
            score_map[code] = score
            track_map[code] = candidate_entry_track(item, fields=("track", "signal_type"))
            trigger_map[code] = signal_type
    codes.sort(key=lambda code: (-candidate_score_value(score_map.get(code)), code))
    return _ConfirmedSignals(codes, score_map, track_map, trigger_map)


def _merge_confirmed_metadata(
    score_map: dict[str, float],
    track_map: dict[str, str],
    confirmed: _ConfirmedSignals,
) -> None:
    for code, confirmed_score in confirmed.score_map.items():
        score = candidate_score_value(confirmed_score)
        if code not in score_map or score > candidate_score_value(score_map.get(code)):
            score_map[code] = score
            track_map[code] = confirmed.track_map.get(code, track_map.get(code, ""))


def _merge_codes(selected: list[str], confirmed: list[str], pending_mode: str, merge_order: str) -> list[str]:
    if pending_mode == "only":
        return confirmed
    if pending_mode != "both":
        return selected
    if merge_order == "confirmed_first":
        seen = set(confirmed)
        return list(confirmed) + [code for code in selected if code not in seen]
    seen = set(selected)
    return list(selected) + [code for code in confirmed if code not in seen]


def _apply_selection_guards(codes: list[str], ctx: _DayContext, config: BacktestReplayConfig) -> list[str]:
    if config.abc_filter and codes:
        codes = apply_abc_filter(codes, ctx.day_df_map, ctx.result.triggers)
    if config.top_n > 0 and codes:
        codes = codes[: config.top_n]
    return codes


def _name_score_map(
    result: FunnelResult,
    confirmed: _ConfirmedSignals,
    *,
    prefer_confirmed: bool = False,
) -> dict[str, tuple[float, str]]:
    out = combine_trigger_scores(result.triggers)
    for item in result.candidate_entries or []:
        code = str(item.get("code", "")).strip()
        if code:
            _set_best_trigger_name(
                out, code, candidate_score_value(item.get("score")), str(item.get("entry_type", "alpha"))
            )
    for code, signal_type in confirmed.trigger_map.items():
        if prefer_confirmed:
            _set_confirmed_trigger_name(out, code, confirmed.score_map.get(code, 0.0), signal_type)
        else:
            _set_best_trigger_name(out, code, confirmed.score_map.get(code, 0.0), f"{signal_type}(确认)")
    return out


def _set_confirmed_trigger_name(out: dict[str, tuple[float, str]], code: str, score: float, signal_type: str) -> None:
    current_score = candidate_score_value((out.get(code) or (0.0, ""))[0])
    out[code] = (max(current_score, candidate_score_value(score)), f"{signal_type}(确认)")


def _set_best_trigger_name(out: dict[str, tuple[float, str]], code: str, score: float, name: str) -> None:
    current_score = candidate_score_value((out.get(code) or (0.0, ""))[0])
    normalized_score = candidate_score_value(score)
    if code not in out or normalized_score > current_score:
        out[code] = (normalized_score, name)


def _append_trade_records(
    records: list[TradeRecord],
    ctx: _TradeContext,
    selected: _RankedSelection,
    all_df_map: dict[str, pd.DataFrame],
    trade_dates: list[date],
    name_map: dict[str, str],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
    intraday_cache: dict,
    config: BacktestReplayConfig,
) -> int:
    missing_skipped = 0
    for code in selected.codes:
        record, skipped = _trade_record_for_code(
            code, ctx, selected, all_df_map, trade_dates, name_map, ohlc_cache, intraday_cache, config
        )
        missing_skipped += int(skipped)
        if record is not None:
            records.append(record)
    return missing_skipped


def _trade_record_for_code(
    code: str,
    ctx: _TradeContext,
    selected: _RankedSelection,
    all_df_map: dict[str, pd.DataFrame],
    trade_dates: list[date],
    name_map: dict[str, str],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
    intraday_cache: dict,
    config: BacktestReplayConfig,
) -> tuple[TradeRecord | None, bool]:
    full_df = all_df_map.get(code)
    if full_df is None or full_df.empty:
        return None, False
    plan, missing_skipped = _entry_plan(full_df, code, ctx, trade_dates, intraday_cache, config)
    if plan is None:
        return None, missing_skipped
    day_ohlc = _ohlc_for_code(code, full_df, ohlc_cache)
    exit_close, exit_date, exit_reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=trade_dates,
        actual_entry_idx=plan.actual_entry_idx,
        actual_exit_idx=plan.actual_exit_idx,
        actual_exit_anchor=plan.actual_exit_anchor,
        signal_date=ctx.signal_date,
        entry_close=plan.entry_close,
        config=config.exit,
    )
    if exit_close is None or exit_date is None:
        return None, False
    return _make_trade_record(
        code, ctx, selected, name_map, trade_dates, day_ohlc, plan, exit_close, exit_date, exit_reason, config
    ), False


def _entry_plan(
    full_df: pd.DataFrame,
    code: str,
    ctx: _TradeContext,
    trade_dates: list[date],
    intraday_cache: dict,
    config: BacktestReplayConfig,
) -> tuple[_EntryPlan | None, bool]:
    entry_close, actual_entry_date, source = entry_on_or_after(
        full_df,
        code,
        ctx.entry_target_date,
        mode=config.entry_price_mode,
        entry_time=config.entry_price_time,
        fallback=config.entry_price_fallback,
        intraday_cache=intraday_cache,
        intraday_price_fetcher=config.intraday_entry_price_fetcher,
        skip_limit_up=(config.board != "us"),
    )
    if entry_close is None or entry_close <= 0 or actual_entry_date is None:
        return None, source == "tail_1455_missing_skip"
    entry_idx = _trade_date_index(trade_dates, actual_entry_date, ctx.idx + 1)
    max_hold = config.max_atr_hold_days if config.exit.exit_mode == "atr" else config.hold_days
    exit_idx = entry_idx + max_hold
    if exit_idx >= len(trade_dates) and config.exit.exit_mode != "atr":
        return None, False
    exit_idx = min(exit_idx, len(trade_dates) - 1)
    return _EntryPlan(entry_close, actual_entry_date, entry_idx, exit_idx, trade_dates[exit_idx], source), False


def _trade_date_index(trade_dates: list[date], target: date, fallback: int) -> int:
    try:
        return trade_dates.index(target)
    except ValueError:
        return fallback


def _ohlc_for_code(
    code: str,
    full_df: pd.DataFrame,
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
) -> dict[date, tuple[float, float, float, float]]:
    if code not in ohlc_cache:
        ohlc_cache[code] = build_daily_ohlc_lookup(full_df)
    return ohlc_cache[code]


def _make_trade_record(
    code: str,
    ctx: _TradeContext,
    selected: _RankedSelection,
    name_map: dict[str, str],
    trade_dates: list[date],
    day_ohlc: dict[date, tuple[float, float, float, float]],
    plan: _EntryPlan,
    exit_close: float,
    exit_date: date,
    exit_reason: str,
    config: BacktestReplayConfig,
) -> TradeRecord:
    actual_exit_idx = _trade_date_index(trade_dates, exit_date, plan.actual_exit_idx)
    window = trade_dates[plan.actual_entry_idx + 1 : actual_exit_idx + 1]
    mfe_pct, mae_pct = calc_trade_excursion_pct(day_ohlc, window, plan.entry_close)
    entry_exec = plan.entry_close * (1.0 + config.buy_friction_pct / 100.0)
    exit_exec = exit_close * (1.0 - config.sell_friction_pct / 100.0)
    _, trigger_name = selected.trigger_name_map.get(code, (0.0, "Layer3_Backup"))
    return TradeRecord(
        signal_date=ctx.signal_date,
        entry_date=plan.actual_entry_date,
        exit_date=exit_date,
        code=code,
        name=name_map.get(code, code),
        trigger=trigger_name,
        score=candidate_score_value(selected.score_map.get(code)),
        entry_close=plan.entry_close,
        exit_close=exit_close,
        ret_pct=(exit_exec - entry_exec) / entry_exec * 100.0 if entry_exec > 0 else 0.0,
        track=selected.track_map.get(code, ""),
        regime=ctx.regime,
        entry_price_source=plan.entry_price_source,
        entry_target_time=config.entry_price_time if config.entry_price_mode == "tail_1455" else "",
        exit_reason=exit_reason,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        signal_confirmed=code in selected.confirmed_codes,
    )


def _report_progress(
    idx: int,
    max_idx: int,
    signal_count: int,
    progress: ProgressReporter | None,
    started_at: float,
) -> None:
    if (idx + 1) % 20 != 0 and (idx + 1) != max_idx:
        return
    current = idx + 1
    elapsed = max(monotonic() - started_at, 0.0)
    eta = elapsed / current * (max_idx - current) if current else 0.0
    detail = (
        f"{current}/{max_idx}, signals={signal_count}, elapsed={_format_duration(elapsed)}, eta={_format_duration(eta)}"
    )
    logger.info("回放进度 %s", detail)
    if progress is not None and max_idx > 0:
        progress("回放交易", detail, 0.4 + current / max_idx * 0.6)


def _format_duration(seconds: float) -> str:
    total_minutes = max(int(seconds // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
