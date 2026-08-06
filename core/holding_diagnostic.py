"""
持仓健康诊断模块

复用 wyckoff_engine 已有的 L2 通道分类、L4 触发检测、L5 退出信号、
吸筹阶段分析、派发识别等能力，对任意持仓个股做结构化健康诊断。

用法:
    from core.holding_diagnostic import HoldingInput, diagnose_holdings, format_diagnostic_text

    diagnostics = diagnose_holdings(
        holdings=[HoldingInput(code, name, cost, buy_dt), ...],
        df_map=df_map,
        bench_df=bench_df,
    )
    rendered = [format_diagnostic_text(d) for d in diagnostics]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import pandas as pd

from core.candidate_lanes import build_l1_candidate_lane_entries
from core.intraday_shakeout import (
    PATH_DISTRIBUTION,
    PATH_WASHOUT,
    IntradayPathResult,
    classify_intraday_path,
    describe_intraday_path,
)
from core.limit_move import classify_limit_move, describe_limit_move
from core.price_targets import PriceTargets, compute_price_targets
from core.wyckoff_engine import (
    FunnelConfig,
    _detect_evr,
    _detect_lps,
    _detect_sos,
    _detect_spring,
    analyze_accum_stage,
    layer2_strength_detailed,
    layer5_exit_signals,
    sort_by_date_if_needed,
)

logger = logging.getLogger(__name__)

_LIMIT_MOVE_DAY_CHANGE_PCT = -5.0  # 当日跌幅达到此阈值才触发涨跌停/日内路径核查


@dataclass(frozen=True)
class HoldingInput:
    """诊断输入的持仓标识。

    ``buy_dt`` 必须显式给出：退出结构只能看建仓后的价格路径，缺失时会静默退化成
    全历史，让建仓前的暴跌反杀新仓。留空是"确实无建仓日"的显式声明，不是默认值。
    """

    code: str
    name: str
    cost: float
    buy_dt: str

    @classmethod
    def from_position(cls, position: dict) -> HoldingInput:
        code = str(position.get("code", "") or "").strip()
        cost = position.get("cost", position.get("cost_price", 0.0))
        return cls(
            code=code,
            name=str(position.get("name", "") or code).strip() or code,
            cost=float(cost or 0.0),
            buy_dt=str(position.get("buy_dt", position.get("buy_date", "")) or "").strip(),
        )


@dataclass
class HoldingDiagnostic:
    """单只持仓的结构化健康诊断结果"""

    code: str
    name: str
    cost: float
    latest_close: float
    pnl_pct: float  # 浮盈亏 %

    # 均线结构
    ma5: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    ma_pattern: str = "数据不足"  # 多头排列 / 空头排列 / MA50>MA200 / MA50<MA200
    ma200_bias_pct: float | None = None

    # Wyckoff 定位
    l2_channel: str = "未入选"  # 主升通道 / 潜伏通道 / 吸筹通道 / ...
    accum_stage: str | None = None  # Accum_A / Accum_B / Accum_C
    track: str = "Unknown"  # Trend / Accum / Unknown
    l4_triggers: list[str] = field(default_factory=list)  # ["SOS", "Spring", ...]
    candidate_lane: str = ""
    candidate_entry_type: str = ""
    candidate_score: float = 0.0

    # 退出信号 (来自 layer5_exit_signals)
    exit_signal: str | None = None  # stop_loss / distribution_warning / upthrust_warning
    exit_price: float | None = None
    exit_reason: str = ""

    # 止损/止盈参考（回测网格验证的最优组合：SL-7% / TP+18%，夏普2.493）
    stop_loss_7pct: float = 0.0  # 成本 × 0.93
    stop_loss_status: str = "安全"  # 已穿止损 / 逼近止损 / 安全
    take_profit_18pct: float = 0.0  # 成本 × 1.18
    take_profit_status: str = "未达标"  # 已达标 / 接近目标 / 未达标

    # 技术位目标价（量度运动/前高/ATR倍数，供参考，不是盈利预测）
    target_conservative: float | None = None  # 三个技术位中最保守的一个
    target_aggressive: float | None = None  # 三个技术位中最乐观的一个

    # 量能与振幅
    vol_ratio_20_60: float = 0.0
    range_60d_pct: float = 0.0
    ret_10d_pct: float = 0.0
    ret_20d_pct: float = 0.0
    from_year_high_pct: float = 0.0
    from_year_low_pct: float = 0.0

    # 极端当日行情识别（涨跌停/日内路径），仅当日跌幅显著且提供分钟线时才会填充
    day_change_pct: float = 0.0
    limit_move_desc: str = ""
    intraday_path: str = ""  # washout / distribution / strong / neutral / insufficient_data / ""(未核查)
    intraday_path_desc: str = ""

    # 综合评级
    health: str = "🟢健康"
    health_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SeriesSnapshot:
    df: pd.DataFrame
    close: pd.Series
    high: pd.Series
    low: pd.Series
    volume: pd.Series
    latest_close: float
    pnl_pct: float


@dataclass(frozen=True)
class _MaSnapshot:
    ma5: float | None
    ma20: float | None
    ma50: float | None
    ma200: float | None
    pattern: str
    ma200_bias_pct: float | None


@dataclass(frozen=True)
class _WyckoffSnapshot:
    l2_channel: str
    track: str
    accum_stage: str | None
    l4_triggers: list[str]
    exit_signal: str | None
    exit_price: float | None
    exit_reason: str


@dataclass(frozen=True)
class _RiskSnapshot:
    stop_loss_7pct: float
    stop_status: str
    take_profit_18pct: float
    take_profit_status: str
    vol_ratio: float
    range_60d: float
    ret_10d: float
    ret_20d: float
    from_year_high: float
    from_year_low: float


# ── 通道 → 轨道映射 ──

_TREND_CHANNELS = {"主升通道", "趋势延续", "点火破局"}
_ACCUM_CHANNELS = {"潜伏通道", "吸筹通道", "地量蓄势", "暗中护盘"}


def _classify_track(channel: str) -> str:
    """
    通道 → 轨道映射。
    引擎可能输出多标签（如 "主升通道+点火破局"），用子串匹配而非精确匹配。
    Trend 优先：只要包含任一趋势通道就归为 Trend。
    """
    for t in _TREND_CHANNELS:
        if t in channel:
            return "Trend"
    for a in _ACCUM_CHANNELS:
        if a in channel:
            return "Accum"
    return "Unknown"


def _calc_ma_pattern(
    close_val: float,
    ma50: float | None,
    ma200: float | None,
) -> str:
    if ma50 is None or ma200 is None:
        return "数据不足"
    if close_val > ma50 > ma200:
        return "多头排列"
    if close_val < ma50 < ma200:
        return "空头排列"
    if ma50 > ma200:
        return "MA50>MA200(偏强)"
    return "MA50<MA200(偏弱)"


def _series_snapshot(df: pd.DataFrame, cost: float) -> _SeriesSnapshot:
    df_s = sort_by_date_if_needed(df).copy()
    close = pd.to_numeric(df_s["close"], errors="coerce")
    high = pd.to_numeric(df_s["high"], errors="coerce")
    low = pd.to_numeric(df_s["low"], errors="coerce")
    volume = pd.to_numeric(df_s["volume"], errors="coerce")
    latest_close = float(close.iloc[-1]) if not close.empty else 0.0
    pnl_pct = (latest_close - cost) / cost * 100.0 if cost > 0 else 0.0
    return _SeriesSnapshot(df_s, close, high, low, volume, latest_close, pnl_pct)


def _ma_snapshot(close: pd.Series, latest_close: float) -> _MaSnapshot:
    ma5 = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else None
    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    pattern = _calc_ma_pattern(latest_close, ma50, ma200)
    ma200_bias = (latest_close - ma200) / ma200 * 100 if ma200 and ma200 > 0 else None
    return _MaSnapshot(ma5, ma20, ma50, ma200, pattern, ma200_bias)


def _l2_channel(code: str, df: pd.DataFrame, bench_df: pd.DataFrame | None, cfg: FunnelConfig) -> str:
    try:
        diag_cfg = replace(cfg, enable_rps_filter=False)
        _, channel_map, _ = layer2_strength_detailed([code], {code: df}, bench_df, diag_cfg)
        return channel_map.get(code, "未入选")
    except Exception:
        logger.debug("L2 channel classification failed for %s", code, exc_info=True)
        return "未入选"


def _accum_stage(df: pd.DataFrame, cfg: FunnelConfig) -> str | None:
    try:
        return analyze_accum_stage(df, cfg)
    except Exception:
        logger.debug("Accumulation stage analysis failed", exc_info=True)
        return None


def _l4_triggers(code: str, df: pd.DataFrame, cfg: FunnelConfig) -> list[str]:
    try:
        triggers = []
        if _detect_sos(df, cfg, code=code) is not None:
            triggers.append("SOS")
        if _detect_spring(df, cfg, code=code) is not None:
            triggers.append("Spring")
        if _detect_lps(df, cfg, code=code) is not None:
            triggers.append("LPS")
        if _detect_evr(df, cfg, code=code) is not None:
            triggers.append("EVR")
        return triggers
    except Exception:
        logger.debug("L4 trigger detection failed", exc_info=True)
        return []


def _exit_snapshot(
    code: str,
    df: pd.DataFrame,
    accum_stage: str | None,
    cfg: FunnelConfig,
    buy_dt: str = "",
) -> tuple[str | None, float | None, str]:
    try:
        exit_df = _exit_history_since_entry(df, buy_dt)
        accum_map = {code: accum_stage} if accum_stage else {}
        sig = layer5_exit_signals([code], {code: exit_df}, accum_map, cfg).get(code, {})
        if not sig:
            return None, None, ""
        return sig.get("signal"), sig.get("price"), sig.get("reason", "")
    except Exception:
        logger.debug("L5 exit signal detection failed", exc_info=True)
        return None, None, ""


def _exit_history_since_entry(df: pd.DataFrame, buy_dt: str) -> pd.DataFrame:
    """退出结构只能使用建仓后的价格路径，避免历史高点反杀新仓。"""
    entry = pd.to_datetime(str(buy_dt or "").strip(), errors="coerce")
    if pd.isna(entry) or "date" not in df.columns:
        return df
    dates = pd.to_datetime(df["date"], errors="coerce")
    sliced = df.loc[dates >= entry].copy()
    return sliced if not sliced.empty else df


def _wyckoff_snapshot(
    code: str,
    series: _SeriesSnapshot,
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    buy_dt: str = "",
) -> _WyckoffSnapshot:
    l2_channel = _l2_channel(code, series.df, bench_df, cfg)
    accum_stage = _accum_stage(series.df, cfg)
    exit_signal, exit_price, exit_reason = _exit_snapshot(code, series.df, accum_stage, cfg, buy_dt)
    return _WyckoffSnapshot(
        l2_channel=l2_channel,
        track=_classify_track(l2_channel),
        accum_stage=accum_stage,
        l4_triggers=_l4_triggers(code, series.df, cfg),
        exit_signal=exit_signal,
        exit_price=exit_price,
        exit_reason=exit_reason,
    )


def _stop_status(cost: float, latest_close: float) -> tuple[float, str]:
    stop_loss_7pct = cost * 0.93
    if stop_loss_7pct <= 0:
        return stop_loss_7pct, "无成本价"
    if latest_close <= stop_loss_7pct:
        return stop_loss_7pct, "已穿止损"
    if (latest_close - stop_loss_7pct) / stop_loss_7pct < 0.02:
        return stop_loss_7pct, "逼近止损(<2%)"
    return stop_loss_7pct, "安全"


def _take_profit_status(cost: float, latest_close: float) -> tuple[float, str]:
    # 回测网格验证：SL-7%/TP+18% 是各周期最稳健参数组合（夏普2.493），
    # 实盘持仓诊断只提醒止损、不提醒止盈是纪律缺口的来源之一。
    take_profit_18pct = cost * 1.18
    if take_profit_18pct <= 0:
        return take_profit_18pct, "无成本价"
    if latest_close >= take_profit_18pct:
        return take_profit_18pct, "已达标"
    if (take_profit_18pct - latest_close) / take_profit_18pct < 0.02:
        return take_profit_18pct, "接近目标(<2%)"
    return take_profit_18pct, "未达标"


def _risk_snapshot(series: _SeriesSnapshot, cost: float) -> _RiskSnapshot:
    stop_loss_7pct, stop_status = _stop_status(cost, series.latest_close)
    take_profit_18pct, take_profit_status = _take_profit_status(cost, series.latest_close)
    vol_20 = float(series.volume.tail(20).mean()) if len(series.volume) >= 20 else 0
    vol_60 = float(series.volume.tail(60).mean()) if len(series.volume) >= 60 else 0
    h60 = float(series.high.tail(60).max()) if len(series.high) >= 60 else float(series.high.max())
    l60 = float(series.low.tail(60).min()) if len(series.low) >= 60 else float(series.low.min())
    lookback_250 = min(len(series.high), 250)
    h_year = float(series.high.tail(lookback_250).max())
    l_year = float(series.low.tail(lookback_250).min())
    return _RiskSnapshot(
        stop_loss_7pct=stop_loss_7pct,
        stop_status=stop_status,
        take_profit_18pct=take_profit_18pct,
        take_profit_status=take_profit_status,
        vol_ratio=vol_20 / vol_60 if vol_60 > 0 else 0,
        range_60d=(h60 - l60) / l60 * 100 if l60 > 0 else 0,
        ret_10d=(series.latest_close / float(series.close.iloc[-11]) - 1) * 100 if len(series.close) >= 11 else 0,
        ret_20d=(series.latest_close / float(series.close.iloc[-21]) - 1) * 100 if len(series.close) >= 21 else 0,
        from_year_high=(series.latest_close - h_year) / h_year * 100 if h_year > 0 else 0,
        from_year_low=(series.latest_close - l_year) / l_year * 100 if l_year > 0 else 0,
    )


def _extreme_day_snapshot(
    code: str,
    name: str,
    series: _SeriesSnapshot,
    intraday_df: pd.DataFrame | None,
) -> tuple[float, str, IntradayPathResult | None]:
    """当日涨跌幅显著时，识别涨跌停状态 + 日内路径（洗盘/出货）。

    数据不足或跌幅未达阈值时静默跳过，不影响原有诊断流程。
    """
    if len(series.close) < 2:
        return 0.0, "", None
    prev_close = float(series.close.iloc[-2])
    if prev_close <= 0:
        return 0.0, "", None
    day_change_pct = (series.latest_close / prev_close - 1.0) * 100.0
    if day_change_pct > _LIMIT_MOVE_DAY_CHANGE_PCT:
        return day_change_pct, "", None

    last_row = series.df.iloc[-1]
    limit_state = classify_limit_move(
        code=code,
        name=name,
        prev_close=prev_close,
        open_=float(last_row.get("open", series.latest_close)),
        high=float(series.high.iloc[-1]),
        low=float(series.low.iloc[-1]),
        close=series.latest_close,
    )
    limit_desc = describe_limit_move(limit_state)

    path_result: IntradayPathResult | None = None
    if intraday_df is not None and not intraday_df.empty:
        support = float(series.close.tail(20).min()) if len(series.close) >= 20 else 0.0
        path_result = classify_intraday_path(intraday_df, support_level=support, day_change_pct=day_change_pct)

    return day_change_pct, limit_desc, path_result


def _health_rating(
    ma: _MaSnapshot,
    wyckoff: _WyckoffSnapshot,
    risk: _RiskSnapshot,
    pnl_pct: float,
    intraday_path: IntradayPathResult | None = None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if risk.stop_status == "已穿止损":
        reasons.append("已穿止损线(-7%)")
    if wyckoff.exit_signal == "stop_loss":
        reasons.append("结构止损（从高点回撤>10%）")
    if ma.pattern == "空头排列":
        reasons.append("均线空头排列")
    if risk.range_60d > 50:
        reasons.append(f"60日振幅过大(>{risk.range_60d:.0f}%)")
    if risk.ret_10d < -15 and not (intraday_path and intraday_path.path_type == PATH_WASHOUT):
        reasons.append(f"近10日暴跌({risk.ret_10d:+.1f}%)")

    if wyckoff.exit_signal in {"distribution_warning", "upthrust_warning"}:
        reasons.append("高位派发预警")
    if risk.stop_status == "逼近止损(<2%)":
        reasons.append("逼近止损线")
    if pnl_pct < -5:
        reasons.append("浮亏超过5%")
    if ma.pattern == "MA50<MA200(偏弱)" and pnl_pct < 0:
        reasons.append("均线偏弱且浮亏")
    if risk.vol_ratio < 0.5:
        reasons.append("量能严重萎缩")
    if intraday_path and intraday_path.path_type == PATH_DISTRIBUTION:
        reasons.append("当日盘中路径确认出货/破位（非单纯洗盘）")
    elif intraday_path and intraday_path.path_type == PATH_WASHOUT:
        # 洗盘结论必须始终可见，不能被其他常规 warning 挤掉，
        # 否则用户看到的仍是"跌了就是走弱"的归因。
        reasons.append(f"当日{describe_intraday_path(intraday_path)}，跌幅不必等同走弱")

    positive = _positive_reasons(ma, wyckoff, risk)
    danger_count = sum(1 for r in reasons if any(k in r for k in ["已穿", "暴跌", "空头排列", "结构止损", "出货"]))
    warn_count = len(reasons) - danger_count
    if danger_count >= 1:
        return "🔴危险", reasons
    if warn_count >= 2 or warn_count == 1 and not positive:
        return "🟡警戒", reasons
    return "🟢健康", positive if positive and not reasons else reasons


def _positive_reasons(ma: _MaSnapshot, wyckoff: _WyckoffSnapshot, risk: _RiskSnapshot | None = None) -> list[str]:
    positive = []
    if risk is not None and risk.take_profit_status == "已达标":
        # 回测验证 SL-7%/TP+18% 最稳健，止盈达标不算风险，但必须显式提醒，
        # 避免"只提示止损、不提示止盈"导致落袋纪律缺失。
        positive.append(f"已达TP+18%目标({risk.take_profit_18pct:.2f})，建议分批止盈")
    if ma.pattern == "多头排列":
        positive.append("多头排列")
    if any(t in wyckoff.l2_channel for t in _TREND_CHANNELS):
        positive.append(f"L2通道:{wyckoff.l2_channel}")
    if wyckoff.l4_triggers:
        positive.append(f"L4信号:{'+'.join(wyckoff.l4_triggers)}")
    return positive


def _candidate_lane_entry(code: str, series: _SeriesSnapshot, wyckoff: _WyckoffSnapshot) -> dict:
    l2_symbols = [code] if wyckoff.l2_channel and wyckoff.l2_channel != "未入选" else []
    entries = build_l1_candidate_lane_entries(
        l1_symbols=[code],
        df_map={code: series.df},
        sector_map={},
        top_sectors=[],
        l2_symbols=l2_symbols,
        channel_map={code: wyckoff.l2_channel},
        limit=5,
    )
    return entries[0] if entries else {}


def diagnose_one_stock(
    code: str,
    name: str,
    cost: float,
    df: pd.DataFrame,
    bench_df: pd.DataFrame | None = None,
    cfg: FunnelConfig | None = None,
    intraday_df: pd.DataFrame | None = None,
    buy_dt: str = "",
) -> HoldingDiagnostic:
    """
    对单只股票执行全面 Wyckoff 健康诊断。

    Parameters
    ----------
    code : 6位股票代码
    name : 股票名称
    cost : 持仓成本价
    df   : 该股 320 日 OHLCV（需包含 date/open/high/low/close/volume 列）
    bench_df : 大盘基准 OHLCV（用于 L2 通道 RS 计算，可选）
    cfg  : FunnelConfig，默认使用全局默认值
    intraday_df : 当日 1 分钟 K 线（可选）。当日跌幅显著时，用于区分"洗盘"与"出货/
        确认破位"，避免把跌停/暴力回踩直接等同于走弱确认。
    """
    if cfg is None:
        cfg = FunnelConfig()

    series = _series_snapshot(df, cost)
    ma = _ma_snapshot(series.close, series.latest_close)
    wyckoff = _wyckoff_snapshot(code, series, bench_df, cfg, buy_dt)
    candidate_entry = _candidate_lane_entry(code, series, wyckoff)
    risk = _risk_snapshot(series, cost)
    targets = compute_price_targets(series.close, series.high, series.low)
    extreme = _extreme_day_snapshot(code, name, series, intraday_df)
    health, reasons = _health_rating(ma, wyckoff, risk, series.pnl_pct, extreme[2])
    return _build_diagnostic(
        code, name, cost, series, ma, wyckoff, candidate_entry, risk, targets, extreme, health, reasons
    )


def _build_diagnostic(
    code: str,
    name: str,
    cost: float,
    series: _SeriesSnapshot,
    ma: _MaSnapshot,
    wyckoff: _WyckoffSnapshot,
    candidate_entry: dict,
    risk: _RiskSnapshot,
    targets: PriceTargets | None,
    extreme: tuple[float, str, IntradayPathResult | None],
    health: str,
    reasons: list[str],
) -> HoldingDiagnostic:
    day_change_pct, limit_desc, path_result = extreme
    return HoldingDiagnostic(
        code=code,
        name=name,
        cost=cost,
        latest_close=series.latest_close,
        pnl_pct=series.pnl_pct,
        ma5=ma.ma5,
        ma20=ma.ma20,
        ma50=ma.ma50,
        ma200=ma.ma200,
        ma_pattern=ma.pattern,
        ma200_bias_pct=ma.ma200_bias_pct,
        l2_channel=wyckoff.l2_channel,
        accum_stage=wyckoff.accum_stage,
        track=wyckoff.track,
        l4_triggers=wyckoff.l4_triggers,
        candidate_lane=str(candidate_entry.get("lane", "") or ""),
        candidate_entry_type=str(candidate_entry.get("entry_type", "") or ""),
        candidate_score=float(candidate_entry.get("score", 0.0) or 0.0),
        exit_signal=wyckoff.exit_signal,
        exit_price=wyckoff.exit_price,
        exit_reason=wyckoff.exit_reason,
        stop_loss_7pct=risk.stop_loss_7pct,
        stop_loss_status=risk.stop_status,
        take_profit_18pct=risk.take_profit_18pct,
        take_profit_status=risk.take_profit_status,
        target_conservative=targets.conservative if targets else None,
        target_aggressive=targets.aggressive if targets else None,
        vol_ratio_20_60=risk.vol_ratio,
        range_60d_pct=risk.range_60d,
        ret_10d_pct=risk.ret_10d,
        ret_20d_pct=risk.ret_20d,
        from_year_high_pct=risk.from_year_high,
        from_year_low_pct=risk.from_year_low,
        day_change_pct=day_change_pct,
        limit_move_desc=limit_desc,
        intraday_path=path_result.path_type if path_result else "",
        intraday_path_desc=describe_intraday_path(path_result) if path_result else "",
        health=health,
        health_reasons=reasons,
    )


def diagnose_holdings(
    holdings: list[HoldingInput],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None = None,
    cfg: FunnelConfig | None = None,
    intraday_df_map: dict[str, pd.DataFrame] | None = None,
) -> list[HoldingDiagnostic]:
    """
    批量诊断持仓。

    Parameters
    ----------
    holdings : [HoldingInput, ...]，buy_dt 必填（无建仓日时显式传空串）
    df_map   : {code: DataFrame} 每只股票的 OHLCV 数据
    bench_df : 大盘基准 OHLCV
    cfg      : FunnelConfig
    intraday_df_map : {code: DataFrame} 当日 1 分钟 K 线（可选），用于洗盘/出货识别
    """
    results = []
    intraday_map = intraday_df_map or {}
    for holding in holdings:
        df = df_map.get(holding.code)
        if df is None or df.empty:
            # 无数据时返回最小诊断
            results.append(
                HoldingDiagnostic(
                    code=holding.code,
                    name=holding.name,
                    cost=holding.cost,
                    latest_close=0.0,
                    pnl_pct=0.0,
                    health="🔴危险",
                    health_reasons=["无法获取行情数据"],
                )
            )
            continue
        results.append(
            diagnose_one_stock(
                holding.code,
                holding.name,
                holding.cost,
                df,
                bench_df,
                cfg,
                intraday_map.get(holding.code),
                buy_dt=holding.buy_dt,
            )
        )
    return results


def format_diagnostic_text(d: HoldingDiagnostic) -> str:
    """将诊断结果格式化为结构化文本，可注入 LLM prompt 或终端显示。"""
    lines = [
        f"{d.health} {d.code} {d.name} | 盈亏: {d.pnl_pct:+.2f}%",
        f"  成本: {d.cost:.2f} | 现价: {d.latest_close:.2f}",
    ]

    # 均线
    ma_parts = [f"均线: {d.ma_pattern}"]
    if d.ma200_bias_pct is not None:
        ma_parts.append(f"MA200乖离: {d.ma200_bias_pct:+.1f}%")
    lines.append("  " + " | ".join(ma_parts))

    # Wyckoff 定位
    wy_parts = [f"通道: {d.l2_channel}", f"轨道: {d.track}"]
    if d.accum_stage:
        wy_parts.append(f"阶段: {d.accum_stage}")
    if d.l4_triggers:
        wy_parts.append(f"L4: {'+'.join(d.l4_triggers)}")
    if d.candidate_lane:
        wy_parts.append(f"候选车道: {d.candidate_entry_type or d.candidate_lane}({d.candidate_score:.1f})")
    lines.append("  " + " | ".join(wy_parts))

    # 退出信号
    if d.exit_signal:
        exit_parts = [f"退出信号: {d.exit_signal}"]
        if d.exit_price is not None:
            exit_parts.append(f"触发价: {d.exit_price:.2f}")
        if d.exit_reason:
            exit_parts.append(d.exit_reason)
        lines.append("  " + " | ".join(exit_parts))

    # 止损 / 止盈
    lines.append(f"  止损(-7%): {d.stop_loss_7pct:.2f} → {d.stop_loss_status}")
    lines.append(f"  止盈(+18%): {d.take_profit_18pct:.2f} → {d.take_profit_status}")

    # 技术位目标价（量度运动/前高/ATR倍数，仅供参考）
    if d.target_conservative is not None or d.target_aggressive is not None:
        target_parts = []
        if d.target_conservative is not None:
            target_parts.append(f"保守{d.target_conservative:.2f}")
        if d.target_aggressive is not None:
            target_parts.append(f"乐观{d.target_aggressive:.2f}")
        lines.append(f"  目标价(技术位参考): {' / '.join(target_parts)}")

    # 量能
    lines.append(
        f"  量比(20/60): {d.vol_ratio_20_60:.2f} | 60日振幅: {d.range_60d_pct:.1f}% | "
        f"近10日: {d.ret_10d_pct:+.1f}% | 近20日: {d.ret_20d_pct:+.1f}%"
    )

    # 当日极端行情（涨跌停/日内路径）
    if d.limit_move_desc or d.intraday_path_desc:
        extreme_parts = [f"当日: {d.day_change_pct:+.1f}%"]
        if d.limit_move_desc:
            extreme_parts.append(d.limit_move_desc)
        if d.intraday_path_desc:
            extreme_parts.append(f"盘中路径: {d.intraday_path_desc}")
        lines.append("  " + " | ".join(extreme_parts))

    # 评级理由
    if d.health_reasons:
        lines.append(f"  理由: {', '.join(d.health_reasons)}")

    return "\n".join(lines)


def format_diagnostic_for_llm(d: HoldingDiagnostic) -> str:
    """生成简洁版诊断文本，适合注入 Step4 LLM prompt 中。"""
    parts = [
        f"[持仓诊断] {d.health}",
        f"通道:{d.l2_channel} 轨道:{d.track}",
        f"均线:{d.ma_pattern}",
    ]
    if d.ma200_bias_pct is not None:
        parts.append(f"MA200乖离:{d.ma200_bias_pct:+.1f}%")
    if d.accum_stage:
        parts.append(f"阶段:{d.accum_stage}")
    if d.l4_triggers:
        parts.append(f"信号:{'+'.join(d.l4_triggers)}")
    if d.candidate_lane:
        parts.append(f"候选车道:{d.candidate_entry_type or d.candidate_lane}({d.candidate_score:.1f})")
    if d.exit_signal:
        parts.append(f"退出:{d.exit_signal}")
        if d.exit_price is not None:
            parts.append(f"触发价:{d.exit_price:.2f}")
    parts.append(f"止损状态:{d.stop_loss_status}")
    parts.append(f"止盈状态:{d.take_profit_status}")
    parts.append(f"量比:{d.vol_ratio_20_60:.2f} 振幅:{d.range_60d_pct:.0f}%")
    if d.intraday_path_desc:
        parts.append(f"当日{d.day_change_pct:+.1f}%/{d.intraday_path_desc}")
    if d.health_reasons:
        parts.append(f"原因:{','.join(d.health_reasons[:3])}")
    return " | ".join(parts)
