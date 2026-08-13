"""Risk metadata for volatile trend-continuation candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

TREND_DRAWDOWN_WINDOW = 60
TREND_HIGH_DRAWDOWN_PCT = 20.0
TREND_EXTREME_DRAWDOWN_PCT = 30.0


@dataclass(frozen=True)
class TrendDrawdownRisk:
    drawdown_pct: float
    label: str
    rank_penalty: float


def max_drawdown_pct(close: pd.Series, window: int = TREND_DRAWDOWN_WINDOW) -> float | None:
    recent = pd.to_numeric(close, errors="coerce").dropna().tail(max(int(window), 2))
    if len(recent) < 2:
        return None
    drawdown = recent / recent.cummax() - 1.0
    return abs(float(drawdown.min()) * 100.0)


def classify_trend_drawdown(close: pd.Series, window: int = TREND_DRAWDOWN_WINDOW) -> TrendDrawdownRisk | None:
    drawdown = max_drawdown_pct(close, window)
    if drawdown is None:
        return None
    return classify_trend_drawdown_pct(drawdown)


def classify_trend_drawdown_pct(drawdown: float) -> TrendDrawdownRisk:
    if drawdown >= TREND_EXTREME_DRAWDOWN_PCT:
        label = f"60日极高波动({drawdown:.1f}%)"
        penalty = 0.04 + min((drawdown - TREND_EXTREME_DRAWDOWN_PCT) / 20.0, 1.0) * 0.04
    elif drawdown >= TREND_HIGH_DRAWDOWN_PCT:
        label = f"60日高波动({drawdown:.1f}%)"
        penalty = 0.02 + min((drawdown - TREND_HIGH_DRAWDOWN_PCT) / 10.0, 1.0) * 0.02
    else:
        label, penalty = "", 0.0
    return TrendDrawdownRisk(float(drawdown), label, penalty)


def annotate_trend_drawdown_risk(
    entries: list[dict[str, Any]],
    df_map: dict[str, pd.DataFrame],
    channel_map: dict[str, str],
) -> None:
    for entry in entries:
        code = str(entry.get("code", "")).strip()
        if "趋势延续" not in str(channel_map.get(code, "")):
            continue
        frame = df_map.get(code)
        if frame is None or frame.empty:
            continue
        risk = classify_trend_drawdown(frame.get("close", pd.Series(dtype=float)))
        if risk is None:
            continue
        metrics = dict(entry.get("metrics") or {})
        metrics["trend_drawdown60_pct"] = round(risk.drawdown_pct, 4)
        entry["metrics"] = metrics
        if risk.label:
            current = str(entry.get("risk", "") or "").strip()
            items = [item for item in (current, risk.label) if item]
            entry["risk"] = " / ".join(dict.fromkeys(items))
