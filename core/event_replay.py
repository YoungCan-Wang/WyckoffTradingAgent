"""威科夫事件历史重放 — 把"只看最后一根"的检测器变成一条时间线。

生产检测器（``_detect_spring`` 等）都是针对最新一根 K 线设计的：它们读
``df.iloc[-1]`` 和 ``[-2]``，返回一个分数，不返回日期。图表要标注历史上
每一次 spring / SOS，就需要知道"这件事发生在哪一天、哪个价位"。

做法是**重放**而不是重写：把 ``df.iloc[:i+1]`` 逐根喂给同一批检测器，
第 i 根就是它们眼里的"最后一根"。这样规则只有一份 —— 复制一套"历史版"
检测逻辑，迟早会和生产版本漂移，届时图上标的和漏斗选的就不是一回事了。

成本已实测：320 根 × 4 检测器约 0.27 秒，可以同步跑在 IPC 调用里。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 事件锚定在哪个价位：spring/LPS 是下探动作，锚在最低价（影线尖）；
# SOS/EVR 是放量动作，锚在收盘价。锚错了标注就会飘在离 K 线很远的地方。
_ANCHOR_LOW = "low"
_ANCHOR_CLOSE = "close"

# 每种事件的最小间隔（交易日）。检测器在连续几根上往往都成立，
# 逐根都标会糊成一片；只保留一簇里分数最高的那根。
DEFAULT_MIN_GAP = 5


@dataclass(frozen=True)
class WyckoffMark:
    """图上一个可标注的威科夫事件。

    ``bar`` 是它在原始 frame 里的行号。稀疏化要按**真实 K 线距离**判断相邻，
    只用命中序会把相隔很远的两次事件误判成一簇。
    """

    date: str
    price: float
    kind: str
    score: float
    bar: int

    def as_dict(self) -> dict[str, Any]:
        # bar 一并带出：画图端可以直接索引 bars 数组，不必按日期字符串查找。
        return {
            "date": self.date,
            "price": self.price,
            "kind": self.kind,
            "score": self.score,
            "bar": self.bar,
        }


def _detectors() -> list[tuple[str, Callable[..., float | None], str]]:
    """(kind, 检测函数, 锚定列)。延迟导入：wyckoff_engine 拉起来不便宜。"""
    from core import wyckoff_engine as we

    return [
        ("spring", we._detect_spring, _ANCHOR_LOW),
        ("lps", we._detect_lps, _ANCHOR_LOW),
        ("sos", we._detect_sos, _ANCHOR_CLOSE),
        ("evr", we._detect_evr, _ANCHOR_CLOSE),
    ]


def replay_events(
    frame: pd.DataFrame,
    *,
    cfg: Any = None,
    code: str = "",
    min_gap: int = DEFAULT_MIN_GAP,
) -> list[dict[str, Any]]:
    """在 ``frame`` 上重放检测器，返回按日期排序的事件列表。

    ``frame`` 需要 date/open/high/low/close/volume 英文列（``normalize_hist_df``
    的输出）。列名不对或数据太短时返回空列表，不抛异常 —— 图表少几个标注可以
    接受，整张图打不开不行。
    """
    if frame is None or frame.empty:
        return []
    required = {"date", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        logger.warning("replay skipped: missing columns %s", sorted(required - set(frame.columns)))
        return []

    if cfg is None:
        from core.wyckoff_engine import FunnelConfig

        cfg = FunnelConfig()

    detectors = _detectors()
    # 最短窗口：喂给检测器的切片太短它们只会一路返回 None，白跑。
    warmup = _warmup_bars(cfg)
    if len(frame) <= warmup:
        return []

    hits: list[WyckoffMark] = []
    for i in range(warmup, len(frame)):
        window = frame.iloc[: i + 1]
        bar = frame.iloc[i]
        for kind, detect, anchor in detectors:
            score = _safe_detect(detect, window, cfg, code)
            if score is None:
                continue
            price = _price_of(bar, anchor)
            if price is None:
                continue
            hits.append(WyckoffMark(date=str(bar["date"])[:10], price=price, kind=kind, score=float(score), bar=i))

    return [mark.as_dict() for mark in _thin_clusters(hits, min_gap=min_gap)]


def _warmup_bars(cfg: Any) -> int:
    """所有检测器里最长的回看窗口 —— 在此之前没有一个能成立。"""
    windows = [
        getattr(cfg, "spring_support_window", 60) + 2,
        getattr(cfg, "lps_vol_ref_window", 20) + getattr(cfg, "lps_lookback", 5),
        getattr(cfg, "sos_vol_window", 20) + 2,
        getattr(cfg, "evr_vol_window", 20) + 2,
    ]
    return max(int(w) for w in windows)


def _safe_detect(detect: Callable[..., float | None], window: pd.DataFrame, cfg: Any, code: str) -> float | None:
    """检测器对畸形数据可能抛异常；单根出错不该让整条时间线失败。"""
    try:
        return detect(window, cfg, None, code)
    except TypeError:
        # 少数检测器签名没有 max_bias_200 / code。
        try:
            return detect(window, cfg)
        except Exception:
            return None
    except Exception:
        return None


def _price_of(bar: pd.Series, column: str) -> float | None:
    try:
        value = float(bar[column])
    except (TypeError, ValueError, KeyError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return value


def _thin_clusters(marks: list[WyckoffMark], *, min_gap: int) -> list[WyckoffMark]:
    """同类事件连续成立时只留分数最高的一根。

    按 kind 分组，用**K 线行号**判断间隔：相邻两次同类事件相距不足
    ``min_gap`` 根时视作一簇。用行号而非日历天，因为停牌会拉开日历距离；
    也不能用"命中序"，那会把相隔很远的两次事件误并成一簇。
    """
    if not marks:
        return []
    kept: list[WyckoffMark] = []
    for kind in sorted({m.kind for m in marks}):
        same = sorted((m for m in marks if m.kind == kind), key=lambda m: m.bar)
        cluster: list[WyckoffMark] = [same[0]]
        for mark in same[1:]:
            # 按真实 K 线行号判距离：停牌拉开日历天，但行号仍连续。
            if mark.bar - cluster[-1].bar < min_gap:
                cluster.append(mark)
                continue
            kept.append(max(cluster, key=lambda m: m.score))
            cluster = [mark]
        kept.append(max(cluster, key=lambda m: m.score))
    return sorted(kept, key=lambda m: (m.bar, m.kind))
