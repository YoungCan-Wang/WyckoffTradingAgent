"""Medium-term institutional Slow-Bull (Frog-in-the-Pan / high log-R^2) pure engine.

Decoupled from short-term 5-day Step4 OMS execution and intraday radar.
Provides pure cross-sectional and single-symbol diagnostics for:
- Stage A (``slow_bull_emerge``): low-vol base -> gentle breakout emergence
- Stage B (``slow_bull_pullback`` / ``slow_bull_watch``): smooth FIP trend + pullback
- Institutional liquidity & financial moat filtering (``Moat_Full``)
- Market rotation climate risk tag (``tech_rotation_warning``)
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.cn_boards import is_supported_cn_board
from core.limit_move import is_st_risk_warning, limit_pct
from core.trade_friction import round_trip_cost_pct
from core.trend_drawdown_risk import max_drawdown_pct

FIN_EXEMPT_SECTORS: frozenset[str] = frozenset({"银行", "保险", "证券", "多元金融"})
_STAGE_A_TYPE = "slow_bull_emerge"
_STAGE_B_PULLBACK_TYPE = "slow_bull_pullback"
_STAGE_B_WATCH_TYPE = "slow_bull_watch"
_LIMIT_TOUCH_TOLERANCE = 0.015


@dataclass(frozen=True)
class SlowBullConfig:
    """Thresholds verified across 2020-07..2026-11 A-share PIT research."""

    include_bse: bool = True
    min_bars_stage_a: int = 120
    min_bars_stage_b: int = 65
    min_close_price: float = 2.0

    # Institutional Moat
    min_market_cap_yi: float = 150.0
    require_market_cap: bool = True
    min_avg_amount_20d_yi: float = 1.50
    min_amount_median_ratio_20d: float = 0.45
    min_turnover_20d_pct: float = 0.30
    require_turnover: bool = False
    min_annualized_roe_pct: float = 6.0
    max_debt_ratio_pct: float = 80.0
    min_dividend_ttm_pct: float = 2.0
    allow_cycle_cashflow_bypass: bool = False
    cycle_bypass_min_ocf_to_rev_pct: float = 10.0

    # Stage A: Early emergence (StageA_Strict)
    prior_base_max_range_pct: float = 24.0
    prior_base_max_amp_pct: float = 3.0
    prior_ma_convergence_max_pct: float = 4.0
    emerge_vol_ratio_min: float = 1.20
    emerge_vol_ratio_max: float = 1.85
    emerge_max_1d_gain_15d_pct: float = 3.5
    rps50_current_range: tuple[float, float] = (65.0, 85.0)
    rps50_prior_min_range: tuple[float, float] = (25.0, 55.0)
    rs_near_high_ratio: float = 0.985

    # Stage B: In-progress Slow Bull + Pullback
    min_log_r2_40: float = 0.80
    min_ret_40d_pct: float = 8.0
    min_yang_ratio_40: float = 0.55
    max_limit_up_40d: int = 0
    max_1d_gain_40d_pct: float = 4.5
    max_mean_amp_40d_pct: float = 3.0
    max_drawdown_40d_pct: float = 5.0
    pullback_vol_ratio_max: float = 0.80
    pullback_dist_ma20_max_pct: float = 5.5
    pullback_ma5_band_pct: float = 1.5

    # Market climate warning
    chinext_20d_surge_pct: float = 12.0
    chinext_vs_csi300_20d_spread_pct: float = 6.0
    market_amount_5d_20d_ratio_warn: float = 1.25


DEFAULT_SLOW_BULL_CONFIG = SlowBullConfig()


@dataclass(frozen=True)
class SlowBullScanResult:
    """Structured output returned by :func:`scan_slow_bull_candidates`."""

    trade_date: str
    counts: dict[str, int]
    climate: dict[str, Any]
    emerge_candidates: list[dict[str, Any]]
    pullback_candidates: list[dict[str, Any]]
    watch_candidates: list[dict[str, Any]]
    friction_round_trip_pct: float
    exit_guidance: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pick_metric(metrics: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not metrics:
        return None
    for key in keys:
        if key in metrics:
            val = _finite_float(metrics.get(key))
            if val is not None:
                return val
    return None


def ols_signed_log_r2(closes: np.ndarray | pd.Series | list[float]) -> tuple[float, float]:
    """Compute signed OLS R^2 of ln(close) against bar index ``0..n-1``."""
    arr = np.asarray(closes, dtype=np.float64)
    arr = arr[np.isfinite(arr) & (arr > 0.0)]
    if len(arr) < 2:
        return 0.0, 0.0
    y = np.log(arr)
    x = np.arange(len(y), dtype=np.float64)
    dx = x - x.mean()
    dy = y - y.mean()
    sxx = float(np.dot(dx, dx))
    ss_tot = float(np.dot(dy, dy))
    if sxx <= 1e-12 or ss_tot <= 1e-12:
        return 0.0, 0.0
    sxy = float(np.dot(dx, dy))
    slope = sxy / sxx
    r2 = min(max((sxy * sxy) / (sxx * ss_tot), 0.0), 1.0)
    return (r2 if slope > 0.0 else -r2), slope


def annualize_roe(roe_val: float | None, period_end: str | None) -> float | None:
    """Scale YTD cumulative ROE to annualized ROE from report period month."""
    roe = _finite_float(roe_val)
    if roe is None:
        return None
    digits = "".join(ch for ch in str(period_end or "") if ch.isdigit())
    month = digits[4:6] if len(digits) >= 6 else ""
    factor = {"03": 4.0, "06": 2.0, "09": 4.0 / 3.0, "12": 1.0}.get(month, 1.0)
    return round(roe * factor, 4)


def _prepare_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    work = df.copy()
    if "date" in work.columns and not work["date"].is_monotonic_increasing:
        work = work.sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "prev_close", "volume", "amount", "pct_chg", "turnover"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work[work["close"].notna() & (work["close"] > 0)].reset_index(drop=True)
    if work.empty:
        return None
    if "prev_close" not in work.columns or work["prev_close"].isna().all():
        work["prev_close"] = work["close"].shift(1)
    if "pct_chg" not in work.columns or work["pct_chg"].isna().all():
        prev = work["prev_close"]
        work["pct_chg"] = np.where(prev > 0, (work["close"] / prev - 1.0) * 100.0, 0.0)
    else:
        fill = np.where(work["prev_close"] > 0, (work["close"] / work["prev_close"] - 1.0) * 100.0, 0.0)
        work["pct_chg"] = work["pct_chg"].fillna(pd.Series(fill, index=work.index))
    return work


def _mean_turnover_20d(df: pd.DataFrame, turnover_pct: float | None) -> float | None:
    explicit = _finite_float(turnover_pct)
    if explicit is not None:
        return explicit
    if "turnover" not in df.columns:
        return None
    tail = pd.to_numeric(df["turnover"].tail(20), errors="coerce").dropna()
    return float(tail.mean()) if not tail.empty else None


def _check_financial_quality(
    *,
    sector: str,
    financial: Mapping[str, Any] | None,
    dv_ttm_pct: float | None,
    cfg: SlowBullConfig,
) -> tuple[bool, list[str], dict[str, Any]]:
    is_fin = sector.strip() in FIN_EXEMPT_SECTORS
    if not financial:
        return True, [], {"roe_annualized_pct": None, "financial_exempt_sector": is_fin, "financial_known": False}
    period_end = str(financial.get("period_end") or financial.get("end_date") or "")
    roe_raw = _pick_metric(financial, "roe", "roe_diluted", "roe_dt")
    roe_ann = annualize_roe(roe_raw, period_end)
    debt = _pick_metric(financial, "debt_to_asset_ratio", "debt_to_assets", "debt_ratio")
    ocf = _pick_metric(financial, "operating_cash_to_revenue", "ocf_to_or")
    reasons: list[str] = []
    cycle_bypass = False
    if roe_ann is None or roe_ann < cfg.min_annualized_roe_pct:
        can_bypass = cfg.allow_cycle_cashflow_bypass and ocf is not None and ocf >= cfg.cycle_bypass_min_ocf_to_rev_pct
        if can_bypass:
            cycle_bypass = True
        else:
            reasons.append(f"年化ROE<{cfg.min_annualized_roe_pct:.1f}%")
    if not is_fin and (debt is None or debt > cfg.max_debt_ratio_pct):
        reasons.append(f"资产负债率>{cfg.max_debt_ratio_pct:.0f}%")
    dv = _finite_float(dv_ttm_pct)
    cash_ok = is_fin or (ocf is not None and ocf > 0.0) or (dv is not None and dv >= cfg.min_dividend_ttm_pct)
    if not cash_ok:
        reasons.append(f"经营现金流<=0且股息率<{cfg.min_dividend_ttm_pct:.1f}%")
    info = {
        "roe_annualized_pct": roe_ann,
        "debt_to_asset_ratio": debt,
        "operating_cash_to_revenue": ocf,
        "dv_ttm_pct": dv,
        "financial_exempt_sector": is_fin,
        "cycle_cashflow_bypass": cycle_bypass,
        "financial_known": True,
    }
    return len(reasons) == 0, reasons, info


def evaluate_slow_bull_moat(
    df: pd.DataFrame,
    *,
    market_cap_yi: float | None = None,
    dv_ttm_pct: float | None = None,
    turnover_pct: float | None = None,
    sector: str = "",
    financial: Mapping[str, Any] | None = None,
    cfg: SlowBullConfig = DEFAULT_SLOW_BULL_CONFIG,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Evaluate capital, liquidity steadiness, and financial quality moat."""
    reasons: list[str] = []
    mv = _finite_float(market_cap_yi)
    if mv is None:
        if cfg.require_market_cap:
            reasons.append(f"市值缺失(要求>={cfg.min_market_cap_yi:.0f}亿)")
    elif mv < cfg.min_market_cap_yi:
        reasons.append(f"市值{mv:.1f}亿<{cfg.min_market_cap_yi:.0f}亿")
    amt_tail = pd.to_numeric(df.get("amount", pd.Series(dtype=float)).tail(20), errors="coerce").dropna()
    amt20_yi = float(amt_tail.mean() / 1e8) if not amt_tail.empty else None
    amt_med_ratio = float(amt_tail.median() / amt_tail.mean()) if not amt_tail.empty and amt_tail.mean() > 0 else None
    if amt20_yi is None or amt20_yi < cfg.min_avg_amount_20d_yi:
        reasons.append(f"20日均成交额<{cfg.min_avg_amount_20d_yi:.2f}亿")
    if amt_med_ratio is None or amt_med_ratio < cfg.min_amount_median_ratio_20d:
        reasons.append(f"成交额中位均值比<{cfg.min_amount_median_ratio_20d:.2f}")
    turn20 = _mean_turnover_20d(df, turnover_pct)
    if turn20 is None:
        if cfg.require_turnover:
            reasons.append("换手率缺失")
    elif turn20 < cfg.min_turnover_20d_pct:
        reasons.append(f"换手率{turn20:.2f}%<{cfg.min_turnover_20d_pct:.2f}%")
    fin_ok, fin_reasons, fin_info = _check_financial_quality(
        sector=sector,
        financial=financial,
        dv_ttm_pct=dv_ttm_pct,
        cfg=cfg,
    )
    if not fin_ok:
        reasons.extend(fin_reasons)
    metrics = {
        "market_cap_yi": mv,
        "amt20_yi": round(amt20_yi, 3) if amt20_yi is not None else None,
        "amt20_med_ratio": round(amt_med_ratio, 3) if amt_med_ratio is not None else None,
        "turnover20_pct": round(turn20, 3) if turn20 is not None else None,
        **fin_info,
    }
    return len(reasons) == 0, reasons, metrics


def _is_eligible_frame(code: str, name: str, df: pd.DataFrame | None, cfg: SlowBullConfig) -> bool:
    if df is None or len(df) < cfg.min_bars_stage_b:
        return False
    if not is_supported_cn_board(code, include_bse=cfg.include_bse):
        return False
    if is_st_risk_warning(code, name):
        return False
    last_close = _finite_float(df["close"].iloc[-1])
    return last_close is not None and last_close >= cfg.min_close_price


def compute_cross_sectional_rps50_history(
    all_df_map: Mapping[str, pd.DataFrame],
    *,
    name_map: Mapping[str, str] | None = None,
    cfg: SlowBullConfig = DEFAULT_SLOW_BULL_CONFIG,
    history_days: int = 21,
) -> tuple[dict[str, tuple[float | None, float | None]], dict[str, int]]:
    """Compute PIT-style cross-sectional RPS50 current and [t-20..t-10] min."""
    names = name_map or {}
    eligible_closes: dict[str, pd.Series] = {}
    for code, raw_df in all_df_map.items():
        frame = _prepare_ohlcv(raw_df)
        if (
            frame is None
            or "date" not in frame.columns
            or not _is_eligible_frame(code, names.get(code, ""), frame, cfg)
        ):
            continue
        series = frame.drop_duplicates(subset=["date"]).set_index("date")["close"]
        eligible_closes[code] = series
    if not eligible_closes:
        return {}, {"eligible_symbols": 0}
    panel = pd.DataFrame(eligible_closes).sort_index().tail(max(history_days + 55, 80))
    ret50 = (panel / panel.shift(50) - 1.0) * 100.0
    rps = ret50.rank(axis=1, pct=True, ascending=True, method="average") * 100.0
    if rps.empty:
        return {}, {"eligible_symbols": len(eligible_closes)}
    latest = rps.iloc[-1]
    prior_window = rps.iloc[-21:-10] if len(rps) >= 21 else rps.iloc[:-1]
    prior_min = prior_window.min(axis=0) if not prior_window.empty else pd.Series(dtype=float)
    out = {code: (_finite_float(latest.get(code)), _finite_float(prior_min.get(code))) for code in eligible_closes}
    latest_valid = int(latest.notna().sum())
    return out, {"eligible_symbols": len(eligible_closes), "latest_rps_universe": latest_valid}


def detect_tech_rotation_warning(
    csi300_df: pd.DataFrame | None,
    chinext_df: pd.DataFrame | None,
    *,
    all_df_map: Mapping[str, pd.DataFrame] | None = None,
    cfg: SlowBullConfig = DEFAULT_SLOW_BULL_CONFIG,
) -> dict[str, Any]:
    """Flag fast high-beta tech-bull rotation risk for defensive slow-bull holdings."""
    csi_ret = _tail_return_pct(csi300_df, 20)
    cyb_ret = _tail_return_pct(chinext_df, 20)
    spread = round(cyb_ret - csi_ret, 2) if csi_ret is not None and cyb_ret is not None else None
    amt_ratio = _market_amount_ratio(all_df_map)
    surge_hit = cyb_ret is not None and cyb_ret >= cfg.chinext_20d_surge_pct
    spread_hit = spread is not None and spread >= cfg.chinext_vs_csi300_20d_spread_pct
    amt_ok = amt_ratio is None or amt_ratio >= cfg.market_amount_5d_20d_ratio_warn
    warned = bool(surge_hit and spread_hit and amt_ok)
    advisory = (
        "创业板显著放量领跑沪深300：高股息/低波慢牛阶段 B 底仓面临阶段性资金抽血滞涨风险，控仓并优先保留阶段 A 刚启动标的"
        if warned
        else "未触发高弹性科技抽血预警，低波慢牛底仓结构正常"
    )
    return {
        "tech_rotation_warning": warned,
        "chinext_ret_20d_pct": round(cyb_ret, 2) if cyb_ret is not None else None,
        "csi300_ret_20d_pct": round(csi_ret, 2) if csi_ret is not None else None,
        "spread_20d_pct": spread,
        "market_amt_5d_20d_ratio": round(amt_ratio, 3) if amt_ratio is not None else None,
        "advisory": advisory,
    }


def _tail_return_pct(df: pd.DataFrame | None, window: int) -> float | None:
    prep = _prepare_ohlcv(df)
    if prep is None or len(prep) < window + 1:
        return None
    base = _finite_float(prep["close"].iloc[-(window + 1)])
    end = _finite_float(prep["close"].iloc[-1])
    if base is None or end is None or base <= 0:
        return None
    return (end / base - 1.0) * 100.0


def _market_amount_ratio(all_df_map: Mapping[str, pd.DataFrame] | None) -> float | None:
    if not all_df_map:
        return None
    totals = np.zeros(20, dtype=np.float64)
    counts = np.zeros(20, dtype=np.int32)
    for df in all_df_map.values():
        if df is None or "amount" not in df.columns or len(df) < 20:
            continue
        vals = pd.to_numeric(df["amount"].tail(20), errors="coerce").to_numpy(dtype=np.float64)
        mask = np.isfinite(vals) & (vals > 0)
        totals[mask] += vals[mask]
        counts[mask] += 1
    if int((counts > 0).sum()) < 20 or totals.mean() <= 0:
        return None
    return float(totals[-5:].mean() / totals.mean())


def _aligned_rs_stats(df: pd.DataFrame, csi300_df: pd.DataFrame | None) -> dict[str, float | bool | None]:
    if csi300_df is None or csi300_df.empty or "close" not in csi300_df.columns:
        return {"excess_csi300_40d_pct": None, "rs_new_high": None, "rs_near_high": None}
    csi = _prepare_ohlcv(csi300_df)
    if csi is None or len(csi) < 41:
        return {"excess_csi300_40d_pct": None, "rs_new_high": None, "rs_near_high": None}
    if "date" in df.columns and "date" in csi.columns:
        merged = pd.merge(df[["date", "close"]], csi[["date", "close"]], on="date", suffixes=("", "_bm"))
    else:
        n = min(len(df), len(csi))
        merged = pd.DataFrame({"close": df["close"].tail(n).values, "close_bm": csi["close"].tail(n).values})
    if len(merged) < 41:
        return {"excess_csi300_40d_pct": None, "rs_new_high": None, "rs_near_high": None}
    rs_line = (merged["close"] / merged["close_bm"]).to_numpy(dtype=np.float64)
    stock_ret40 = (float(merged["close"].iloc[-1]) / float(merged["close"].iloc[-41]) - 1.0) * 100.0
    bm_ret40 = (float(merged["close_bm"].iloc[-1]) / float(merged["close_bm"].iloc[-41]) - 1.0) * 100.0
    rs_new_high = bool(len(rs_line) >= 70 and np.nanmax(rs_line[-10:]) >= np.nanmax(rs_line[-70:-10]))
    rs_near_high = bool(len(rs_line) >= 70 and rs_line[-1] >= 0.985 * np.nanmax(rs_line[-70:]))
    return {
        "excess_csi300_40d_pct": round(stock_ret40 - bm_ret40, 2),
        "rs_new_high": rs_new_high,
        "rs_near_high": rs_near_high,
    }


def _compute_symbol_metrics(
    code: str,
    name: str,
    df: pd.DataFrame,
    csi300_df: pd.DataFrame | None,
) -> dict[str, Any]:
    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else close
    low = df["low"].astype(float) if "low" in df.columns else close
    prev = df["prev_close"].astype(float)
    pct = df["pct_chg"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    amp = np.where(prev > 0, (high - low) / prev * 100.0, np.nan)
    ma5, ma10 = close.rolling(5).mean(), close.rolling(10).mean()
    ma20, ma60 = close.rolling(20).mean(), close.rolling(60).mean()
    ret_idx = -41 if len(close) >= 41 else -40
    ret40 = (float(close.iloc[-1]) / float(close.iloc[ret_idx]) - 1.0) * 100.0
    log_r2_40, _ = ols_signed_log_r2(close.tail(40))
    mdd40 = max_drawdown_pct(close, window=40)
    lim_val = float(limit_pct(code, name) or 10.0)
    lim_up_40 = int((pct.tail(40) >= lim_val * (1.0 - _LIMIT_TOUCH_TOLERANCE)).sum())
    yang_40 = float((pct.tail(40) > 0).mean())
    neg_40 = float((pct.tail(40) < 0).mean())
    fip_id_40 = (1.0 if ret40 >= 0 else -1.0) * (neg_40 - yang_40)
    vol_ma20 = float(vol.tail(20).mean()) if len(vol) >= 20 else 0.0
    vol_ratio_1_20 = float(vol.iloc[-1]) / vol_ma20 if vol_ma20 > 0 else None
    prior_slice = close.iloc[-75:-15] if len(close) >= 75 else pd.Series(dtype=float)
    prior_rng = (float(prior_slice.max()) / float(prior_slice.min()) - 1.0) * 100.0 if len(prior_slice) >= 20 else None
    prior_amp = float(np.nanmean(amp[-75:-15])) if len(amp) >= 75 else None
    ma_gap_series = (np.abs(ma20 - ma60) / ma60 * 100.0).iloc[-21:-10] if len(close) >= 80 else pd.Series(dtype=float)
    vol_prior = float(vol.iloc[-70:-10].mean()) if len(vol) >= 70 else 0.0
    rs_stats = _aligned_rs_stats(df, csi300_df)
    return {
        "trade_date": str(df["date"].iloc[-1]) if "date" in df.columns else "",
        "close": round(float(close.iloc[-1]), 3),
        "ma5": float(ma5.iloc[-1]),
        "ma10": float(ma10.iloc[-1]),
        "ma20": float(ma20.iloc[-1]),
        "ma20_prev5": float(ma20.iloc[-6]) if len(ma20) >= 6 else float(ma20.iloc[-1]),
        "ma60": float(ma60.iloc[-1]),
        "low_last": float(low.iloc[-1]),
        "ret_40d_pct": round(ret40, 2),
        "log_r2_40": round(log_r2_40, 4),
        "fip_id_40": round(fip_id_40, 4),
        "yang_ratio_40": round(yang_40, 4),
        "limit_up_40d": lim_up_40,
        "max_1d_40_pct": round(float(pct.tail(40).max()), 2),
        "max_1d_15_pct": round(float(pct.tail(15).max()), 2),
        "mean_amp_40_pct": round(float(np.nanmean(amp[-40:])), 2),
        "mdd_40_pct": round(float(mdd40 or 0.0), 2),
        "vol_ratio_1_20": round(vol_ratio_1_20, 3) if vol_ratio_1_20 is not None else None,
        "vol_ratio_10_60prior": round(float(vol.tail(10).mean()) / vol_prior, 3) if vol_prior > 0 else None,
        "dist_ma5_pct": round((float(close.iloc[-1]) / float(ma5.iloc[-1]) - 1.0) * 100.0, 2),
        "dist_ma10_pct": round((float(close.iloc[-1]) / float(ma10.iloc[-1]) - 1.0) * 100.0, 2),
        "dist_ma20_pct": round((float(close.iloc[-1]) / float(ma20.iloc[-1]) - 1.0) * 100.0, 2),
        "range_60_prior_pct": round(prior_rng, 2) if prior_rng is not None else None,
        "mean_amp_60_prior_pct": round(prior_amp, 2) if prior_amp is not None else None,
        "ma_gap_prior_pct": round(float(ma_gap_series.min()), 2) if not ma_gap_series.dropna().empty else None,
        **rs_stats,
    }


def _evaluate_stage_a(
    metrics: Mapping[str, Any],
    bars: int,
    rps50_current: float | None,
    rps50_prior_min: float | None,
    cfg: SlowBullConfig,
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if bars < cfg.min_bars_stage_a:
        return False, [f"有效K线<{cfg.min_bars_stage_a}日"]
    rng, amp = metrics.get("range_60_prior_pct"), metrics.get("mean_amp_60_prior_pct")
    if rng is None or rng > cfg.prior_base_max_range_pct:
        blockers.append(f"前期60日振幅箱体>{cfg.prior_base_max_range_pct:.0f}%")
    if amp is None or amp > cfg.prior_base_max_amp_pct:
        blockers.append(f"前期日均振幅>{cfg.prior_base_max_amp_pct:.1f}%")
    gap = metrics.get("ma_gap_prior_pct")
    if gap is None or gap > cfg.prior_ma_convergence_max_pct:
        blockers.append(f"前期MA20/MA60未粘合(>{cfg.prior_ma_convergence_max_pct:.0f}%)")
    if not (
        metrics["close"] > metrics["ma20"]
        and metrics["ma20"] >= metrics["ma60"] * 0.99
        and metrics["ma20"] > metrics["ma20_prev5"]
    ):
        blockers.append("未站稳MA20且均线未开始抬头")
    if metrics.get("rs_new_high") is not True or metrics.get("rs_near_high") is not True:
        blockers.append("相对沪深300强弱线未创60日新高")
    vr = metrics.get("vol_ratio_10_60prior")
    if vr is None or vr < cfg.emerge_vol_ratio_min or vr > cfg.emerge_vol_ratio_max:
        blockers.append(f"10/60日温和放量不在[{cfg.emerge_vol_ratio_min:.2f},{cfg.emerge_vol_ratio_max:.2f}]")
    if metrics["limit_up_40d"] > cfg.max_limit_up_40d or metrics["max_1d_15_pct"] > cfg.emerge_max_1d_gain_15d_pct:
        blockers.append(f"近15日最大单日涨幅>{cfg.emerge_max_1d_gain_15d_pct:.1f}%或存在涨停")
    lo_c, hi_c = cfg.rps50_current_range
    lo_p, hi_p = cfg.rps50_prior_min_range
    if rps50_current is None or not (lo_c <= rps50_current <= hi_c):
        blockers.append(f"RPS50不在[{lo_c:.0f},{hi_c:.0f}]")
    if rps50_prior_min is None or not (lo_p <= rps50_prior_min <= hi_p):
        blockers.append(f"前期RPS50不在[{lo_p:.0f},{hi_p:.0f}]")
    return len(blockers) == 0, blockers


def _evaluate_stage_b(metrics: Mapping[str, Any], cfg: SlowBullConfig) -> tuple[bool, bool, list[str]]:
    blockers: list[str] = []
    if metrics["log_r2_40"] < cfg.min_log_r2_40:
        blockers.append(f"40日对数R^2({metrics['log_r2_40']:.3f})<{cfg.min_log_r2_40:.2f}")
    if metrics["ret_40d_pct"] < cfg.min_ret_40d_pct:
        blockers.append(f"40日涨幅({metrics['ret_40d_pct']:.1f}%)<{cfg.min_ret_40d_pct:.0f}%")
    if metrics["yang_ratio_40"] < cfg.min_yang_ratio_40:
        blockers.append(f"阳线占比({metrics['yang_ratio_40']:.1%})<{cfg.min_yang_ratio_40:.0%}")
    if metrics["limit_up_40d"] > cfg.max_limit_up_40d or metrics["max_1d_40_pct"] > cfg.max_1d_gain_40d_pct:
        blockers.append(f"40日有涨停或单日涨幅>{cfg.max_1d_gain_40d_pct:.1f}%")
    if metrics["mean_amp_40_pct"] > cfg.max_mean_amp_40d_pct:
        blockers.append(f"40日均振幅({metrics['mean_amp_40_pct']:.2f}%)>{cfg.max_mean_amp_40d_pct:.1f}%")
    if metrics["mdd_40_pct"] > cfg.max_drawdown_40d_pct:
        blockers.append(f"40日最大回撤({metrics['mdd_40_pct']:.2f}%)>{cfg.max_drawdown_40d_pct:.1f}%")
    if not (metrics["ma5"] > metrics["ma20"] > metrics["ma60"]):
        blockers.append("未满足MA5>MA20>MA60")
    trend_ok = len(blockers) == 0
    vr = metrics.get("vol_ratio_1_20")
    ma5_touch = abs(metrics["dist_ma5_pct"]) <= cfg.pullback_ma5_band_pct
    ma10_touch = metrics["low_last"] <= metrics["ma10"] * 1.01 and metrics["close"] >= metrics["ma10"] * 0.99
    pullback_ok = bool(
        trend_ok
        and vr is not None
        and vr < cfg.pullback_vol_ratio_max
        and metrics["close"] >= metrics["ma20"] * 0.995
        and metrics["dist_ma20_pct"] <= cfg.pullback_dist_ma20_max_pct
        and (ma5_touch or ma10_touch)
    )
    if trend_ok and not pullback_ok:
        blockers.append("等待缩量回踩MA5/MA10(量比<0.80)")
    return trend_ok, pullback_ok, blockers


def _score_candidate(metrics: Mapping[str, Any], matched: list[str], moat: Mapping[str, Any]) -> float:
    base = 75.0 if _STAGE_A_TYPE in matched else (68.0 if _STAGE_B_PULLBACK_TYPE in matched else 55.0)
    r2_bonus = max(float(metrics.get("log_r2_40") or 0.0), 0.0) * 12.0
    mdd_bonus = max(5.0 - float(metrics.get("mdd_40_pct") or 5.0), 0.0) * 1.2
    roe = min(max(float(moat.get("roe_annualized_pct") or 6.0), 0.0), 25.0) * 0.25
    return round(min(base + r2_bonus + mdd_bonus + roe, 99.9), 2)


_EXIT_GUIDANCE = {
    _STAGE_A_TYPE: "全天候净超额主信号：建议 20~60 个交易日持有（基准 H=60d 扣费+3.89%，夏普0.545）",
    _STAGE_B_PULLBACK_TYPE: "防御底仓信号：建议 -7%硬止损 + +15%激活10%回撤止盈（基准卡玛0.498，最大回撤-14.85%）",
    "climate_rule": "触发 tech_rotation_warning 时仅优先阶段 A，严格控制阶段 B 静态底仓比例以防科技牛市滞涨抽血",
}


def _ineligible_diagnosis(code: str, name: str, sector: str) -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "sector": sector,
        "eligible": False,
        "signal_type": "none",
        "matched_stages": [],
        "moat_passed": False,
        "moat_blockers": ["未通过基础板块/ST/价格或至少65根有效K线门槛"],
        "stage_a_blockers": [],
        "stage_b_blockers": [],
        "research_only": True,
        "new_buy_allowed": False,
    }


def _resolve_matched_stages(moat_ok: bool, a_ok: bool, b_trend: bool, b_pullback: bool) -> tuple[list[str], str, str]:
    matched: list[str] = []
    if moat_ok and a_ok:
        matched.append(_STAGE_A_TYPE)
    if moat_ok and b_pullback:
        matched.append(_STAGE_B_PULLBACK_TYPE)
    elif moat_ok and b_trend:
        matched.append(_STAGE_B_WATCH_TYPE)
    signal_type = matched[0] if matched else "none"
    exit_plan = (
        "60日固定持有（实证净收益+3.89%，夏普0.545）"
        if signal_type == _STAGE_A_TYPE
        else "40~60日持有或移动止盈：-7%硬止损 + +15%激活10%回撤止盈（实证卡玛0.498，最大回撤-14.85%）"
    )
    return matched, signal_type, exit_plan


def diagnose_slow_bull_symbol(
    code: str,
    df: pd.DataFrame | None,
    *,
    name: str = "",
    csi300_df: pd.DataFrame | None = None,
    market_cap_yi: float | None = None,
    dv_ttm_pct: float | None = None,
    turnover_pct: float | None = None,
    sector: str = "",
    financial: Mapping[str, Any] | None = None,
    rps50_current: float | None = None,
    rps50_prior_min: float | None = None,
    cfg: SlowBullConfig = DEFAULT_SLOW_BULL_CONFIG,
) -> dict[str, Any]:
    """Evaluate one stock across institutional moat, Stage A emerge, and Stage B pullback."""
    prep = _prepare_ohlcv(df)
    if not _is_eligible_frame(code, name, prep, cfg):
        return _ineligible_diagnosis(code, name, sector)
    assert prep is not None
    moat_ok, moat_reasons, moat_metrics = evaluate_slow_bull_moat(
        prep,
        market_cap_yi=market_cap_yi,
        dv_ttm_pct=dv_ttm_pct,
        turnover_pct=turnover_pct,
        sector=sector,
        financial=financial,
        cfg=cfg,
    )
    metrics = _compute_symbol_metrics(code, name, prep, csi300_df)
    metrics["rps50_current"] = round(rps50_current, 2) if rps50_current is not None else None
    metrics["rps50_prior_min"] = round(rps50_prior_min, 2) if rps50_prior_min is not None else None
    a_ok, a_blockers = _evaluate_stage_a(metrics, len(prep), rps50_current, rps50_prior_min, cfg)
    b_trend, b_pullback, b_blockers = _evaluate_stage_b(metrics, cfg)
    matched, signal_type, exit_plan = _resolve_matched_stages(moat_ok, a_ok, b_trend, b_pullback)
    public_metrics = {
        k: v for k, v in metrics.items() if k not in {"ma5", "ma10", "ma20", "ma20_prev5", "ma60", "low_last"}
    }
    return {
        "code": code,
        "name": name,
        "sector": sector,
        "trade_date": metrics["trade_date"],
        "eligible": True,
        "moat_passed": moat_ok,
        "moat_blockers": moat_reasons,
        "moat": moat_metrics,
        "signal_type": signal_type,
        "matched_stages": matched,
        "score": _score_candidate(metrics, matched, moat_metrics) if matched else 0.0,
        "metrics": public_metrics,
        "stage_a_blockers": a_blockers,
        "stage_b_blockers": b_blockers,
        "recommended_exit_plan": exit_plan,
        "research_only": True,
        "new_buy_allowed": False,
    }


def scan_slow_bull_candidates(
    all_df_map: Mapping[str, pd.DataFrame],
    *,
    csi300_df: pd.DataFrame | None = None,
    chinext_df: pd.DataFrame | None = None,
    name_map: Mapping[str, str] | None = None,
    market_cap_map: Mapping[str, float] | None = None,
    dv_ttm_map: Mapping[str, float] | None = None,
    turnover_map: Mapping[str, float] | None = None,
    sector_map: Mapping[str, str] | None = None,
    financial_map: Mapping[str, Mapping[str, Any]] | None = None,
    cfg: SlowBullConfig = DEFAULT_SLOW_BULL_CONFIG,
    top_n: int = 20,
) -> SlowBullScanResult:
    """Run full-market cross-sectional RPS50 and institutional Slow-Bull scan."""
    names, caps = name_map or {}, market_cap_map or {}
    dvs, turns = dv_ttm_map or {}, turnover_map or {}
    sectors, fins = sector_map or {}, financial_map or {}
    rps_map, rps_counts = compute_cross_sectional_rps50_history(all_df_map, name_map=names, cfg=cfg)
    climate = detect_tech_rotation_warning(csi300_df, chinext_df, all_df_map=all_df_map, cfg=cfg)
    emerge, pullback, watch = [], [], []
    moat_passed, latest_dates = 0, []
    for code, df in sorted(all_df_map.items()):
        rps_cur, rps_prior = rps_map.get(code, (None, None))
        row = diagnose_slow_bull_symbol(
            code,
            df,
            name=names.get(code, ""),
            csi300_df=csi300_df,
            market_cap_yi=caps.get(code),
            dv_ttm_pct=dvs.get(code),
            turnover_pct=turns.get(code),
            sector=sectors.get(code, ""),
            financial=fins.get(code),
            rps50_current=rps_cur,
            rps50_prior_min=rps_prior,
            cfg=cfg,
        )
        if row.get("trade_date"):
            latest_dates.append(str(row["trade_date"]))
        moat_passed += int(bool(row["moat_passed"]))
        if _STAGE_A_TYPE in row["matched_stages"]:
            emerge.append(row)
        if _STAGE_B_PULLBACK_TYPE in row["matched_stages"]:
            pullback.append(row)
        elif _STAGE_B_WATCH_TYPE in row["matched_stages"]:
            watch.append(row)
    limit_n = max(int(top_n), 1)
    return SlowBullScanResult(
        trade_date=max(latest_dates) if latest_dates else "",
        counts={
            "input_symbols": len(all_df_map),
            "pit_eligible": int(rps_counts.get("eligible_symbols", 0)),
            "latest_rps_universe": int(rps_counts.get("latest_rps_universe", 0)),
            "moat_passed": moat_passed,
            "emerge_count": len(emerge),
            "pullback_count": len(pullback),
            "watch_count": len(watch),
        },
        climate=climate,
        emerge_candidates=sorted(emerge, key=_candidate_sort_key)[:limit_n],
        pullback_candidates=sorted(pullback, key=_candidate_sort_key)[:limit_n],
        watch_candidates=sorted(watch, key=_candidate_sort_key)[:limit_n],
        friction_round_trip_pct=round(round_trip_cost_pct(), 4),
        exit_guidance=dict(_EXIT_GUIDANCE),
    )


def _candidate_sort_key(item: dict[str, Any]) -> tuple[float, str]:
    return (-item["score"], item["code"])


__all__ = [
    "FIN_EXEMPT_SECTORS",
    "SlowBullConfig",
    "SlowBullScanResult",
    "annualize_roe",
    "compute_cross_sectional_rps50_history",
    "detect_tech_rotation_warning",
    "diagnose_slow_bull_symbol",
    "evaluate_slow_bull_moat",
    "ols_signed_log_r2",
    "scan_slow_bull_candidates",
]
