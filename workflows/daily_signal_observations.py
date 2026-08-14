"""Signal-observation helpers for the daily funnel workflow."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from core.candidate_metadata import build_candidate_signal_metadata_map, candidate_signal_triggers, merge_trigger_maps
from core.candidate_metadata import code6 as _last_six_digits
from core.candidate_policy import candidate_score_value
from utils.env import env_flag as _env_flag
from utils.env import env_float as _env_float
from utils.env import env_int as _env_int
from utils.safe import safe_float as _safe_float

LogFn = Callable[[str, str | None], None]


def apply_step3_springboard_updates(payload: list[dict], updates: dict[str, dict]) -> None:
    if not payload or not updates:
        return
    for row in payload:
        code = _last_six_digits(row.get("code", ""))
        if code in updates:
            row.update(updates[code])


def shadow_observation_inputs(step2_details: dict) -> tuple[dict[str, list[tuple[str, float]]], dict[str, str], dict]:
    score_map = {
        str(code).strip(): candidate_score_value(score)
        for code, score in (step2_details.get("shadow_score_map") or {}).items()
        if str(code).strip()
    }
    triggers: dict[str, list[tuple[str, float]]] = {}
    source_map: dict[str, str] = {}
    for signal_type, source_key in (("shadow_added", "shadow_added"), ("shadow_removed", "shadow_removed")):
        rows: list[tuple[str, float]] = []
        for code in step2_details.get(signal_type, []) or []:
            code_s = str(code).strip()
            if code_s:
                rows.append((code_s, candidate_score_value(score_map.get(code_s))))
                source_map[code_s] = source_key
        if rows:
            triggers[signal_type] = rows
    return triggers, source_map, score_map


def build_external_capital_context_map(
    step2_details: dict,
    ai_codes: list[str],
    logs_path: str | None,
    *,
    trade_date: str,
    log_fn: LogFn | None = None,
) -> dict[str, dict]:
    if os.getenv("FUNNEL_EXTERNAL_CAPITAL_CONTEXT", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {}
    codes = _external_capital_codes(step2_details, ai_codes)
    if not codes:
        return {}
    # 上限 20 -> 150。原因两面：
    # 放宽——龙虎榜/融资融券/大宗交易/资金流等六个源都是「按交易日整表拉一次、本地按代码
    # 匹配」，Tushare 调用次数与标的数无关，卡 20 纯属白丢样本（实测 5,453 条 observation
    # 里只有 3 条带资金特征，0.06%）。逐标的的只有 tick，由 TICK_MAX_SYMBOLS 单独限流。
    # 但不取全量——资金片段会写进 features_json，中位 570B/行，是实打实的 Supabase 存储成本。
    # 实测每日不重复候选中位 116、p90 148、最大 165，150 覆盖 90% 的交易日；再往上只多摊到
    # 尾部的低分候选，样本价值低而每行都要付存储。
    max_symbols = _env_int("FUNNEL_EXTERNAL_CAPITAL_MAX_SYMBOLS", 150, minimum=1)
    include_tick = _env_flag("FUNNEL_EXTERNAL_CAPITAL_TICK_CONTEXT")
    tick_max = _env_int("FUNNEL_EXTERNAL_CAPITAL_TICK_MAX_SYMBOLS", 3, minimum=0)
    tick_min = _env_float("FUNNEL_EXTERNAL_CAPITAL_TICK_MIN_AMOUNT_YUAN", 1_000_000.0)
    try:
        from integrations.external_capital_context import build_external_capital_context

        requested = codes[:max_symbols]
        out = build_external_capital_context(
            requested,
            trade_date,
            include_tick=include_tick,
            tick_max_symbols=tick_max,
            tick_min_amount_yuan=tick_min,
        )
        _log(
            log_fn,
            f"外部资金佐证: requested={len(requested)}, features={len(out)}, tick={'on' if include_tick else 'off'}",
            logs_path,
        )
        return out
    except Exception as exc:
        _log(log_fn, f"外部资金佐证失败（已降级）: {exc}", logs_path)
        return {}


def load_dynamic_shadow_health_map(regime: str) -> dict[Any, dict[str, Any]]:
    if os.getenv("FUNNEL_DYNAMIC_SHADOW_PROMOTION", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {}
    try:
        from core.signal_feedback import normalize_signal_type
        from integrations.supabase_signal_feedback import load_signal_health_snapshot

        rows = load_signal_health_snapshot(market="cn")
    except Exception:
        return {}
    horizon = _env_int("FUNNEL_DYNAMIC_SHADOW_HORIZON", 5, minimum=1)
    regime_key = str(regime or "NEUTRAL").strip().upper()
    out: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if int(row.get("horizon_days") or 0) != horizon:
            continue
        signal = normalize_signal_type(row.get("signal_type"))
        row_regime = str(row.get("regime") or "ALL").strip().upper() or "ALL"
        if not signal or row_regime not in {regime_key, "ALL"}:
            continue
        out[(signal, row_regime)] = row
        if row_regime == regime_key or signal not in out:
            out[signal] = row
    return out


def build_signal_observation_rows(
    step2_details: dict, regime: str, ai_codes: list[str], *, trade_date: str
) -> list[dict]:
    from core.signal_feedback import build_signal_observations

    metrics, name_map, sector_map, stage_map, channel_map, close_map, springboard_map, footprint_map = (
        _observation_context(step2_details)
    )
    selected_for_ai = step2_details.get("selected_for_ai", []) or []
    return build_signal_observations(
        trade_date,
        _primary_observation_triggers(step2_details),
        regime=regime,
        selected_for_ai=selected_for_ai,
        ai_recommended=ai_codes,
        name_map=name_map,
        sector_map=sector_map,
        score_map=step2_details.get("priority_score_map", {}) or {},
        stage_map=stage_map,
        channel_map=channel_map,
        latest_close_map=close_map,
        source_map=_signal_observation_source_map(step2_details),
        springboard_map=springboard_map,
        footprint_map=footprint_map,
        source_context_map=step2_details.get("source_context_map") or {},
        candidate_metadata_map=_candidate_metadata_map(step2_details),
        entry_quality_map=build_entry_quality_map(step2_details),
        selection_mode=os.getenv("FUNNEL_AI_SELECTION_MODE", "quota"),
        selection_mode_map=_signal_observation_selection_mode_map(step2_details),
        policy_version=f"dynamic:{os.getenv('FUNNEL_DYNAMIC_POLICY', 'off')}",
        rank_map={str(code): idx + 1 for idx, code in enumerate(selected_for_ai)},
        health_context_map=step2_details.get("dynamic_shadow_health_map") or {},
        dynamic_promotion_map=_dynamic_promotion_map(step2_details),
    )


def build_shadow_observation_rows(step2_details: dict, regime: str, *, trade_date: str) -> list[dict]:
    from core.signal_feedback import build_signal_observations

    shadow_triggers, shadow_source_map, shadow_score_map = shadow_observation_inputs(step2_details)
    if not shadow_triggers:
        return []
    _, name_map, sector_map, stage_map, channel_map, close_map, _, footprint_map = _observation_context(step2_details)
    return build_signal_observations(
        trade_date,
        shadow_triggers,
        regime=regime,
        name_map=name_map,
        sector_map=sector_map,
        score_map=shadow_score_map,
        stage_map=stage_map,
        channel_map=channel_map,
        latest_close_map=close_map,
        source_map=shadow_source_map,
        footprint_map=footprint_map,
        source_context_map=step2_details.get("source_context_map") or {},
        entry_quality_map=build_entry_quality_map(step2_details),
        selection_mode="shadow",
        policy_version=f"dynamic:{os.getenv('FUNNEL_DYNAMIC_POLICY', 'off')}",
        health_context_map=step2_details.get("dynamic_shadow_health_map") or {},
        dynamic_promotion_map=_dynamic_promotion_map(step2_details),
    )


def build_external_seed_signal_rows(step2_details: dict, regime: str, *, trade_date: str) -> list[dict]:
    from core.signal_feedback import build_signal_observations

    metrics, name_map, sector_map, stage_map, channel_map, close_map, springboard_map, footprint_map = (
        _observation_context(step2_details)
    )
    selected = {str(code).strip() for code in step2_details.get("selected_for_ai", []) if str(code).strip()}
    triggers = {
        signal_type: [(code, score) for code, score in hits if str(code).strip() not in selected]
        for signal_type, hits in (metrics.get("external_seed_l4_triggers") or {}).items()
    }
    triggers = {signal_type: hits for signal_type, hits in triggers.items() if hits}
    if not triggers:
        return []
    source = f"external_seed:{metrics.get('external_seed_source') or 'external'}"
    source_map = {str(code): source for hits in triggers.values() for code, _score in hits}
    return build_signal_observations(
        trade_date,
        triggers,
        regime=regime,
        name_map=name_map,
        sector_map=sector_map,
        score_map=step2_details.get("priority_score_map", {}) or {},
        stage_map=stage_map,
        channel_map=channel_map,
        latest_close_map=close_map,
        source_map=source_map,
        springboard_map=springboard_map,
        footprint_map=footprint_map,
        source_context_map=step2_details.get("source_context_map") or {},
        entry_quality_map=build_entry_quality_map(step2_details),
        selection_mode="external_seed_shadow",
        policy_version=f"external_seed:{metrics.get('external_seed_source') or 'external'}",
        health_context_map=step2_details.get("dynamic_shadow_health_map") or {},
        dynamic_promotion_map=_dynamic_promotion_map(step2_details),
    )


def _dynamic_promotion_map(step2_details: dict) -> dict[str, dict[str, Any]]:
    return {
        _last_six_digits(row.get("code")): row
        for row in step2_details.get("dynamic_shadow_promoted", []) or []
        if _last_six_digits(row.get("code"))
    }


def persist_external_seed_observations(
    step2_details: dict,
    logs_path: str | None,
    *,
    dry_run: bool = False,
    log_fn: LogFn | None = None,
) -> None:
    rows = (step2_details.get("metrics", {}) or {}).get("external_seed_observation_rows") or []
    if not rows:
        return
    if dry_run:
        _log(log_fn, f"预演模式: 跳过外部观察入库 rows={len(rows)}", logs_path)
        return
    try:
        from integrations.supabase_external_seeds import upsert_external_seed_observations

        written = upsert_external_seed_observations(rows)
        _log(log_fn, f"外部观察入库: rows={len(rows)}, written={written}", logs_path)
    except Exception as exc:
        _log(log_fn, f"外部观察入库失败（已降级）: {exc}", logs_path)


def persist_signal_observations(
    step2_details: dict,
    benchmark_context: dict,
    ai_codes: list[str],
    logs_path: str | None,
    *,
    trade_date: str,
    dry_run: bool = False,
    log_fn: LogFn | None = None,
) -> bool:
    if not step2_details:
        return True
    if dry_run:
        _log(log_fn, "预演模式: 跳过信号观察样本入库", logs_path)
        return True
    try:
        from integrations.supabase_signal_feedback import upsert_signal_observations

        regime = str((benchmark_context or {}).get("regime") or "NEUTRAL")
        step2_details.setdefault("dynamic_shadow_health_map", load_dynamic_shadow_health_map(regime))
        if "source_context_map" not in step2_details:
            step2_details["source_context_map"] = build_external_capital_context_map(
                step2_details,
                ai_codes,
                logs_path,
                trade_date=trade_date,
                log_fn=log_fn,
            )
        rows = build_signal_observation_rows(step2_details, regime, ai_codes, trade_date=trade_date)
        rows.extend(build_shadow_observation_rows(step2_details, regime, trade_date=trade_date))
        rows.extend(build_external_seed_signal_rows(step2_details, regime, trade_date=trade_date))
        written = upsert_signal_observations(rows)
        _log(log_fn, f"信号观察样本入库: rows={len(rows)}, written={written}", logs_path)
        return True
    except Exception as exc:
        _log(log_fn, f"信号观察样本入库失败: {exc}", logs_path)
        return False


def empty_springboard_fields() -> dict:
    return {
        "springboard_a": False,
        "springboard_b": False,
        "springboard_c": False,
        "springboard_grade": "none",
        "springboard_met_count": 0,
        "springboard_support": None,
        "springboard_touch_count": 0,
        "springboard_evidence": {},
        "springboard_scored": False,
    }


def build_springboard_map(step2_details: dict) -> dict[str, dict]:
    from core.signal_confirmation import score_springboard_abc

    all_df_map = step2_details.get("all_df_map", {})
    triggers = _primary_observation_triggers(step2_details)
    out: dict[str, dict] = {}
    for sig_type, hits in triggers.items():
        for code, _score in hits:
            code_s = str(code).strip()
            sig_s = str(sig_type).strip().lower()
            if not code_s or not sig_s:
                continue
            key = f"{sig_s}:{code_s}"
            df = all_df_map.get(code_s)
            out[key] = (
                empty_springboard_fields()
                if df is None or df.empty
                else _springboard_fields(score_springboard_abc(df, sig_s))
            )
            out.setdefault(code_s, out[key])
    return out


def _external_capital_codes(step2_details: dict, ai_codes: list[str]) -> list[str]:
    ordered: list[str] = []
    for raw in list(step2_details.get("selected_for_ai", []) or []) + list(ai_codes or []):
        code = str(raw or "").strip()
        if code and code not in ordered:
            ordered.append(code)
    ranked: dict[str, float] = {}
    triggers = _primary_observation_triggers(step2_details) or step2_details.get("formal_triggers") or {}
    for hits in triggers.values():
        for raw_code, raw_score in hits or []:
            code = str(raw_code or "").strip()
            if code:
                ranked[code] = max(ranked.get(code, float("-inf")), _safe_float(raw_score))
    for code in sorted(ranked, key=lambda candidate: ranked[candidate], reverse=True):
        if code not in ordered:
            ordered.append(code)
    return ordered


def _observation_context(step2_details: dict) -> tuple[dict, dict, dict, dict, dict, dict, dict, dict]:
    metrics = step2_details.get("metrics", {}) or {}
    footprint_map = step2_details.get("footprint_map")
    if footprint_map is None:
        footprint_map = _build_footprint_map(step2_details)
        step2_details["footprint_map"] = footprint_map
    return (
        metrics,
        step2_details.get("name_map", {}) or {},
        step2_details.get("sector_map", {}) or {},
        metrics.get("accum_stage_map", {}) or {},
        metrics.get("layer2_channel_map", {}) or {},
        metrics.get("latest_close_map", {}) or {},
        step2_details.get("springboard_map") or build_springboard_map(step2_details),
        footprint_map,
    )


def _build_footprint_map(step2_details: dict) -> dict[str, dict]:
    from core.price_action_footprint import build_price_action_footprint_map

    metrics = step2_details.get("metrics", {}) or {}
    df_map = step2_details.get("all_df_map") or metrics.get("all_df_map") or {}
    return build_price_action_footprint_map(_merge_observation_trigger_maps(step2_details), df_map)


def _merge_observation_trigger_maps(step2_details: dict) -> dict[str, list[tuple[str, float]]]:
    metrics = step2_details.get("metrics", {}) or {}
    return merge_trigger_maps(
        step2_details.get("review_triggers") or step2_details.get("triggers") or {},
        _candidate_trigger_map(step2_details),
        metrics.get("external_seed_l4_triggers") or {},
    )


def _primary_observation_triggers(step2_details: dict) -> dict[str, list[tuple[str, float]]]:
    return merge_trigger_maps(
        step2_details.get("review_triggers") or step2_details.get("triggers") or {},
        _candidate_trigger_map(step2_details),
    )


def _candidate_trigger_map(step2_details: dict) -> dict[str, list[tuple[str, float]]]:
    return candidate_signal_triggers(step2_details.get("candidate_entries") or [])


def _candidate_metadata_map(step2_details: dict) -> dict[str, dict[str, Any]]:
    return build_candidate_signal_metadata_map(
        step2_details.get("candidate_entries") or [],
        step2_details.get("mainline_candidates") or [],
    )


def build_entry_quality_map(step2_details: dict) -> dict[str, dict[str, Any]]:
    existing = step2_details.get("entry_quality_map")
    if isinstance(existing, dict):
        return existing
    rows = _entry_quality_source_rows(step2_details)
    if not rows:
        step2_details["entry_quality_map"] = {}
        return {}
    out = _entry_quality_map_from_rows(rows)
    step2_details["entry_quality_map"] = out
    return out


def _entry_quality_source_rows(step2_details: dict) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("candidate_entries", "step3_symbols_info", "symbols_for_report"):
        rows.extend(row for row in step2_details.get(key) or [] if isinstance(row, dict))
    return rows


def _entry_quality_map_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from workflows.step3_entry_quality import annotate_entry_quality

    annotated = annotate_entry_quality(_pd_dataframe(rows)).to_dict("records")
    out: dict[str, dict[str, Any]] = {}
    for original, row in zip(rows, annotated, strict=False):
        code = _last_six_digits(original.get("code") or row.get("code"))
        payload = _entry_quality_payload(row if _has_entry_quality_inputs(original) else original)
        if code and payload and code not in out:
            out[code] = payload
    return out


def _pd_dataframe(rows: list[dict[str, Any]]):
    import pandas as pd

    return pd.DataFrame(rows)


def _has_entry_quality_inputs(row: dict[str, Any]) -> bool:
    return any(row.get(key) not in (None, "") for key in ("rs_10", "min_vol_ratio_5d", "bias_200", "avg_amount_20_yi"))


def _entry_quality_payload(row: dict[str, Any]) -> dict[str, Any]:
    score = row.get("entry_quality_score")
    grade = str(row.get("entry_quality_grade") or "").strip()
    tag = str(row.get("entry_quality_tag") or "").strip()
    risk_flags = row.get("entry_risk_flags")
    bucket = row.get("entry_priority_bucket")
    if score in (None, "") and not grade and not tag and risk_flags in (None, ""):
        return {}
    return {"score": score, "grade": grade, "tag": tag, "risk_flags": risk_flags, "priority_bucket": bucket}


def _signal_observation_source_map(step2_details: dict) -> dict[str, str]:
    metrics = step2_details.get("metrics", {}) or {}
    bypass_pool = {str(c).strip() for c in step2_details.get("l2_bypass_pool", []) if str(c).strip()}
    strategic_pool = {str(c).strip() for c in step2_details.get("strategic_l2_bypass_pool", []) if str(c).strip()}
    bypass_codes = {str(c).strip() for c in step2_details.get("l2_bypass_selected", []) if str(c).strip()}
    strategic_codes = {str(c).strip() for c in step2_details.get("strategic_l2_bypass_selected", []) if str(c).strip()}
    external_codes = {str(c).strip() for c in step2_details.get("external_seed_selected", []) if str(c).strip()}
    candidate_codes = {str(row.get("code", "")).strip() for row in step2_details.get("candidate_entries", []) or []}
    mainline_codes = {str(row.get("code", "")).strip() for row in step2_details.get("mainline_candidates", []) or []}
    dynamic_codes = {str(row.get("code", "")).strip() for row in step2_details.get("dynamic_shadow_promoted", []) or []}
    source_map = {code: "l2_bypass_shadow" for code in bypass_pool}
    source_map.update({code: "strategic_l2_bypass_shadow" for code in strategic_pool})
    source_map.update({code: "candidate_lane" for code in candidate_codes})
    source_map.update({code: "mainline" for code in mainline_codes})
    source_map.update({code: "l2_bypass" for code in bypass_codes})
    source_map.update({code: "strategic_l2_bypass" for code in strategic_codes})
    source_map.update({code: "dynamic_shadow_promotion" for code in dynamic_codes})
    source_map.update(
        {code: f"external_seed:{metrics.get('external_seed_source') or 'external'}" for code in external_codes}
    )
    return source_map


def _signal_observation_selection_mode_map(step2_details: dict) -> dict[str, str]:
    bypass_pool = {str(c).strip() for c in step2_details.get("l2_bypass_pool", []) if str(c).strip()}
    strategic_pool = {str(c).strip() for c in step2_details.get("strategic_l2_bypass_pool", []) if str(c).strip()}
    bypass_selected = {str(c).strip() for c in step2_details.get("l2_bypass_selected", []) if str(c).strip()}
    strategic_selected = {
        str(c).strip() for c in step2_details.get("strategic_l2_bypass_selected", []) if str(c).strip()
    }
    candidate_codes = {str(row.get("code", "")).strip() for row in step2_details.get("candidate_entries", []) or []}
    mainline_codes = {str(row.get("code", "")).strip() for row in step2_details.get("mainline_candidates", []) or []}
    dynamic_codes = {str(row.get("code", "")).strip() for row in step2_details.get("dynamic_shadow_promoted", []) or []}
    out = {code: "l2_bypass_shadow" for code in bypass_pool}
    out.update({code: "strategic_l2_bypass_shadow" for code in strategic_pool})
    out.update({code: "candidate_lane_shadow" for code in candidate_codes})
    out.update({code: "mainline_shadow" for code in mainline_codes})
    out.update({code: "l2_bypass" for code in bypass_selected})
    out.update({code: "strategic_l2_bypass" for code in strategic_selected})
    out.update({code: "dynamic_shadow_promotion" for code in dynamic_codes})
    return out


def _springboard_fields(result: dict) -> dict:
    return {
        "springboard_a": bool(result.get("a")),
        "springboard_b": bool(result.get("b")),
        "springboard_c": bool(result.get("c")),
        "springboard_grade": str(result.get("grade") or "none"),
        "springboard_met_count": int(result.get("met_count") or 0),
        "springboard_support": result.get("support"),
        "springboard_touch_count": int(result.get("touch_count") or 0),
        "springboard_evidence": result.get("evidence") or {},
        "springboard_scored": True,
    }


def _log(log_fn: LogFn | None, message: str, logs_path: str | None) -> None:
    if log_fn is not None:
        log_fn(message, logs_path)
