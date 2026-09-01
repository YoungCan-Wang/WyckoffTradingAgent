"""Persistence helpers for daily job workflow outputs."""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime
from typing import Any

from core.candidate_metadata import build_candidate_metadata_map, code6
from core.funnel_report import FunnelReportMaps, build_symbol_report_row
from core.mainline_engine import TRADEABLE_MAINLINE_STATUSES
from core.market_trade_mode import MarketTradeMode, resolve_market_trade_mode
from core.signal_feedback import signal_track
from integrations.recommendation_payload import (
    mark_ai_recommendations,
    prepare_recommendation_payload,
    upsert_recommendation_payload,
    write_recommendation_backup_artifact,
)
from integrations.supabase_market_signal import upsert_market_signal_daily
from utils.safe import has_value
from utils.safe import safe_float as _safe_float
from workflows.funnel_render_context import rebuild_report_maps
from workflows.step4_pipeline import TZ, is_confirmed_step4_candidate
from workflows.step4_text import clean_text as _clean_text

RECOMMENDATION_MAINLINE_STATUSES = TRADEABLE_MAINLINE_STATUSES
# priority_rank 只有排序上下文才有意义，补一个 0 会让 selection_rank 读到假名次。
_ENRICH_SKIP_KEYS = frozenset({"priority_rank"})
RECOMMENDATION_STRATEGIC_MIN_THEME_SCORE = 0.45
RECOMMENDATION_STRATEGIC_MIN_STOCK_SCORE = 0.55
STEP3_CONFIRMED_REVIEW_CAP = "STEP3_CONFIRMED_REVIEW_CAP"


def _env_cap(name: str, default: int) -> int:
    try:
        return max(int(float(os.getenv(name, str(default)))), 0)
    except (TypeError, ValueError):
        return default


def persist_benchmark_context(
    benchmark_context: dict,
    logs_path: str | None,
    *,
    dry_run: bool,
    trade_date: str,
    log_fn,
) -> bool:
    if not benchmark_context:
        return True
    if dry_run:
        log_fn("预演模式: 跳过市场信号写库(benchmark)", logs_path)
        return True
    payload = benchmark_context_payload(benchmark_context)
    ok = upsert_market_signal_daily(trade_date, payload)
    log_fn(
        f"市场信号写库(benchmark): ok={ok}, trade_date={trade_date}, regime={payload.get('benchmark_regime')}",
        logs_path,
    )
    return bool(ok)


def persist_recommendations(
    symbols_info: list[dict],
    logs_path: str | None,
    *,
    dry_run: bool,
    trade_date: str,
    log_fn,
    step2_details: dict | None = None,
    benchmark_context: dict | None = None,
    trade_mode: MarketTradeMode | None = None,
) -> tuple[int | None, list[dict], bool]:
    write_symbols = recommendation_write_symbols(
        symbols_info,
        step2_details=step2_details,
        benchmark_context=benchmark_context,
        trade_mode=trade_mode,
    )
    if dry_run:
        log_fn(
            f"预演模式: 跳过推荐记录入库 raw_count={len(symbols_info)}, write_count={len(write_symbols)}",
            logs_path,
        )
        return None, [], True
    try:
        recommend_date = int(trade_date.replace("-", ""))
        if not write_symbols:
            log_fn(f"推荐记录入库: raw_count={len(symbols_info)}, write_count=0（跨日确认候选为空，跳过）", logs_path)
            return recommend_date, [], True
        payload = prepare_recommendation_payload(recommend_date, write_symbols)
        write_recommendation_backup(recommend_date, payload, logs_path, ai_codes=None, log_fn=log_fn)
        rec_ok = upsert_recommendation_payload(payload)
        log_fn(
            "推荐记录入库: "
            f"ok={rec_ok}, raw_count={len(symbols_info)}, write_count={len(write_symbols)}, "
            f"payload_count={len(payload)}, date={recommend_date}",
            logs_path,
        )
        return recommend_date, payload, bool(rec_ok)
    except Exception as e:
        log_fn(f"推荐记录入库失败: {e}", logs_path)
        return None, [], False


def recommendation_write_symbols(
    symbols_info: list[dict],
    *,
    step2_details: dict | None = None,
    benchmark_context: dict | None = None,
    trade_mode: MarketTradeMode | None = None,
) -> list[dict]:
    mode = trade_mode or resolve_market_trade_mode((benchmark_context or {}).get("regime"))
    rows = [_tracking_symbol(item, mode) for item in symbols_info if is_recommendation_tracking_candidate(item)]
    if step2_details:
        rows.extend(_springboard_tracking_symbols(step2_details, mode))
        _enrich_tracking_rows(rows, step2_details)
    return _dedupe_tracking_symbols(rows)


def _enrich_tracking_rows(rows: list[dict], step2_details: dict) -> None:
    """补齐绕过 build_symbol_report_row 的写入行的报告字段族。

    起跳板行(`_springboard_tracking_row`)与跨日确认行(`_confirmed_symbol_info`)各自
    从零拼 dict,行业轮动/主题/资金迁移/离场信号字段全部缺失,归因表里这些列长期
    100% NULL。这里用同一套映射复原并只填缺失键,已有值(含 mainline 行)不动。
    """
    metrics = step2_details.get("metrics") or {}
    if not metrics:
        return
    maps = rebuild_report_maps(
        metrics,
        name_map=step2_details.get("name_map") or {},
        sector_map=step2_details.get("sector_map") or {},
        triggers=step2_details.get("review_triggers") or step2_details.get("triggers") or {},
    )
    priority_scores = step2_details.get("priority_score_map") or {}
    stage_map = metrics.get("accum_stage_map") or {}
    for row in rows:
        _seed_signal_family(row)
        _fill_report_fields(row, maps, priority_scores, stage_map)


def _seed_signal_family(row: dict) -> None:
    """跨日确认行只带单数 signal_type,归因列读的是 signal_types/primary_signal。"""
    signal = _clean_text(row.get("signal_type")).lower()
    if not signal:
        return
    if not row.get("signal_types"):
        row["signal_types"] = [signal]
    if not _clean_text(row.get("primary_signal")):
        row["primary_signal"] = signal


def _fill_report_fields(
    row: dict,
    maps: FunnelReportMaps,
    priority_scores: dict[str, Any],
    stage_map: dict[str, str],
) -> None:
    code = code6(row.get("code"))
    score = _safe_float(row.get("score"))
    reference = build_symbol_report_row(
        code,
        rank=int(_safe_float(row.get("priority_rank"), 0.0) or 0),
        tag=str(row.get("tag") or ""),
        track=str(row.get("track") or ""),
        stage=str(row.get("stage") or stage_map.get(code, "") or ""),
        score=score,
        priority_score=_safe_float(priority_scores.get(code), score),
        selection_source=str(row.get("selection_source") or ""),
        selection_is_fill=bool(row.get("selection_is_fill")),
        market_regime=str(row.get("market_regime") or ""),
        maps=maps,
    )
    for key, value in reference.items():
        if key in _ENRICH_SKIP_KEYS:
            continue
        if not has_value(row.get(key)) and has_value(value):
            row[key] = value


def step3_review_symbols(
    symbols_info: list[dict],
    *,
    step2_details: dict | None = None,
    trade_mode: MarketTradeMode | None = None,
) -> list[dict]:
    if step2_details is not None and "selected_for_ai" in step2_details:
        return _selected_funnel_review_symbols(symbols_info, step2_details)
    return [item for item in symbols_info if is_recommendation_review_candidate(item)]


def _selected_funnel_review_symbols(symbols_info: list[dict], step2_details: dict) -> list[dict]:
    """Reserve Step3 seats for validated and dynamic-shadow candidates."""
    rows: list[dict] = []
    seen: set[str] = set()
    by_code = {code6(item.get("code")): item for item in symbols_info if isinstance(item, dict)}
    by_code.update(_dynamic_shadow_review_symbols(step2_details, by_code))
    confirmed_cap = _env_cap(STEP3_CONFIRMED_REVIEW_CAP, 3)
    added = 0
    for item in symbols_info:
        if added >= confirmed_cap:
            break
        code = code6(item.get("code")) if isinstance(item, dict) else ""
        if not code or code in seen or not is_confirmed_step4_candidate(item):
            continue
        seen.add(code)
        rows.append({**item, "input_order": len(rows)})
        added += 1
    dynamic_cap = _env_cap("FUNNEL_DYNAMIC_SHADOW_PROMOTION_CAP", 1)
    dynamic_added = 0
    selected_codes = {code6(code) for code in step2_details.get("selected_for_ai", []) or []}
    for promotion in step2_details.get("dynamic_shadow_promoted", []) or []:
        if dynamic_added >= dynamic_cap:
            break
        code = code6(promotion.get("code"))
        item = by_code.get(code)
        if not code or item is None or code in seen or code in selected_codes:
            continue
        seen.add(code)
        rows.append(
            {
                **item,
                "selection_source": "dynamic_shadow_promotion",
                "dynamic_shadow_score": promotion.get("dynamic_score"),
                "dynamic_shadow_promotion": promotion.get("promotion"),
                "input_order": len(rows),
            }
        )
        dynamic_added += 1
    for code in [code6(code) for code in step2_details.get("selected_for_ai", []) or []]:
        item = by_code.get(code)
        if not code or item is None or code in seen:
            continue
        seen.add(code)
        rows.append({**item, "selection_source": "step2_selected_for_ai", "input_order": len(rows)})
    return rows


def _dynamic_shadow_review_symbols(step2_details: dict, existing: dict[str, dict]) -> dict[str, dict]:
    names = step2_details.get("name_map") or {}
    sectors = step2_details.get("sector_map") or {}
    rows: dict[str, dict] = {}
    for promotion in step2_details.get("dynamic_shadow_promoted", []) or []:
        code = code6(promotion.get("code"))
        if not code:
            continue
        signal = str(promotion.get("signal_type") or "").strip().lower()
        track = signal_track(signal)
        score = _safe_float(promotion.get("dynamic_score"))
        base = existing.get(code, {})
        rows[code] = {
            **base,
            "code": code,
            "name": base.get("name") or names.get(code, code),
            "tag": base.get("tag") or f"动态影子晋级 | {signal.upper()} | 影子分 {score:.1f}",
            "track": base.get("track") or track,
            "stage": base.get("stage") or "",
            "score": score,
            "priority_score": score,
            "selection_source": "dynamic_shadow_promotion",
            "selection_is_fill": False,
            "signal_type": signal,
            "industry": base.get("industry") or sectors.get(code, ""),
            "dynamic_shadow_score": score,
            "dynamic_shadow_promotion": promotion.get("promotion") or {},
        }
    return rows


def is_recommendation_tracking_candidate(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return is_confirmed_step4_candidate(item)


def is_recommendation_review_candidate(item: dict) -> bool:
    if not is_recommendation_tracking_candidate(item):
        return False
    return _is_mainline_recommendation(item) or _is_strategic_theme_recommendation(item)


def _is_mainline_recommendation(item: dict) -> bool:
    lane = _clean_text(item.get("candidate_lane") or item.get("signal_key") or item.get("entry_type"))
    status = _clean_text(item.get("candidate_status") or item.get("status") or item.get("recommend_reason"))
    return lane == "mainline" and status in RECOMMENDATION_MAINLINE_STATUSES


def _is_strategic_theme_recommendation(item: dict) -> bool:
    state = _clean_text(item.get("strategic_theme_state")).lower()
    if state == "decay":
        return False
    return (
        bool(_clean_text(item.get("strategic_theme")))
        and _safe_float(item.get("strategic_theme_score")) >= RECOMMENDATION_STRATEGIC_MIN_THEME_SCORE
        and _safe_float(item.get("strategic_stock_score")) >= RECOMMENDATION_STRATEGIC_MIN_STOCK_SCORE
    )


def _tracking_symbol(item: dict, trade_mode: MarketTradeMode) -> dict:
    row = dict(item)
    row["market_regime"] = str(row.get("market_regime") or trade_mode.regime)
    row["candidate_status"] = _tracking_status(row, trade_mode)
    row["selection_source"] = _tracking_source(row, trade_mode)
    if not _clean_text(row.get("tag")):
        row["tag"] = row["candidate_status"]
    return row


def _springboard_tracking_symbols(step2_details: dict, trade_mode: MarketTradeMode) -> list[dict]:
    triggers = (
        step2_details.get("formal_triggers")
        or step2_details.get("review_triggers")
        or step2_details.get("triggers")
        or {}
    )
    springboard_map = step2_details.get("springboard_map") or {}
    metadata_map = _candidate_metadata(step2_details)
    rows: list[dict] = []
    for signal_type, hits in triggers.items():
        for raw_code, raw_score in hits or []:
            code = code6(raw_code)
            if not code:
                continue
            springboard = springboard_map.get(f"{str(signal_type).lower()}:{code}") or springboard_map.get(code) or {}
            if int(springboard.get("springboard_met_count") or 0) < 2:
                continue
            rows.append(
                _springboard_tracking_row(
                    code,
                    str(signal_type),
                    raw_score,
                    step2_details,
                    springboard,
                    metadata_map.get(code, {}),
                    trade_mode,
                )
            )
    return rows


def _springboard_tracking_row(
    code: str,
    signal_type: str,
    score: object,
    step2_details: dict,
    springboard: dict,
    metadata: dict,
    trade_mode: MarketTradeMode,
) -> dict:
    metrics = step2_details.get("metrics", {}) or {}
    grade = str(springboard.get("springboard_grade") or springboard.get("springboard_combo") or "").strip()
    status = _tracking_status({"candidate_status": metadata.get("candidate_status")}, trade_mode, springboard=True)
    return {
        "code": code,
        "name": (step2_details.get("name_map") or {}).get(code, code),
        "tag": f"{signal_type.upper()}起跳板结构({grade or '2/3+'})",
        "track": signal_track(signal_type),
        "initial_price": (metrics.get("latest_close_map") or {}).get(code, 0.0),
        "score": _safe_float(score),
        "priority_score": _safe_float((step2_details.get("priority_score_map") or {}).get(code), _safe_float(score)),
        "primary_signal": signal_type,
        "signal_types": [signal_type],
        "market_regime": trade_mode.regime,
        **metadata,
        "selection_source": _tracking_source({"selection_source": "l4_springboard"}, trade_mode),
        "candidate_status": status,
        "stage": (metrics.get("accum_stage_map") or {}).get(code, ""),
        "industry": (step2_details.get("sector_map") or {}).get(code, ""),
        **springboard,
    }


def _candidate_metadata(step2_details: dict) -> dict[str, dict[str, Any]]:
    return build_candidate_metadata_map(
        step2_details.get("candidate_entries", []) or [],
        step2_details.get("mainline_candidates", []) or [],
    )


def _tracking_status(row: dict, trade_mode: MarketTradeMode, *, springboard: bool = False) -> str:
    existing = _clean_text(row.get("candidate_status"))
    if not trade_mode.allow_recommendation_write:
        # allow_ai_review=False (RISK_OFF/CRASH/BLACK_SWAN) 与 repair_review
        # (BEAR_REBOUND/PANIC_REPAIR) 拦截强度不同，标签需区分，避免复盘/信号
        # 反馈闭环把"完全禁新仓的影子观察"和"允许复核但禁写正式推荐"混为一谈。
        return "禁新仓-影子观察" if not trade_mode.allow_ai_review else "市场拦截观察"
    if existing:
        return existing
    return "跨日确认观察" if springboard else "AI复核候选"


def _tracking_source(row: dict, trade_mode: MarketTradeMode) -> str:
    base = _clean_text(row.get("selection_source")) or _clean_text(row.get("source_type")) or "funnel"
    return f"{base}:market_blocked" if not trade_mode.allow_recommendation_write else base


def _dedupe_tracking_symbols(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        code = code6(row.get("code"))
        if not code:
            continue
        row["code"] = code
        old = best.get(code)
        if old is None:
            best[code] = row
            continue
        preferred, other = (row, old) if _tracking_rank(row) > _tracking_rank(old) else (old, row)
        best[code] = _merge_tracking_signals(preferred, other)
    return list(best.values())


def _merge_tracking_signals(preferred: dict, other: dict) -> dict:
    merged = dict(preferred)
    signals: list[str] = []
    for row in (preferred, other):
        values = row.get("signal_types") or [row.get("primary_signal")]
        for value in values:
            signal = _clean_text(value).lower()
            if signal and signal not in signals:
                signals.append(signal)
    merged["signal_types"] = signals
    if len(signals) > 1 and _clean_text(merged.get("selection_source")).startswith("l4_springboard"):
        prefix = "双形态共振" if len(signals) == 2 else "多形态共振"
        merged["tag"] = f"{prefix}({'+'.join(signal.upper() for signal in signals)})"
    return merged


def _tracking_rank(row: dict) -> tuple[int, float]:
    met = int(row.get("springboard_met_count") or 0)
    score = _safe_float(row.get("priority_score") if row.get("priority_score") not in (None, "") else row.get("score"))
    source = _clean_text(row.get("selection_source"))
    selected_bonus = 0 if source.startswith("l4_springboard") else 10
    return selected_bonus + met, score


def write_recommendation_backup(
    recommend_trade_date_int: int,
    payload: list[dict],
    logs_path: str | None,
    *,
    ai_codes: list[str] | None,
    log_fn,
) -> None:
    output_dir = os.getenv("DAILY_JOB_ARTIFACTS_DIR", "").strip()
    if not output_dir or not payload:
        return
    try:
        paths = write_recommendation_backup_artifact(
            recommend_trade_date_int,
            payload,
            output_dir,
            ai_codes=ai_codes,
        )
        if paths:
            log_fn(f"推荐记录备份 artifact: {', '.join(paths)}", logs_path)
    except Exception as e:
        log_fn(f"推荐记录备份 artifact 失败: {e}", logs_path)


def mark_step3_recommendations(
    recommend_trade_date_int: int | None,
    step3_springboard_codes: list[str],
    step3_springboard_updates: dict[str, dict] | None,
    logs_path: str | None,
    *,
    dry_run: bool,
    log_fn,
    step3_verdicts: dict[str, str] | None = None,
) -> None:
    if dry_run:
        log_fn("预演模式: 跳过推荐记录AI标记", logs_path)
        return
    if recommend_trade_date_int is None:
        return
    try:
        ai_mark_ok = mark_ai_recommendations(
            recommend_date=recommend_trade_date_int,
            ai_codes=step3_springboard_codes,
            springboard_updates=step3_springboard_updates,
            step3_verdicts=step3_verdicts,
        )
        verdict_counts = Counter((step3_verdicts or {}).values())
        log_fn(
            "推荐记录AI标记: "
            f"ok={ai_mark_ok}, date={recommend_trade_date_int}, ai_count={len(step3_springboard_codes)}, "
            f"verdicts={dict(verdict_counts) or '无'}",
            logs_path,
        )
    except Exception as e:
        log_fn(f"推荐记录AI标记失败: {e}", logs_path)


def persist_theme_radar(step2_details: dict, logs_path: str | None, *, dry_run: bool, log_fn) -> None:
    metrics = (step2_details or {}).get("metrics", {}) or {}
    snapshot = metrics.get("theme_radar_current") or metrics.get("theme_radar") or {}
    if dry_run or not snapshot:
        return
    try:
        from integrations.theme_radar_storage import persist_theme_radar_snapshot

        result = persist_theme_radar_snapshot(snapshot, local_fallback=False)
        log_fn(
            f"主题雷达写库: supabase={result.get('supabase', 0)}, sqlite={result.get('sqlite', 0)}",
            logs_path,
        )
    except Exception as exc:
        log_fn(f"主题雷达写库失败: {exc}", logs_path)


def benchmark_context_payload(benchmark_context: dict) -> dict:
    breadth = dict(benchmark_context.get("breadth") or {})
    return {
        "benchmark_regime": str(benchmark_context.get("regime", "") or "").strip().upper() or None,
        "main_index_code": str(benchmark_context.get("main_code", "000001") or "000001").strip(),
        "main_index_close": benchmark_context.get("close"),
        "main_index_ma50": benchmark_context.get("ma50"),
        "main_index_ma200": benchmark_context.get("ma200"),
        "main_index_recent3_cum_pct": benchmark_context.get("recent3_cum_pct"),
        "main_index_today_pct": benchmark_context.get("main_today_pct"),
        "smallcap_index_code": str(benchmark_context.get("smallcap_code", "") or "").strip() or None,
        "smallcap_close": benchmark_context.get("smallcap_close"),
        "smallcap_recent3_cum_pct": benchmark_context.get("smallcap_recent3_cum_pct"),
        "source_jobs": {
            "daily_job": {
                "updated_at": datetime.now(TZ).isoformat(),
                "writer": "step2_benchmark_context",
                "breadth": breadth,
            }
        },
    }
