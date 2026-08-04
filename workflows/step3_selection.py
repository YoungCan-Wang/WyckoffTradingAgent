"""Step3 candidate selection and operation-gate helpers."""

from __future__ import annotations

import pandas as pd

from core.candidate_tracks import normalize_candidate_track
from core.funnel_taxonomy import source_label
from workflows.step3_compression import select_compressed_step3_candidates
from workflows.step3_entry_quality import annotate_entry_quality
from workflows.step3_runtime_config import Step3RuntimeConfig
from workflows.step3_text import clean_text
from workflows.step3_upstream_selection import (
    finite_numeric_series,
    has_upstream_priority_context,
    select_upstream_priority_candidates,
)


def candidate_source_label(row: pd.Series) -> str:
    source = clean_text(row.get("selection_source"))
    label = source_label(source)
    confirmed = clean_text(row.get("signal_status")).lower() == "confirmed"
    if confirmed and label and label != "跨日确认":
        return f"{label}+跨日确认"
    if confirmed:
        return "跨日确认"
    return label


def normalize_step3_candidates(candidate_rows: list[dict]) -> pd.DataFrame:
    if not candidate_rows:
        return pd.DataFrame()
    candidates_df = pd.DataFrame(candidate_rows)
    candidates_df["code"] = candidates_df["code"].astype(str).str.strip()
    candidates_df["input_order"] = pd.to_numeric(candidates_df.get("input_order"), errors="coerce")
    candidates_df["input_order"] = (
        candidates_df["input_order"].fillna(pd.Series(range(len(candidates_df)), index=candidates_df.index)).astype(int)
    )
    track_values = candidates_df.get("track", pd.Series("", index=candidates_df.index))
    candidates_df["track"] = track_values.map(normalize_candidate_track)
    candidates_df["policy_tag"] = ""
    return annotate_entry_quality(candidates_df)


def select_step3_candidates(
    candidates_df: pd.DataFrame,
    regime: str,
    runtime_config: Step3RuntimeConfig,
) -> pd.DataFrame:
    selected_df = candidates_df.copy()
    _fill_wyckoff_score(selected_df)
    selected_df["industry_rank"] = pd.NA
    effective_context_cap = _resolve_step3_context_cap(len(candidates_df), runtime_config)
    if has_upstream_priority_context(candidates_df, runtime_config):
        selected_df = select_upstream_priority_candidates(candidates_df, runtime_config, effective_context_cap)
    elif runtime_config.enable_compression:
        selected_df = select_compressed_step3_candidates(candidates_df, regime, runtime_config, effective_context_cap)
    else:
        print(f"[step3] 候选压缩未启用: selected=全量{len(selected_df)}")
    selected_df = _apply_context_cap(selected_df, effective_context_cap, runtime_config)
    selected_df = annotate_entry_quality(selected_df)
    _fill_wyckoff_score(selected_df)
    if "industry_rank" not in selected_df.columns:
        selected_df["industry_rank"] = pd.NA
    return selected_df


def _fill_wyckoff_score(df: pd.DataFrame) -> None:
    df["wyckoff_score"] = finite_numeric_series(df.get("priority_score"), df.index)
    df["wyckoff_score"] = df["wyckoff_score"].where(
        df["wyckoff_score"].notna(),
        finite_numeric_series(df.get("funnel_score"), df.index),
    )


def _resolve_step3_context_cap(raw_count: int, runtime_config: Step3RuntimeConfig) -> int:
    raw_n = max(int(raw_count), 0)
    if raw_n <= 0:
        return 0
    if runtime_config.max_ai_input > 0:
        return min(runtime_config.max_ai_input, raw_n)
    if runtime_config.default_context_cap > 0:
        return min(runtime_config.default_context_cap, raw_n)
    return raw_n


def _apply_context_cap(
    selected_df: pd.DataFrame,
    effective_context_cap: int,
    runtime_config: Step3RuntimeConfig,
) -> pd.DataFrame:
    if effective_context_cap <= 0 or len(selected_df) <= effective_context_cap:
        return selected_df
    before_n = len(selected_df)
    selected_df = selected_df.head(effective_context_cap).reset_index(drop=True)
    print(
        f"[step3] 上下文硬上限生效: selected {before_n} -> {len(selected_df)} "
        f"(effective_context_cap={effective_context_cap}, env_STEP3_MAX_AI_INPUT={runtime_config.max_ai_input}, "
        f"default_cap={runtime_config.default_context_cap})"
    )
    return selected_df
