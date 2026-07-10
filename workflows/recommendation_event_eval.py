"""Evaluate recommendation rows against fixed-horizon price targets."""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.candidate_guards import policy_candidate_guard_summary
from core.candidate_quality import (
    ai_review_quality_gate_reason,
    entry_quality_risk_penalty,
    risk_adjusted_quality_metrics,
)
from core.constants import TABLE_SIGNAL_OBSERVATIONS
from core.recommendation_event_metrics import build_horizon_event, summarize_horizon_events
from integrations.recommendation_performance import (
    TRACKING_TABLE_BY_MARKET,
    group_records_by_market_code,
    latest_market_records,
    resolve_tracking_market,
)
from integrations.recommendation_tracking_common import chunked, ohlc_map_from_tickflow_hist, recommend_date_to_yyyymmdd
from integrations.supabase_base import create_admin_client, is_admin_configured
from utils.json_text import parse_json_object as _json_map

_RANKING_STRATEGIES = (
    "score_only",
    "ai_then_score",
    "recommend_count",
    "candidate_shadow_then_score",
    "entry_quality_then_score",
)
_GRADE_FALLBACK_SCORE = {
    "S": 90.0,
    "A": 75.0,
    "B": 60.0,
    "C": 45.0,
    "D": 30.0,
    "unknown": -1.0,
}
_DECISION_MIN_READY_ROWS = 10
_DECISION_MIN_HIT_LIFT_PCT = 5.0
_DECISION_MIN_MFE_LIFT_PCT = 0.0
_DECISION_MAX_MAE_WORSE_PCT = -1.0


@dataclass(frozen=True)
class RecommendationEventEvalRequest:
    market: str = "cn"
    horizon_days: int = 5
    target_pct: float = 10.0
    max_dates: int = 30
    kline_count: int = 160
    output_dir: str = "artifacts/recommendation_event_eval"
    top_k: tuple[int, ...] = (1, 3, 5)


def run_recommendation_event_eval(request: RecommendationEventEvalRequest) -> int:
    result = build_recommendation_event_eval(request)
    output_dir = Path(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "summary.json", result["summary"])
    _write_json(output_dir / "events.json", result["events"])
    _write_markdown(output_dir / "summary.md", result)
    summary = result["summary"]["all"]
    print(
        "[recommendation-event-eval] "
        f"ready={summary['rows_ready']}/{summary['rows_total']} "
        f"hit_rate={summary['hit_rate_pct']}% "
        f"target={request.target_pct}%/{request.horizon_days}d"
    )
    print(f"[recommendation-event-eval] wrote {output_dir}")
    return 0


def build_recommendation_event_eval(request: RecommendationEventEvalRequest) -> dict[str, Any]:
    if not is_admin_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")

    market = resolve_tracking_market(request.market)
    records = _fetch_records(market, request.max_dates)
    feature_map = _fetch_observation_feature_map(market, records)
    grouped = group_records_by_market_code(records, market)
    hist_by_code = _fetch_hist_by_code(api_key, sorted(grouped), market, request.kline_count)
    events = _build_events(grouped, hist_by_code, request, feature_map)
    events_by_date = _events_by_date(events)
    ranked_by_strategy = _ranked_events_by_strategy(events_by_date)
    summary = _build_summary(events, request.top_k, events_by_date, ranked_by_strategy)
    policy_selection = _policy_selection(
        events,
        summary.get("ranking_decision") or {},
        events_by_date,
        ranked_by_strategy,
    )
    result = {
        "metadata": _metadata(request, market, records, grouped),
        "summary": summary,
        "daily": _daily_summary(events_by_date),
        "policy_selection": policy_selection,
        "events": events,
    }
    if guard_summary := policy_candidate_guard_summary(policy_selection):
        result["candidate_guard_summary"] = guard_summary
    return result


def _fetch_records(market: str, max_dates: int) -> list[dict[str, Any]]:
    table = TRACKING_TABLE_BY_MARKET[market]
    client = create_admin_client()
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        resp = client.table(table).select("*").order("recommend_date", desc=False).range(start, start + 999).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            return latest_market_records(rows, max_dates)
        start += 1000


def _fetch_observation_feature_map(
    market: str,
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    dates = sorted({_date_iso(recommend_date_to_yyyymmdd(row.get("recommend_date"))) for row in records})
    dates = [day for day in dates if day]
    if not dates:
        return {}
    client = create_admin_client()
    rows: list[dict[str, Any]] = []
    try:
        for batch in chunked(dates, 100):
            resp = (
                client.table(TABLE_SIGNAL_OBSERVATIONS)
                .select("trade_date,code,features_json")
                .eq("market", market)
                .in_("trade_date", batch)
                .execute()
            )
            rows.extend(resp.data or [])
    except Exception:
        return {}
    return _observation_feature_map(rows, market)


def _observation_feature_map(rows: list[dict[str, Any]], market: str = "cn") -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (_code_key(row.get("code"), market), _date_compact(row.get("trade_date")))
        features = _json_map(row.get("features_json"))
        if key[0] and key[1] and features:
            out[key] = _merge_quality_features(out.get(key, {}), features)
    return out


def _fetch_hist_by_code(
    api_key: str,
    codes: list[str],
    market: str,
    kline_count: int,
) -> dict[str, pd.DataFrame | None]:
    symbol_map = _tickflow_symbol_map(codes, market)
    hist_map = _fetch_histories(api_key, sorted(set(symbol_map.values())), kline_count)
    return {code: hist_map.get(symbol) for code, symbol in symbol_map.items()}


def _tickflow_symbol_map(codes: list[str], market: str) -> dict[str, str]:
    if market != "cn":
        return {code: code for code in codes if code}
    from integrations.tickflow_client import normalize_cn_symbol

    return {code: symbol for code in codes if (symbol := normalize_cn_symbol(code))}


def _fetch_histories(api_key: str, symbols: list[str], kline_count: int) -> dict[str, pd.DataFrame]:
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=api_key)
    hist_map: dict[str, pd.DataFrame] = {}
    for batch in chunked(symbols, _batch_size()):
        hist_map.update(client.get_klines_batch(batch, period="1d", count=max(int(kline_count), 1), adjust="forward"))
    return hist_map


def _build_events(
    grouped: dict[str, list[dict[str, Any]]],
    hist_by_code: dict[str, pd.DataFrame | None],
    request: RecommendationEventEvalRequest,
    feature_map: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    feature_map = feature_map or {}
    for code, rows in sorted(grouped.items()):
        ohlc = ohlc_map_from_tickflow_hist(hist_by_code.get(code))
        events.extend(_event_with_quality(row, code, ohlc, request, feature_map) for row in rows)
    return sorted(events, key=lambda item: (item.get("recommend_date") or 0, str(item.get("code") or "")))


def _event_with_quality(
    row: dict[str, Any],
    code: str,
    ohlc: dict[str, dict[str, float]],
    request: RecommendationEventEvalRequest,
    feature_map: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    event = build_horizon_event(row, ohlc, horizon_days=request.horizon_days, target_pct=request.target_pct)
    key = (_code_key(code, resolve_tracking_market(request.market)), _date_compact(row.get("recommend_date")))
    return {**event, **_quality_feature_fields(row, feature_map.get(key, {}))}


def _build_summary(
    events: list[dict[str, Any]],
    top_k: tuple[int, ...],
    events_by_date: dict[int, list[dict[str, Any]]] | None = None,
    ranked_by_strategy: dict[str, dict[int, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    events_by_date = _events_by_date(events) if events_by_date is None else events_by_date
    ranked_by_strategy = ranked_by_strategy or _ranked_events_by_strategy(events_by_date)
    top_k_by_strategy = _top_k_by_strategy(ranked_by_strategy, top_k)
    lift_by_strategy = _strategy_lift_summary(top_k_by_strategy)
    summary = {
        "all": summarize_horizon_events(events),
        "ai": summarize_horizon_events([event for event in events if event.get("is_ai_recommended")]),
        "non_ai": summarize_horizon_events([event for event in events if not event.get("is_ai_recommended")]),
        "top_k": top_k_by_strategy["score_only"],
        "top_k_by_strategy": top_k_by_strategy,
        "top_k_lift_vs_score_only": lift_by_strategy,
        "ranking_decision": _ranking_decision(lift_by_strategy),
        "candidate_shadow_grade": _grade_summary(events, "candidate_shadow_grade"),
        "entry_quality_grade": _grade_summary(events, "entry_quality_grade"),
    }
    return summary


def _grade_summary(events: list[dict[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[_grade(event.get(field))].append(event)
    order = ("S", "A", "B", "C", "D", "unknown")
    return {grade: summarize_horizon_events(groups[grade]) for grade in order if grade in groups}


def _top_k_by_strategy(
    ranked_by_strategy: dict[str, dict[int, list[dict[str, Any]]]], top_k: tuple[int, ...]
) -> dict[str, dict[str, Any]]:
    return {
        strategy: {
            str(k): _top_k_summary_from_ranked(ranked_by_strategy.get(strategy, {}), k, strategy)
            for k in sorted({max(int(value), 1) for value in top_k})
        }
        for strategy in _RANKING_STRATEGIES
    }


def _strategy_lift_summary(top_k_by_strategy: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    baseline = top_k_by_strategy.get("score_only") or {}
    result: dict[str, dict[str, Any]] = {}
    for strategy, top_rows in top_k_by_strategy.items():
        if strategy == "score_only":
            continue
        result[strategy] = {
            str(k): _strategy_lift_row(item, baseline.get(str(k), {}))
            for k, item in top_rows.items()
            if str(k) in baseline
        }
    return result


def _strategy_lift_row(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "ranking": row.get("ranking"),
        "baseline_ranking": baseline.get("ranking", "score_only"),
        "top_k": row.get("top_k"),
        "rows_ready": row.get("rows_ready", 0),
        "baseline_rows_ready": baseline.get("rows_ready", 0),
        "hit_rate_delta_pct": _delta(row, baseline, "hit_rate_pct"),
        "close_win_rate_delta_pct": _delta(row, baseline, "close_win_rate_pct"),
        "avg_close_return_delta_pct": _delta(row, baseline, "avg_close_return_horizon_pct"),
        "close_payoff_delta": _delta(row, baseline, "close_payoff_ratio"),
        "avg_mfe_delta_pct": _delta(row, baseline, "avg_mfe_horizon_pct"),
        "avg_mae_delta_pct": _delta(row, baseline, "avg_mae_horizon_pct"),
        "mfe_mae_delta": _delta(row, baseline, "mfe_mae_ratio"),
    }


def _ranking_decision(lift_by_strategy: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidates = {
        strategy: _ranking_strategy_decision(strategy, top_rows) for strategy, top_rows in lift_by_strategy.items()
    }
    promotable = [item for item in candidates.values() if item.get("status") == "candidate"]
    watch = [item for item in candidates.values() if item.get("status") == "watch"]
    best = _best_decision(promotable or watch)
    status = "candidate" if promotable else "watch" if watch else _fallback_decision_status(candidates)
    return {
        "status": status,
        "recommended_strategy": best.get("strategy", "score_only") if promotable else "score_only",
        "recommended_top_k": best.get("top_k") if promotable else None,
        "watch_strategy": best.get("strategy") if watch and not promotable else None,
        "reason": _ranking_decision_reason(status, best),
        "thresholds": {
            "min_ready_rows": _DECISION_MIN_READY_ROWS,
            "min_hit_lift_pct": _DECISION_MIN_HIT_LIFT_PCT,
            "min_mfe_lift_pct": _DECISION_MIN_MFE_LIFT_PCT,
            "max_mae_worse_pct": _DECISION_MAX_MAE_WORSE_PCT,
        },
        "candidates": candidates,
    }


def _ranking_strategy_decision(strategy: str, top_rows: dict[str, Any]) -> dict[str, Any]:
    rows = [_decision_candidate(strategy, k, row) for k, row in top_rows.items()]
    return _best_decision(rows) or _empty_decision(strategy)


def _decision_candidate(strategy: str, top_k: str, row: dict[str, Any]) -> dict[str, Any]:
    ready = int(row.get("rows_ready") or 0)
    baseline_ready = int(row.get("baseline_rows_ready") or 0)
    hit_lift = _optional_number(row.get("hit_rate_delta_pct")) or 0.0
    mfe_lift = _optional_number(row.get("avg_mfe_delta_pct")) or 0.0
    mae_delta = _optional_number(row.get("avg_mae_delta_pct"))
    sample_ok = ready >= _DECISION_MIN_READY_ROWS and baseline_ready >= _DECISION_MIN_READY_ROWS
    lift_ok = hit_lift >= _DECISION_MIN_HIT_LIFT_PCT and mfe_lift >= _DECISION_MIN_MFE_LIFT_PCT
    risk_ok = mae_delta is not None and mae_delta >= _DECISION_MAX_MAE_WORSE_PCT
    return {
        "strategy": strategy,
        "top_k": str(top_k),
        "status": _candidate_status(sample_ok, lift_ok, risk_ok, _decision_score(hit_lift, mfe_lift, mae_delta)),
        "decision_score": _decision_score(hit_lift, mfe_lift, mae_delta),
        "sample_ok": sample_ok,
        "lift_ok": lift_ok,
        "risk_ok": risk_ok,
        **row,
    }


def _candidate_status(sample_ok: bool, lift_ok: bool, risk_ok: bool, score: float) -> str:
    if not sample_ok:
        return "insufficient_sample"
    if lift_ok and risk_ok and score > 0:
        return "candidate"
    if risk_ok and score > 0:
        return "watch"
    return "keep_score_only"


def _decision_score(hit_lift: float, mfe_lift: float, mae_delta: float | None) -> float:
    risk_adjustment = mae_delta if mae_delta is not None else -5.0
    return round(hit_lift + 0.5 * mfe_lift + min(max(risk_adjustment, -5.0), 2.0), 2)


def _best_decision(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda item: (item.get("decision_score", -999.0), item.get("rows_ready", 0)), default={})


def _empty_decision(strategy: str) -> dict[str, Any]:
    return {"strategy": strategy, "status": "insufficient_sample", "decision_score": 0.0}


def _fallback_decision_status(candidates: dict[str, dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in candidates.values()}
    return "insufficient_sample" if statuses == {"insufficient_sample"} else "keep_score_only"


def _ranking_decision_reason(status: str, best: dict[str, Any]) -> str:
    if status == "candidate":
        return f"{best.get('strategy')} top{best.get('top_k')} passed lift and risk gates"
    if status == "watch":
        return f"{best.get('strategy')} improved some metrics but did not pass all promotion gates"
    if status == "insufficient_sample":
        return "not enough ready labeled rows to change ranking"
    return "no alternative ranking beat score_only after risk adjustment"


def _policy_selection(
    events: list[dict[str, Any]],
    decision: dict[str, Any],
    events_by_date: dict[int, list[dict[str, Any]]] | None = None,
    ranked_by_strategy: dict[str, dict[int, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    by_date = _events_by_date(events) if events_by_date is None else events_by_date
    if not by_date:
        return _empty_policy_selection(decision)
    latest_date = max(by_date)
    strategy = _policy_strategy(decision)
    top_k = _policy_top_k(decision)
    latest_ranked = _ranked_latest_events(by_date, ranked_by_strategy, strategy, latest_date)
    latest_events = latest_ranked[:top_k]
    quality_gate_reason = _policy_quality_gate_reason(latest_events, str(decision.get("status", "unknown")))
    status = "watch" if quality_gate_reason else str(decision.get("status", "unknown"))
    picks = [_policy_pick(row, idx + 1, strategy, status, quality_gate_reason) for idx, row in enumerate(latest_events)]
    return {
        "status": status,
        "selection_strategy": strategy,
        "top_k": top_k,
        "recommend_date": latest_date,
        "uses_promoted_ranking": decision.get("status") == "candidate" and status == "candidate",
        "watch_strategy": decision.get("watch_strategy"),
        "reason": quality_gate_reason or decision.get("reason", ""),
        "action_plan": _policy_selection_action_plan(status, picks, quality_gate_reason),
        "picks": picks,
    }


def _empty_policy_selection(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": decision.get("status", "unknown"),
        "selection_strategy": "score_only",
        "top_k": 0,
        "recommend_date": None,
        "uses_promoted_ranking": False,
        "watch_strategy": decision.get("watch_strategy"),
        "reason": "no recommendation events available",
        "action_plan": _policy_selection_action_plan(str(decision.get("status", "unknown")), []),
        "picks": [],
    }


def _policy_selection_action_plan(
    policy_status: str,
    picks: list[dict[str, Any]],
    quality_gate_reason: str = "",
) -> dict[str, Any]:
    codes = [str(pick.get("code") or "").strip() for pick in picks if str(pick.get("code") or "").strip()]
    ai_review_allowed = bool(codes) and policy_status == "candidate"
    payload = {
        "primary_action": "generate_ai_report" if ai_review_allowed else "watch_latest_policy_selection",
        "candidate_action": "generate_ai_report" if ai_review_allowed else "watch_only",
        "new_buy_allowed": False,
        "ai_review_allowed": ai_review_allowed,
        "trade_readiness": "research_only",
        "review_status": "ready_for_ai_review" if ai_review_allowed else "watch_only",
        "reason": quality_gate_reason or _policy_selection_action_reason(policy_status, ai_review_allowed),
        "next_step": _policy_pick_next_step(policy_status),
    }
    if ai_review_allowed:
        payload["next_tool"] = {
            "tool": "generate_ai_report",
            "args": {"stock_codes": codes[:10]},
            "reason": "推荐事件评估只读候选已过排序门槛，先生成 AI 研报再做攻防决策",
        }
    return payload


def _policy_selection_action_reason(policy_status: str, ai_review_allowed: bool) -> str:
    if ai_review_allowed:
        return "只读推荐事件评估已通过排序接入门槛，可进入 AI 研报；不直接触发买入"
    if policy_status == "insufficient_sample":
        return "只读推荐事件评估样本不足，继续观察；不直接触发买入"
    if policy_status == "watch":
        return "只读推荐事件评估仅为观察项，尚未通过全部门槛；不直接触发买入"
    return "只读推荐事件评估未通过排序接入门槛，继续观察；不直接触发买入"


def _policy_strategy(decision: dict[str, Any]) -> str:
    if decision.get("status") == "candidate":
        return str(decision.get("recommended_strategy") or "score_only")
    return "score_only"


def _policy_top_k(decision: dict[str, Any]) -> int:
    if decision.get("status") != "candidate":
        return 1
    try:
        return max(int(decision.get("recommended_top_k") or 1), 1)
    except (TypeError, ValueError):
        return 1


def _policy_pick(
    event: dict[str, Any],
    rank: int,
    strategy: str,
    policy_status: str,
    quality_gate_reason: str = "",
) -> dict[str, Any]:
    return {
        "rank": rank,
        "selection_strategy": strategy,
        "code": event.get("code"),
        "name": event.get("name"),
        "recommend_date": event.get("recommend_date"),
        "is_ai_recommended": bool(event.get("is_ai_recommended")),
        "funnel_score": event.get("funnel_score"),
        "recommend_count": event.get("recommend_count"),
        "candidate_shadow_score": event.get("candidate_shadow_score"),
        "candidate_shadow_grade": event.get("candidate_shadow_grade"),
        "entry_quality_score": event.get("entry_quality_score"),
        "entry_quality_grade": event.get("entry_quality_grade"),
        "entry_quality_risk_flags": event.get("entry_quality_risk_flags") or [],
        **risk_adjusted_quality_metrics(event),
        "label_ready": bool(event.get("label_ready")),
        "label_status": event.get("label_status"),
        "action_status": _policy_pick_action_status(policy_status),
        "quality_factors": _policy_pick_quality_factors(event),
        "risk_factors": _policy_pick_risk_factors(event, policy_status, quality_gate_reason),
        "next_step": _policy_pick_next_step(policy_status),
    }


def _policy_pick_action_status(policy_status: str) -> str:
    return "ready_for_ai_review" if policy_status == "candidate" else "watch_only"


def _policy_pick_quality_factors(event: dict[str, Any]) -> list[str]:
    factors: list[str] = []
    if grade := _clean_text(event.get("candidate_shadow_grade")):
        factors.append(f"候选影子评级 {grade}")
    if grade := _clean_text(event.get("entry_quality_grade")):
        factors.append(f"入场质量评级 {grade}")
    if event.get("is_ai_recommended"):
        factors.append("已进入 AI 推荐")
    return factors


def _policy_quality_gate_reason(events: list[dict[str, Any]], policy_status: str) -> str:
    if policy_status != "candidate":
        return ""
    for event in events:
        if reason := ai_review_quality_gate_reason(event, _clean_text(event.get("code")) or "最新候选"):
            return reason
    return ""


def _policy_pick_risk_factors(
    event: dict[str, Any],
    policy_status: str,
    quality_gate_reason: str = "",
) -> list[str]:
    risks = [_clean_text(item) for item in event.get("entry_quality_risk_flags") or [] if _clean_text(item)]
    if quality_gate_reason:
        risks.append(quality_gate_reason)
    elif policy_status != "candidate":
        risks.append("排序接入门槛未过，按 score_only 观察")
    if not event.get("label_ready"):
        risks.append("最新候选的未来窗口标签尚未成熟")
    return risks


def _policy_pick_next_step(policy_status: str) -> str:
    if policy_status == "candidate":
        return "生成 AI 研报并结合持仓形成攻防决策"
    return "先作为观察候选复核，等待更多样本或研报证据后再升级"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _top_k_summary(events: list[dict[str, Any]], k: int, strategy: str = "score_only") -> dict[str, Any]:
    return _top_k_summary_by_date(_events_by_date(events), k, strategy)


def _top_k_summary_by_date(
    events_by_date: dict[int, list[dict[str, Any]]],
    k: int,
    strategy: str = "score_only",
) -> dict[str, Any]:
    ranked_by_date = {rec_date: _rank_events(rows, strategy) for rec_date, rows in events_by_date.items()}
    return _top_k_summary_from_ranked(ranked_by_date, k, strategy)


def _top_k_summary_from_ranked(
    ranked_by_date: dict[int, list[dict[str, Any]]],
    k: int,
    strategy: str,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    for rows in ranked_by_date.values():
        selected.extend(rows[:k])
    summary = summarize_horizon_events(selected)
    summary["days_covered"] = len(ranked_by_date)
    summary["top_k"] = k
    summary["ranking"] = strategy
    return summary


def _daily_summary(events_by_date: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for rec_date, day_events in sorted(events_by_date.items()):
        summary = summarize_horizon_events(day_events)
        rows.append({"recommend_date": rec_date, **summary})
    return rows


def _events_by_date(events: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        rec_date = event.get("recommend_date")
        if rec_date:
            grouped[int(rec_date)].append(event)
    return dict(grouped)


def _ranked_events_by_strategy(
    events_by_date: dict[int, list[dict[str, Any]]],
) -> dict[str, dict[int, list[dict[str, Any]]]]:
    return {
        strategy: {rec_date: _rank_events(rows, strategy) for rec_date, rows in events_by_date.items()}
        for strategy in _RANKING_STRATEGIES
    }


def _ranked_latest_events(
    events_by_date: dict[int, list[dict[str, Any]]],
    ranked_by_strategy: dict[str, dict[int, list[dict[str, Any]]]] | None,
    strategy: str,
    latest_date: int,
) -> list[dict[str, Any]]:
    if ranked_by_strategy:
        ranked = ranked_by_strategy.get(strategy, {}).get(latest_date)
        if ranked is not None:
            return ranked
    return _rank_events(events_by_date[latest_date], strategy)


def _rank_events(events: list[dict[str, Any]], strategy: str) -> list[dict[str, Any]]:
    return sorted(events, key=lambda event: _rank_key(event, strategy), reverse=True)


def _rank_key(event: dict[str, Any], strategy: str) -> tuple[Any, ...]:
    ai = 1.0 if event.get("is_ai_recommended") else 0.0
    score = _number(event.get("funnel_score"), -1.0)
    count = _number(event.get("recommend_count"), 0.0)
    code = str(event.get("code") or "")
    if strategy == "ai_then_score":
        return ai, score, count, code
    if strategy == "recommend_count":
        return count, score, ai, code
    if strategy == "candidate_shadow_then_score":
        return _risk_adjusted_quality_rank(event, "candidate_shadow"), score, ai, count, code
    if strategy == "entry_quality_then_score":
        return _risk_adjusted_quality_rank(event, "entry_quality"), score, ai, count, code
    return score, ai, count, code


def _metadata(
    request: RecommendationEventEvalRequest,
    market: str,
    records: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "market": market,
        "horizon_days": request.horizon_days,
        "target_pct": request.target_pct,
        "max_dates": request.max_dates,
        "kline_count": request.kline_count,
        "records": len(records),
        "codes": len(grouped),
    }


def _write_markdown(path: Path, result: dict[str, Any]) -> None:
    meta = result["metadata"]
    lines = [
        "# Recommendation Event Evaluation",
        "",
        f"- Market: `{meta['market']}`",
        f"- Target: `{meta['target_pct']}%` within `{meta['horizon_days']}` future trading days",
        f"- Records: `{meta['records']}` rows / `{meta['codes']}` codes",
        "",
        "| Slice | Ready | Hit rate | Close win | Avg close | Payoff | Avg MFE | Avg MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(_summary_markdown_rows(result["summary"]))
    lines.extend(_strategy_markdown(result["summary"]["top_k_by_strategy"]))
    lines.extend(_strategy_lift_markdown(result["summary"].get("top_k_lift_vs_score_only") or {}))
    lines.extend(_ranking_decision_markdown(result["summary"].get("ranking_decision") or {}))
    lines.extend(_policy_selection_markdown(result.get("policy_selection") or {}))
    lines.extend(_quality_markdown(result["summary"]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary_markdown_rows(summary: dict[str, Any]) -> list[str]:
    rows = [_summary_row("all", summary["all"]), _summary_row("ai", summary["ai"])]
    rows.extend(_summary_row(f"top{k}", item) for k, item in summary["top_k"].items())
    return rows


def _strategy_markdown(top_k_by_strategy: dict[str, dict[str, Any]]) -> list[str]:
    rows = [
        "",
        "## Ranking Strategy Comparison",
        "",
        "| Strategy | Top-K | Ready | Hit rate | Close win | Avg close | Payoff | Avg MFE | Avg MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy, top_rows in top_k_by_strategy.items():
        for k, item in top_rows.items():
            ready = f"{item.get('rows_ready', 0)}/{item.get('rows_total', 0)}"
            rows.append(
                f"| {strategy} | {k} | {ready} | {_fmt(item.get('hit_rate_pct'))}% | "
                f"{_fmt(item.get('close_win_rate_pct'))}% | {_fmt(item.get('avg_close_return_horizon_pct'))}% | "
                f"{_fmt(item.get('close_payoff_ratio'))} | {_fmt(item.get('avg_mfe_horizon_pct'))}% | "
                f"{_fmt(item.get('avg_mae_horizon_pct'))}% |"
            )
    return rows


def _strategy_lift_markdown(lift_by_strategy: dict[str, dict[str, Any]]) -> list[str]:
    rows = [
        "",
        "## Ranking Lift vs score_only",
        "",
        "| Strategy | Top-K | Ready | Hit Δ | Close win Δ | Avg close Δ | Payoff Δ | MFE Δ | MAE Δ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy, top_rows in lift_by_strategy.items():
        for k, item in top_rows.items():
            ready = f"{item.get('rows_ready', 0)}/{item.get('baseline_rows_ready', 0)}"
            rows.append(
                f"| {strategy} | {k} | {ready} | {_fmt_delta(item.get('hit_rate_delta_pct'))}pp | "
                f"{_fmt_delta(item.get('close_win_rate_delta_pct'))}pp | "
                f"{_fmt_delta(item.get('avg_close_return_delta_pct'))}pp | "
                f"{_fmt_delta(item.get('close_payoff_delta'))} | {_fmt_delta(item.get('avg_mfe_delta_pct'))}pp | "
                f"{_fmt_delta(item.get('avg_mae_delta_pct'))}pp |"
            )
    return rows


def _ranking_decision_markdown(decision: dict[str, Any]) -> list[str]:
    rows = [
        "",
        "## Ranking Decision Gate",
        "",
        f"- Status: `{decision.get('status', 'unknown')}`",
        f"- Recommended strategy: `{decision.get('recommended_strategy', 'score_only')}`",
        f"- Recommended Top-K: `{decision.get('recommended_top_k') or 'n/a'}`",
        f"- Reason: {decision.get('reason', '-')}",
        "",
        "| Strategy | Best Top-K | Status | Score | Ready | Hit Δ | MFE Δ | MAE Δ |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for strategy, item in (decision.get("candidates") or {}).items():
        ready = f"{item.get('rows_ready', 0)}/{item.get('baseline_rows_ready', 0)}"
        rows.append(
            f"| {strategy} | {item.get('top_k', 'n/a')} | {item.get('status', 'unknown')} | "
            f"{_fmt(item.get('decision_score'))} | {ready} | {_fmt_delta(item.get('hit_rate_delta_pct'))}pp | "
            f"{_fmt_delta(item.get('avg_mfe_delta_pct'))}pp | {_fmt_delta(item.get('avg_mae_delta_pct'))}pp |"
        )
    return rows


def _policy_selection_markdown(selection: dict[str, Any]) -> list[str]:
    action_plan = selection.get("action_plan") if isinstance(selection.get("action_plan"), dict) else {}
    rows = [
        "",
        "## Latest Policy Selection",
        "",
        f"- Policy status: `{selection.get('status', 'unknown')}`",
        f"- Selection strategy: `{selection.get('selection_strategy', 'score_only')}`",
        f"- Recommended date: `{selection.get('recommend_date') or 'n/a'}`",
        f"- Uses promoted ranking: `{bool(selection.get('uses_promoted_ranking'))}`",
        f"- Trade readiness: `{action_plan.get('trade_readiness', 'research_only')}`",
        f"- New buy allowed: `{bool(action_plan.get('new_buy_allowed'))}`",
        f"- AI review allowed: `{bool(action_plan.get('ai_review_allowed'))}`",
        f"- Next step: {action_plan.get('next_step') or '-'}",
        f"- Reason: {selection.get('reason') or action_plan.get('reason') or '-'}",
        "",
        "| Rank | Code | Name | Action | AI | Funnel | Quality | Shadow | Entry | Label | Risks |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    picks = selection.get("picks") if isinstance(selection.get("picks"), list) else []
    if not picks:
        return [*rows, "| n/a | - | - | - | - | n/a | n/a | n/a | n/a | - | - |"]
    for pick in picks:
        if isinstance(pick, dict):
            rows.append(_policy_pick_markdown_row(pick))
    return rows


def _policy_pick_markdown_row(pick: dict[str, Any]) -> str:
    shadow = _quality_cell(pick.get("candidate_shadow_score"), pick.get("candidate_shadow_grade"))
    entry = _quality_cell(pick.get("entry_quality_score"), pick.get("entry_quality_grade"))
    risks = _markdown_join(pick.get("risk_factors"), 2)
    return (
        f"| {pick.get('rank', '')} | {_markdown_cell(pick.get('code'))} | {_markdown_cell(pick.get('name'))} | "
        f"{_markdown_cell(pick.get('action_status') or '-')} | {'Y' if pick.get('is_ai_recommended') else 'N'} | "
        f"{_fmt(pick.get('funnel_score'))} | {_fmt(pick.get('risk_adjusted_quality_score'))} | "
        f"{shadow} | {entry} | {_markdown_cell(pick.get('label_status') or '-')} | {risks} |"
    )


def _quality_cell(score: Any, grade: Any) -> str:
    grade_text = str(grade or "").strip()
    score_text = _fmt(score)
    return f"{grade_text}/{score_text}" if grade_text else score_text


def _markdown_join(value: Any, limit: int) -> str:
    if not isinstance(value, list):
        return "-"
    items = [_markdown_cell(item) for item in value[:limit] if str(item or "").strip()]
    return "; ".join(items) or "-"


def _markdown_cell(value: Any) -> str:
    return str(value or "").strip().replace("|", "/") or "-"


def _quality_markdown(summary: dict[str, Any]) -> list[str]:
    rows = ["", "## Candidate Quality Slices"]
    rows.extend(_quality_table("Candidate shadow grade", summary.get("candidate_shadow_grade") or {}))
    rows.extend(_quality_table("Entry quality grade", summary.get("entry_quality_grade") or {}))
    return rows


def _quality_table(title: str, grade_rows: dict[str, Any]) -> list[str]:
    rows = [
        "",
        f"### {title}",
        "",
        "| Grade | Ready | Hit rate | Close win | Avg close | Payoff | Avg MFE | Avg MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if not grade_rows:
        return [*rows, "| n/a | 0/0 | n/a | n/a | n/a | n/a | n/a | n/a |"]
    return [*rows, *(_summary_row(grade, item) for grade, item in grade_rows.items())]


def _summary_row(label: str, row: dict[str, Any]) -> str:
    ready = f"{row.get('rows_ready', 0)}/{row.get('rows_total', 0)}"
    return (
        f"| {label} | {ready} | {_fmt(row.get('hit_rate_pct'))}% | "
        f"{_fmt(row.get('close_win_rate_pct'))}% | {_fmt(row.get('avg_close_return_horizon_pct'))}% | "
        f"{_fmt(row.get('close_payoff_ratio'))} | {_fmt(row.get('avg_mfe_horizon_pct'))}% | "
        f"{_fmt(row.get('avg_mae_horizon_pct'))}% |"
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _number(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _delta(row: dict[str, Any], baseline: dict[str, Any], key: str) -> float | None:
    left = _optional_number(row.get(key))
    right = _optional_number(baseline.get(key))
    if left is None or right is None:
        return None
    return round(left - right, 2)


def _quality_rank(event: dict[str, Any], prefix: str) -> float:
    if (score := _optional_number(event.get(f"{prefix}_score"))) is not None:
        return score
    return _GRADE_FALLBACK_SCORE[_grade(event.get(f"{prefix}_grade"))]


def _risk_adjusted_quality_rank(event: dict[str, Any], prefix: str) -> float:
    return max(0.0, _quality_rank(event, prefix) - entry_quality_risk_penalty(event))


def _quality_feature_fields(row: dict[str, Any], observed_features: dict[str, Any] | None = None) -> dict[str, Any]:
    features = _merge_quality_features(_json_map(observed_features), _json_map(row.get("features_json")))
    shadow = _json_map(features.get("candidate_shadow_score"))
    entry = _json_map(features.get("entry_quality"))
    return {
        "candidate_shadow_score": _optional_number(shadow.get("score")),
        "candidate_shadow_grade": _grade(shadow.get("grade")),
        "entry_quality_score": _optional_number(entry.get("score")),
        "entry_quality_grade": _grade(entry.get("grade")),
        "entry_quality_risk_flags": _str_list(entry.get("risk_flags")),
    }


def _merge_quality_features(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ("candidate_shadow_score", "entry_quality"):
        value = _json_map(overlay.get(key))
        if value:
            merged[key] = value
    return merged


def _grade(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    return text if text in {"S", "A", "B", "C", "D"} else "unknown"


def _optional_number(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return round(value, 2) if math.isfinite(value) else None


def _str_list(raw: Any) -> list[str]:
    if isinstance(raw, list | tuple | set):
        return [text for item in raw if (text := str(item or "").strip())]
    text = str(raw or "").strip()
    return [text] if text else []


def _code_key(raw: Any, market: str) -> str:
    text = str(raw or "").strip()
    if market == "cn":
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits[-6:].zfill(6) if digits else ""
    return text.upper()


def _date_iso(raw: Any) -> str:
    text = _date_compact(raw)
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if len(text) == 8 else ""


def _date_compact(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())[:8]


def _fmt(raw: Any) -> str:
    return "n/a" if raw is None else str(raw)


def _fmt_delta(raw: Any) -> str:
    if raw is None:
        return "n/a"
    value = _optional_number(raw)
    if value is None:
        return "n/a"
    return f"{value:+g}"


def _batch_size() -> int:
    raw = os.getenv("RECOMMENDATION_EVENT_EVAL_BATCH_SIZE", "").strip()
    try:
        return max(min(int(float(raw or 80)), 100), 1)
    except (TypeError, ValueError):
        return 80
