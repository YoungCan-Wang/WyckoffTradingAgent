"""Compact as-run decision trace for strong-move review."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.candidate_ranker import TRIGGER_LABELS
from core.candidate_tracks import best_candidate_entry_map
from core.cn_boards import is_supported_cn_board
from core.funnel_taxonomy import (
    REVIEW_STAGE_BASE_REJECT,
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_TRIGGER_MISS,
    lane_label,
)
from core.review_shadow_lanes import attach_shadow_signal
from core.wyckoff_engine import sort_by_date_if_needed

REVIEW_TRACE_SCHEMA = "review_trace_v1"
_BLOCKING_EXIT_SIGNALS = {"stop_loss", "distribution_warning", "upthrust_warning"}
_SHADOW_NEAR_L2_MAX_GAP_PCT = 10.0


def write_review_trace_artifact(inputs: Any, triggers: dict, metrics: dict, output_dir: str) -> Path | None:
    if not str(output_dir or "").strip():
        return None
    payload = build_review_trace(inputs, triggers, metrics)
    path = Path(output_dir) / f"review_trace_{payload['trade_date'].replace('-', '')}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temp_path.replace(path)
    return path


def build_review_trace(inputs: Any, triggers: dict, metrics: dict) -> dict[str, Any]:
    layers = inputs.layers
    candidates = inputs.candidates
    symbols = _decision_rows(inputs, triggers)
    config_payload = asdict(inputs.cfg)
    return {
        "schema_version": REVIEW_TRACE_SCHEMA,
        "market": "cn",
        "trade_date": inputs.window.end_trade_date.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "run": {
            "github_run_id": os.getenv("GITHUB_RUN_ID", ""),
            "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
            "git_sha": os.getenv("GITHUB_SHA", ""),
        },
        "config_digest": _digest(config_payload),
        "policy": _review_policy(inputs.cfg),
        "data_quality": metrics.get("data_quality") or {},
        "market_context": _market_context(metrics),
        "counts": {
            "universe": len(inputs.pool.symbols),
            "trace_rows": len(symbols),
            "l1": len(layers.l1_passed),
            "l2": len(layers.l2_passed),
            "l3": len(layers.l3_passed),
            "candidates": len(candidates.candidate_entries),
        },
        "symbols": symbols,
    }


def load_review_trace_artifact(path: str | Path, expected_trade_date: date | str) -> dict[str, Any]:
    with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != REVIEW_TRACE_SCHEMA:
        raise ValueError("review trace schema mismatch")
    if payload.get("market") != "cn":
        raise ValueError("review trace market mismatch")
    expected = expected_trade_date.isoformat() if isinstance(expected_trade_date, date) else str(expected_trade_date)
    if str(payload.get("trade_date") or "") != expected:
        raise ValueError(f"review trace date mismatch: expected={expected}, got={payload.get('trade_date')}")
    if not isinstance(payload.get("symbols"), dict):
        raise ValueError("review trace symbols missing")
    return payload


def _decision_rows(inputs: Any, triggers: dict) -> dict[str, dict[str, Any]]:
    layers = inputs.layers
    candidates = inputs.candidates
    l1_set = set(layers.l1_passed)
    l2_set = set(layers.l2_passed)
    l3_set = set(layers.l3_passed)
    entry_map = best_candidate_entry_map(candidates.candidate_entries)
    hit_map = _trigger_labels(triggers)
    blocked = _blocked_exit_map(candidates.exit_signals)
    return {
        code: attach_shadow_signal(
            _decision_row(code, inputs, l1_set, l2_set, l3_set, entry_map, hit_map, blocked),
            near_l2_max_gap_pct=_SHADOW_NEAR_L2_MAX_GAP_PCT,
        )
        for code in inputs.pool.symbols
    }


def _decision_row(
    code: str,
    inputs: Any,
    l1_set: set[str],
    l2_set: set[str],
    l3_set: set[str],
    entry_map: dict[str, dict],
    hit_map: dict[str, list[str]],
    blocked: dict[str, dict],
) -> dict[str, Any]:
    name = str(inputs.ref_data.name_map.get(code, code)).strip() or code
    sector = str(inputs.ref_data.sector_map.get(code, "")).strip()
    base = {
        "name": name,
        "sector": sector,
        "l1_eligible": code in l1_set,
        "l2_eligible": code in l2_set,
        "l3_eligible": code in l3_set,
        "l2_channel": str(inputs.layers.l2_channel_map.get(code, "")),
        "trigger_labels": list(hit_map.get(code, [])),
        "risk_signal": str((blocked.get(code) or {}).get("signal") or ""),
    }
    if code not in inputs.all_df_map:
        return {**base, "stage": "数据失败", "reason": "日线拉取失败/超时"}
    if code not in l1_set:
        reason = _l1_rejection_reason(code, inputs)
        return {**base, "stage": REVIEW_STAGE_BASE_REJECT, "reason": reason}
    if code in entry_map:
        entry = _entry_payload(entry_map[code])
        return {**base, "stage": REVIEW_STAGE_CANDIDATE_HIT, "reason": _candidate_reason(entry), "entry": entry}
    if code not in l2_set:
        detail = str(inputs.layers.l2_rejections.get(code) or "八通道均未通过")
        return {**base, "stage": REVIEW_STAGE_STRENGTH_MISS, "reason": f"结构强度不足：{detail}"}
    if code not in l3_set:
        return {**base, "stage": REVIEW_STAGE_THEME_MISS, "reason": f"题材/行业共振不足（{sector or '未知行业'}）"}
    if code in blocked:
        return {**base, "stage": REVIEW_STAGE_RISK_BLOCK, "reason": _risk_reason(blocked[code], hit_map.get(code, []))}
    if code in hit_map:
        return {**base, "stage": REVIEW_STAGE_TRIGGER_HIT, "reason": "、".join(hit_map[code])}
    return {**base, "stage": REVIEW_STAGE_TRIGGER_MISS, "reason": "未触发 Spring/LPS/EVR/SOS 等买点确认"}


def _l1_rejection_reason(code: str, inputs: Any) -> str:
    cfg = inputs.cfg
    name = str(inputs.ref_data.name_map.get(code, ""))
    if cfg.require_cn_main_or_chinext and not is_supported_cn_board(code, include_bse=cfg.include_bse_board):
        return "非A股目标板块代码"
    if "ST" in name.upper():
        return "ST股票"
    frame = sort_by_date_if_needed(inputs.all_df_map[code])
    cap_reason = _market_cap_reason(code, frame, inputs)
    if cap_reason:
        return cap_reason
    close = pd.to_numeric(frame.get("close"), errors="coerce").dropna()
    if not close.empty and float(close.iloc[-1]) < float(cfg.l1_min_close_price):
        return f"股价不足: {float(close.iloc[-1]):.2f} < {cfg.l1_min_close_price:.2f}"
    amount_reason = _amount_reason(frame, cfg)
    if amount_reason:
        return amount_reason
    financial = inputs.ref_data.financial_map.get(code) or {}
    if financial.get("roe") is not None and float(financial["roe"]) < -10:
        return f"财务准入失败: ROE={float(financial['roe']):.2f}% < -10%"
    if financial.get("debt_to_asset_ratio") is not None and float(financial["debt_to_asset_ratio"]) > 85:
        return f"财务准入失败: 资产负债率={float(financial['debt_to_asset_ratio']):.2f}% > 85%"
    return "未通过基础准入（综合条件不满足）"


def _market_cap_reason(code: str, frame: pd.DataFrame, inputs: Any) -> str:
    cap_map = inputs.ref_data.market_cap_map
    if not cap_map:
        return ""
    cfg = inputs.cfg
    cap = float(cap_map.get(code, 0.0) or 0.0)
    if cap < cfg.l1_delist_risk_cap_floor_yi:
        return f"市值不足: {cap:.2f}亿 < 退市风险地板{cfg.l1_delist_risk_cap_floor_yi:.2f}亿"
    if cap >= cfg.min_market_cap_yi:
        return ""
    avg_amount = _average_amount_wan(frame, cfg.amount_avg_window)
    if avg_amount < cfg.l1_cap_bypass_amount_wan:
        return f"市值{cap:.2f}亿且成交额不足小市值旁路{cfg.l1_cap_bypass_amount_wan:.1f}万"
    return ""


def _amount_reason(frame: pd.DataFrame, cfg: Any) -> str:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce").dropna().tail(cfg.amount_avg_window)
    threshold = float(cfg.min_avg_amount_wan) * 10000.0
    if amount.empty or float(amount.mean()) < threshold:
        return f"成交额不足: {_average_amount_wan(frame, cfg.amount_avg_window):.1f}万 < {cfg.min_avg_amount_wan:.1f}万"
    positive = amount[amount > 0]
    if not cfg.amount_skew_check_enabled or len(positive) < 5:
        return ""
    skew = positive.skew()
    median_weak = float(positive.median()) < threshold * cfg.amount_median_min_ratio
    pass_days_weak = float((positive >= threshold).mean()) < cfg.amount_pass_days_min_ratio
    if pd.notna(skew) and float(skew) >= cfg.amount_skew_max and (median_weak or pass_days_weak):
        return "成交额由少数尖峰日扭曲，持续流动性不足"
    return ""


def _average_amount_wan(frame: pd.DataFrame, window: int) -> float:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce").dropna().tail(window)
    return float(amount.mean()) / 10000.0 if not amount.empty else 0.0


def _trigger_labels(triggers: dict) -> dict[str, list[str]]:
    hit_map: dict[str, list[str]] = {}
    for trigger, label in TRIGGER_LABELS.items():
        for code, _score in triggers.get(trigger, []):
            labels = hit_map.setdefault(str(code), [])
            if label not in labels:
                labels.append(label)
    return hit_map


def _blocked_exit_map(exit_signals: dict) -> dict[str, dict]:
    return {
        str(code): dict(raw or {})
        for code, raw in (exit_signals or {}).items()
        if str((raw or {}).get("signal") or "").strip() in _BLOCKING_EXIT_SIGNALS
    }


def _entry_payload(entry: dict) -> dict[str, Any]:
    return {
        "code": str(entry.get("code") or ""),
        "entry_type": str(entry.get("entry_type") or entry.get("signal_key") or "candidate"),
        "signal_key": str(entry.get("signal_key") or ""),
        "track": str(entry.get("track") or ""),
        "score": _float(entry.get("score")),
        "opportunity": str(entry.get("opportunity") or ""),
        "timing": str(entry.get("timing") or ""),
        "risk": str(entry.get("risk") or ""),
    }


def _candidate_reason(entry: dict) -> str:
    entry_type = str(entry.get("entry_type") or "candidate")
    parts = [f"候选车道: {lane_label(entry_type) or entry_type}", f"score={entry.get('score', 0.0):.2f}"]
    parts.extend(str(entry[key]) for key in ("opportunity", "timing", "risk") if str(entry.get(key) or "").strip())
    return " | ".join(parts)


def _risk_reason(signal: dict, trigger_labels: list[str]) -> str:
    label = {
        "stop_loss": "触发结构止损",
        "distribution_warning": "触发Distribution派发警告",
        "upthrust_warning": "触发Upthrust/UTAD假突破派发警告",
    }.get(str(signal.get("signal") or ""), "触发风控硬剔除")
    parts = [label]
    if signal.get("price") is not None:
        parts.append(f"参考价={_float(signal.get('price')):.2f}")
    if trigger_labels:
        parts.append(f"买点确认={'、'.join(trigger_labels)}")
    if str(signal.get("reason") or "").strip():
        parts.append(str(signal["reason"]).strip())
    return " | ".join(parts)


def _review_policy(cfg: Any) -> dict[str, Any]:
    return {
        "min_market_cap_yi": cfg.min_market_cap_yi,
        "min_avg_amount_wan": cfg.min_avg_amount_wan,
        "l1_min_close_price": cfg.l1_min_close_price,
        "ma_long": cfg.ma_long,
        "rps_fast_min": cfg.rps_fast_min,
        "rps_slow_min": cfg.rps_slow_min,
        "shadow_near_l2_max_gap_pct": _SHADOW_NEAR_L2_MAX_GAP_PCT,
    }


def _market_context(metrics: dict[str, Any]) -> dict[str, Any]:
    context = metrics.get("benchmark_context") or {}
    tuned = context.get("tuned") or {}
    return {
        "regime": str(context.get("regime") or ""),
        "trade_mode": str(metrics.get("trade_mode") or ""),
        "min_avg_amount_wan": _float(tuned.get("min_avg_amount_wan")),
        "rps_fast_min": _float(tuned.get("rps_fast_min")),
        "rps_slow_min": _float(tuned.get("rps_slow_min")),
    }


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
