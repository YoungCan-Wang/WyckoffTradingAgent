"""Step3 prompt input assembly."""

from __future__ import annotations

import pandas as pd

from core.candidate_report_semantics import candidate_semantic_parts
from core.candidate_tracks import normalize_candidate_track
from tools.debug_io import dump_model_input
from tools.report_builder import generate_stock_payload
from tools.track_prompt_builder import build_track_user_message
from workflows.step3_entry_quality import entry_quality_policy_tag
from workflows.step3_models import Step3TrackInputs
from workflows.step3_selection import candidate_source_label

TRACK_LABELS = {
    "Trend": "Trend轨（右侧主升 / 放量点火）",
    "Accum": "Accum轨（左侧潜伏 / Spring / LPS）",
}


def build_step3_track_inputs(
    selected_df: pd.DataFrame,
    code_to_df: dict[str, pd.DataFrame],
    items: list[dict],
    financial_map: dict[str, dict],
) -> Step3TrackInputs:
    payloads_by_track, df_by_track, selected_codes_by_track, items_by_track = _empty_track_input_parts(selected_df)
    item_by_code = {str(item.get("code")): item for item in items if isinstance(item, dict)}
    for _, row in selected_df.iterrows():
        _append_track_row(
            row=row,
            stock_df=code_to_df.get(str(row["code"])),
            financial_map=financial_map,
            item_by_code=item_by_code,
            payloads_by_track=payloads_by_track,
            df_by_track=df_by_track,
            selected_codes_by_track=selected_codes_by_track,
            items_by_track=items_by_track,
        )
    return Step3TrackInputs(payloads_by_track, df_by_track, selected_codes_by_track, items_by_track)


def build_step3_benchmark_lines(benchmark_context: dict, sector_rotation_ctx: dict) -> list[str]:
    if not benchmark_context:
        return []
    benchmark_lines = _benchmark_header_lines(benchmark_context)
    _extend_price_volume_lines(benchmark_lines, benchmark_context)
    _extend_breadth_lines(benchmark_lines, benchmark_context)
    _extend_rotation_lines(benchmark_lines, sector_rotation_ctx)
    return benchmark_lines


def build_step3_track_requests(
    active_tracks: list[str],
    track_inputs: Step3TrackInputs,
    candidates_df: pd.DataFrame,
    benchmark_lines: list[str],
    benchmark_context: dict,
    *,
    compressed: bool,
    model_label: str,
    system_prompt: str,
) -> list[dict]:
    track_counts = candidates_df["track"].value_counts().to_dict() if "track" in candidates_df.columns else {}
    current_regime = str(benchmark_context.get("regime", "")) if benchmark_context else ""
    return [
        _build_track_request(
            track,
            track_inputs,
            benchmark_lines,
            compressed=compressed,
            raw_count=int(track_counts.get(track, len(track_inputs.payloads_by_track.get(track, [])))),
            regime=current_regime,
            model_label=model_label,
            system_prompt=system_prompt,
        )
        for track in active_tracks
    ]


def build_step3_preview_report(track_requests: list[dict]) -> str:
    preview_blocks = [
        "# 🧪 Step3 模型输入预演（未调用大模型）",
        "",
        f"- 输入股票数: `{sum(int(x.get('selected_count', 0) or 0) for x in track_requests)}`",
        "- 模式: `STEP3_SKIP_LLM=1`",
        "",
    ]
    for req in track_requests:
        track = str(req.get("track", ""))
        preview_blocks.extend(["## " + TRACK_LABELS.get(track, track), "", str(req.get("user_message", "") or ""), ""])
    return "\n".join(preview_blocks).strip()


def _empty_track_input_parts(selected_df: pd.DataFrame) -> tuple[dict, dict, dict, dict]:
    return (
        {"Trend": [], "Accum": []},
        {"Trend": selected_df.iloc[0:0].copy(), "Accum": selected_df.iloc[0:0].copy()},
        {"Trend": [], "Accum": []},
        {"Trend": [], "Accum": []},
    )


def _append_track_row(
    *,
    row: pd.Series,
    stock_df: pd.DataFrame | None,
    financial_map: dict[str, dict],
    item_by_code: dict[str, dict],
    payloads_by_track: dict[str, list[str]],
    df_by_track: dict[str, pd.DataFrame],
    selected_codes_by_track: dict[str, list[str]],
    items_by_track: dict[str, list[dict]],
) -> None:
    if stock_df is None:
        return
    code = str(row["code"])
    track_key = _track_key(row)
    payloads_by_track.setdefault(track_key, []).append(_build_stock_payload(row, stock_df, financial_map, track_key))
    df_by_track[track_key] = pd.concat([df_by_track[track_key], row.to_frame().T], ignore_index=True)
    selected_codes_by_track[track_key].append(code)
    if code in item_by_code:
        items_by_track[track_key].append(item_by_code[code])


def _build_stock_payload(
    row: pd.Series,
    stock_df: pd.DataFrame,
    financial_map: dict[str, dict],
    track_key: str,
) -> str:
    code = str(row["code"])
    source_label = candidate_source_label(row)
    exit_price_raw = pd.to_numeric(row.get("exit_price"), errors="coerce")
    return generate_stock_payload(
        stock_code=code,
        stock_name=str(row.get("name", code)),
        wyckoff_tag=_wyckoff_tag(row, source_label),
        df=stock_df,
        industry=str(row.get("industry", "")),
        market_cap_yi=pd.to_numeric(row.get("market_cap_yi"), errors="coerce"),
        avg_amount_20_yi=pd.to_numeric(row.get("avg_amount_20_yi"), errors="coerce"),
        policy_tag=_policy_text(row),
        track=track_key,
        stage=_row_text_or_none(row, "stage"),
        sector_state=_row_text_or_none(row, "sector_state"),
        sector_state_code=_row_text_or_none(row, "sector_state_code"),
        sector_note=_row_text_or_none(row, "sector_note"),
        exit_signal=_row_text_or_none(row, "exit_signal"),
        exit_price=float(exit_price_raw) if pd.notna(exit_price_raw) else None,
        exit_reason=_row_text_or_none(row, "exit_reason"),
        financial_metrics=financial_map.get(code),
        springboard_grade=_row_text_or_none(row, "springboard_grade"),
        candidate_source=source_label,
        signal_status=_row_text_or_none(row, "signal_status"),
        confirm_date=_row_text_or_none(row, "confirm_date"),
        confirm_reason=_row_text_or_none(row, "confirm_reason"),
    )


def _track_key(row: pd.Series) -> str:
    return normalize_candidate_track(row.get("track"))


def _policy_text(row: pd.Series) -> str | None:
    parts = []
    semantic = candidate_semantic_parts(
        candidate_reasons=row.get("candidate_reasons"),
        candidate_status=row.get("candidate_status"),
        stock_role_score=row.get("stock_role_score"),
        candidate_lane=row.get("candidate_lane"),
        explicit_theme=row.get("candidate_theme"),
        explicit_phase=row.get("candidate_phase"),
        explicit_role=row.get("candidate_role"),
    )
    if semantic:
        parts.append("主线语义:" + "/".join(semantic))
    for value in (row.get("policy_tag"), entry_quality_policy_tag(row)):
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(dict.fromkeys(parts)) or None


def _row_text_or_none(row: pd.Series, field: str) -> str | None:
    text = str(row.get(field, "")).strip()
    return text or None


def _wyckoff_tag(row: pd.Series, source_label: str) -> str:
    wyckoff_tag = str(row.get("tag", "")).strip()
    return f"[{source_label}] {wyckoff_tag}".strip() if source_label else wyckoff_tag


def _benchmark_header_lines(benchmark_context: dict) -> list[str]:
    from core.execution_playbook import step3_playbook_lines

    lines = step3_playbook_lines(benchmark_context.get("regime"))
    lines.extend(
        [
            "[宏观水温 / Benchmark Context]",
            f"regime={benchmark_context.get('regime')}, "
            f"close={benchmark_context.get('close')}, "
            f"ma50={benchmark_context.get('ma50')}, "
            f"ma200={benchmark_context.get('ma200')}, "
            f"ma50_slope_5d={benchmark_context.get('ma50_slope_5d')}",
            f"recent3_cum_pct={benchmark_context.get('recent3_cum_pct')}",
        ]
    )
    return lines


def _extend_price_volume_lines(benchmark_lines: list[str], benchmark_context: dict) -> None:
    if benchmark_context.get("main_vol_ratio_5_20") is not None:
        benchmark_lines.append(
            f"main_vol_ratio_5_20={benchmark_context.get('main_vol_ratio_5_20'):.3f}, "
            f"main_volume_state={benchmark_context.get('main_volume_state')}"
        )
    market_pv_summary = str(benchmark_context.get("market_pv_summary", "") or "").strip()
    market_pv_outlook = str(benchmark_context.get("market_pv_outlook", "") or "").strip()
    if market_pv_summary or market_pv_outlook:
        benchmark_lines.append("[大盘量价推演 / Price-Volume Outlook]")
    if market_pv_summary:
        benchmark_lines.append(market_pv_summary)
    if market_pv_outlook:
        benchmark_lines.append(market_pv_outlook)


def _extend_breadth_lines(benchmark_lines: list[str], benchmark_context: dict) -> None:
    breadth_ctx = benchmark_context.get("breadth", {}) or {}
    if breadth_ctx:
        benchmark_lines.append(
            f"breadth_pct={breadth_ctx.get('ratio_pct')}, breadth_delta_pct={breadth_ctx.get('delta_pct')}"
        )


def _extend_rotation_lines(benchmark_lines: list[str], sector_rotation_ctx: dict) -> None:
    rotation_headline = str(sector_rotation_ctx.get("headline", "")).strip()
    rotation_lines = sector_rotation_ctx.get("overview_lines", []) or []
    if not rotation_headline and not rotation_lines:
        return
    benchmark_lines.append("[板块轮动 / Sector Rotation]")
    if rotation_headline:
        benchmark_lines.append(rotation_headline)
    benchmark_lines.extend(rotation_lines[:4])


def _build_track_request(
    track: str,
    track_inputs: Step3TrackInputs,
    benchmark_lines: list[str],
    *,
    compressed: bool,
    raw_count: int,
    regime: str,
    model_label: str,
    system_prompt: str,
) -> dict:
    payloads = track_inputs.payloads_by_track.get(track, [])
    user_message = build_track_user_message(
        track=track,
        benchmark_lines=benchmark_lines,
        payloads=payloads,
        compressed=compressed,
        raw_count=raw_count,
        selected_count=len(payloads),
        regime=regime,
    )
    _dump_track_input(track, track_inputs, model_label, system_prompt, user_message)
    return {"track": track, "user_message": user_message, "selected_count": len(payloads)}


def _dump_track_input(
    track: str,
    track_inputs: Step3TrackInputs,
    model_label: str,
    system_prompt: str,
    user_message: str,
) -> None:
    dump_model_input(
        step_prefix="step3",
        items=track_inputs.items_by_track.get(track, []),
        model=model_label,
        system_prompt=system_prompt,
        user_message=user_message,
        name_hint=track.lower(),
    )
