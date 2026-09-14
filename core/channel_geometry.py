"""价格通道几何：滚动最小二乘回归通道。

来源是雪球作者「趋势前沿」的两句纲领——趋势由价格通道指示、目标由黄金分割线逐线指示
（issue #429）。他手工在 swing 高低点上摆平行轨，这里做客观版本：中轴取窗口内 close 的
最小二乘直线，上下轨取残差的最大/最小值平移，即「最小二乘中轴 + 宽度扩张」（#429 §3）。

与 `wyckoff_engine._creek_line` 的关系：那里是「最近 5 个 swing 高点取两点斜率」的原始
供给线，只有单侧、只用两点、且带斜率上限。这里是它的推广（双轨 + 平行约束 + 触轨计数），
但**不复用**它的 swing 点：swing 分形在个股上每窗口只有 2~4 个点，5000 只 × 500 天的
面板上既慢又不稳；回归通道用全窗口样本，能向量化。

本模块只做纯函数，不落库、不进漏斗。是否值得进生产由 IC 实测决定
（scripts/scan_factor_ic.py 的 chan_* 因子）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_WINDOW = 120
# 轨用「截至 lag 个 bar 之前」的窗口拟合,再外推到当日判位置。
# lag=0 时上下轨就是含当日残差的极值,价格被构造性地锁在通道内(实测 pos 恒为 0~100),
# 「突破上轨/跌破下轨」根本无法表达——而这正是 #429 要否决的那类状态。
# 人的画法本就是先在历史上画线、再看今天站在线的哪一侧,故默认留 5 个 bar 的外推段。
DEFAULT_LAG = 5
# 残差落在极值 15% 带内算一次触轨。手工画线时「碰到轨」本就是目测,不存在精确值。
TOUCH_BAND = 0.15
# 通道宽度低于收盘价的 0.01% 视作没有通道。不能只判 span>0:完美直线的残差是 1e-14 级的
# 浮点噪声,span 为正但 pos=(close-下轨)/span 会放大成任意值,长期停牌与一字板窗口都会中招。
# 真实通道宽度中位数在 30% 量级,这条下限离它有三个数量级余量。
MIN_SPAN_PCT = 0.01
FIB_LEVELS = (0.382, 0.5, 0.618, 0.786, 1.0)
# 越过窗口高点后按 1.618 扩展位给目标,否则 fib_room 在突破时无定义。
FIB_EXTENSION = 1.618
_PANEL_KEYS = ("slope", "pos", "span", "touch", "r2", "fib")


@dataclass(frozen=True)
class ChannelPanels:
    """逐日逐股的通道量,index=日期,columns=代码。"""

    slope: pd.DataFrame  # 中轴斜率,每 bar 占价格的百分比
    pos: pd.DataFrame  # 收盘在通道内的位置,0=下轨 100=上轨,突破可越界
    width: pd.DataFrame  # (上轨-下轨)/close*100
    r2: pd.DataFrame  # 中轴拟合度,越高说明「通道清晰」
    touches: pd.DataFrame  # 上下轨触碰次数之和
    fib_room: pd.DataFrame  # 到上方下一条黄金分割线的距离,占 close 的百分比


def _fit_window(
    block: np.ndarray, close_now: np.ndarray, x_dev: np.ndarray, sxx: float, lag: int
) -> dict[str, np.ndarray]:
    """在 (window, n_codes) 的拟合块上逐列做最小二乘,再把轨外推 lag 个 bar 判当日位置。

    含 NaN 的列(停牌、上市不足窗口)由 NaN 传播自动作废,不插补——补出来的轨是假的。
    """
    ybar = block.mean(axis=0)
    slope = (x_dev[:, None] * (block - ybar)).sum(axis=0) / sxx
    resid = block - (ybar + slope * x_dev[:, None])
    hi, lo = resid.max(axis=0), resid.min(axis=0)
    span = hi - lo
    axis_now = ybar + slope * (x_dev[-1] + lag)
    with np.errstate(invalid="ignore", divide="ignore"):
        live = span > np.abs(close_now) * (MIN_SPAN_PCT / 100.0)
        pos = np.where(live, (close_now - (axis_now + lo)) / span * 100.0, np.nan)
        band = span * TOUCH_BAND
        touch = np.where(live, ((resid >= hi - band) | (resid <= lo + band)).sum(axis=0), np.nan)
        ss_tot = ((block - ybar) ** 2).sum(axis=0)
        r2 = np.where(ss_tot > 0, 1.0 - (resid**2).sum(axis=0) / ss_tot, np.nan)
    return {"slope": slope, "pos": pos, "span": span, "touch": touch, "r2": r2, "fib": _fib_room(block, close_now)}


def _fib_room(block: np.ndarray, close: np.ndarray) -> np.ndarray:
    """到上方下一条黄金分割线的距离(%)。分割区间取拟合窗口内的最低-最高。"""
    lo, hi = block.min(axis=0), block.max(axis=0)
    rng = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        gaps = np.full_like(close, np.inf)
        for frac in FIB_LEVELS:
            gap = (lo + rng * frac) - close
            gaps = np.where((gap > 0) & (gap < gaps), gap, gaps)
        ext = (lo + rng * FIB_EXTENSION) - close
        gaps = np.where(np.isfinite(gaps), gaps, ext)
        return np.where(close > 0, gaps / close * 100.0, np.nan)


def regression_channel_panels(
    close: pd.DataFrame, window: int = DEFAULT_WINDOW, lag: int = DEFAULT_LAG
) -> ChannelPanels:
    """滚动回归通道。按日期循环、在代码维度向量化:窗口内的残差极值依赖该窗口自身的拟合,
    无法用 rolling max 预计算,只能逐日重算;5000 列 × 500 日在 numpy 下是秒级。

    只用 T 日及之前的数据:拟合块的最后一根是 T-lag,当日 close 只作为被判位置的点。
    """
    values = close.to_numpy(dtype=float)
    n_rows = values.shape[0]
    x_dev = np.arange(window, dtype=float) - (window - 1) / 2.0
    sxx = float((x_dev**2).sum())
    out = {key: np.full_like(values, np.nan) for key in _PANEL_KEYS}
    for end in range(window - 1 + lag, n_rows):
        stop = end - lag + 1
        fitted = _fit_window(values[stop - window : stop], values[end], x_dev, sxx, lag)
        invalid = ~np.isfinite(fitted["slope"])
        for key in _PANEL_KEYS:
            out[key][end] = np.where(invalid, np.nan, fitted[key])
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(values > 0, values, np.nan)
        slope_pct = out["slope"] / scale * 100.0
        width_pct = out["span"] / scale * 100.0
    return ChannelPanels(
        slope=_as_frame(slope_pct, close),
        pos=_as_frame(out["pos"], close),
        width=_as_frame(width_pct, close),
        r2=_as_frame(out["r2"], close),
        touches=_as_frame(out["touch"], close),
        fib_room=_as_frame(out["fib"], close),
    )


def _as_frame(values: np.ndarray, like: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(values, index=like.index, columns=like.columns)
