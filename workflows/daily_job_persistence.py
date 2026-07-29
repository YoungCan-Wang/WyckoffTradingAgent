"""Persistence helpers for daily job workflow outputs."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from core.candidate_metadata import build_candidate_metadata_map, code6
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
from utils.safe import safe_float as _safe_float
from workflows.step4_pipeline import TZ, is_confirmed_step4_candidate
from workflows.step4_text import clean_text as _clean_text

RECOMMENDATION_MAINLINE_STATUSES = TRADEABLE_MAINLINE_STATUSES
RECOMMENDATION_STRATEGIC_MIN_THEME_SCORE = 0.45
RECOMMENDATION_STRATEGIC_MIN_STOCK_SCORE = 0.55
STEP3_REPAIR_REVIEW_SPRINGBOARD_CAP = "STEP3_REPAIR_REVIEW_SPRINGBOARD_CAP"
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
    return _dedupe_tracking_symbols(rows)


def step3_review_symbols(
    symbols_info: list[dict],
    *,
    step2_details: dict | None = None,
    trade_mode: MarketTradeMode | None = None,
) -> list[dict]:
    mode = trade_mode or resolve_market_trade_mode(None)
    if step2_details is not None and "selected_for_ai" in step2_details and mode.mode != "repair_review":
        return _selected_funnel_review_symbols(symbols_info, step2_details)
    strict = [item for item in symbols_info if is_recommendation_review_candidate(item)]
    extras = repair_review_springboard_symbols(
        step2_details,
        mode,
        exclude_codes={code6(item.get("code")) for item in strict},
    )
    return strict + extras


def _selected_funnel_review_symbols(symbols_info: list[dict], step2_details: dict) -> list[dict]:
    """漏斗当日选中的候选，外加跨日确认候选。

    两者缺一不可：LLM 的起跳板硬门槛只放行 `跨日确认=confirmed`，而 `selected_for_ai`
    装的是当日新触发的信号——按状态机定义它们此刻只能是 pending，最快次日才可能转
    confirmed。只送 `selected_for_ai` 会让第三阵营在结构上恒为空（生产实测连续 10 个
    交易日零起跳板，期间确认候选最多的一天有 38 只，与送审名单零交集）。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    by_code = {code6(item.get("code")): item for item in symbols_info if isinstance(item, dict)}
    for code in [code6(code) for code in step2_details.get("selected_for_ai", []) or []]:
        item = by_code.get(code)
        if not code or item is None or code in seen:
            continue
        seen.add(code)
        rows.append({**item, "input_order": len(rows)})

    confirmed_cap = _env_cap(STEP3_CONFIRMED_REVIEW_CAP, 12)
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
    return rows


def repair_review_springboard_symbols(
    step2_details: dict | None,
    trade_mode: MarketTradeMode | None,
    *,
    exclude_codes: set[str] | None = None,
) -> list[dict]:
    if not _allow_repair_springboard_review(step2_details, trade_mode):
        return []
    mode = trade_mode or resolve_market_trade_mode(None)
    excluded = exclude_codes or set()
    rows = _dedupe_tracking_symbols(_springboard_tracking_symbols(step2_details or {}, mode))
    rows.sort(key=_tracking_rank, reverse=True)
    out: list[dict] = []
    for row in rows:
        code = code6(row.get("code"))
        if not code or code in excluded:
            continue
        out.append(_step3_repair_springboard_row(row))
        if len(out) >= _repair_springboard_review_cap():
            break
    return out


def _allow_repair_springboard_review(step2_details: dict | None, trade_mode: MarketTradeMode | None) -> bool:
    if not step2_details or _repair_springboard_review_cap() <= 0:
        return False
    mode = trade_mode or resolve_market_trade_mode(None)
    return mode.mode == "repair_review" and not mode.allow_recommendation_write


def _step3_repair_springboard_row(row: dict) -> dict:
    out = dict(row)
    confirmed = is_confirmed_step4_candidate(out)
    out["selection_source"] = "l4_springboard"
    out["source_type"] = "repair_springboard_review"
    out["signal_status"] = "confirmed" if confirmed else "pending"
    out["candidate_status"] = "修复复核候选"
    out["selection_is_fill"] = False
    out.setdefault("structure_reason", out.get("tag") or "修复期起跳板结构复核")
    if not confirmed:
        out.pop("confirm_reason", None)
        out.pop("confirm_date", None)
    return out


def _repair_springboard_review_cap() -> int:
    return _env_cap(STEP3_REPAIR_REVIEW_SPRINGBOARD_CAP, 3)


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
        if old is None or _tracking_rank(row) > _tracking_rank(old):
            best[code] = row
    return list(best.values())


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
        )
        log_fn(
            "推荐记录AI标记: "
            f"ok={ai_mark_ok}, date={recommend_trade_date_int}, ai_count={len(step3_springboard_codes)}",
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
