"""Step3 operation-pool parsing and confirmation gate."""

from __future__ import annotations

import pandas as pd

from tools.report_parser import extract_ops_codes_from_markdown, try_parse_structured_report
from workflows.step3_runtime_config import Step3RuntimeConfig
from workflows.step3_text import clean_text


def build_signal_confirmed_preview(selected_df: pd.DataFrame) -> str:
    if selected_df is None or selected_df.empty or "signal_status" not in selected_df.columns:
        return ""
    confirmed_df = selected_df[selected_df["signal_status"].astype(str).str.lower().eq("confirmed")].copy()
    if confirmed_df.empty:
        return ""

    lines = ["## ✅ 跨日确认已通过（前置）"]
    ordered = confirmed_df.sort_values("input_order", kind="stable")
    for _, row in ordered.head(5).iterrows():
        code = clean_text(row.get("code"))
        name = clean_text(row.get("name")) or code
        signal_type = clean_text(row.get("signal_type")) or clean_text(row.get("tag")) or "-"
        signal_date = clean_text(row.get("signal_date")) or "-"
        confirm_date = clean_text(row.get("confirm_date")) or "-"
        reason = clean_text(row.get("confirm_reason")) or "确认条件已满足"
        lines.append(f"- {code} {name} | {signal_type} | {signal_date} → {confirm_date} | {reason}")
    if len(ordered) > 5:
        lines.append(f"- 另 {len(ordered) - 5} 只详见结构化审计数据")
    return "\n".join(lines) + "\n\n---\n"


def build_unconfirmed_ops_block(blocked_codes: list[str], code_name: dict[str, str]) -> str:
    if not blocked_codes:
        return ""
    lines = ["## 🚧 跨日确认未通过（前置）"]
    for code in blocked_codes:
        lines.append(f"- {code} {code_name.get(code, code)}：模型曾列入起跳板，但跨日确认未通过，降级为观察")
    return "\n".join(lines) + "\n\n---\n"


def resolve_step3_operation_codes(
    report: str,
    selected_codes: list[str],
    selected_df: pd.DataFrame,
    runtime_config: Step3RuntimeConfig,
) -> tuple[dict[str, str], list[str], list[str]]:
    code_name = {str(row.get("code")): str(row.get("name", row.get("code"))) for _, row in selected_df.iterrows()}
    selected_set = set(selected_codes)
    ops_codes = extract_ops_codes_from_markdown(report, selected_set)
    structured = try_parse_structured_report(report=report, allowed_codes=selected_set, code_name=code_name)
    if not ops_codes and structured and structured.get("operation_pool"):
        ops_codes.extend(_structured_operation_codes(structured["operation_pool"], ops_codes))
    ops_codes, blocked_unconfirmed_ops = _filter_confirmed_ops_codes(ops_codes, selected_df, runtime_config)
    return code_name, ops_codes, blocked_unconfirmed_ops


def _structured_operation_codes(operation_pool: list[dict], existing_codes: list[str]) -> list[str]:
    new_codes: list[str] = []
    seen = set(existing_codes)
    for item in operation_pool:
        code = str(item.get("code", "")).strip()
        if code and code not in seen:
            new_codes.append(code)
            seen.add(code)
    return new_codes


def _filter_confirmed_ops_codes(
    ops_codes: list[str],
    selected_df: pd.DataFrame,
    runtime_config: Step3RuntimeConfig,
) -> tuple[list[str], list[str]]:
    if not runtime_config.require_confirmed_operation or selected_df is None or selected_df.empty:
        return ops_codes, []
    status_map = {
        str(row.get("code", "")).strip(): str(row.get("signal_status", "")).strip().lower()
        for _, row in selected_df.iterrows()
    }
    kept = [code for code in ops_codes if status_map.get(str(code).strip()) == "confirmed"]
    blocked = [code for code in ops_codes if code not in kept]
    return kept, blocked
