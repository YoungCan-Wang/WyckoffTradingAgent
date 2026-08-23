"""
批量 AI 研报（Step3）
拉取选中股票的 OHLCV → 特征工程 → AI 三阵营分析 → 飞书/企微/钉钉推送
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from core.prompts import WYCKOFF_FUNNEL_SYSTEM_PROMPT
from workflows.step3_candidates import build_step3_candidate_bundle, load_step3_market_context
from workflows.step3_inputs import (
    build_step3_benchmark_lines,
    build_step3_track_inputs,
    build_step3_track_requests,
)
from workflows.step3_llm import call_step3_track_reports, route_label
from workflows.step3_models import (
    Step3CandidateBundle,
    Step3LlmResult,
    Step3RagResult,
    Step3RunOptions,
    Step3SelectionState,
    Step3TrackPlan,
)
from workflows.step3_rag import apply_step3_rag_veto
from workflows.step3_reporting import maybe_return_step3_preview, send_empty_step3_report, send_step3_final_report
from workflows.step3_runtime_config import Step3RuntimeConfig, step3_runtime_config_from_env
from workflows.step3_selection import select_step3_candidates


def _normalize_step3_items(symbols_info: list[dict] | list[str]) -> list[dict]:
    items: list[dict] = []
    for item in symbols_info:
        if isinstance(item, str):
            items.append({"code": item, "name": item, "tag": ""})
        else:
            items.append(item)
    return items


def _runtime_config_for_items(runtime_config: Step3RuntimeConfig, items: list[dict]) -> Step3RuntimeConfig:
    if any(str(item.get("selection_source") or "").strip() == "explicit_report_input" for item in items):
        return replace(runtime_config, respect_upstream_priority=True)
    return runtime_config


def _prepare_step3_selection(
    items: list[dict],
    benchmark_context: dict | None,
    runtime_config: Step3RuntimeConfig,
) -> Step3SelectionState:
    market_context = load_step3_market_context(items, benchmark_context, runtime_config)
    candidate_bundle = build_step3_candidate_bundle(items, market_context, runtime_config)
    candidates_df = candidate_bundle.candidates_df
    if candidates_df.empty:
        empty_rag = Step3RagResult(selected_df=candidates_df, preview="", veto_lines=[])
        return Step3SelectionState(market_context, candidate_bundle, candidates_df, empty_rag)
    selected_df = select_step3_candidates(candidates_df, market_context.regime, runtime_config)
    rag_result = apply_step3_rag_veto(selected_df, runtime_config)
    return Step3SelectionState(market_context, candidate_bundle, rag_result.selected_df, rag_result)


def _empty_step3_candidate_result(candidate_bundle: Step3CandidateBundle) -> tuple[bool, str, str] | None:
    if not candidate_bundle.candidates_df.empty:
        return None
    if candidate_bundle.failed:
        detail = ", ".join(f"{s}({e})" for s, e in candidate_bundle.failed)
        print(f"[step3] OHLCV 全部拉取失败: {detail}")
        return (False, "data_all_failed", "")
    return (True, "no_data_but_no_error", "")


def _build_step3_track_plan(
    items: list[dict],
    selection: Step3SelectionState,
    options: Step3RunOptions,
) -> Step3TrackPlan | None:
    market_context = selection.market_context
    bundle = selection.candidate_bundle
    selected_df = selection.selected_df
    track_inputs = build_step3_track_inputs(selected_df, bundle.code_to_df, items, market_context.financial_map)
    benchmark_lines = build_step3_benchmark_lines(
        market_context.benchmark_context,
        market_context.sector_rotation_ctx,
    )
    active_tracks = [track for track in ["Trend", "Accum"] if track_inputs.payloads_by_track.get(track)]
    if not active_tracks:
        detail = ", ".join(f"{s}({e})" for s, e in bundle.failed) if bundle.failed else "无可用 payload"
        print(f"[step3] 候选存在，但未能生成可用模型输入: {detail}")
        return None
    track_requests = build_step3_track_requests(
        active_tracks=active_tracks,
        track_inputs=track_inputs,
        candidates_df=bundle.candidates_df,
        benchmark_lines=benchmark_lines,
        benchmark_context=market_context.benchmark_context,
        compressed=options.runtime_config.enable_compression,
        model_label=route_label(options.provider, options.model),
        system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
    )
    return Step3TrackPlan(track_inputs, track_requests, active_tracks)


def _run_step3_selection(
    *,
    options: Step3RunOptions,
    items: list[dict],
    selection: Step3SelectionState,
    report_progress,
) -> tuple[bool, str, str]:
    market_context = selection.market_context
    bundle = selection.candidate_bundle
    selected_df = selection.selected_df
    rag_result = selection.rag_result
    selected_codes = [str(x) for x in selected_df["code"].tolist()]
    if not selected_codes:
        return send_empty_step3_report(
            options=options,
            items=items,
            benchmark_context=market_context.benchmark_context,
            selected_df=selected_df,
            rag_veto_preview=rag_result.preview,
            rag_veto_lines=rag_result.veto_lines,
        )
    track_plan = _build_step3_track_plan(items, selection, options)
    if not track_plan:
        return (False, "payload_build_failed", "")
    preview_result = maybe_return_step3_preview(options, track_plan.track_requests, WYCKOFF_FUNNEL_SYSTEM_PROMPT)
    if preview_result:
        return preview_result
    llm_result = _step3_llm_or_cache(
        options=options,
        selected_df=selected_df,
        rag_lines=rag_result.veto_lines,
        market_context=market_context,
        track_plan=track_plan,
        report_progress=report_progress,
    )
    if not llm_result.ok:
        return (False, llm_result.status, "")
    return send_step3_final_report(
        options=options,
        active_tracks=track_plan.active_tracks,
        track_inputs=track_plan.track_inputs,
        selected_df=selected_df,
        selected_codes=selected_codes,
        benchmark_context=market_context.benchmark_context,
        rag_veto_preview=rag_result.preview,
        rag_veto_lines=rag_result.veto_lines,
        failed=bundle.failed,
        llm_result=llm_result,
        report_progress=report_progress,
    )


def run(
    symbols_info: list[dict] | list[str],
    webhook_url: str,
    api_key: str,
    model: str,
    benchmark_context: dict | None = None,
    *,
    notify: bool = True,
    provider: str = "gemini",
    llm_base_url: str = "",
    wecom_webhook: str = "",
    dingtalk_webhook: str = "",
) -> tuple[bool, str, str]:
    """
    拉取 OHLCV → 第五步特征工程 → AI 研报 → 飞书/企微/钉钉发送。
    symbols_info: list[{"code", "name", "tag"}] 或 list[str]（向后兼容）。
    """
    runtime_config = step3_runtime_config_from_env()
    items = _normalize_step3_items(symbols_info)
    runtime_config = _runtime_config_for_items(runtime_config, items)
    options = Step3RunOptions(
        webhook_url,
        api_key,
        model,
        notify,
        provider,
        llm_base_url,
        wecom_webhook,
        dingtalk_webhook,
        runtime_config,
    )
    if not items:
        print("[step3] 无输入股票，发送空研报和合规简报")
        return send_empty_step3_report(
            options=options,
            items=[],
            benchmark_context=benchmark_context or {},
            selected_df=pd.DataFrame(),
            rag_veto_preview="",
            rag_veto_lines=[],
        )
    print(f"[step3] AI 输入股票数={len(items)}（全量命中输入）")
    from utils.progress import report_progress

    report_progress("研报准备", f"输入{len(items)}只", 0.1)

    selection = _prepare_step3_selection(items, benchmark_context, options.runtime_config)
    empty_result = _empty_step3_candidate_result(selection.candidate_bundle)
    if empty_result:
        return empty_result
    return _run_step3_selection(
        options=options,
        items=items,
        selection=selection,
        report_progress=report_progress,
    )


def _step3_llm_or_cache(
    *,
    options: Step3RunOptions,
    selected_df: pd.DataFrame,
    rag_lines: list[str],
    market_context,
    track_plan: Step3TrackPlan,
    report_progress,
) -> Step3LlmResult:
    from core.report_snapshot_cache import (
        build_step3_snapshot_payload,
        data_snapshot_hash,
        default_cache_dir,
        load_cached_report,
        selected_rows_from_df,
        snapshot_cache_enabled,
        store_cached_report,
    )

    payload = build_step3_snapshot_payload(
        trade_date=str(getattr(market_context.window, "end_trade_date", "")),
        regime=str(market_context.regime or ""),
        model=options.model,
        selected_rows=selected_rows_from_df(selected_df),
        rag_veto_lines=rag_lines,
    )
    snapshot = data_snapshot_hash(payload)
    cache_dir = default_cache_dir()
    if snapshot_cache_enabled():
        hit = load_cached_report(snapshot, cache_dir)
        if hit and hit.get("report"):
            print(f"[step3] 命中 snapshot cache {snapshot[:24]}")
            return Step3LlmResult(True, "ok_cache", str(hit["report"]), dict(hit.get("used_models") or {}))
    result = call_step3_track_reports(
        track_plan.track_requests,
        track_plan.track_inputs,
        selected_df,
        options,
        WYCKOFF_FUNNEL_SYSTEM_PROMPT,
        report_progress,
    )
    if result.ok and snapshot_cache_enabled():
        store_cached_report(snapshot, {"report": result.report, "used_models": result.used_models}, cache_dir)
    return result
