"""Signal observation, outcome, and health aggregation helpers."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean, median
from typing import Any

from core.candidate_metadata import code6
from core.candidate_selection_score import score_candidate_shadow
from utils.safe import finite_float, safe_float

SIGNAL_TRACK: dict[str, str] = {
    "sos": "Trend",
    "evr": "Trend",
    "trend_pullback": "Trend",
    "trend_breakout": "Trend",
    "trend_lane_pullback": "Trend",
    "sector_strength": "Trend",
    "wyckoff_structure": "Trend",
    "mainline": "Trend",
    "spring": "Accum",
    "lps": "Accum",
    "compression": "Accum",
}
KNOWN_SIGNALS = set(SIGNAL_TRACK)
BLOCKED_REGISTRY_STATUSES = {"EXPERIMENTAL", "RETIRED"}
DATA_LINEAGE_VERSION = "candidate_evidence_lineage_v1"
ENTRY_QUALITY_VERSION = "step3_entry_quality_v1"


def normalize_signal_type(raw: Any) -> str:
    return str(raw or "").strip().lower()


def signal_track(signal_type: Any) -> str:
    return SIGNAL_TRACK.get(normalize_signal_type(signal_type), "Trend")


def _code(raw: Any) -> str:
    return str(raw or "").strip()


def _text_list(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, list) else re.split(r"[,，、;；\n]+", str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _iter_trigger_rows(triggers: dict[str, list[tuple[str, float]]]):
    for signal_type, hits in (triggers or {}).items():
        sig = normalize_signal_type(signal_type)
        for code, score in hits or []:
            code_s = _code(code)
            if code_s and sig:
                yield sig, code_s, safe_float(score)


def _springboard_observation_fields(
    signal_type: str,
    code: str,
    springboard_map: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    fields = (springboard_map or {}).get(f"{signal_type}:{code}") or (springboard_map or {}).get(code)
    if not fields:
        return {}
    return {
        "springboard_grade": fields.get("springboard_grade"),
        "springboard_met_count": fields.get("springboard_met_count"),
        "springboard_a": fields.get("springboard_a"),
        "springboard_b": fields.get("springboard_b"),
        "springboard_c": fields.get("springboard_c"),
        "springboard_support": fields.get("springboard_support"),
        "springboard_touch_count": fields.get("springboard_touch_count"),
        "springboard_evidence": fields.get("springboard_evidence") or {},
    }


def _footprint_fields(
    signal_type: str,
    code: str,
    footprint_map: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    fields = (footprint_map or {}).get(f"{signal_type}:{code}") or (footprint_map or {}).get(code)
    return dict(fields or {})


def _source_context_fields(
    signal_type: str,
    code: str,
    source_context_map: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    fields = (source_context_map or {}).get(f"{signal_type}:{code}") or (source_context_map or {}).get(code)
    return dict(fields or {})


def _coverage_grade(score: float) -> str:
    if score >= 75:
        return "strong"
    if score >= 50:
        return "medium"
    if score >= 25:
        return "thin"
    return "weak"


def _source_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return "missing"
    if text.startswith("ok"):
        return "ok"
    if text.startswith("error"):
        return "error"
    if text.startswith("skipped"):
        return "skipped"
    return "unknown"


def _external_capital_lineage(source_context: dict[str, Any]) -> dict[str, Any]:
    source_status = source_context.get("source_status") if isinstance(source_context.get("source_status"), dict) else {}
    source_keys = (
        "lhb",
        "margin",
        "block_trade",
        "stock_moneyflow",
        "hsgt_top10",
        "tick_large_order",
    )
    providers = [key for key in source_keys if isinstance(source_context.get(key), dict) and source_context.get(key)]
    statuses = {str(key): _source_status(value) for key, value in source_status.items()}
    errors = [key for key, value in statuses.items() if value == "error"]
    if providers:
        status = "ok" if not errors else "partial"
    elif errors:
        status = "error"
    elif statuses:
        status = "missing"
    else:
        status = "missing"
    return {
        "status": status,
        "providers": providers,
        "source_status": source_status,
    }


def _lineage_part(key: str, coverage: float, payload: dict[str, Any] | None) -> tuple[float, list[str], dict[str, Any]]:
    if payload:
        return coverage, [key], {key: {"status": "ok", **payload}}
    return 0.0, [], {key: {"status": "missing"}}


def _daily_signal_lineage(signal_type: str, trigger_score: float, priority_score: float):
    return _lineage_part(
        "daily_signal",
        20.0,
        {
            "signal_type": signal_type,
            "trigger_score": round(float(trigger_score or 0.0), 4),
            "priority_score": round(float(priority_score or 0.0), 4),
        },
    )


def _price_action_lineage(footprint: dict[str, Any]):
    payload = (
        {
            "provider": "daily_kline",
            "bias": footprint.get("bias"),
            "tags": footprint.get("tags") or [],
            "negative_tags": footprint.get("negative_tags") or [],
        }
        if footprint
        else None
    )
    return _lineage_part("price_action", 20.0, payload)


def _springboard_lineage(springboard: dict[str, Any]):
    payload = (
        {
            "grade": springboard.get("springboard_grade"),
            "met_count": springboard.get("springboard_met_count"),
        }
        if springboard
        else None
    )
    return _lineage_part("springboard", 25.0, payload)


def _external_lineage(source_context: dict[str, Any]):
    external = _external_capital_lineage(source_context)
    keys = ["external_capital"] if external["providers"] else []
    return 25.0 if keys else 0.0, keys, {"external_capital": external}


def _merge_lineage_parts(
    parts: list[tuple[float, list[str], dict[str, Any]]],
) -> tuple[float, list[str], dict[str, Any]]:
    coverage = 0.0
    evidence_keys: list[str] = []
    sources: dict[str, Any] = {}
    for score, keys, source in parts:
        coverage += score
        evidence_keys.extend(keys)
        sources.update(source)
    return coverage, evidence_keys, sources


def _selection_lineage(selection_source: str, selected_for_ai: bool, ai_recommended: bool, candidate_rank: int | None):
    return {
        "source": selection_source or "funnel",
        "selected_for_ai": selected_for_ai,
        "ai_recommended": ai_recommended,
        "candidate_rank": candidate_rank,
    }


def _lineage_result(coverage: float, evidence_keys: list[str], sources: dict[str, Any]) -> dict[str, Any]:
    missing_keys = [key for key in ("price_action", "springboard", "external_capital") if key not in evidence_keys]
    score = round(max(0.0, min(100.0, coverage)), 1)
    return {
        "version": DATA_LINEAGE_VERSION,
        "coverage_score": score,
        "coverage_grade": _coverage_grade(score),
        "evidence_keys": evidence_keys,
        "missing_keys": missing_keys,
        "sources": sources,
    }


def _data_lineage(
    signal_type: str,
    trigger_score: float,
    priority_score: float,
    footprint: dict[str, Any],
    springboard: dict[str, Any],
    source_context: dict[str, Any],
    *,
    selected_for_ai: bool,
    ai_recommended: bool,
    selection_source: str,
    candidate_rank: int | None,
) -> dict[str, Any]:
    parts = [
        _daily_signal_lineage(signal_type, trigger_score, priority_score),
        _price_action_lineage(footprint),
        _springboard_lineage(springboard),
        _external_lineage(source_context),
    ]
    coverage, evidence_keys, sources = _merge_lineage_parts(parts)
    if selected_for_ai or ai_recommended:
        coverage += 5.0
        evidence_keys.append("ai_review")
    sources["selection"] = _selection_lineage(selection_source, selected_for_ai, ai_recommended, candidate_rank)
    return _lineage_result(coverage, evidence_keys, sources)


def _features_json(
    signal_type: str,
    trigger_score: float,
    priority_score: float,
    footprint: dict[str, Any],
    springboard: dict[str, Any],
    source_context: dict[str, Any],
    data_lineage: dict[str, Any],
    candidate_metadata: dict[str, Any] | None = None,
    entry_quality: dict[str, Any] | None = None,
    health_context: dict[str, Any] | None = None,
    dynamic_promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if candidate_metadata:
        out["candidate_metadata"] = candidate_metadata
    if entry_quality:
        out["entry_quality"] = entry_quality
    if footprint:
        out["price_action_footprint"] = footprint
    if springboard:
        out["springboard"] = springboard
    if source_context:
        out["source_context"] = source_context
    out["data_lineage"] = data_lineage
    shadow_score = score_candidate_shadow(
        signal_type=signal_type,
        trigger_score=trigger_score,
        priority_score=priority_score,
        footprint=footprint,
        springboard=springboard,
        source_context=source_context,
        health_context=health_context,
    )
    if dynamic_promotion:
        shadow_score["dynamic_promotion_snapshot"] = dynamic_promotion
    out["candidate_shadow_score"] = shadow_score
    return out


def _entry_quality_fields(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    score = finite_float(raw.get("score", raw.get("entry_quality_score")))
    grade = str(raw.get("grade") or raw.get("entry_quality_grade") or "").strip()
    tag = str(raw.get("tag") or raw.get("entry_quality_tag") or "").strip()
    risk_flags = _text_list(raw.get("risk_flags", raw.get("entry_risk_flags")))
    out: dict[str, Any] = {"version": ENTRY_QUALITY_VERSION}
    if score is not None:
        out["score"] = round(score, 1)
    if grade:
        out["grade"] = grade
    if tag:
        out["tag"] = tag
    if risk_flags:
        out["risk_flags"] = risk_flags
    bucket = finite_float(raw.get("priority_bucket", raw.get("entry_priority_bucket")))
    if bucket is not None:
        out["priority_bucket"] = int(bucket)
    return out if len(out) > 1 else {}


def _trigger_tags_by_code(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, list[str]]:
    tags: dict[str, set[str]] = {}
    for signal_type, hits in triggers.items():
        for code, _score in hits:
            code_s = _code(code)
            if code_s:
                tags.setdefault(code_s, set()).add(str(signal_type))
    return {code: sorted(values) for code, values in tags.items()}


def _observation_feature_inputs(
    signal_type: str,
    code: str,
    trigger_score: float,
    ctx: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    springboard = _springboard_observation_fields(signal_type, code, ctx["springboard_map"])
    footprint = _footprint_fields(signal_type, code, ctx["footprint_map"])
    source_context = _source_context_fields(signal_type, code, ctx["source_context_map"])
    candidate_metadata = _candidate_metadata_for_signal(ctx["candidate_metadata_map"], code, signal_type)
    entry_quality = _entry_quality_fields(
        ctx["entry_quality_map"].get(code6(code)) or ctx["entry_quality_map"].get(code)
    )
    priority_score = safe_float(ctx["score_map"].get(code))
    selected_for_ai = code in ctx["selected"]
    ai_recommended = code in ctx["recommended"]
    candidate_rank = ctx["rank_map"].get(code)
    health_context = _health_context_for_signal(ctx["health_context_map"], signal_type, ctx["regime"])
    dynamic_promotion = ctx["dynamic_promotion_map"].get(code) or {}
    data_lineage = _data_lineage(
        signal_type,
        trigger_score,
        priority_score,
        footprint,
        springboard,
        source_context,
        selected_for_ai=selected_for_ai,
        ai_recommended=ai_recommended,
        selection_source=ctx["source_map"].get(code, "funnel"),
        candidate_rank=candidate_rank,
    )
    features = _features_json(
        signal_type,
        trigger_score,
        priority_score,
        footprint,
        springboard,
        source_context,
        data_lineage,
        candidate_metadata,
        entry_quality,
        health_context,
        dynamic_promotion,
    )
    return priority_score, {"features_json": features, **springboard}


def _signal_observation_row(
    trade_date: str,
    market: str,
    regime: str,
    now_iso: str,
    item: tuple[str, str, float],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    signal_type, code, trigger_score = item
    stage = ctx["stage_map"].get(code, "")
    channel = ctx["channel_map"].get(code, "")
    priority_score, feature_fields = _observation_feature_inputs(signal_type, code, trigger_score, ctx)
    candidate_metadata = _candidate_metadata_for_signal(ctx["candidate_metadata_map"], code, signal_type)
    return {
        "market": market,
        "trade_date": trade_date,
        "code": code,
        "name": ctx["name_map"].get(code, code),
        "signal_type": signal_type,
        "track": signal_track(signal_type),
        "regime": str(regime or "NEUTRAL").strip().upper() or "NEUTRAL",
        "industry": ctx["sector_map"].get(code, ""),
        "stage": stage,
        "channel": channel,
        "profile_tag": channel or signal_track(signal_type),
        "stage_tag": stage,
        "trigger_tags": ctx["trigger_tags"].get(code, [signal_type]),
        "selection_mode": ctx["selection_mode_map"].get(code, ctx["selection_mode"]),
        "policy_version": ctx["policy_version"],
        "candidate_rank": ctx["rank_map"].get(code),
        "trigger_score": trigger_score,
        "priority_score": priority_score,
        "entry_price": safe_float(ctx["latest_close_map"].get(code), default=0.0),
        "selected_for_ai": code in ctx["selected"],
        "ai_recommended": code in ctx["recommended"],
        "source": ctx["source_map"].get(code, "funnel"),
        "strategy_version": candidate_metadata.get("strategy_version"),
        "candidate_lane": candidate_metadata.get("candidate_lane"),
        "entry_type": candidate_metadata.get("entry_type"),
        "signal_key": candidate_metadata.get("signal_key"),
        "candidate_status": candidate_metadata.get("candidate_status"),
        "lifecycle_status": "ACTIVE",
        "updated_at": now_iso,
        **feature_fields,
    }


def _candidate_metadata_for_signal(
    metadata_map: dict[Any, dict[str, Any]], code: str, signal_type: str
) -> dict[str, Any]:
    from core.candidate_metadata import candidate_metadata_for_signal

    return candidate_metadata_for_signal(metadata_map, code, signal_type)


def _derived_rank_map(selected_for_ai: list[str] | None, rank_map: dict[str, int] | None) -> dict[str, int]:
    derived = {_code(code): idx + 1 for idx, code in enumerate(selected_for_ai or []) if _code(code)}
    derived.update(rank_map or {})
    return derived


def _health_context_for_signal(
    health_map: dict[Any, dict[str, Any]],
    signal_type: str,
    regime: str,
) -> dict[str, Any]:
    regime_key = str(regime or "NEUTRAL").strip().upper()
    return dict(
        health_map.get((signal_type, regime_key))
        or health_map.get((signal_type, "ALL"))
        or health_map.get(signal_type)
        or {}
    )


def _observation_context(triggers: dict[str, list[tuple[str, float]]], kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "selected": {_code(c) for c in kwargs["selected_for_ai"] or []},
        "recommended": {_code(c) for c in kwargs["ai_recommended"] or []},
        "trigger_tags": _trigger_tags_by_code(triggers),
        "name_map": kwargs["name_map"] or {},
        "sector_map": kwargs["sector_map"] or {},
        "score_map": kwargs["score_map"] or {},
        "stage_map": kwargs["stage_map"] or {},
        "channel_map": kwargs["channel_map"] or {},
        "latest_close_map": kwargs["latest_close_map"] or {},
        "source_map": kwargs["source_map"] or {},
        "springboard_map": kwargs["springboard_map"],
        "footprint_map": kwargs["footprint_map"],
        "source_context_map": kwargs["source_context_map"],
        "candidate_metadata_map": kwargs["candidate_metadata_map"] or {},
        "entry_quality_map": kwargs["entry_quality_map"] or {},
        "selection_mode": kwargs["selection_mode"],
        "selection_mode_map": kwargs["selection_mode_map"] or {},
        "policy_version": kwargs["policy_version"],
        "rank_map": _derived_rank_map(kwargs["selected_for_ai"], kwargs["rank_map"]),
        "regime": str(kwargs.get("regime") or "NEUTRAL").strip().upper(),
        "health_context_map": kwargs.get("health_context_map") or {},
        "dynamic_promotion_map": kwargs.get("dynamic_promotion_map") or {},
    }


def build_signal_observations(
    trade_date: str,
    triggers: dict[str, list[tuple[str, float]]],
    *,
    market: str = "cn",
    regime: str = "NEUTRAL",
    selected_for_ai: list[str] | None = None,
    ai_recommended: list[str] | None = None,
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    score_map: dict[str, float] | None = None,
    stage_map: dict[str, str] | None = None,
    channel_map: dict[str, str] | None = None,
    latest_close_map: dict[str, float] | None = None,
    source_map: dict[str, str] | None = None,
    springboard_map: dict[str, dict[str, Any]] | None = None,
    footprint_map: dict[str, dict[str, Any]] | None = None,
    source_context_map: dict[str, dict[str, Any]] | None = None,
    candidate_metadata_map: dict[str, dict[str, Any]] | None = None,
    entry_quality_map: dict[str, dict[str, Any]] | None = None,
    selection_mode: str = "",
    selection_mode_map: dict[str, str] | None = None,
    policy_version: str = "",
    rank_map: dict[str, int] | None = None,
    health_context_map: dict[Any, dict[str, Any]] | None = None,
    dynamic_promotion_map: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ctx = _observation_context(triggers, locals())
    now_iso = datetime.now(UTC).isoformat()
    return [
        _signal_observation_row(trade_date, market, regime, now_iso, item, ctx) for item in _iter_trigger_rows(triggers)
    ]


def classify_health(
    sample_count: int,
    win_rate_pct: float | None,
    avg_return_pct: float | None,
    *,
    min_samples: int = 30,
) -> tuple[str, float, str]:
    # K线复算实盘样本显示 149 组信号在 min_samples=20/权重0.8 下近似满权参与打分，
    # 样本不足却几乎不打折扣地影响候选排序。min_samples 提到 30、权重降到 0.6，
    # 让统计意义不足的信号在动态策略中的话语权明显收窄。
    if sample_count < min_samples:
        return "INSUFFICIENT", 0.6, f"samples {sample_count}<{min_samples}"
    win = float(win_rate_pct or 0.0)
    avg = float(avg_return_pct or 0.0)
    if win < 35.0 and avg < 0.0:
        return "DECAYED", 0.4, f"win={win:.1f}%, avg={avg:+.2f}%"
    if win < 40.0 or avg < 0.0:
        return "WATCH", 0.75, f"win={win:.1f}%, avg={avg:+.2f}%"
    return "HEALTHY", 1.0, f"win={win:.1f}%, avg={avg:+.2f}%"


def _done_return(row: dict[str, Any]) -> float | None:
    if str(row.get("status", "")).strip().lower() != "done":
        return None
    raw = row.get("return_pct")
    return None if raw is None else safe_float(raw)


def _health_row(
    as_of_date: str,
    market: str,
    key: tuple[str, str, str, int],
    rows: list[dict[str, Any]],
    min_samples: int,
) -> dict[str, Any]:
    signal_type, track, regime, horizon = key
    returns = [r for r in (_done_return(row) for row in rows) if r is not None]
    drawdowns = [safe_float(row.get("max_drawdown_pct")) for row in rows if row.get("max_drawdown_pct") is not None]
    win_rate = float(sum(1 for r in returns if r > 0) / len(returns) * 100.0) if returns else None
    avg_ret = float(mean(returns)) if returns else None
    state, weight, reason = classify_health(len(returns), win_rate, avg_ret, min_samples=min_samples)
    from core.signal_decay import decay_fields_from_returns

    decay = decay_fields_from_returns(returns)
    return {
        "market": market,
        "as_of_date": as_of_date,
        "signal_type": signal_type,
        "track": track,
        "regime": regime,
        "horizon_days": horizon,
        "sample_count": len(returns),
        "win_rate_pct": win_rate,
        "avg_return_pct": avg_ret,
        "median_return_pct": float(median(returns)) if returns else None,
        "avg_drawdown_pct": float(mean(drawdowns)) if drawdowns else None,
        "health_state": state,
        "weight_multiplier": weight,
        "reason": reason,
        "ic": decay.get("ic"),
        "sharpe": decay.get("sharpe"),
        "decay_lifecycle": decay.get("decay_lifecycle"),
        "decay_reason": decay.get("decay_reason"),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _is_global_registry_regime(regime: Any) -> bool:
    return str(regime or "").strip().upper() in {"", "ALL"}


def _registry_status_by_signal(rows: list[dict[str, Any]] | None) -> dict[str, str]:
    """Read signal-level lifecycle status from global rows only.

    Regime-scoped rows carry weights; mixing them into this map lets a stale
    ACTIVE/WATCH regime row overwrite global RETIRED (last write wins).
    """
    out: dict[str, str] = {}
    for row in rows or []:
        signal_type = normalize_signal_type(row.get("signal_type"))
        if signal_type and _is_global_registry_regime(row.get("regime")):
            out[signal_type] = str(row.get("status") or "ACTIVE").strip().upper()
    return out


def _next_registry_status(signal_type: str, health_state: str, current_status: str) -> str:
    if current_status == "RETIRED":
        return "RETIRED"
    if health_state == "HEALTHY":
        return "ACTIVE"
    if health_state == "INSUFFICIENT":
        return "ACTIVE" if signal_type in KNOWN_SIGNALS else "EXPERIMENTAL"
    if health_state == "DECAYED" and current_status == "WATCH":
        return "RETIRED"
    return "WATCH"


def summarize_signal_health(
    outcomes: list[dict[str, Any]],
    *,
    as_of_date: str,
    market: str = "cn",
    min_samples: int = 30,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        signal_type = normalize_signal_type(row.get("signal_type"))
        if not signal_type:
            continue
        track = str(row.get("track") or signal_track(signal_type))
        regime = str(row.get("regime") or "NEUTRAL").strip().upper() or "NEUTRAL"
        horizon = int(row.get("horizon_days") or 0)
        if horizon <= 0:
            continue
        groups[(signal_type, track, regime, horizon)].append(row)
        groups[(signal_type, track, "ALL", horizon)].append(row)
    return [_health_row(as_of_date, market, key, rows, min_samples) for key, rows in sorted(groups.items())]


def _registry_row_key(signal_type: str, regime: Any) -> tuple[str, str]:
    regime_norm = str(regime or "").strip().upper()
    if regime_norm in {"", "ALL"}:
        return signal_type, ""
    return signal_type, regime_norm


def _preserved_registry_rows(
    registry_rows: list[dict[str, Any]] | None,
    *,
    market: str,
    horizon_days: int,
    covered_keys: set[tuple[str, str]],
    status_by_signal: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Keep lifecycle rows absent from this health window.

    Full registry replace only emits keys present in health at registry_horizon.
    Without this merge, RETIRED/EXPERIMENTAL rows (and regime downweights) with no
    recent outcomes disappear; dynamic policy then defaults missing signals to ACTIVE.
    Regime-scoped survivors still inherit the signal-level status map so a stale
    ACTIVE/WATCH weight row cannot outlive global RETIRED.
    """
    preserved: list[dict[str, Any]] = []
    now = datetime.now(UTC).isoformat()
    for row in registry_rows or []:
        signal_type = normalize_signal_type(row.get("signal_type"))
        if not signal_type:
            continue
        key = _registry_row_key(signal_type, row.get("regime"))
        if key in covered_keys:
            continue
        kept = dict(row)
        status = (status_by_signal or {}).get(signal_type) or kept.get("status") or "ACTIVE"
        kept.update(
            {
                "market": market,
                "signal_type": signal_type,
                "regime": key[1],
                "status": str(status).strip().upper(),
                "track": kept.get("track") or signal_track(signal_type),
                "horizon_days": int(kept.get("horizon_days") or horizon_days),
                "updated_at": now,
            }
        )
        preserved.append(kept)
    return preserved


def _next_global_registry_status(
    health_rows: list[dict[str, Any]],
    *,
    target_horizon: int,
    status_by_signal: dict[str, str],
) -> dict[str, str]:
    next_status = dict(status_by_signal)
    for row in health_rows:
        if int(row.get("horizon_days") or 0) != target_horizon:
            continue
        if not _is_global_registry_regime(row.get("regime")):
            continue
        signal_type = normalize_signal_type(row.get("signal_type"))
        if not signal_type:
            continue
        state = str(row.get("health_state") or "INSUFFICIENT")
        current_status = status_by_signal.get(signal_type, "ACTIVE")
        next_status[signal_type] = _next_registry_status(signal_type, state, current_status)
    return next_status


def build_signal_registry_updates(
    health_rows: list[dict[str, Any]],
    *,
    market: str = "cn",
    horizon_days: int = 10,
    registry_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    target_horizon = horizon_days
    status_by_signal = _registry_status_by_signal(registry_rows)
    # 先算全局下一状态，再让 regime 拆分行跟随；否则同一次 rebuild 会出现
    # 全局 RETIRED、regime 仍 WATCH/ACTIVE，filter 按行序 last-wins 放行已下线信号。
    next_status = _next_global_registry_status(
        health_rows,
        target_horizon=target_horizon,
        status_by_signal=status_by_signal,
    )
    # 按regime拆分存储：每个 (signal_type, regime) 都有独立行，
    # resolve_signal_weight_multiplier 会优先命中带regime的精确行，
    # 避免像 launchpad 这样"允许买入市况下正期望、但被全局统计拖累"的信号被误降权。
    updates = []
    covered_keys: set[tuple[str, str]] = set()
    for row in health_rows:
        if int(row.get("horizon_days") or 0) != target_horizon:
            continue
        regime = str(row.get("regime") or "ALL").strip().upper() or "ALL"
        signal_type = normalize_signal_type(row.get("signal_type"))
        if not signal_type:
            continue
        key = _registry_row_key(signal_type, regime)
        covered_keys.add(key)
        updates.append(
            {
                "market": market,
                "signal_type": signal_type,
                "track": row.get("track") or signal_track(signal_type),
                "regime": key[1],
                "status": _status_with_decay(next_status.get(signal_type, "ACTIVE"), row),
                "weight_multiplier": row.get("weight_multiplier") or 1.0,
                "sample_count": row.get("sample_count") or 0,
                "win_rate_pct": row.get("win_rate_pct"),
                "avg_return_pct": row.get("avg_return_pct"),
                "horizon_days": target_horizon,
                "reason": row.get("reason") or "",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    updates.extend(
        _preserved_registry_rows(
            registry_rows,
            market=market,
            horizon_days=target_horizon,
            covered_keys=covered_keys,
            status_by_signal=next_status,
        )
    )
    return updates


def _status_with_decay(status: str, row: dict[str, Any]) -> str:
    from core.signal_decay import registry_status_for_decay

    return registry_status_for_decay(str(row.get("decay_lifecycle") or "monitoring"), status)
