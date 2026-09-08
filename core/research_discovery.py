"""Read-only discovery inventory; a detected candidate never grants execution."""

from __future__ import annotations

from collections import Counter
from typing import Any

from core.candidate_metadata import code6
from core.market_trade_mode import resolve_market_trade_mode


def build_research_discovery(trace: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    decisions = trace.get("symbols") or {}
    rows = {
        code6(code): {"code": code6(code), **row, "discovery_sources": ["production_trace"]}
        for code, row in decisions.items()
        if code6(code) and (row.get("l2_eligible") or row.get("entry") or row.get("shadow_lane"))
    }
    for source in ("candidate_entries", "mainline_candidates", "leader_radar_rows"):
        for item in metrics.get(source) or []:
            code = code6(item.get("code"))
            if not code:
                continue
            row = rows.setdefault(code, {"code": code, **(decisions.get(code) or {}), "discovery_sources": []})
            row["discovery_sources"].append(source)
            row[source] = item
    context = trace.get("market_context") or metrics.get("benchmark_context") or {}
    regime = str(context.get("regime") or "UNKNOWN")
    quality = trace.get("data_quality") or metrics.get("data_quality") or {}
    candidates = [_research_row(rows[code], regime, quality) for code in sorted(rows)]
    universe = int((trace.get("counts") or {}).get("universe") or metrics.get("total_symbols") or 0)
    return {
        "schema_version": "research_discovery_v1",
        "trade_date": trace.get("trade_date") or metrics.get("end_trade_date") or "",
        "source_run": trace.get("run") or {},
        "config_digest": trace.get("config_digest") or "",
        "scope": "L2结构发现、近L2影子观察、候选车道、主线和趋势雷达的并集；不等于推荐表或BUY",
        "coverage": {
            "universe": universe,
            "decision_rows": len(decisions),
            "complete_trace": bool(universe and len(decisions) == universe),
        },
        "counts": {"total": len(candidates), **dict(Counter(row["research_status"] for row in candidates))},
        "signal_counts": dict(Counter(row["signal_state"] for row in candidates)),
        "execution_counts": dict(Counter(row["execution_permission"] for row in candidates)),
        "new_buy_allowed": False,
        "direct_buy_allowed": False,
        "candidates": candidates,
    }


def _research_row(row: dict[str, Any], regime: str, quality: dict[str, Any]) -> dict[str, Any]:
    entry = row.get("entry") or row.get("candidate_entries") or {}
    mainline = row.get("mainline_candidates") or {}
    leader = row.get("leader_radar_rows") or {}
    detected = bool(entry or row.get("trigger_labels"))
    blockers = _execution_blockers(row, regime, quality)
    signal = str(entry.get("signal_key") or entry.get("entry_type") or "")
    return {
        "code": row["code"],
        "name": str(row.get("name") or mainline.get("name") or entry.get("name") or leader.get("name") or row["code"]),
        "theme": str(mainline.get("theme") or entry.get("theme") or ""),
        "discovery_sources": list(dict.fromkeys(row["discovery_sources"])),
        "discovery_stage": str(row.get("stage") or mainline.get("status") or "研究发现"),
        "discovery_reason": str(row.get("reason") or entry.get("opportunity") or leader.get("reason") or ""),
        "research_status": "execution_blocked"
        if blockers
        else "ready_for_review"
        if detected
        else "awaiting_confirmation",
        "signal_state": "candidate_detected" if detected else "awaiting_confirmation",
        "signal_types": list(row.get("trigger_labels") or []) or ([signal] if signal else []),
        "confirmation_state": "not_evaluated",
        "timing_condition": str(entry.get("timing") or mainline.get("entry_type") or "等待量价买点及跨日确认"),
        "blocking_reasons": blockers,
        "risk_flags": [str(value) for value in mainline.get("risk_flags") or []]
        + ([str(entry["risk"])] if entry.get("risk") else []),
        "next_step": "先复核拦截条件；解除后仍需信号确认、价格与账户OMS闸门"
        if blockers
        else "复核信号及跨日确认，再检查入场价格和账户OMS闸门",
        "market_regime": regime,
        "execution_permission": "blocked" if blockers else "not_evaluated",
        "trade_readiness": "research_only",
        "new_buy_allowed": False,
        "direct_buy_allowed": False,
    }


def _execution_blockers(row: dict[str, Any], regime: str, quality: dict[str, Any]) -> list[str]:
    blockers = []
    mode = resolve_market_trade_mode(regime)
    if not mode.allow_recommendation_write:
        blockers.append(f"market_gate:{regime}:{mode.reason}")
    if quality.get("trade_readiness") == "observe_only":
        blockers.append("data_quality:" + (", ".join(quality.get("reasons") or []) or "observe_only"))
    if signal := str(row.get("risk_signal") or ""):
        blockers.append(f"exit_signal:{signal}")
    if row.get("stage") in {"数据失败", "基础准入淘汰"}:
        blockers.append(str(row.get("reason") or row["stage"]))
    if status := str((row.get("mainline_candidates") or {}).get("status") or ""):
        if status == "过热不追":
            blockers.append(status)
    return blockers
