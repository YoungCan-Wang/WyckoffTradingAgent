"""Pure statistics for strategy attribution reports."""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date
from typing import Any

from core.strategy_policy_governor import build_strategy_policy_governor, governor_recommendation_rows
from utils.json_text import parse_json_object as _json_map


def build_strategy_attribution_payload(
    *,
    report_date: date,
    market: str,
    window_start: date,
    window_end: date,
    horizons: list[int],
    observations: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    shadow_runs: list[dict[str, Any]],
    backtest_confirmation_json: dict[str, Any] | None = None,
    formal_dynamic_approval_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    joined = join_outcomes(outcomes, observations)
    focus_horizon = 3 if 3 in horizons else horizons[0]
    signal_stats = group_stats(joined, "signal_type", horizons)
    signal_context_stats = signal_context_stats_json(joined, horizons)
    score_stats = score_stats_json(
        joined,
        horizons,
        observations=observations,
        outcomes=outcomes,
    )
    shadow = shadow_stats(shadow_runs, joined, horizons)
    governor = build_strategy_policy_governor(
        signal_stats_json=signal_stats,
        signal_context_stats_json=signal_context_stats,
        score_bucket_stats_json=score_stats,
        shadow_diff_stats_json=shadow,
        backtest_confirmation_json=backtest_confirmation_json,
        formal_dynamic_approval_json=formal_dynamic_approval_json,
        horizons=horizons,
    )
    shadow["policy_governor"] = governor
    return {
        "report_date": report_date.isoformat(),
        "market": market,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "horizons": horizons,
        "summary_json": group_stats(joined, "horizon_days", horizons),
        "signal_stats_json": signal_stats,
        "signal_context_stats_json": signal_context_stats,
        "score_bucket_stats_json": score_stats,
        "shadow_diff_stats_json": shadow,
        "top_winners_json": ranked_outcomes(joined, focus_horizon, reverse=True),
        "top_losers_json": ranked_outcomes(joined, focus_horizon, reverse=False),
        "recommendations_json": governor_recommendation_rows(governor),
    }


def join_outcomes(outcomes: list[dict[str, Any]], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    obs_by_id = {row.get("id"): row for row in observations}
    joined = []
    for row in outcomes:
        obs = obs_by_id.get(row.get("observation_id"), {})
        item = dict(row)
        for key in _OBSERVATION_FIELDS:
            item[key] = obs.get(key)
        item.update(_candidate_shadow_fields(obs))
        item.update(_entry_quality_fields(obs))
        item.update(_data_lineage_fields(obs))
        joined.append(item)
    return joined


def group_stats(rows: list[dict[str, Any]], group_key: str, horizons: list[int]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        horizon_rows = [row for row in rows if int(row.get("horizon_days") or 0) == horizon]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in horizon_rows:
            groups[str(row.get(group_key) or "unknown")].append(row)
        result[str(horizon)] = {key: stats(group_rows) for key, group_rows in sorted(groups.items())}
    return result


def signal_context_stats_json(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for horizon in horizons:
        grouped = _signal_context_groups(rows, horizon)
        result[str(horizon)] = [_signal_context_row(key, group_rows) for key, group_rows in sorted(grouped.items())]
    return result


def _signal_context_groups(
    rows: list[dict[str, Any]], horizon: int
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(row.get("horizon_days") or 0) != int(horizon):
            continue
        groups[_signal_context_key(row)].append(row)
    return groups


def _signal_context_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("signal_type") or "unknown").strip().lower() or "unknown",
        str(row.get("regime") or "ALL").strip().upper() or "ALL",
        str(row.get("candidate_lane") or "unknown").strip().lower() or "unknown",
        str(row.get("entry_type") or "unknown").strip().lower() or "unknown",
    )


def _signal_context_row(key: tuple[str, str, str, str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    signal, regime, lane, entry_type = key
    return {
        "signal_type": signal,
        "regime": regime,
        "candidate_lane": lane,
        "entry_type": entry_type,
        **stats(rows),
    }


def score_stats_json(
    joined: list[dict[str, Any]],
    horizons: list[int],
    *,
    observations: list[dict[str, Any]] | None = None,
    outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    score_stats = _score_bucket_stats(joined, horizons)
    score_stats["_candidate_shadow_grade"] = _candidate_shadow_stats(joined, horizons)
    score_stats["_entry_quality_grade"] = _entry_quality_stats(joined, horizons)
    score_stats["_data_lineage"] = _data_lineage_stats(joined, horizons)
    score_stats["_selection_mode"] = group_stats(joined, "selection_mode", horizons)
    score_stats["_strategy_version"] = group_stats(joined, "strategy_version", horizons)
    score_stats["_candidate_lane"] = group_stats(joined, "candidate_lane", horizons)
    score_stats["_entry_type"] = group_stats(joined, "entry_type", horizons)
    if observations is not None and outcomes is not None:
        score_stats["_observation_coverage"] = observation_coverage_stats(observations, outcomes, horizons)
    return score_stats


def ranked_outcomes(rows: list[dict[str, Any]], horizon: int, *, reverse: bool) -> list[dict[str, Any]]:
    picked = [
        row for row in rows if int(row.get("horizon_days") or 0) == horizon and num(row.get("return_pct")) is not None
    ]
    ranked = sorted(picked, key=lambda row: num(row.get("return_pct")) or 0, reverse=reverse)
    return [{key: row.get(key) for key in _RANKED_KEYS} for row in ranked[:20]]


def shadow_stats(
    shadow_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    horizons: list[int],
) -> dict[str, Any]:
    if not shadow_rows:
        return {"count": 0, "outcome_stats": {}}
    ordered = sorted(shadow_rows, key=_shadow_sort_key)
    added = sum(len(row.get("diff_added") or []) for row in shadow_rows)
    removed = sum(len(row.get("diff_removed") or []) for row in shadow_rows)
    return {
        "count": len(shadow_rows),
        "avg_added": round(added / len(shadow_rows), 2),
        "avg_removed": round(removed / len(shadow_rows), 2),
        "latest": _shadow_latest_summary(ordered[-1]),
        "outcome_stats": _shadow_outcome_stats(ordered, outcome_rows, horizons),
    }


def recommendations(rows: list[dict[str, Any]], horizons: list[int]) -> list[dict[str, str]]:
    signal_stats = group_stats(rows, "signal_type", horizons)
    recs = []
    for horizon, stats_by_signal in signal_stats.items():
        recs.extend(_recommendation_rows(horizon, stats_by_signal))
    return recs


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [value for value in (num(row.get("return_pct")) for row in rows) if value is not None]
    dds = [value for value in (num(row.get("max_drawdown_pct")) for row in rows) if value is not None]
    if not vals:
        return {"count": 0}
    return {
        "count": len(vals),
        "avg_return_pct": round(sum(vals) / len(vals), 2),
        "median_return_pct": round(statistics.median(vals), 2),
        "win_rate_pct": round(sum(value > 0 for value in vals) / len(vals) * 100, 1),
        "big_win_rate_pct": round(sum(value >= 5 for value in vals) / len(vals) * 100, 1),
        "big_loss_rate_pct": round(sum(value <= -5 for value in vals) / len(vals) * 100, 1),
        "avg_drawdown_pct": round(sum(dds) / len(dds), 2) if dds else None,
        "best_return_pct": round(max(vals), 2),
        "worst_return_pct": round(min(vals), 2),
    }


def num(raw: Any) -> float | None:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


_OBSERVATION_FIELDS = (
    "name",
    "industry",
    "signal_type",
    "track",
    "regime",
    "source",
    "channel",
    "selected_for_ai",
    "ai_recommended",
    "priority_score",
    "trigger_score",
    "stage",
    "springboard_grade",
    "springboard_met_count",
    "selection_mode",
    "policy_version",
    "strategy_version",
    "candidate_lane",
    "entry_type",
    "candidate_status",
    "source",
)

_RANKED_KEYS = [
    "trade_date",
    "code",
    "name",
    "signal_type",
    "track",
    "regime",
    "return_pct",
    "max_drawdown_pct",
    "priority_score",
    "candidate_shadow_score",
    "candidate_shadow_grade",
    "entry_quality_score",
    "entry_quality_grade",
    "entry_quality_risk_flags",
    "data_lineage_coverage_score",
    "data_lineage_coverage_grade",
    "data_lineage_evidence_keys",
    "selection_mode",
    "strategy_version",
    "candidate_lane",
    "entry_type",
]


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _pct(part: int, total: int) -> float:
    return round(part / total * 100.0, 1) if total else 0.0


def _has_features(row: dict[str, Any]) -> bool:
    return bool(_json_map(row.get("features_json")))


def _is_current_like(row: dict[str, Any]) -> bool:
    mode = str(row.get("selection_mode") or "").strip()
    strategy = str(row.get("strategy_version") or "").strip()
    return mode in {"tradeable_l4", "candidate_lane_shadow", "mainline_shadow"} or strategy.startswith(
        "candidate_lane_"
    )


def _is_legacy_like(row: dict[str, Any]) -> bool:
    mode = str(row.get("selection_mode") or "").strip()
    strategy = str(row.get("strategy_version") or "").strip()
    return mode in {"shadow", "l2_bypass_shadow", "strategic_l2_bypass_shadow"} or strategy == "legacy_layered"


def _candidate_shadow_fields(obs: dict[str, Any]) -> dict[str, Any]:
    features = _json_map(obs.get("features_json"))
    shadow_score = _json_map(features.get("candidate_shadow_score"))
    return {
        "candidate_shadow_score": num(shadow_score.get("score")),
        "candidate_shadow_grade": str(shadow_score.get("grade") or "unknown").strip() or "unknown",
        "candidate_shadow_positive_tags": shadow_score.get("positive_tags") or [],
        "candidate_shadow_negative_tags": shadow_score.get("negative_tags") or [],
    }


def _entry_quality_fields(obs: dict[str, Any]) -> dict[str, Any]:
    features = _json_map(obs.get("features_json"))
    entry_quality = _json_map(features.get("entry_quality"))
    return {
        "entry_quality_score": num(entry_quality.get("score")),
        "entry_quality_grade": str(entry_quality.get("grade") or "unknown").strip() or "unknown",
        "entry_quality_tag": str(entry_quality.get("tag") or "").strip(),
        "entry_quality_risk_flags": _str_list(entry_quality.get("risk_flags")),
    }


def _data_lineage_fields(obs: dict[str, Any]) -> dict[str, Any]:
    features = _json_map(obs.get("features_json"))
    lineage = _json_map(features.get("data_lineage"))
    coverage_grade = str(lineage.get("coverage_grade") or "unknown").strip() or "unknown"
    return {
        "data_lineage_coverage_score": num(lineage.get("coverage_score")),
        "data_lineage_coverage_grade": coverage_grade,
        "data_lineage_evidence_keys": _str_list(lineage.get("evidence_keys")),
        "data_lineage_missing_keys": _str_list(lineage.get("missing_keys")),
    }


def _score_bucket_stats(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in horizons:
        horizon_rows = [
            row
            for row in rows
            if int(row.get("horizon_days") or 0) == horizon and num(row.get("priority_score")) is not None
        ]
        result[str(horizon)] = _score_bucket_for_horizon(horizon_rows)
    return result


def _score_bucket_for_horizon(horizon_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = sorted(num(row.get("priority_score")) for row in horizon_rows)
    if not scores:
        return {}
    low_cut = scores[len(scores) // 3]
    high_cut = scores[len(scores) * 2 // 3]
    return {
        "low": stats([row for row in horizon_rows if (num(row.get("priority_score")) or 0) <= low_cut]),
        "mid": stats([row for row in horizon_rows if low_cut < (num(row.get("priority_score")) or 0) <= high_cut]),
        "high": stats([row for row in horizon_rows if (num(row.get("priority_score")) or 0) > high_cut]),
    }


def _candidate_shadow_stats(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    result = group_stats(rows, "candidate_shadow_grade", horizons)
    return _sort_grouped_stats(result, {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "unknown": 5})


def _entry_quality_stats(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    result = group_stats(rows, "entry_quality_grade", horizons)
    return _sort_grouped_stats(result, {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "unknown": 5})


def _coverage_grade_stats(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    result = group_stats(rows, "data_lineage_coverage_grade", horizons)
    return _sort_grouped_stats(result, {"strong": 0, "medium": 1, "thin": 2, "weak": 3, "unknown": 4})


def _sort_grouped_stats(result: dict[str, Any], order: dict[str, int]) -> dict[str, Any]:
    for horizon, stats_by_grade in list(result.items()):
        result[horizon] = {
            grade: grade_stats
            for grade, grade_stats in sorted(stats_by_grade.items(), key=lambda item: order.get(item[0], 99))
        }
    return result


def _evidence_key_stats(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in horizons:
        horizon_rows = [row for row in rows if int(row.get("horizon_days") or 0) == horizon]
        keys = sorted({key for row in horizon_rows for key in row.get("data_lineage_evidence_keys") or []})
        result[str(horizon)] = {
            key: stats([row for row in horizon_rows if key in (row.get("data_lineage_evidence_keys") or [])])
            for key in keys
        }
    return result


def _coverage_summary(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in horizons:
        horizon_rows = [row for row in rows if int(row.get("horizon_days") or 0) == horizon]
        result[str(horizon)] = _coverage_summary_for_horizon(horizon_rows)
    return result


def _coverage_summary_for_horizon(horizon_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [
        score for score in (num(row.get("data_lineage_coverage_score")) for row in horizon_rows) if score is not None
    ]
    if not scores:
        return {"count": 0}
    return {
        "count": len(scores),
        "avg_coverage_score": round(sum(scores) / len(scores), 1),
        "strong_rate_pct": round(
            sum(str(row.get("data_lineage_coverage_grade")) == "strong" for row in horizon_rows)
            / len(horizon_rows)
            * 100,
            1,
        )
        if horizon_rows
        else 0.0,
    }


def _data_lineage_stats(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    return {
        "coverage_summary": _coverage_summary(rows, horizons),
        "coverage_grade": _coverage_grade_stats(rows, horizons),
        "evidence_key": _evidence_key_stats(rows, horizons),
    }


def observation_coverage_stats(
    observations: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    horizons: list[int],
) -> dict[str, Any]:
    outcome_horizons: dict[Any, set[int]] = defaultdict(set)
    for row in outcomes:
        obs_id = row.get("observation_id")
        horizon = int(row.get("horizon_days") or 0)
        if obs_id is not None and horizon > 0:
            outcome_horizons[obs_id].add(horizon)
    return {
        "signal_type": _observation_group_coverage(observations, outcome_horizons, horizons, "signal_type"),
        "selection_mode": _observation_group_coverage(observations, outcome_horizons, horizons, "selection_mode"),
        "strategy_version": _observation_group_coverage(observations, outcome_horizons, horizons, "strategy_version"),
        "candidate_lane": _observation_group_coverage(observations, outcome_horizons, horizons, "candidate_lane"),
        "entry_type": _observation_group_coverage(observations, outcome_horizons, horizons, "entry_type"),
    }


def _observation_group_coverage(
    observations: list[dict[str, Any]],
    outcome_horizons: dict[Any, set[int]],
    horizons: list[int],
    group_key: str,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        groups[str(row.get(group_key) or "unknown")].append(row)
    return {
        key: _observation_coverage_row(rows, outcome_horizons, horizons)
        for key, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    }


def _observation_coverage_row(
    rows: list[dict[str, Any]],
    outcome_horizons: dict[Any, set[int]],
    horizons: list[int],
) -> dict[str, Any]:
    total = len(rows)
    with_any = sum(1 for row in rows if outcome_horizons.get(row.get("id")))
    result = {
        "observations": total,
        "with_any_outcome": with_any,
        "outcome_coverage_pct": _pct(with_any, total),
        "features_coverage_pct": _pct(sum(_has_features(row) for row in rows), total),
        "current_like_pct": _pct(sum(_is_current_like(row) for row in rows), total),
        "legacy_like_pct": _pct(sum(_is_legacy_like(row) for row in rows), total),
        "latest_trade_date": max((str(row.get("trade_date") or "")[:10] for row in rows), default=""),
    }
    for horizon in horizons:
        covered = sum(horizon in outcome_horizons.get(row.get("id"), set()) for row in rows)
        result[f"h{horizon}_coverage_pct"] = _pct(covered, total)
    return result


def _outcome_by_code_date(
    rows: list[dict[str, Any]],
    horizons: list[int],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    outcome_map: dict[tuple[str, str, int], dict[str, Any]] = {}
    valid_horizons = {int(horizon) for horizon in horizons}
    for row in rows:
        horizon = int(row.get("horizon_days") or 0)
        if horizon not in valid_horizons or num(row.get("return_pct")) is None:
            continue
        key = (str(row.get("trade_date") or "")[:10], str(row.get("code") or "").strip(), horizon)
        outcome_map.setdefault(key, row)
    return outcome_map


def _shadow_side_stats(
    shadow_rows: list[dict[str, Any]],
    outcome_map: dict[tuple[str, str, int], dict[str, Any]],
    *,
    side: str,
    horizon: int,
) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    total = 0
    for row in shadow_rows:
        trade_date = str(row.get("trade_date") or "")[:10]
        for code in row.get(side) or []:
            total += 1
            outcome = outcome_map.get((trade_date, str(code).strip(), horizon))
            if outcome:
                matched.append(outcome)
    side_stats = stats(matched)
    side_stats.update(
        {
            "shadow_candidates": total,
            "matched_outcomes": side_stats.get("count", 0),
            "missing_outcomes": max(total - int(side_stats.get("count", 0)), 0),
        }
    )
    return side_stats


def _shadow_outcome_stats(
    shadow_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    horizons: list[int],
) -> dict[str, Any]:
    outcome_map = _outcome_by_code_date(outcome_rows, horizons)
    return {
        str(horizon): {
            "added": _shadow_side_stats(shadow_rows, outcome_map, side="diff_added", horizon=horizon),
            "removed": _shadow_side_stats(shadow_rows, outcome_map, side="diff_removed", horizon=horizon),
        }
        for horizon in horizons
    }


def _shadow_latest_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": row.get("trade_date"),
        "regime": row.get("regime"),
        "schema_version": row.get("schema_version") or "legacy",
        "snapshot_level": row.get("snapshot_level") or "full",
        "selection_summary": row.get("selection_summary") or _legacy_selection_summary(row),
        "diff_added_sample": _code_sample(row.get("diff_added")),
        "diff_removed_sample": _code_sample(row.get("diff_removed")),
        "policy_summary": row.get("policy_summary") or {},
        "registry_summary": row.get("registry_summary") or _legacy_snapshot_count(row, "registry_snapshot"),
        "health_summary": row.get("health_summary") or _legacy_snapshot_count(row, "health_snapshot"),
    }


def _code_sample(raw: Any, limit: int = 12) -> list[str]:
    codes = raw if isinstance(raw, list) else []
    out = []
    seen = set()
    for item in codes:
        code = str(item or "").strip()
        if code and code not in seen:
            out.append(code)
            seen.add(code)
        if len(out) >= limit:
            break
    return out


def _shadow_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("trade_date") or ""), str(row.get("created_at") or row.get("updated_at") or ""))


def _legacy_selection_summary(row: dict[str, Any]) -> dict[str, Any]:
    base = row.get("base_selected") or []
    shadow = row.get("shadow_selected") or []
    added = row.get("diff_added") or []
    removed = row.get("diff_removed") or []
    base_set = {str(code) for code in base}
    shadow_set = {str(code) for code in shadow}
    overlap = len(base_set & shadow_set)
    return {
        "base_count": len(base),
        "shadow_count": len(shadow),
        "overlap_count": overlap,
        "diff_added_count": len(added),
        "diff_removed_count": len(removed),
        "jaccard": round(overlap / max(len(base_set | shadow_set), 1), 4),
    }


def _legacy_snapshot_count(row: dict[str, Any], key: str) -> dict[str, Any]:
    snapshot = row.get(key) or []
    return {"count": len(snapshot), "legacy_full_snapshot": bool(snapshot)}


def _recommendation_rows(horizon: str, stats_by_signal: dict[str, Any]) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    for signal, signal_stats in stats_by_signal.items():
        if signal_stats.get("count", 0) < 10:
            continue
        if signal_stats.get("avg_return_pct", 0) < -3 or signal_stats.get("big_loss_rate_pct", 0) >= 50:
            recs.append(
                {
                    "type": "downweight",
                    "horizon": horizon,
                    "target": signal,
                    "reason": json.dumps(signal_stats, ensure_ascii=False),
                }
            )
    return recs
