"""Step2 funnel execution and persistence for the daily job."""

from __future__ import annotations

from datetime import datetime

import workflows.daily_job_persistence as daily_persistence
import workflows.daily_signal_observations as signal_observations
from core.market_trade_mode import MarketTradeMode
from workflows.daily_job_common import Step2StageResult, log_line
from workflows.daily_job_runtime import DailyJobConfig
from workflows.step4_order_config import resolve_live_market_trade_mode
from workflows.step4_pipeline import TZ, latest_trade_date_str


def run_step2_block(
    run_step2, cfg: DailyJobConfig, summary: list[dict]
) -> tuple[Step2StageResult, bool, int | None, list[dict]]:
    step2 = run_step2_stage(run_step2, cfg.webhook, cfg.preview_only, cfg.logs_path)
    summary.append(step2.summary_item)
    has_blocking_failure = step2.blocking_failure
    recommend_date, recommendation_payload, persistence_ok = persist_step2_outputs(step2, cfg)
    has_blocking_failure = has_blocking_failure or not persistence_ok
    return step2, has_blocking_failure, recommend_date, recommendation_payload


def run_step2_stage(run_step2, webhook: str, preview_only: bool, logs_path: str | None) -> Step2StageResult:
    t0 = datetime.now(TZ)
    step2_ok = False
    step2_err = None
    symbols_info: list[dict] = []
    benchmark_context: dict = {}
    step2_details: dict = {}
    try:
        step2_ok, symbols_info, benchmark_context, step2_details = _run_step2_with_etf_metrics(
            run_step2, webhook, preview_only
        )
        step2_err = None if step2_ok else "飞书发送失败"
    except Exception as e:
        step2_err = str(e)
    elapsed = (datetime.now(TZ) - t0).total_seconds()
    summary_item = {
        "step": "Wyckoff Funnel",
        "ok": step2_ok and step2_err is None,
        "err": step2_err,
        "elapsed_s": round(elapsed, 1),
        "output": f"{len(symbols_info)} symbols",
    }
    log_line(
        f"Step2 Wyckoff Funnel: ok={step2_ok}, symbols={len(symbols_info)}, elapsed={elapsed:.1f}s, err={step2_err}",
        logs_path,
    )
    return Step2StageResult(
        ok=step2_ok,
        symbols_info=symbols_info,
        benchmark_context=benchmark_context,
        details=step2_details,
        summary_item=summary_item,
        blocking_failure=bool(step2_err),
    )


def persist_step2_outputs(step2: Step2StageResult, cfg: DailyJobConfig) -> tuple[int | None, list[dict], bool]:
    trade_mode = resolve_live_market_trade_mode((step2.benchmark_context or {}).get("regime"))
    persistence_ok = True
    if not step2.blocking_failure and step2.benchmark_context:
        persistence_ok = daily_persistence.persist_benchmark_context(
            step2.benchmark_context,
            cfg.logs_path,
            dry_run=cfg.preview_only,
            trade_date=latest_trade_date_str(),
            log_fn=log_line,
        )
    if step2.ok and step2.details:
        persist_step2_observations(step2, cfg)
        run_signal_confirmation(
            step2.symbols_info, step2.details, step2.benchmark_context, cfg.logs_path, dry_run=cfg.preview_only
        )
        _prepare_step3_review_input(step2, trade_mode, cfg)
    if step2.ok and (step2.symbols_info or step2.details):
        recommend_date, payload, recommendation_ok = daily_persistence.persist_recommendations(
            step2.symbols_info,
            cfg.logs_path,
            dry_run=cfg.preview_only,
            trade_date=latest_trade_date_str(),
            log_fn=log_line,
            step2_details=step2.details,
            benchmark_context=step2.benchmark_context,
            trade_mode=trade_mode,
        )
        return recommend_date, payload, persistence_ok and recommendation_ok
    return None, [], persistence_ok


def _prepare_step3_review_input(step2: Step2StageResult, trade_mode: MarketTradeMode, cfg: DailyJobConfig) -> None:
    scored = run_springboard_scoring(step2.symbols_info, step2.details)
    log_line(f"Step2.7 起跳板评分: scored={scored}/{len(step2.symbols_info)}", cfg.logs_path)
    step2.details["trade_mode"] = {
        "regime": trade_mode.regime,
        "mode": trade_mode.mode,
        "label": trade_mode.label,
        "action": trade_mode.action,
        "reason": trade_mode.reason,
    }
    promoted = _prepare_dynamic_shadow_review(step2, cfg)
    step2.details["step3_symbols_info"] = (
        daily_persistence.step3_review_symbols(
            step2.symbols_info,
            step2_details=step2.details,
            trade_mode=trade_mode,
        )
        if trade_mode.allow_ai_review
        else []
    )
    log_line(
        "Step2.8 AI研报输入收口: "
        f"raw={len(step2.symbols_info)}, "
        f"funnel_selected={len(step2.details.get('selected_for_ai', []) or [])}, "
        f"signal_confirmed={len(step2.details.get('signal_confirmed_selected', []) or [])}, "
        f"dynamic_shadow_promoted={len(promoted)}, "
        f"step3_input={len(step2.details['step3_symbols_info'])}, "
        f"trade_mode={trade_mode.mode}",
        cfg.logs_path,
    )
    if not trade_mode.allow_recommendation_write:
        log_line(f"Step2.8 市场闸门: {trade_mode.action}，推荐表仅写观察候选", cfg.logs_path)


def _prepare_dynamic_shadow_review(step2: Step2StageResult, cfg: DailyJobConfig) -> list[dict]:
    try:
        from workflows.dynamic_shadow_promotion import prepare_dynamic_shadow_promotions

        promoted = prepare_dynamic_shadow_promotions(
            step2.details,
            str((step2.benchmark_context or {}).get("regime") or "NEUTRAL"),
            trade_date=latest_trade_date_str(),
            logs_path=cfg.logs_path,
            log_fn=log_line,
        )
        log_line(f"Step2.75 动态影子晋级: eligible={len(promoted)}（仅增加 Step3 复核资格）", cfg.logs_path)
        return promoted
    except Exception as exc:
        log_line(f"Step2.75 动态影子晋级失败（已降级）: {exc}", cfg.logs_path)
        return []


def persist_step2_observations(step2: Step2StageResult, cfg: DailyJobConfig) -> None:
    daily_persistence.persist_theme_radar(
        step2.details,
        cfg.logs_path,
        dry_run=cfg.preview_only,
        log_fn=log_line,
    )
    signal_observations.persist_external_seed_observations(
        step2.details,
        cfg.logs_path,
        dry_run=cfg.preview_only,
        log_fn=log_line,
    )


def run_signal_confirmation(
    symbols_info: list[dict],
    step2_details: dict,
    benchmark_context: dict | None,
    logs_path: str | None,
    *,
    dry_run: bool = False,
) -> list[dict]:
    confirmed_extra: list[dict] = []
    try:
        from workflows.step2_signal_confirmation import run_step2_5

        triggers_raw = step2_details.get("triggers", {})
        all_df_map = step2_details.get("all_df_map", {})
        candidate_entries = _selected_candidate_entries(step2_details)
        if (triggers_raw or candidate_entries) and all_df_map:
            confirmed_extra = run_step2_5(
                signal_date=latest_trade_date_str(),
                triggers=triggers_raw,
                df_map=all_df_map,
                regime=(benchmark_context.get("regime") or "NEUTRAL").strip().upper()
                if benchmark_context
                else "NEUTRAL",
                name_map=step2_details.get("name_map", {}),
                sector_map=step2_details.get("sector_map", {}),
                candidate_entries=candidate_entries,
                mainline_candidates=step2_details.get("mainline_candidates", []) or [],
                dry_run=dry_run,
            )
            merge_confirmed_signals(symbols_info, step2_details, confirmed_extra)
            suffix = "（preview dry-run，不写库）" if dry_run else ""
            log_line(f"Step2.5 信号确认{suffix}: confirmed={len(confirmed_extra)}", logs_path)
    except Exception as e:
        log_line(f"Step2.5 信号确认失败（已降级）: {e}", logs_path)
    return confirmed_extra


def _selected_candidate_entries(step2_details: dict) -> list[dict]:
    selected = {str(code).strip() for code in step2_details.get("selected_for_ai", []) or [] if str(code).strip()}
    if not selected:
        return []
    return [
        item
        for item in step2_details.get("candidate_entries", []) or []
        if str(item.get("code", "")).strip() in selected
    ]


def merge_confirmed_signals(symbols_info: list[dict], step2_details: dict, confirmed_extra: list[dict]) -> None:
    existing_by_code = {str(item.get("code", "")).strip(): item for item in symbols_info if isinstance(item, dict)}
    for confirmed in confirmed_extra:
        code = str(confirmed.get("code", "")).strip()
        if not code:
            continue
        existing = existing_by_code.get(code)
        if existing is None:
            symbols_info.append(confirmed)
            existing_by_code[code] = confirmed
            continue
        for key, value in confirmed.items():
            if value in (None, ""):
                continue
            if key == "selection_source" and str(existing.get("selection_source", "")).strip():
                continue
            existing[key] = value
    step2_details["signal_confirmed_selected"] = confirmed_extra


def run_springboard_scoring(symbols_info: list[dict], step2_details: dict) -> int:
    springboard_map = signal_observations.build_springboard_map(step2_details)
    step2_details["springboard_map"] = springboard_map
    scored = 0
    for item in symbols_info:
        code = str(item.get("code", "")).strip()
        fields = springboard_map.get(code) or signal_observations.empty_springboard_fields()
        item.update(fields)
        if fields.get("springboard_scored"):
            scored += 1
    return scored


def _run_step2_with_etf_metrics(run_step2, webhook: str, preview_only: bool):
    result = run_step2("" if preview_only else webhook, notify=not preview_only, return_details=True)
    step2_ok, symbols_info, benchmark_context, step2_details = result
    if benchmark_context and step2_details:
        metrics = step2_details.get("metrics", {}) or {}
        benchmark_context["etf_enhancement"] = metrics.get("etf_enhancement", {}) or {}
        benchmark_context["etf_candidates"] = metrics.get("etf_candidates", []) or []
    return step2_ok, symbols_info, benchmark_context, step2_details
