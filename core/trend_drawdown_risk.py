"""Risk metadata for volatile trend-continuation candidates.

分档依据来自 ``scripts/ablate_trend_drawdown_gate.py`` 的实测（口径与数字见
docs/ITERATION_STRATEGY.md 对应小节，产物在 artifacts/trend_drawdown_ablation/）：

- 20% 处的**波动分离度** +2.22pct，随机负控制带宽 [−0.43, +0.22]，超出约 5 倍 →
  这条线区分的是波动，不是方向；因此只做风险标注与排序惩罚，不做召回否决。
- 阈值宽度扫描 14%–28% 的分离度稳定在 +2.1~+2.7pct，30% 之后衰减到 +1.65 →
  20% 落在稳定区内，不是过拟合出来的点。
- 20–30% 与 ≥30% 两档在**波动**上不可分（差 +0.25pct，随机带宽 [−0.62, +0.39]），
  但在**下行**上可分（MAE 差 −0.996pct，随机带宽 [−0.632, +0.645]）。所以第二档
  的语义是「下行更深」而非「波动更高」，惩罚也按下行差距给。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

TREND_DRAWDOWN_WINDOW = 60
TREND_HIGH_DRAWDOWN_PCT = 20.0
TREND_DEEP_DRAWDOWN_PCT = 30.0


@dataclass(frozen=True)
class TrendDrawdownRisk:
    drawdown_pct: float
    label: str
    rank_penalty: float


def max_drawdown_pct(close: pd.Series, window: int = TREND_DRAWDOWN_WINDOW) -> float | None:
    """窗口内最大回撤（正数百分比）。本函数是全仓唯一实现，layer2 也从这里取。"""
    recent = pd.to_numeric(close, errors="coerce").dropna().tail(max(int(window), 2))
    if len(recent) < 2:
        return None
    return abs(float((recent / recent.cummax() - 1.0).min()) * 100.0)


def classify_trend_drawdown(close: pd.Series, window: int = TREND_DRAWDOWN_WINDOW) -> TrendDrawdownRisk | None:
    drawdown = max_drawdown_pct(close, window)
    if drawdown is None:
        return None
    return classify_trend_drawdown_pct(drawdown)


def classify_trend_drawdown_pct(drawdown: float) -> TrendDrawdownRisk:
    """按 60 日回撤给出风险标签与排序惩罚。

    第二档命名为「深回撤」而非「极高波动」：实测两档的波动幅度差落在随机带宽内不可分，
    真正可分的是下行（MAE 差约 −1.0pct）。标签必须说数据支持的那件事。
    """
    if drawdown >= TREND_DEEP_DRAWDOWN_PCT:
        label = f"60日深回撤({drawdown:.1f}%)"
        penalty = 0.04 + min((drawdown - TREND_DEEP_DRAWDOWN_PCT) / 20.0, 1.0) * 0.04
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
