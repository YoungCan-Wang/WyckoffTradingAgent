"""Run Step4 directly from Supabase recommendation_tracking rows."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.constants import TABLE_RECOMMENDATION_TRACKING
from integrations.llm_client import get_provider_credentials, resolve_provider_name
from integrations.supabase_base import close_client, create_admin_client
from workflows.step4_pipeline import (
    TZ,
    latest_trade_date_str,
    load_step4_target,
    log_line,
    run_step4_pipeline,
)


@dataclass(frozen=True)
class Step4FromSupabaseRequest:
    recommend_date: str = ""
    logs_path: str = ""


def run_step4_from_supabase(request: Step4FromSupabaseRequest) -> int:
    recommend_date = resolve_recommend_date(request.recommend_date)
    logs_path = resolve_logs_path(request.logs_path)
    symbols_info, ai_codes = load_recommendations_with_logs(recommend_date, logs_path)
    if not symbols_info:
        log_line(f"Step4 direct run: no recommendation rows for date={recommend_date}", logs_path)
        return 1
    if not ai_codes:
        log_line(f"Step4 direct run: no AI recommended rows for date={recommend_date}", logs_path)
        return 1
    step4_target = load_target_or_log(logs_path)
    if not step4_target:
        return 1
    summary = execute_step4_from_rows(recommend_date, symbols_info, ai_codes, step4_target, logs_path)
    log_line("Step4 direct run summary: " + json.dumps(summary, ensure_ascii=False), logs_path)
    return 0 if summary.get("ok") else 1


def resolve_recommend_date(raw: str) -> int:
    text = str(raw or "").strip() or os.getenv("STEP4_RECOMMEND_DATE", "").strip()
    if not text:
        return int(latest_trade_date_str().replace("-", ""))
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 8:
        raise ValueError(f"invalid recommend date: {raw!r}")
    return int(digits)


def resolve_logs_path(raw: str) -> str:
    if str(raw or "").strip():
        return str(raw).strip()
    return str(Path(os.getenv("LOGS_DIR", "logs")) / f"step4_from_supabase_{datetime.now(TZ):%Y%m%d_%H%M%S}.log")


def load_recommendations_with_logs(recommend_date: int, logs_path: str) -> tuple[list[dict], list[str]]:
    log_line(f"Step4 direct run: loading Supabase recommendations date={recommend_date}", logs_path)
    symbols_info, ai_codes = load_recommendations(recommend_date)
    log_line(
        f"Step4 direct run: recommendation_rows={len(symbols_info)}, "
        f"ai_codes={len(ai_codes)} ({', '.join(ai_codes) if ai_codes else 'none'})",
        logs_path,
    )
    return symbols_info, ai_codes


def load_target_or_log(logs_path: str) -> dict | None:
    step4_target, reason = load_step4_target()
    if not step4_target:
        log_line(f"Step4 direct run: target unavailable ({reason})", logs_path)
        return None
    log_line(f"Step4 direct run: target={step4_target['portfolio_id']}", logs_path)
    return step4_target


def execute_step4_from_rows(
    recommend_date: int,
    symbols_info: list[dict],
    ai_codes: list[str],
    step4_target: dict,
    logs_path: str,
) -> dict:
    provider = resolve_provider_name("STEP4_LLM_PROVIDER", "efficiency")
    api_key, model, llm_base_url = get_provider_credentials(provider)
    return run_step4_pipeline(
        step4_target=step4_target,
        symbols_info=symbols_info,
        step3_springboard_codes=ai_codes,
        step3_report_text=build_external_report(recommend_date, symbols_info, ai_codes),
        benchmark_context={},
        api_key=api_key,
        model=model,
        provider=provider,
        llm_base_url=llm_base_url,
        logs_path=logs_path,
    )


def norm_code(raw: object) -> str:
    text = str(raw or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else text


def load_recommendations(recommend_date: int) -> tuple[list[dict], list[str]]:
    rows = fetch_recommendation_rows(recommend_date)
    symbols_info = [item for row in rows if (item := recommendation_item(row))]
    ai_codes = sorted({norm_code(row.get("code")) for row in rows if row.get("is_ai_recommended")})
    return symbols_info, [code for code in ai_codes if code]


def fetch_recommendation_rows(recommend_date: int) -> list[dict[str, Any]]:
    client = create_admin_client()
    try:
        resp = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select(
                "code,name,recommend_reason,initial_price,current_price,"
                "funnel_score,priority_score,capital_migration_bonus,selection_source,"
                "stage,industry,candidate_lane,entry_type,primary_signal,signal_types,"
                "candidate_status,candidate_risk,"
                "is_ai_recommended,recommend_count,recommend_date"
            )
            .eq("recommend_date", recommend_date)
            .execute()
        )
        return list(resp.data or [])
    finally:
        close_client(client)


def recommendation_item(row: dict[str, Any]) -> dict | None:
    code = norm_code(row.get("code"))
    if not code:
        return None
    score = row.get("funnel_score")
    priority_score = row.get("priority_score") if row.get("priority_score") not in (None, "") else score
    signal_types = row.get("signal_types") if isinstance(row.get("signal_types"), list) else []
    primary_signal = str(row.get("primary_signal") or "").strip()
    if not primary_signal and signal_types:
        primary_signal = str(signal_types[0] or "").strip()
    entry_type = str(row.get("entry_type") or "").strip()
    return {
        "code": code,
        "name": str(row.get("name") or code).strip(),
        "tag": str(row.get("recommend_reason") or "").strip(),
        "score": score,
        "priority_score": priority_score,
        "funnel_score": score,
        "capital_migration_bonus": row.get("capital_migration_bonus"),
        "selection_source": row.get("selection_source"),
        "stage": row.get("stage"),
        "industry": row.get("industry"),
        "candidate_lane": row.get("candidate_lane"),
        "entry_type": entry_type,
        "primary_signal": primary_signal,
        "signal_types": signal_types,
        # Step4 regime×signal 闸门读 signal_type；回读行必须还原，否则对空串 fail-open。
        "signal_type": primary_signal or entry_type,
        "candidate_status": row.get("candidate_status"),
        "candidate_risk": row.get("candidate_risk"),
        "initial_price": row.get("initial_price"),
        "current_price": row.get("current_price"),
        "recommend_count": row.get("recommend_count"),
        "source_type": "supabase_recommendation_tracking",
    }


def build_external_report(recommend_date: int, symbols_info: list[dict], ai_codes: list[str]) -> str:
    ai_set = set(ai_codes)
    lines = [f"Supabase复用今日Step3起跳板候选，recommend_date={recommend_date}，候选={', '.join(ai_codes)}。"]
    for item in symbols_info:
        if item["code"] in ai_set:
            capital_bonus = item.get("capital_migration_bonus")
            capital_text = f" | capital_migration={capital_bonus}" if capital_bonus not in (None, "") else ""
            lines.append(
                f"- {item['code']} {item['name']} | {item.get('tag') or '-'} | "
                f"score={item.get('funnel_score')}{capital_text}"
            )
    return "\n".join(lines)
