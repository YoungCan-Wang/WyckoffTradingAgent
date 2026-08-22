"""Structured bull/bear/risk overlay. Default shadow: cannot upgrade permission."""

from __future__ import annotations

from typing import Any

from utils.env import env_bool
from utils.safe import safe_float


def debate_veto_enabled() -> bool:
    return env_bool("STEP3_RISK_VETO", False)


def build_debate_record(row: dict[str, Any]) -> dict[str, Any]:
    score = safe_float(row.get("score") or row.get("watch_score")) or 0.0
    close = safe_float(row.get("close")) or 0.0
    signal = str(row.get("signal_type") or row.get("tag") or "").strip().lower()
    bull = _bull_case(signal, score)
    bear = _bear_case(signal, score)
    risk_veto = score < 0 or signal in {"utad", "upthrust"}
    return {
        "code": str(row.get("code") or ""),
        "bull": bull,
        "bear": bear,
        "risk": "结构或评分触发独立风控否决" if risk_veto else "未见独立风控否决条件",
        "risk_veto": risk_veto,
        "close": close,
        "score": score,
        "shadow": not debate_veto_enabled(),
    }


def debate_block(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    lines = ["## 多空辩论（参谋层，不能升级交易许可）", ""]
    for item in records:
        mode = "shadow" if item.get("shadow") else "veto_armed"
        lines.append(
            f"- `{item.get('code')}` [{mode}] 多：{item.get('bull')} ／ 空：{item.get('bear')} ／ "
            f"风控：{item.get('risk')}"
        )
    return "\n".join(lines)


def shadow_or_apply_veto(records: list[dict[str, Any]]) -> list[str]:
    if not debate_veto_enabled():
        return []
    return [str(item.get("code")) for item in records if item.get("risk_veto") and item.get("code")]


def _bull_case(signal: str, score: float) -> str:
    if signal in {"spring", "lps", "compression"}:
        return f"吸筹类信号 {signal}，评分 {score:.2f}"
    if signal in {"sos", "evr", "trend_pullback"}:
        return f"趋势类信号 {signal}，评分 {score:.2f}"
    return f"评分 {score:.2f}，缺少明确多头结构"


def _bear_case(signal: str, score: float) -> str:
    if score < 0:
        return "评分为负，需求未被确认"
    if signal in {"utad", "upthrust"}:
        return f"{signal} 更像派发/假突破"
    return "单模型看多，缺少对抗式证伪"
