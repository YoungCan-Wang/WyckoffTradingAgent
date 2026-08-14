"""
L3 候选排名工具 + 触发器标签常量。

综合动量、缩量、触发信号、板块共振等维度对候选股打分排名。
"""

from __future__ import annotations

import pandas as pd

from core._price_math import to_numeric as _to_numeric
from core.candidate_policy import candidate_score_value
from core.sector_rotation import SECTOR_STATE_SCORE_BONUS
from core.trend_drawdown_risk import classify_trend_drawdown, classify_trend_drawdown_pct

# ── 全局常量 ──

TRIGGER_LABELS = {
    "sos": "SOS（量价点火）",
    "spring": "Spring（终极震仓）",
    "lps": "LPS（缩量回踩）",
    "evr": "Effort vs Result（放量不跌）",
    "compression": "Compression（压缩蓄势）",
    "trend_pullback": "TrendPB（趋势回踩）",
    "early_breakout": "早期突破（潜在大涨）",
    "launchpad": "Launchpad（主升预备）",
    "tight_base": "Tight Base（强势平台）",
    "accumulation_ready": "低位转强（吸筹完成）",
}

TRIGGER_SHORT_LABELS = {
    "sos": "SOS",
    "spring": "Spring",
    "lps": "LPS",
    "evr": "EVR",
    "compression": "Compress",
    "trend_pullback": "TrendPB",
    "early_breakout": "EarlyBO",
    "launchpad": "Launchpad",
    "tight_base": "TightBase",
    "accumulation_ready": "AccumReady",
}

# 分组展示优先级：SOS > EVR > Spring > LPS > TrendPB > Compression
TRIGGER_GROUP_ORDER = [
    "early_breakout",
    "launchpad",
    "tight_base",
    "sos",
    "trend_pullback",
    "accumulation_ready",
    "evr",
    "spring",
    "lps",
    "compression",
]
TRIGGER_GROUP_TITLES = {
    "early_breakout": "🚀 早期突破",
    "launchpad": "🛫 主升预备",
    "tight_base": "📦 强势平台",
    "sos": "⚡ SOS 量价点火",
    "evr": "📊 EVR 放量不跌",
    "spring": "🌀 Spring 终极震仓",
    "lps": "🔄 LPS 缩量回踩",
    "trend_pullback": "📈 TrendPB 趋势回踩",
    "compression": "🔻 Compression 压缩蓄势",
    "accumulation_ready": "🌱 低位转强",
}


def calc_close_return_pct(close_series: pd.Series, lookback: int) -> float | None:
    """计算 close 序列的 N 日收益率（%）。"""
    s = _to_numeric(close_series).dropna()
    lb = max(int(lookback), 1)
    if len(s) <= lb:
        return None
    start = float(s.iloc[-lb - 1])
    end = float(s.iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100.0


def _trigger_score_map(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, float]:
    score_map: dict[str, float] = {}
    for key in TRIGGER_LABELS:
        for code, score in triggers.get(key, []):
            code_s = str(code).strip()
            if code_s:
                score_map[code_s] = max(candidate_score_value(score_map.get(code_s)), candidate_score_value(score))
    return score_map


def _candidate_metrics(
    df: pd.DataFrame | None,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    if df is None or df.empty:
        return None, None, None, None, None
    frame = df.sort_values("date")
    close = pd.to_numeric(frame.get("close"), errors="coerce")
    volume = pd.to_numeric(frame.get("volume"), errors="coerce")
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_ma20.replace(0, pd.NA)
    min_vol_ratio_5d = pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()
    return (
        calc_close_return_pct(close, 20),
        calc_close_return_pct(close, 5),
        calc_close_return_pct(close, 3),
        min_vol_ratio_5d,
        (risk.drawdown_pct if (risk := classify_trend_drawdown(close)) else None),
    )


def _candidate_rank_rows(
    l3_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    trigger_score_map: dict[str, float],
    l2_channel_map: dict[str, str] | None,
    sector_rotation_map: dict[str, dict] | None,
) -> list[dict]:
    rows: list[dict] = []
    channel_map = l2_channel_map or {}
    rotation_map = sector_rotation_map or {}
    for code in l3_symbols:
        industry = str(sector_map.get(code, "") or "未知行业")
        ret20, ret5, ret3, min_vol_ratio_5d, drawdown60 = _candidate_metrics(df_map.get(code))
        rows.append(
            {
                "code": code,
                "industry": industry,
                "ret20": ret20,
                "ret5": ret5,
                "ret3": ret3,
                "min_vol_ratio_5d": min_vol_ratio_5d,
                "drawdown60": drawdown60,
                "trigger_score": candidate_score_value(trigger_score_map.get(code)),
                "l2_channel": str(channel_map.get(code, "") or "未标注通道"),
                "sector_state": str((rotation_map.get(industry, {}) or {}).get("state", "") or ""),
            }
        )
    return rows


def _fill_rank_inputs(rank_df: pd.DataFrame) -> pd.DataFrame:
    for col, fill_default in (("ret20", 0.0), ("ret5", 0.0), ("ret3", 0.0), ("min_vol_ratio_5d", 1.0)):
        rank_df[col] = pd.to_numeric(rank_df[col], errors="coerce")
        if rank_df[col].notna().any():
            rank_df[col] = rank_df[col].fillna(float(rank_df[col].median()))
        else:
            rank_df[col] = rank_df[col].fillna(fill_default)
    return rank_df


def _add_rank_quantiles(rank_df: pd.DataFrame) -> pd.DataFrame:
    rank_df["q20"] = rank_df["ret20"].rank(pct=True, ascending=True, method="average")
    rank_df["q5"] = rank_df["ret5"].rank(pct=True, ascending=True, method="average")
    rank_df["q3"] = rank_df["ret3"].rank(pct=True, ascending=True, method="average")
    rank_df["dry_q"] = rank_df["min_vol_ratio_5d"].rank(pct=True, ascending=False, method="average")
    if rank_df["trigger_score"].nunique(dropna=False) > 1:
        rank_df["trigger_q"] = rank_df["trigger_score"].rank(pct=True, ascending=True, method="average")
    else:
        rank_df["trigger_q"] = rank_df["trigger_score"].apply(lambda x: 1.0 if float(x) > 0 else 0.0)
    return rank_df


def _add_watch_score(rank_df: pd.DataFrame, top_sectors: list[str]) -> pd.DataFrame:
    hot_sector_set = set(top_sectors or [])
    rank_df["hot_bonus"] = rank_df["industry"].isin(hot_sector_set).astype(float) * 0.02
    rank_df["sector_bonus"] = rank_df["sector_state"].map(lambda x: float(SECTOR_STATE_SCORE_BONUS.get(str(x), 0.0)))
    rank_df["extension_penalty"] = _extension_penalty_series(rank_df)
    rank_df["drawdown_risk_penalty"] = _drawdown_risk_penalty_series(rank_df)
    rank_df["watch_score"] = (
        0.25 * rank_df["q20"]
        + 0.20 * rank_df["q5"]
        + 0.05 * rank_df["q3"]
        + 0.20 * rank_df["dry_q"]
        + 0.30 * rank_df["trigger_q"]
        + rank_df["hot_bonus"]
        + rank_df["sector_bonus"]
        - rank_df["extension_penalty"]
        - rank_df["drawdown_risk_penalty"]
    )
    return rank_df


def _extension_penalty_series(rank_df: pd.DataFrame) -> pd.Series:
    ret20 = _numeric_rank_column(rank_df, "ret20")
    ret5 = _numeric_rank_column(rank_df, "ret5")
    ret20_penalty = ((ret20 - 45.0) / 55.0).clip(lower=0.0, upper=1.0) * 0.30
    ret5_penalty = ((ret5 - 18.0) / 22.0).clip(lower=0.0, upper=1.0) * 0.10
    return ret20_penalty + ret5_penalty


def _numeric_rank_column(rank_df: pd.DataFrame, column: str) -> pd.Series:
    if column not in rank_df:
        return pd.Series(0.0, index=rank_df.index)
    return pd.to_numeric(rank_df[column], errors="coerce").fillna(0.0)


def _drawdown_risk_penalty_series(rank_df: pd.DataFrame) -> pd.Series:
    drawdown = _numeric_rank_column(rank_df, "drawdown60")
    values = drawdown.map(lambda value: classify_trend_drawdown_pct(value).rank_penalty)
    is_trend_cont = rank_df["l2_channel"].astype(str).str.contains("趋势延续", regex=False)
    return values.where(is_trend_cont, 0.0)


def rank_l3_candidates(
    l3_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    triggers: dict[str, list[tuple[str, float]]],
    top_sectors: list[str],
    l2_channel_map: dict[str, str] | None = None,
    sector_rotation_map: dict[str, dict] | None = None,
) -> tuple[list[str], dict[str, float]]:
    """
    对 L3 股票做统一优先级排序，仅用于 AI 输入队列。

    打分权重：
      0.25 * q20 (20日动量) + 0.20 * q5 (5日) + 0.05 * q3 (3日)
      + 0.20 * dry_q (缩量程度) + 0.30 * trigger_q (Wyckoff 触发强度)
      + hot_bonus (热门板块) + sector_bonus (板块轮动状态)
      - extension_penalty (短线过热/加速延展)
      - drawdown_risk_penalty (60 日历史波动风险，非准入门槛)
    """
    if not l3_symbols:
        return ([], {})

    rank_df = pd.DataFrame(
        _candidate_rank_rows(
            l3_symbols,
            df_map,
            sector_map,
            _trigger_score_map(triggers),
            l2_channel_map,
            sector_rotation_map,
        )
    )
    rank_df = _add_watch_score(_add_rank_quantiles(_fill_rank_inputs(rank_df)), top_sectors)
    rank_df = rank_df.sort_values(
        by=["watch_score", "trigger_score", "extension_penalty", "ret20", "ret5", "ret3", "min_vol_ratio_5d", "code"],
        ascending=[False, False, True, False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)
    ranked_symbols = rank_df["code"].astype(str).tolist()
    score_map = {str(r["code"]): float(r["watch_score"]) for _, r in rank_df.iterrows()}
    return (ranked_symbols, score_map)
