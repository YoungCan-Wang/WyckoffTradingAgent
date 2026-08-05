"""Signal feedback refresh workflow."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from core.signal_feedback import build_signal_registry_updates, summarize_signal_health
from core.signal_lifecycle import evaluate_signal_lifecycle
from integrations.supabase_signal_feedback import (
    load_pending_outcome_observation_ids,
    load_recent_signal_observations,
    load_recent_signal_outcomes,
    load_signal_observations_by_ids,
    load_signal_registry,
    replace_signal_health,
    replace_signal_registry,
    upsert_signal_outcomes,
)

_logger = logging.getLogger(__name__)

_COLUMN_MAP = {"日期": "date", "收盘": "close", "最低": "low"}
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class SignalFeedbackConfig:
    market: str = "cn"
    end_date: str = ""
    as_of_date: str = ""
    horizons: tuple[int, ...] = (1, 3, 5, 10, 20)
    observation_days: int = 120
    outcome_days: int = 180
    pre_days: int = 10
    limit: int = 5000
    outcome_limit: int = 20000
    min_samples: int = 30
    registry_horizon: int = 5
    health_only: bool = False


def parse_horizons(raw: str) -> tuple[int, ...]:
    values = [max(int(item.strip()), 1) for item in str(raw or "").split(",") if item.strip()]
    return tuple(values or [1, 3, 5, 10, 20])


def default_registry_horizon() -> int:
    try:
        return max(int(float(os.getenv("SIGNAL_REGISTRY_HORIZON", "5"))), 1)
    except (TypeError, ValueError):
        return 5


def run_signal_feedback(config: SignalFeedbackConfig, log_fn: LogFn = print) -> dict[str, int]:
    outcome_written = 0 if config.health_only else refresh_outcomes(config, log_fn)
    health_written = refresh_health(config, log_fn)
    return {"outcomes": outcome_written, "health": health_written}


def refresh_outcomes(config: SignalFeedbackConfig, log_fn: LogFn = print) -> int:
    observations = _observations_to_settle(config)
    cache: dict[tuple[str, str, str], pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    skipped = 0
    for obs in observations:
        cache_key = _observation_cache_key(obs, config.market)
        if cache_key not in cache:
            cache[cache_key] = _fetch_history(obs, config.end_date, config.pre_days)
        if cache[cache_key].empty:
            skipped += 1
        rows.extend(_outcome_rows(obs, cache[cache_key], config.horizons))
    written = upsert_signal_outcomes(rows)
    log_fn(
        f"[signal_feedback] outcomes: observations={len(observations)}, rows={len(rows)}, skipped={skipped}, written={written}"
    )
    return written


def _observations_to_settle(config: SignalFeedbackConfig) -> list[dict[str, Any]]:
    """滚动窗口内的最新观测 + 窗口外仍未结算完的补漏观测，按 id 去重。

    outcome 结算依赖未来 K 线数据补齐；仅按 observation_days 滚动窗口拉取会让
    触发时间较早、当时未结算成功的信号一旦滑出窗口就永久卡在 pending。
    """
    recent = load_recent_signal_observations(config.observation_days, config.limit, config.market)
    seen_ids = {obs.get("id") for obs in recent}
    pending_ids = [
        oid for oid in load_pending_outcome_observation_ids(config.outcome_limit, config.market) if oid not in seen_ids
    ]
    backfill = load_signal_observations_by_ids(pending_ids, config.market)
    return recent + backfill


def refresh_health(config: SignalFeedbackConfig, log_fn: LogFn = print) -> int:
    outcomes = load_recent_signal_outcomes(
        config.outcome_days,
        config.outcome_limit,
        config.market,
        raise_on_error=True,
    )
    if not outcomes:
        log_fn("[signal_feedback] health: no trusted outcomes; keep previous health and registry snapshots")
        return 0
    current_registry = load_signal_registry(config.market, raise_on_error=True)
    health_rows = summarize_signal_health(
        outcomes,
        as_of_date=config.as_of_date,
        market=config.market,
        min_samples=config.min_samples,
    )
    registry_rows = build_signal_registry_updates(
        health_rows,
        market=config.market,
        horizon_days=config.registry_horizon,
        registry_rows=current_registry,
    )
    if not health_rows or not registry_rows:
        log_fn("[signal_feedback] health: derived snapshot empty; keep previous health and registry snapshots")
        return 0
    health_written = replace_signal_health(health_rows, market=config.market, as_of_date=config.as_of_date)
    registry_written = replace_signal_registry(registry_rows, market=config.market)
    log_fn(f"[signal_feedback] health: outcomes={len(outcomes)}, health={health_written}, registry={registry_written}")
    return health_written


def _date_minus(raw: Any, days: int) -> str:
    parsed = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    return (parsed - timedelta(days=max(days, 0))).isoformat()


def _normalize_history(raw: pd.DataFrame | None) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    out = raw.rename(columns={k: v for k, v in _COLUMN_MAP.items() if k in raw.columns}).copy()
    keep = [c for c in ("date", "close", "low") if c in out.columns]
    if "date" not in keep or "close" not in keep:
        return pd.DataFrame()
    return out[keep]


def _fetch_history(obs: dict[str, Any], end_date: str, pre_days: int) -> pd.DataFrame:
    if str(obs.get("market") or "cn").lower() != "cn":
        return pd.DataFrame()
    from integrations.data_source import fetch_stock_hist

    code = str(obs.get("code") or "")
    start_date = _date_minus(obs.get("trade_date"), pre_days)
    try:
        raw = fetch_stock_hist(code, start_date, end_date, adjust="qfq")
    except RuntimeError:
        _logger.info("skip delisted/suspended: code=%s, range=%s..%s", code, start_date, end_date)
        return pd.DataFrame()
    return _normalize_history(raw)


def _outcome_rows(obs: dict[str, Any], hist: pd.DataFrame, horizons: tuple[int, ...]) -> list[dict[str, Any]]:
    if hist.empty or obs.get("id") is None:
        return []
    lifecycle = evaluate_signal_lifecycle(
        hist,
        code=str(obs.get("code") or ""),
        signal_date=str(obs.get("trade_date") or ""),
        entry_price=obs.get("entry_price"),
        horizons=horizons,
    )
    return [_outcome_row(obs, outcome) for outcome in lifecycle.outcomes]


def _outcome_row(obs: dict[str, Any], outcome) -> dict[str, Any]:
    return {
        "observation_id": obs["id"],
        "market": obs.get("market") or "cn",
        "trade_date": obs.get("trade_date"),
        "code": str(obs.get("code") or ""),
        "signal_type": str(obs.get("signal_type") or ""),
        "track": str(obs.get("track") or ""),
        "regime": str(obs.get("regime") or "NEUTRAL"),
        "horizon_days": outcome.horizon,
        "status": outcome.status,
        "return_pct": outcome.return_pct,
        "max_drawdown_pct": outcome.max_drawdown_pct,
    }


def _observation_cache_key(obs: dict[str, Any], market: str) -> tuple[str, str, str]:
    return (
        str(obs.get("market") or market),
        str(obs.get("code") or ""),
        str(obs.get("trade_date") or ""),
    )
