"""
Wyckoff Funnel 5 层漏斗筛选引擎

Layer 1: 剥离垃圾（ST / 非目标板块 / 市值 / 成交额）
Layer 2: 八通道甄选（主升/潜伏/吸筹/地量/暗中护盘/趋势延续/加速突破/点火破局）
Layer 2.5: Markup 加速检测
Layer 2.7: Alpha 候选板（潜在大涨结构 + 龙头跟踪）
Layer 3: 板块共振（行业分布 Top-N + RPS 动量）
Layer 4: 威科夫狙击（Spring / SOS / LPS / Effort vs Result）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

from core._price_math import sort_by_date_if_needed, swing_values
from core.candidate_lanes import build_l1_candidate_lane_entries, merge_candidate_entries
from core.candidate_tracks import candidate_entry_sort_key
from core.cn_boards import cn_board, is_supported_cn_board
from core.layer2_strength import (
    BenchmarkContext,
    RpsContext,
    build_benchmark_context,
    build_rps_context,
    evaluate_layer2_symbol,
)
from core.main_force_signal import analyze_main_force_signal
from core.mainline_engine import MainlineEngineConfig, build_mainline_candidates, mainline_candidate_entries
from core.price_targets import compute_price_targets
from core.theme_activity import build_theme_activity_snapshot
from core.theme_radar import normalize_theme_name

logger = logging.getLogger(__name__)

_HIST_COL_MAP = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
}


def normalize_hist_from_fetch(df: pd.DataFrame) -> pd.DataFrame:
    """将 fetch_a_share_csv.fetch_hist 返回的 DataFrame 转为筛选器所需格式。"""
    col_map = {**_HIST_COL_MAP, "换手率": "turnover", "换手": "turnover"}
    out = df.rename(columns=col_map)
    keep = [
        c
        for c in ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]
        if c in out.columns
    ]
    out = out[keep].copy()
    if "pct_chg" not in out.columns and "close" in out.columns:
        out["pct_chg"] = out["close"].astype(float).pct_change() * 100
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _latest_trade_date(df: pd.DataFrame) -> object | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    return s.iloc[-1].date() if not s.empty else None


# Config


@dataclass
class FunnelConfig:
    trading_days: int = 320

    # Layer 1
    require_cn_main_or_chinext: bool = True  # 兼容旧字段名：限制为 A 股交易板块；默认含北交所
    include_bse_board: bool = True
    min_market_cap_yi: float = 25.0
    min_avg_amount_wan: float = 4000.0
    l1_min_close_price: float = 2.0
    l1_delist_risk_cap_floor_yi: float = 10.0
    l1_cap_bypass_amount_wan: float = 8000.0  # 市值不足但日均额 >= 此值可放行
    amount_avg_window: int = 20
    amount_skew_check_enabled: bool = True
    amount_skew_max: float = 3.0
    amount_median_min_ratio: float = 0.45
    amount_pass_days_min_ratio: float = 0.35

    # Layer 2
    ma_short: int = 50
    ma_long: int = 200
    ma_hold: int = 20
    bench_drop_days: int = 3
    bench_drop_threshold: float = -2.0
    rs_window_long: int = 10
    rs_window_short: int = 3
    rs_min_long: float = 2.0  # 10 日 RS 至少跑赢大盘 2%（原 0.0 形同虚设）
    rs_min_short: float = 1.0  # 3 日 RS 至少跑赢大盘 1%
    rs_dynamic_relax_enabled: bool = True
    rs_bench_surge_long_pct: float = 4.0
    rs_bench_surge_short_pct: float = 2.0
    rs_surge_relax_factor: float = 0.35
    rs_structural_bypass_enabled: bool = True
    rs_structural_bypass_rps_slow_min: float = 65.0
    rs_structural_bypass_ret20_floor: float = -4.0
    enable_rs_filter: bool = True
    enable_rps_filter: bool = True
    rps_window_fast: int = 50
    rps_window_slow: int = 120
    rps_fast_min: float = 65.0
    rps_slow_min: float = 70.0
    rps_slow_strong_bypass: float = 80.0  # 中期极强时 RPS50 只需 >= 50 即通过
    rps_fast_bypass_min: float = 50.0
    rps_slope_window: int = 10  # 计算 RPS 斜率的窗口（交易日）
    rps_slope_min: float = 0.5  # RPS 斜率最小值（%/day），用于判断 RPS 是否还在上升
    rps_slope_accel_bypass: float = 1.5  # 斜率 >= 此值时 RPS 绝对值要求放宽（加速旁路）
    rps_accel_fast_min: float = 50.0  # 加速旁路下 RPS50 最低要求
    rps_accel_slow_min: float = 55.0  # 加速旁路下 RPS120 最低要求
    require_bench_latest_alignment: bool = False
    momentum_bias_200_max: float = 0.25
    # Global Anti-Overfitting Restriction（L4 用百分数）
    global_entry_max_bias_200: float = 25.0
    star_entry_max_bias_200: float = 40.0
    trend_entry_max_bias_200: float = 35.0
    strong_rps_slow_min: float = 90.0
    strong_rps_trend_bias_cap: float = 60.0
    strong_rps_star_bias_cap: float = 80.0
    # 绝对 120 日收益地板，防止无全市场 RPS 时用局部排名误放宽。
    strong_ret120_min_pct: float = 40.0

    # Layer 2 预点火观察池
    enable_pre_ignition_watch: bool = True
    pre_ignition_bias_max: float = 0.20
    pre_ignition_rps_slow_min: float = 60.0
    pre_ignition_vol_ratio_min: float = 0.6
    # Layer 2 潜伏通道（长强短弱）
    enable_ambush_channel: bool = True
    ambush_rps_fast_max: float = 45.0
    ambush_rps_slow_min: float = 70.0
    ambush_rs_long_min: float = -2.0
    ambush_rs_short_min: float = -8.0
    ambush_bias_200_abs_max: float = 0.08
    ambush_ret20_max: float = -3.0

    # Layer 2 低位吸筹通道（Wyckoff Accumulation Channel）
    # 不依赖 RPS 强势排名，专门捕捉"已止跌横盘蓄势"的 Phase A/B/C 股票。
    # 触发条件：低位区间 + 横盘振幅小 + 量能萎缩 + 均线胶着（尚未多头排列）。
    # 这类股票应与 L4 Spring/LPS 配合使用，单独出现时仅进观察池。
    # 四个子条件原为严格 and 串联（0.35/30%/0.65/6%），实盘统计显示长期 0 命中，
    # 结构判定权重下放给 L4 Spring/LPS 承接；此处小幅放宽而非删除通道，
    # 保留"低位横盘"这一独立于 RPS 排名的入口。
    enable_accumulation_channel: bool = True
    accum_lookback_days: int = 250  # 年内低点计算窗口（交易日）
    accum_price_from_low_max: float = 0.45  # 现价不超过年内低点 +45%
    accum_range_window: int = 60  # 横盘振幅计算窗口（交易日）
    accum_range_max_pct: float = 40.0  # 窗口内 (high_max-low_min)/low_min 不超过 40%
    accum_vol_dry_window: int = 20  # 量能萎缩统计近 N 日
    accum_vol_dry_ref_window: int = 120  # 量能萎缩对比参考窗口
    accum_vol_dry_ratio: float = 0.75  # 近 N 日均量 / 参考均量 < 此值（量能萎缩）
    accum_ma_gap_max: float = 0.08  # |MA50 - MA200| / MA200 < 此值（均线胶着）

    # Layer 2 地量蓄势通道（Dry Volume Channel）
    # 低位区间内，近期某日出现了年内最低级别的单日成交量，说明卖压完全枯竭。
    enable_dry_vol_channel: bool = True
    dry_vol_lookback: int = 10  # 在最近 N 日内寻找地量
    dry_vol_ref_window: int = 250  # 地量参考窗口（年维度）
    dry_vol_quantile: float = 0.05  # 地量标准：低于年内成交量的 5% 分位数
    dry_vol_price_from_low_max: float = 0.35  # 位阶保护：现价 <= 年内低点 +35%

    # Layer 2 暗中护盘通道（RS Divergence Channel）
    # 大盘近期创新低，但该股拒绝创新低，形成 Higher Low，说明有资金托底。
    enable_rs_divergence_channel: bool = True
    rs_div_bench_window: int = 20  # 大盘近 N 日内需出现新低
    rs_div_stock_window: int = 20  # 个股同期窗口
    rs_div_bench_ref_window: int = 60  # 大盘新低对比的参考窗口（近 60 日）
    rs_div_price_from_low_max: float = 0.50  # 位阶保护：现价 <= 年内低点 +50%

    # Layer 2 趋势延续通道（Trend Continuation Channel）
    # 已确认多头且 RPS 极强的稳定趋势股，不受 bias_200 上限约束。
    # 通过最大回撤排除暴涨暴跌的老妖股。
    enable_trend_cont_channel: bool = True
    trend_cont_rps_slow_min: float = 75.0  # RPS120 >= 此值
    trend_cont_max_drawdown_pct: float = 20.0  # 近 N 日最大回撤 < 此值
    trend_cont_drawdown_window: int = 60  # 回撤计算窗口（交易日）
    trend_cont_vol_ratio_min: float = 0.70  # 近5日均量 / 20日均量，过滤缩量趋势末端

    # Layer 2 加速突破通道（Breakout Acceleration Channel）
    # 从底部结构刚起步：价格站上 MA50 但 MA50 尚未上穿 MA200，短期动量已爆发。
    enable_breakout_accel_channel: bool = True
    breakout_accel_rps_fast_min: float = 70.0  # RPS50 >= 此值
    breakout_accel_ret_window: int = 20  # 近 N 日涨幅计算窗口
    breakout_accel_ret_min: float = 15.0  # 近 N 日涨幅 >= 此值(%)
    breakout_accel_vol_ratio: float = 1.3  # 近 N 日均量 / 前 ref 均量 >= 此值
    breakout_accel_vol_ref_window: int = 60  # 量能参考窗口

    # Layer 3
    # 行业共振过滤：按"行业样本数分位阈值 + 最小样本数"动态过滤，避免固定 TopN 误杀。
    top_n_sectors: int = 5
    sector_min_count: int = 3
    sector_count_quantile: float = 0.70
    sector_super_strength_quantile: float = 0.90  # 小而强板块免死阈值（强度分位）
    sector_heat_bypass_min_count: int = 0  # 0=关闭；>0时 L2 通过 ≥ 此数的板块直接绕行 L3
    use_concept_map: bool = True  # 启用概念模式（有 concept_map 时优先用概念聚合）
    theme_line_min_days: int = 3  # 主线判定最少连续天数
    theme_line_top_n: int = 20  # 每日取 Top N 概念计入热度历史
    l3_keep_strength_min: float = 0.60
    l3_leader_strength_min: float = 0.80
    l3_hot_leader_strength_min: float = 0.55

    # Layer 4 - Spring
    spring_support_window: int = 60
    spring_vol_ratio: float = 1.3
    spring_tr_max_range_pct: float = 30.0
    spring_tr_max_drift_pct: float = 12.0
    # Spring 动态振幅
    spring_tr_atr_window: int = 20  # 计算 ATR 的历史窗口
    spring_tr_atr_max_multiple: float = 4.0  # 区间最大允许振幅为 ATR_pct 的 N 倍(替代固定的30%)
    spring_vol_expand_ratio: float = 1.15  # 收回时的成交量 / 下探时的成交量 > 此值（原 1.3 过严）

    # Layer 4 - LPS
    lps_lookback: int = 3
    lps_ma: int = 20
    lps_ma_tolerance: float = 0.02
    lps_vol_dry_ratio: float = 0.50
    lps_vol_ref_window: int = 60
    lps_ma_rising_window: int = 5
    lps_creek_confirmation_enabled: bool = False
    lps_creek_lookback: int = 60
    lps_creek_breakout_lookback: int = 15
    lps_creek_swing_window: int = 3
    lps_creek_breakout_pct: float = 0.5
    lps_creek_hold_tolerance_pct: float = 2.0
    lps_creek_max_rise_pct_per_bar: float = 0.1

    # Layer 4 research switches (disabled in production until ablation passes)
    regime_trigger_profiles_enabled: bool = False
    regime_risk_on_volume_multiplier: float = 1.15
    regime_defensive_volume_multiplier: float = 0.85
    regime_repair_volume_multiplier: float = 0.90
    signal_sequence_bonus_enabled: bool = False
    signal_sequence_lookback: int = 12
    signal_sequence_score_bonus: float = 0.5

    # Layer 4 - Effort vs Result
    enable_evr_trigger: bool = True
    evr_lookback: int = 3
    evr_vol_ratio: float = 1.8  # 从1.5微调至1.8，略微提高异动门槛
    evr_min_turnover: float = 1.5
    evr_vol_window: int = 20
    evr_max_drop: float = 2.0
    evr_max_rise: float = 2.0
    evr_confirm_days: int = 1
    evr_confirm_allow_break_pct: float = 0.0

    # Layer 4 - Compression (压缩蓄势)
    enable_compression_trigger: bool = True
    compression_lookback: int = 5
    compression_atr_window: int = 20
    compression_atr_quantile: float = 0.20
    compression_vol_decline_ratio: float = 0.60  # 统一"量能枯竭"标准为 0.6倍
    compression_require_direction: bool = True  # 压缩必须处于非下降结构，避免阴跌缩量误判

    # Layer 4 - Trend Pullback (趋势回踩)
    enable_trend_pullback_trigger: bool = True
    trend_pb_lookback: int = 10  # 回踩窗口
    trend_pb_min_pullback_pct: float = 5.0  # 最小回撤深度%
    trend_pb_max_pullback_pct: float = 20.0  # 最大回撤深度%
    trend_pb_vol_shrink_ratio: float = 0.6  # 回落段缩量确认，统一为 0.6
    trend_pb_ma_window: int = 20  # 均线窗口

    # Funnel score
    min_funnel_score: float = 0.15

    # Layer 4 - SOS / JAC (Sign of Strength / Jump Across the Creek)
    sos_pct_min: float = 6.0  # 提高门槛过滤弱突破（原 4.5 追高触发止损率极高）
    sos_vol_ratio: float = 3.0  # 要求更暴力抢筹（原 2.5 噪音太多，修改为 3.0）
    sos_vol_window: int = 20  # 计算点火爆量时的参考窗口
    sos_breakout_window: int = 60  # 把突破箱体延长到 60天 (约3个月)，拒绝 10日小打小闹
    sos_breakout_tolerance: float = 0.01  # 改为 0.01：突破容差 1%（从 2% 改为 1%）
    sos_bypass_rps_slow_min: float = 30.0  # L2 点火破局旁路的最低 RPS120 门槛
    # SOS 动态极值爆量
    sos_vol_quantile_window: int = 60  # 计算量能分位数的滚动窗口
    sos_vol_quantile: float = 0.95  # 要求当日量能突破历史 N 日的 95% 分位数

    # Markup 阶段识别（Layer 2.5）
    enable_markup_detection: bool = True
    markup_ma_crossover_confirm_days: int = 5  # MA50 穿过 MA200 后，需要连续 N 日在上方
    markup_ma_angle_min: float = 2.0  # MA50 的角度（% per 5 days），用于确认上升趋势强度
    markup_rs_positive_min: float = 0.5  # RS_short 需要保持正值且持续增强

    # Leader Radar：独立主升观察池。只标注龙头跟踪，不改写 L4 买点。
    enable_leader_radar: bool = True
    leader_radar_limit: int = 50
    leader_radar_min_score: float = 0.68
    leader_radar_ret20_min: float = 12.0
    leader_radar_ret60_min: float = 35.0
    leader_radar_ret120_min: float = 60.0
    leader_radar_new_high_window: int = 120
    leader_radar_new_high_days_min: int = 3
    leader_radar_pullback_max_pct: float = 28.0
    leader_radar_vol_ratio_min: float = 0.75
    alpha_board_enabled: bool = True
    alpha_board_limit: int = 80
    alpha_min_score: float = 42.0
    alpha_breakout_recent_days: int = 5
    alpha_breakout_prior_window: int = 60
    alpha_breakout_day_pct_min: float = 5.0
    alpha_breakout_mid_break_allow_pct: float = 2.0
    alpha_breakout_close_drawdown_max_pct: float = 6.0
    alpha_breakout_ret20_min: float = 10.0
    alpha_breakout_ret20_max: float = 65.0
    alpha_breakout_ret60_min: float = 15.0
    alpha_breakout_vol_ratio_min: float = 1.05
    alpha_launchpad_ret60_min: float = 18.0
    alpha_launchpad_ret120_min: float = 25.0
    alpha_launchpad_ret20_min: float = -8.0
    alpha_launchpad_ret20_max: float = 28.0
    alpha_tight_base_ret60_min: float = 25.0
    alpha_tight_base_range20_max: float = 22.0
    alpha_tight_base_near_high_min: float = -14.0
    alpha_volatile_pullback_ret20_min: float = 8.0
    alpha_volatile_pullback_ret60_min: float = 20.0
    alpha_volatile_pullback_range20_min: float = 18.0
    alpha_volatile_pullback_near_high_min: float = -25.0
    alpha_volatile_pullback_vol_ratio_max: float = 2.2
    alpha_accum_price_from_low_max: float = 0.65
    alpha_accum_range60_max: float = 45.0
    alpha_bias200_soft_max: float = 95.0
    alpha_ret20_overheat: float = 75.0

    # Accumulation ABC 细化（Layer 2 增强）
    enable_accum_abc_detail: bool = True
    accum_b_test_count: int = 3  # B 阶段需要测试底部至少 N 次
    accum_c_max_drop_ratio: float = 0.03  # C 阶段下跌不超过 A 低的 3%

    # Exit 策略（Layer 5）
    enable_exit_signals: bool = True
    exit_stop_loss_pct: float = -7.0  # 网格优化最佳：-7%/+18%（夏普2.493），-6%偏紧，-8%偏松
    exit_trailing_active_pct: float = 15.0  # 利润激活线：从底部上涨超过此比例，激活移动跟踪止损
    exit_trailing_drawdown_pct: float = -10.0  # 利润保护线：高位跟踪回撤止损幅度（%）
    exit_confirm_days: int = 2  # 洗盘过滤：连续 N 日收盘低于止损线才确认
    exit_vol_confirm_ratio: float = 0.8  # 确认期量比阈值（低于此值视为缩量洗盘不触发）
    exit_holiday_grace_days: int = 1  # 节后宽限期：跨 ≥3 自然日后跳过 N 个交易日止损
    exit_holiday_grace_dynamic_enabled: bool = True
    exit_holiday_grace_max_days: int = 2
    exit_holiday_grace_min_money_flow_score: float = -5.0

    # Distribution 识别：高位缩量警告
    dist_high_threshold_pct: float = 30.0  # 相对 MA200 的高度（%）
    dist_vol_dry_ratio: float = 0.5  # 高位缩量比
    dist_confirm_days: int = 3  # 需要连续确认 N 日
    dist_upthrust_enabled: bool = True
    dist_upthrust_lookback: int = 60
    dist_upthrust_breakout_pct: float = 1.0
    dist_upthrust_close_back_pct: float = 0.3
    dist_upthrust_upper_shadow_ratio: float = 0.35
    dist_upthrust_vol_ratio: float = 1.5
    dist_upthrust_min_bias_200_pct: float = 15.0


class FunnelResult(NamedTuple):
    layer1_symbols: list[str]
    layer2_symbols: list[str]
    layer3_symbols: list[str]
    top_sectors: list[str]
    triggers: dict[str, list[tuple[str, float]]]
    # 威科夫阶段细节
    stage_map: dict[str, str]  # code -> stage_name（如 "Accumulation A"、"Markup"、"Distribution"）
    markup_symbols: list[str]  # 已进入 Markup 的股票
    exit_signals: dict[str, dict]  # code -> stop_loss / distribution_warning / upthrust_warning
    channel_map: dict[str, str]
    leader_radar_symbols: list[str]
    leader_radar_rows: list[dict[str, Any]]
    candidate_entries: list[dict[str, Any]] = []
    layer3_score_map: dict[str, float] = {}


class _FunnelLayerState(NamedTuple):
    df_map: dict[str, pd.DataFrame]
    l1: list[str]
    l2: list[str]
    l3: list[str]
    top_sectors: list[str]
    channel_map: dict[str, str]
    triggers: dict[str, list[tuple[str, float]]]


class _FunnelStageState(NamedTuple):
    stage_map: dict[str, str]
    markup_symbols: list[str]
    exit_signals: dict[str, dict]
    leader_radar_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class Layer2EvaluationContext:
    benchmark: BenchmarkContext
    rps: RpsContext


def build_layer2_evaluation_context(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    *,
    rps_universe: list[str] | None = None,
) -> Layer2EvaluationContext:
    return Layer2EvaluationContext(
        benchmark=build_benchmark_context(
            bench_df,
            cfg,
            sort_frame=sort_by_date_if_needed,
            latest_trade_date=_latest_trade_date,
        ),
        rps=build_rps_context(
            symbols,
            df_map,
            cfg,
            rps_universe=rps_universe,
            sort_frame=sort_by_date_if_needed,
        ),
    )


# Layer 1: 剥离垃圾


def dollar_volume_series(df_sorted: pd.DataFrame) -> pd.Series:
    """成交额序列：amount 字段缺失/恒为 0（如 TickFlow 港股/美股数据源限制）时回退为 close*volume。

    供 core 层 L1 流动性过滤与 workflows 层港股风险门禁/回测共用，
    确保同一份历史数据在不同调用方下的"成交额"口径一致。
    """
    if "amount" not in df_sorted.columns:
        if {"close", "volume"}.issubset(df_sorted.columns):
            close = pd.to_numeric(df_sorted["close"], errors="coerce")
            volume = pd.to_numeric(df_sorted["volume"], errors="coerce")
            return (close * volume).dropna()
        return pd.Series(dtype=float)
    amount = pd.to_numeric(df_sorted["amount"], errors="coerce")
    if (amount.fillna(0.0) <= 0).all() and {"close", "volume"}.issubset(df_sorted.columns):
        close = pd.to_numeric(df_sorted["close"], errors="coerce")
        volume = pd.to_numeric(df_sorted["volume"], errors="coerce")
        amount = close * volume
    return amount.dropna()


def _amount_liquidity_ok(
    df_sorted: pd.DataFrame,
    cfg: FunnelConfig,
    *,
    min_avg_amount_wan: float | None = None,
) -> bool:
    window = max(int(cfg.amount_avg_window), 1)
    amount = dollar_volume_series(df_sorted).tail(window)
    if amount.empty:
        return True
    threshold = float(cfg.min_avg_amount_wan if min_avg_amount_wan is None else min_avg_amount_wan) * 10000
    avg_amt = amount.mean()
    if pd.notna(avg_amt) and avg_amt < threshold:
        return False
    if not cfg.amount_skew_check_enabled or len(amount) < 5:
        return True
    positive = amount[amount > 0]
    if len(positive) < 5:
        return True
    skew = positive.skew()
    median_weak = positive.median() < threshold * cfg.amount_median_min_ratio
    pass_days_weak = float((positive >= threshold).mean()) < cfg.amount_pass_days_min_ratio
    spike_distorted = pd.notna(skew) and float(skew) >= cfg.amount_skew_max
    return not (spike_distorted and (median_weak or pass_days_weak))


def _latest_close_ok(df_sorted: pd.DataFrame, cfg: FunnelConfig) -> bool:
    close = pd.to_numeric(df_sorted.get("close"), errors="coerce").dropna()
    if close.empty:
        return True
    return float(close.iloc[-1]) >= float(cfg.l1_min_close_price)


def _market_cap_floor_ok(cap: float, cfg: FunnelConfig) -> bool:
    return float(cap or 0.0) >= float(cfg.l1_delist_risk_cap_floor_yi)


def _market_cap_ok(
    sym: str, market_cap_map: dict[str, float], df_map: dict[str, pd.DataFrame], cfg: FunnelConfig
) -> bool:
    if not market_cap_map:
        return True
    cap = market_cap_map.get(sym, 0.0)
    if not _market_cap_floor_ok(cap, cfg):
        return False
    if cap >= cfg.min_market_cap_yi:
        return True
    df = df_map.get(sym)
    return bool(
        df is not None
        and not df.empty
        and _amount_liquidity_ok(sort_by_date_if_needed(df), cfg, min_avg_amount_wan=cfg.l1_cap_bypass_amount_wan)
    )


def layer1_filter(
    symbols: list[str],
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    *,
    financial_map: dict[str, dict] | None = None,
) -> list[str]:
    """
    硬过滤：剔除 ST、非目标板块、市值<阈值、近期均成交额<阈值。
    market_cap_map 单位：亿元。若 market_cap_map 为空则跳过市值过滤。
    financial_map 来自 TickFlow，可选；有则追加 ROE / 资产负债率硬过滤。
    """
    fin_available = bool(financial_map)
    passed: list[str] = []
    l1_roe_negative = 0
    l1_high_debt = 0
    for sym in symbols:
        if cfg.require_cn_main_or_chinext and not is_supported_cn_board(sym, include_bse=cfg.include_bse_board):
            continue
        name = name_map.get(sym, "")
        if "ST" in name.upper():
            continue
        if not _market_cap_ok(sym, market_cap_map, df_map, cfg):
            continue
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        df_sorted = sort_by_date_if_needed(df)
        if not _latest_close_ok(df_sorted, cfg):
            continue
        if not _amount_liquidity_ok(df_sorted, cfg):
            continue
        if fin_available:
            metrics = financial_map.get(sym)
            if metrics:
                roe = metrics.get("roe")
                if roe is not None and roe < -10:
                    l1_roe_negative += 1
                    continue
                debt_ratio = metrics.get("debt_to_asset_ratio")
                if debt_ratio is not None and debt_ratio > 85:
                    l1_high_debt += 1
                    continue
        passed.append(sym)
    if fin_available and (l1_roe_negative or l1_high_debt):
        logger.info("[L1] 财务过滤: ROE<-10%%=%s, 负债率>85%%=%s", l1_roe_negative, l1_high_debt)
    return passed


# Layer 2: 强弱甄别


def layer2_strength_detailed(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    *,
    rps_universe: list[str] | None = None,
    channel_hit_counter: dict[str, int] | None = None,
    rejections: dict[str, str] | None = None,
    evaluation_context: Layer2EvaluationContext | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    """
    Layer2 多通道强弱甄别。

    返回：
    - passed: 通过 Layer2 的股票
    - channel_map: code -> 通道标签
    - pre_ignition_list: 预点火观察池（未通过八通道但结构接近）

    channel_hit_counter（可选）: 传入空 dict 原地累加每个通道的原始命中次数
    （不受"是否已被其他通道命中"影响），用于诊断某通道是否长期 0 触发。
    """
    context = evaluation_context or build_layer2_evaluation_context(
        symbols,
        df_map,
        bench_df,
        cfg,
        rps_universe=rps_universe,
    )
    bench_ctx = context.benchmark
    rps_ctx = context.rps

    passed: list[str] = []
    channel_map: dict[str, str] = {}
    pre_ignition_list: list[str] = []
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or len(df) < cfg.ma_long:
            continue
        df_sorted = sort_by_date_if_needed(df)
        if (
            cfg.require_bench_latest_alignment
            and bench_ctx.latest_date is not None
            and _latest_trade_date(df_sorted) != bench_ctx.latest_date
        ):
            continue
        result = evaluate_layer2_symbol(
            sym,
            df_sorted,
            cfg,
            bench_ctx=bench_ctx,
            rps_ctx=rps_ctx,
            detect_sos=_detect_sos,
        )
        if channel_hit_counter is not None:
            for name, hit in result.channels.items():
                if hit:
                    channel_hit_counter[name] = channel_hit_counter.get(name, 0) + 1
        if result.passed:
            passed.append(sym)
            channel_map[sym] = result.channel
        else:
            if rejections is not None:
                _diagnose_symbol_rejection(sym, df_sorted, cfg, bench_ctx, rps_ctx, rejections)
            if result.pre_ignition:
                pre_ignition_list.append(sym)
    return passed, channel_map, pre_ignition_list


def _diagnose_symbol_rejection(
    sym: str,
    df_sorted: pd.DataFrame,
    cfg: FunnelConfig,
    bench_ctx: Any,
    rps_ctx: Any,
    rejections: dict[str, str],
) -> None:
    try:
        import core.layer2_strength as l2s

        state = l2s._symbol_state(df_sorted, cfg, bench_ctx)
        rps_state = l2s._rps_state(sym, df_sorted["close"], df_sorted, cfg, rps_ctx)
        momentum_rs_ok, ambush_rs_ok = l2s._rs_flags(df_sorted, state, cfg, bench_ctx, rps_state.slow)
        rejections[sym] = l2s.diagnose_layer2_symbol_failure(
            sym,
            df_sorted,
            cfg,
            bench_ctx=bench_ctx,
            rps_ctx=rps_ctx,
            rps_state=rps_state,
            momentum_rs_ok=momentum_rs_ok,
            ambush_rs_ok=ambush_rs_ok,
            detect_sos=_detect_sos,
        )
    except Exception as exc:
        logger.warning("diagnose symbol %s L2 failure failed: %s", sym, exc)
        rejections[sym] = "八通道诊断失败"


# Layer 3: 板块共振


def _compute_sector_strength(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame] | None,
) -> dict[str, float]:
    """个股强度：20日收益(40%) + 5日收益(30%) + 3日收益(30%) 的截面百分位分数。"""
    if not df_map:
        return {}
    rows: list[tuple[str, float, float, float]] = []
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        s = sort_by_date_if_needed(df)
        close = pd.to_numeric(s.get("close"), errors="coerce").dropna()
        if len(close) <= 20:
            continue
        ret20 = (float(close.iloc[-1]) - float(close.iloc[-21])) / float(close.iloc[-21]) * 100.0
        ret5 = (
            (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100.0 if len(close) > 5 else ret20
        )
        ret3 = (
            (float(close.iloc[-1]) - float(close.iloc[-4])) / float(close.iloc[-4]) * 100.0 if len(close) > 3 else ret5
        )
        rows.append((sym, ret20, ret5, ret3))
    if not rows:
        return {}
    st_df = pd.DataFrame(rows, columns=["sym", "ret20", "ret5", "ret3"])
    st_df["q20"] = st_df["ret20"].rank(pct=True, ascending=True, method="average")
    st_df["q5"] = st_df["ret5"].rank(pct=True, ascending=True, method="average")
    st_df["q3"] = st_df["ret3"].rank(pct=True, ascending=True, method="average")
    st_df["strength"] = 0.4 * st_df["q20"] + 0.3 * st_df["q5"] + 0.3 * st_df["q3"]
    return st_df.set_index("sym")["strength"].astype(float).to_dict()


def _build_sector_groups(
    symbols: list[str],
    sector_map: dict[str, str],
    concept_map: dict[str, list[str]] | None,
    use_concept: bool,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """构建板块聚合：概念优先(多标签)，行业兜底(单标签)。返回 (counts, sym_sectors)。"""
    counts: dict[str, int] = {}
    sym_sectors: dict[str, list[str]] = {}
    for sym in symbols:
        sectors: list[str] = []
        if use_concept and concept_map:
            sectors = list(dict.fromkeys(str(item).strip() for item in concept_map.get(sym, []) if str(item).strip()))
        if not sectors:
            industry = sector_map.get(sym, "")
            if industry:
                sectors = [industry]
        sym_sectors[sym] = sectors
        for s in sectors:
            counts[s] = counts.get(s, 0) + 1
    return counts, sym_sectors


def _compute_sector_thresholds(
    counts: dict[str, int],
    base_counts: dict[str, int],
    sector_strength_map: dict[str, float],
    cfg: FunnelConfig,
) -> tuple[int, float, float, float, dict[str, float]]:
    """计算板块筛选的各项动态阈值。返回 (count_threshold, pass_threshold, strength_threshold, super_threshold, pass_ratio_map)。"""
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    min_count = max(int(cfg.sector_min_count), 1)
    q = min(max(float(cfg.sector_count_quantile), 0.0), 1.0)
    size_arr = np.array(list(counts.values()), dtype=float)
    q_count = int(np.ceil(np.quantile(size_arr, q))) if size_arr.size > 0 else min_count
    threshold = max(min_count, q_count)

    pass_ratio_map: dict[str, float] = {}
    pass_ratios: list[float] = []
    for sec, cnt in ranked:
        ratio = float(cnt) / float(max(int(base_counts.get(sec, 0)), 1))
        pass_ratio_map[sec] = ratio
        pass_ratios.append(ratio)
    pass_threshold = float(np.quantile(np.array(pass_ratios, dtype=float), q)) if pass_ratios else 0.0

    strength_vals = list(sector_strength_map.values())
    strength_threshold = float(np.quantile(np.array(strength_vals, dtype=float), q)) if strength_vals else 0.0
    super_q = min(max(float(getattr(cfg, "sector_super_strength_quantile", 0.90)), 0.0), 1.0)
    super_threshold = float(np.quantile(np.array(strength_vals, dtype=float), super_q)) if strength_vals else 0.0
    return threshold, pass_threshold, strength_threshold, super_threshold, pass_ratio_map


def _rank_and_filter_sectors(
    counts: dict[str, int],
    base_counts: dict[str, int],
    sector_strength_map: dict[str, float],
    cfg: FunnelConfig,
    hot_concepts: list[str] | None,
) -> tuple[list[str], list[str]]:
    """从板块 counts 中选出 keep_sectors 和 top_sectors。"""
    threshold, pass_threshold, strength_threshold, super_threshold, pass_ratio_map = _compute_sector_thresholds(
        counts, base_counts, sector_strength_map, cfg
    )
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    min_count = max(int(cfg.sector_min_count), 1)
    heat_min = cfg.sector_heat_bypass_min_count
    heat_bypass = {s for s, c in ranked if heat_min > 0 and c >= heat_min}
    hot_set = set(hot_concepts or [])
    normalized_hot_set = {normalize_theme_name(item) for item in hot_set}

    keep_sectors: list[str] = list(heat_bypass)
    for s, c in ranked:
        if s in heat_bypass:
            continue
        str_val = sector_strength_map.get(s, 0.0)
        normal_pass = c >= threshold and pass_ratio_map.get(s, 0.0) >= pass_threshold and str_val >= strength_threshold
        super_pass = c >= min_count and str_val >= super_threshold
        hot_pass = (s in hot_set or normalize_theme_name(s) in normalized_hot_set) and c >= min_count
        if normal_pass or super_pass or hot_pass:
            keep_sectors.append(s)
    if not keep_sectors:
        size_arr = np.array(list(counts.values()), dtype=float)
        max_count = int(size_arr.max()) if size_arr.size > 0 else 0
        keep_sectors = [s for s, c in ranked if c == max_count]

    keep_sectors_sorted = sorted(
        keep_sectors,
        key=lambda s: (
            -(1.0 if s in hot_set or normalize_theme_name(s) in normalized_hot_set else 0.0),
            -sector_strength_map.get(s, 0.0),
            -counts.get(s, 0),
            s,
        ),
    )
    top_n = max(int(cfg.top_n_sectors), 0)
    top_sectors = keep_sectors_sorted[:top_n] if top_n > 0 else keep_sectors_sorted
    return keep_sectors_sorted, top_sectors


def _compute_per_sector_strength(
    counts: dict[str, int],
    sym_sectors: dict[str, list[str]],
    symbols: list[str],
    strength_map: dict[str, float],
) -> dict[str, float]:
    """计算每个板块的中位强度分数。"""
    sector_strength: dict[str, float] = {}
    for sec in counts:
        vals = [strength_map[sym] for sym in symbols if sec in sym_sectors.get(sym, []) and sym in strength_map]
        sector_strength[sec] = float(np.median(vals)) if vals else 0.0
    return sector_strength


def layer3_sector_resonance(
    symbols: list[str],
    sector_map: dict[str, str],
    cfg: FunnelConfig,
    base_symbols: list[str] | None = None,
    df_map: dict[str, pd.DataFrame] | None = None,
    concept_map: dict[str, list[str]] | None = None,
    hot_concepts: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """
    板块共振过滤：概念优先(多标签)，行业兜底(单标签)。
    hot_concepts（主线概念）享有准入优待。
    返回 (filtered_symbols, top_sectors)。
    """
    if base_symbols is None:
        base_symbols = symbols

    use_concept = bool(cfg.use_concept_map and concept_map)
    counts, sym_sectors = _build_sector_groups(symbols, sector_map, concept_map, use_concept)
    if not counts:
        return symbols, []

    base_counts, _ = _build_sector_groups(base_symbols, sector_map, concept_map, use_concept)
    strength_map = _compute_sector_strength(symbols, df_map)
    sector_strength_map = _compute_per_sector_strength(counts, sym_sectors, symbols, strength_map)

    keep_sectors_sorted, top_sectors = _rank_and_filter_sectors(
        counts, base_counts, sector_strength_map, cfg, hot_concepts
    )

    top_sector_set = set(top_sectors)
    keep_sector_set = set(keep_sectors_sorted)
    hot_set = set(hot_concepts or [])
    normalized_hot_set = {normalize_theme_name(item) for item in hot_set}
    filtered: list[str] = []
    for sym in symbols:
        sym_secs = set(sym_sectors.get(sym, []))
        sym_theme_secs = {normalize_theme_name(s) for s in sym_secs}
        sym_strength = strength_map.get(sym, 0.0)
        if sym_secs & top_sector_set:
            filtered.append(sym)
        elif sym_secs & keep_sector_set and sym_strength >= cfg.l3_keep_strength_min:
            filtered.append(sym)
        elif (
            sym_secs & hot_set or sym_theme_secs & normalized_hot_set
        ) and sym_strength >= cfg.l3_hot_leader_strength_min:
            filtered.append(sym)
        elif sym_strength >= cfg.l3_leader_strength_min:
            filtered.append(sym)

    if len(filtered) < 3:
        filtered = list(symbols)

    return filtered, top_sectors


TREND_CHANNEL_TAGS = ("主升通道", "趋势延续", "点火破局", "加速突破")


def _is_star_board(code: str) -> bool:
    return str(code).startswith(("688", "689"))


def _effective_entry_max_bias_200(
    code: str,
    channel: str,
    cfg: FunnelConfig,
    *,
    rps_slow: float | None = None,
    ret120_pct: float | None = None,
) -> float:
    limit = float(cfg.global_entry_max_bias_200)
    is_star = _is_star_board(code)
    if is_star:
        limit = max(limit, float(getattr(cfg, "star_entry_max_bias_200", limit)))
    is_trend = any(tag in str(channel or "") for tag in TREND_CHANNEL_TAGS)
    if is_trend:
        limit = max(limit, float(getattr(cfg, "trend_entry_max_bias_200", limit)))
        limit = _boost_bias_for_strong_rps(
            limit,
            cfg,
            rps_slow=rps_slow,
            ret120_pct=ret120_pct,
            is_star=is_star,
        )
    return limit


def _boost_bias_for_strong_rps(
    limit: float,
    cfg: FunnelConfig,
    *,
    rps_slow: float | None,
    ret120_pct: float | None = None,
    is_star: bool = False,
) -> float:
    if rps_slow is None:
        return limit
    min_rps = float(getattr(cfg, "strong_rps_slow_min", 90.0))
    if float(rps_slow) < min_rps:
        return limit
    min_ret = float(getattr(cfg, "strong_ret120_min_pct", 40.0))
    if ret120_pct is None or float(ret120_pct) < min_ret:
        return limit
    cap_name = "strong_rps_star_bias_cap" if is_star else "strong_rps_trend_bias_cap"
    return max(limit, float(getattr(cfg, cap_name, limit)))


def _universe_rps_slow_map(
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
) -> dict[str, float]:
    """在 df_map 全宇宙上计算 120 日收益百分位（RPS120 近似）。

    必须用全量/近似全量 universe，禁止用 L3 小集合排名冒充全市场 RPS。
    """
    window = max(int(getattr(cfg, "rps_window_slow", 120)), 1)
    rets = _close_return_map(df_map, window)
    if len(rets) < 30:
        # 样本过少时不输出伪 RPS，避免诊断/旁路入口误放宽。
        return {}
    series = pd.Series(rets, dtype=float)
    ranks = series.rank(pct=True, method="average") * 100.0
    return {str(k): float(v) for k, v in ranks.items()}


def _close_return_map(df_map: dict[str, pd.DataFrame], window: int) -> dict[str, float]:
    rets: dict[str, float] = {}
    for sym, df in df_map.items():
        if df is None or getattr(df, "empty", True) or "close" not in df.columns:
            continue
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(close) <= window:
            continue
        start = float(close.iloc[-window - 1])
        end = float(close.iloc[-1])
        if start > 0:
            rets[str(sym)] = (end / start - 1.0) * 100.0
    return rets


def _ret120_pct(df: pd.DataFrame | None, cfg: FunnelConfig) -> float | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    window = max(int(getattr(cfg, "rps_window_slow", 120)), 1)
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) <= window:
        return None
    start = float(close.iloc[-window - 1])
    end = float(close.iloc[-1])
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _entry_bias_limit(cfg: FunnelConfig, max_bias_200: float | None) -> float:
    return float(cfg.global_entry_max_bias_200 if max_bias_200 is None else max_bias_200)


def _bias_200_exceeds_limit(close: pd.Series, cfg: FunnelConfig, max_bias_200: float | None) -> bool:
    if len(close) < 200:
        return False
    ma200_last = close.rolling(200).mean().iloc[-1]
    close_last = close.iloc[-1]
    if pd.isna(ma200_last) or pd.isna(close_last) or float(ma200_last) <= 0:
        return False
    bias_200 = (float(close_last) - float(ma200_last)) / float(ma200_last) * 100.0
    return bias_200 > _entry_bias_limit(cfg, max_bias_200)


# Layer 4: 威科夫狙击


def _is_trading_range_context(zone: pd.DataFrame, cfg: FunnelConfig, df_full: pd.DataFrame = None) -> bool:
    """
    Spring 必须先发生在可接受的交易区间（TR）内。
    使用 ATR_pct 动态计算可接受的合理振幅。
    """
    if zone is None or zone.empty:
        return False
    high = pd.to_numeric(zone.get("high"), errors="coerce")
    low = pd.to_numeric(zone.get("low"), errors="coerce")
    close = pd.to_numeric(zone.get("close"), errors="coerce")
    if high.isna().all() or low.isna().all() or close.isna().all():
        return False

    high_max = float(high.max())
    low_min = float(low.min())
    if low_min <= 0:
        return False
    range_pct = (high_max - low_min) / low_min * 100.0

    # --- 动态 ATR 振幅阈值计算 ---
    max_allowed_range_pct = cfg.spring_tr_max_range_pct  # 兜底默认值 30.0
    if df_full is not None and len(df_full) > getattr(cfg, "spring_tr_atr_window", 20):
        h = pd.to_numeric(df_full["high"], errors="coerce")
        l = pd.to_numeric(df_full["low"], errors="coerce")
        c = pd.to_numeric(df_full["close"], errors="coerce")
        prev_c = c.shift(1)

        # 真实波动幅度 True Range
        tr1 = h - l
        tr2 = (h - prev_c).abs()
        tr3 = (l - prev_c).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(getattr(cfg, "spring_tr_atr_window", 20)).mean()

        last_atr = float(atr.iloc[-1])
        last_c = float(c.iloc[-1])
        if pd.notna(last_atr) and pd.notna(last_c) and last_c > 0:
            atr_pct = (last_atr / last_c) * 100.0
            max_allowed_range_pct = atr_pct * getattr(cfg, "spring_tr_atr_max_multiple", 4.0)
            # 放松动态振幅：不再死板卡 15~45，而是最低保底为原始配置（通常是30%），最高可达 60%，给大蓝筹和大盘股透气
            max_allowed_range_pct = min(max(max_allowed_range_pct, float(cfg.spring_tr_max_range_pct)), 60.0)

    if range_pct > max_allowed_range_pct:
        return False

    c_start = float(close.iloc[0])
    c_end = float(close.iloc[-1])
    if c_start <= 0:
        return False
    drift_pct = abs((c_end - c_start) / c_start * 100.0)
    return not drift_pct > cfg.spring_tr_max_drift_pct


def _detect_market(code: str) -> str:
    """根据代码后缀识别市场：A股=cn, 港股=hk, 美股=us。"""
    upper = code.strip().upper()
    if upper.endswith(".HK"):
        return "hk"
    if upper.endswith(".US"):
        return "us"
    return "cn"


def _is_frozen_board_day(row: pd.Series, *, market: str = "cn") -> bool:
    """判断某交易日是否为一字板（开=高=低=收，全天几乎无波动）。

    A 股涨跌停制度下，一字板当天几乎没有真实换手：价格被封死，
    "放量"和"收盘收回"这类量价确认在物理上不具备意义，不能作为
    有效的 Spring 支撑测试，否则会把"洗出所有筹码的一字跌停"误判为洗盘信号。
    港股/美股无涨跌停制度，技术上不存在一字板，直接返回 False。
    """
    if market in ("hk", "us"):
        return False
    o, h, low_, c = (float(row.get(k, 0.0) or 0.0) for k in ("open", "high", "low", "close"))
    if c <= 0:
        return False
    day_range_pct = (h - low_) / c * 100.0
    return day_range_pct <= 1.0 and abs(o - c) / c * 100.0 <= 1.0


# 20% 涨跌停板块（创业板/科创板）相对 10% 主板/北交所的量能阈值放大系数。
# 用 sqrt(20/10) 而非线性 2 倍：涨跌停幅度翻倍不代表"有效换手"的量能门槛也要翻倍，
# 否则门槛会形同虚设；同一套绝对阈值（如放量3倍）套用在两类板块上，对20%板块
# 明显过松——它们的日常波动率本就更高，"放量"更容易被正常噪音触发。
_REGISTRATION_BOARD_VOL_SCALE = 1.41


def _board_vol_ratio_scale(code: str, *, market: str = "cn") -> float:
    """按涨跌停幅度返回量能阈值缩放系数：A股主板/北交所=1.0，创业板/科创板放大；港股/美股=1.0。"""
    if market in ("hk", "us"):
        return 1.0
    return _REGISTRATION_BOARD_VOL_SCALE if cn_board(code) in {"chinext", "star"} else 1.0


def _spring_support_level(support_zone: pd.DataFrame) -> float:
    swing_lows = swing_values(support_zone["low"], kind="low", window=3)
    if len(swing_lows) >= 2:
        return float(pd.Series(swing_lows[-5:]).median())
    return float(pd.to_numeric(support_zone["close"], errors="coerce").min())


def _detect_spring(
    df: pd.DataFrame, cfg: FunnelConfig, max_bias_200: float | None = None, code: str = ""
) -> float | None:
    """
    Spring（终极震仓）：允许"前一日或当日盘中"跌破近 N 日支撑位，且当日收盘收回并放量。
    返回 score（收回幅度%）或 None。
    """
    if len(df) < cfg.spring_support_window + 2:
        return None
    df_s = sort_by_date_if_needed(df)
    # 修正：支撑位不能包含正在进行跌破测试的前一日（prev）
    support_zone = df_s.iloc[-(cfg.spring_support_window + 2) : -2]
    # 调用时把历史前序 df_full 传进去计算 ATR
    if not _is_trading_range_context(support_zone, cfg, df_full=df_s.iloc[:-2]):
        return None
    support_level = _spring_support_level(support_zone)
    prev = df_s.iloc[-2]
    last = df_s.iloc[-1]

    # 一字跌停/涨停日几乎没有真实换手，排除在有效 Spring 测试之外。
    mkt = _detect_market(code)
    if _is_frozen_board_day(prev, market=mkt) or _is_frozen_board_day(last, market=mkt):
        return None

    if _bias_200_exceeds_limit(pd.to_numeric(df_s["close"], errors="coerce"), cfg, max_bias_200):
        return None

    # 允许单日盘中洗盘（长下影锤子线）：只要 prev/last 至少一日跌破即可。
    if (prev["low"] >= support_level) and (last["low"] >= support_level):
        return None
    if last["close"] <= support_level:
        return None
    vol_scale = _board_vol_ratio_scale(code, market=mkt)
    vol_avg = df_s["volume"].tail(5).iloc[:-1].mean()
    if vol_avg <= 0 or last["volume"] < vol_avg * cfg.spring_vol_ratio * vol_scale:
        return None

    prev_vol = float(prev["volume"]) if pd.notna(prev["volume"]) else 0
    last_vol = float(last["volume"]) if pd.notna(last["volume"]) else 0
    if prev_vol > 0 and last_vol / prev_vol < cfg.spring_vol_expand_ratio:
        return None

    recovery = (last["close"] - support_level) / support_level * 100
    return float(recovery)


def _detect_lps(df: pd.DataFrame, cfg: FunnelConfig, max_bias_200: float | None = None, code: str = "") -> float | None:
    """
    LPS（最后支撑点缩量）：近 N 日回踩 MA20 且缩量。
    返回 score（缩量比）或 None。
    """
    if len(df) < max(cfg.lps_vol_ref_window, cfg.lps_ma) + cfg.lps_lookback:
        return None
    df_s = sort_by_date_if_needed(df)
    close = df_s["close"].astype(float)
    ma = close.rolling(cfg.lps_ma).mean()
    last_ma = ma.iloc[-1]
    if pd.isna(last_ma) or last_ma <= 0:
        return None

    recent = df_s.tail(cfg.lps_lookback)
    last_close = close.iloc[-1]
    if last_close < last_ma:
        return None

    if _bias_200_exceeds_limit(close, cfg, max_bias_200):
        return None

    rising_offset = cfg.lps_lookback + cfg.lps_ma_rising_window
    if len(ma) > rising_offset:
        ma_prev = ma.iloc[-rising_offset]
        if pd.isna(ma_prev) or last_ma <= float(ma_prev):
            return None

    low_near_ma = recent["low"].min()
    if abs(low_near_ma - last_ma) / last_ma > cfg.lps_ma_tolerance:
        return None

    recent_max_vol = recent["volume"].max()
    ref_window_df = df_s.tail(cfg.lps_vol_ref_window + cfg.lps_lookback).iloc[: -cfg.lps_lookback]
    ref_max_vol = ref_window_df["volume"].max() if not ref_window_df.empty else 0
    if ref_max_vol <= 0:
        return None
    vol_ratio = recent_max_vol / ref_max_vol
    # 20%涨跌停板块日常波动更大，缩量比的合格线相应放宽，避免正常噪音误杀有效信号。
    mkt = _detect_market(code)
    if vol_ratio > cfg.lps_vol_dry_ratio * _board_vol_ratio_scale(code, market=mkt):
        return None
    if cfg.lps_creek_confirmation_enabled and not _lps_creek_confirmed(df_s, cfg):
        return None
    return float(vol_ratio)


def _swing_high_points(high: pd.Series, window: int) -> list[tuple[int, float]]:
    values = pd.to_numeric(high, errors="coerce").reset_index(drop=True)
    width = max(int(window), 1)
    points: list[tuple[int, float]] = []
    for index in range(width, len(values) - width):
        current = values.iloc[index]
        span = values.iloc[index - width : index + width + 1].dropna()
        if pd.notna(current) and not span.empty and float(current) == float(span.max()):
            points.append((index, float(current)))
    return points


def _creek_line(points: list[tuple[int, float]], cfg: FunnelConfig) -> tuple[float, float] | None:
    recent = points[-5:]
    if len(recent) < 2 or recent[-1][0] <= recent[0][0]:
        return None
    slope = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
    rise_pct = slope / recent[-1][1] * 100.0 if recent[-1][1] > 0 else float("inf")
    if rise_pct > cfg.lps_creek_max_rise_pct_per_bar:
        return None
    intercept = recent[-1][1] - slope * recent[-1][0]
    return float(slope), float(intercept)


def _lps_creek_confirmed(df: pd.DataFrame, cfg: FunnelConfig) -> bool:
    lps_days = max(int(cfg.lps_lookback), 1)
    breakout_days = max(int(cfg.lps_creek_breakout_lookback), 1)
    anchor_days = max(int(cfg.lps_creek_lookback), 20)
    total = anchor_days + breakout_days + lps_days
    frame = sort_by_date_if_needed(df).tail(total)
    if len(frame) < total:
        return False
    anchor = frame.iloc[:anchor_days]
    line = _creek_line(_swing_high_points(anchor["high"], cfg.lps_creek_swing_window), cfg)
    if line is None:
        return False
    slope, intercept = line
    close = pd.to_numeric(frame["close"], errors="coerce").reset_index(drop=True)
    breakout = close.iloc[anchor_days : anchor_days + breakout_days]
    crossed = any(
        float(value) >= (slope * index + intercept) * (1.0 + cfg.lps_creek_breakout_pct / 100.0)
        for index, value in breakout.items()
        if pd.notna(value) and slope * index + intercept > 0
    )
    current_line = slope * (len(frame) - 1) + intercept
    return bool(
        crossed
        and current_line > 0
        and pd.notna(close.iloc[-1])
        and float(close.iloc[-1]) >= current_line * (1.0 - cfg.lps_creek_hold_tolerance_pct / 100.0)
    )


class _EvrSeries(NamedTuple):
    frame: pd.DataFrame
    close: pd.Series
    low: pd.Series
    volume: pd.Series
    pct_chg: pd.Series


def _evr_series(df: pd.DataFrame) -> _EvrSeries | None:
    df_s = sort_by_date_if_needed(df)
    series = _EvrSeries(
        frame=df_s,
        close=pd.to_numeric(df_s["close"], errors="coerce"),
        low=pd.to_numeric(df_s["low"], errors="coerce"),
        volume=pd.to_numeric(df_s["volume"], errors="coerce"),
        pct_chg=pd.to_numeric(df_s["pct_chg"], errors="coerce"),
    )
    if (
        series.close.isna().all()
        or series.low.isna().all()
        or series.volume.isna().all()
        or series.pct_chg.isna().all()
    ):
        return None
    return series


def _evr_ref_volume_avg(volume: pd.Series, window: int) -> float | None:
    vol_ref = volume.tail(window).iloc[:-2]
    vol_ref_avg = float(vol_ref.mean()) if not vol_ref.empty else 0.0
    return vol_ref_avg if vol_ref_avg > 0 else None


def _evr_candidate_indexes(confirm_days: int) -> tuple[int, ...]:
    return (-2,) if confirm_days > 0 else (-1, -2)


def _evr_turnover_ok(frame: pd.DataFrame, idx: int, cfg: FunnelConfig) -> bool:
    if "turnover" not in frame.columns or float(cfg.evr_min_turnover) <= 0:
        return True
    day_turnover = pd.to_numeric(frame["turnover"], errors="coerce").iloc[idx]
    return bool(pd.isna(day_turnover) or float(day_turnover) >= float(cfg.evr_min_turnover))


def _evr_structure_ok(close: pd.Series, close_last: float) -> bool:
    if len(close) < 4:
        return True
    close_3d_ago = close.iloc[-4]
    return bool(pd.isna(close_3d_ago) or float(close_last) >= float(close_3d_ago) * 0.98)


def _evr_confirmation_ok(series: _EvrSeries, idx: int, confirm_days: int, cfg: FunnelConfig) -> bool:
    if confirm_days <= 0:
        return True
    event_pos = len(series.frame) + idx
    confirm_start = event_pos + 1
    confirm_end = confirm_start + confirm_days
    if confirm_end > len(series.frame):
        return False
    event_low = series.low.iloc[idx]
    confirm_close = series.close.iloc[confirm_start:confirm_end]
    if pd.isna(event_low) or confirm_close.empty or confirm_close.isna().all():
        return False
    allow_break = max(float(cfg.evr_confirm_allow_break_pct), 0.0) / 100.0
    return bool(float(confirm_close.min()) >= float(event_low) * (1.0 - allow_break))


def _detect_evr(df: pd.DataFrame, cfg: FunnelConfig, max_bias_200: float | None = None, code: str = "") -> float | None:
    """
    Effort vs Result（努力无结果）：
    仅识别"相对低位的巨量滞涨/抗跌"，排除高位派发。
    返回 score（量比）或 None。
    """
    min_required = cfg.evr_vol_window + 2 + max(int(cfg.evr_confirm_days), 0)
    if len(df) < min_required:
        return None
    series = _evr_series(df)
    if series is None:
        return None

    close_last = series.close.iloc[-1]
    if _bias_200_exceeds_limit(series.close, cfg, max_bias_200):
        return None

    mkt = _detect_market(code)
    vol_ref_avg = _evr_ref_volume_avg(series.volume, cfg.evr_vol_window)
    if vol_ref_avg is None:
        return None

    # 20%涨跌停板块日常波动更大：量比门槛、"滞涨"允许的日内波幅都需同步放宽，
    # 否则该信号在这些板块上几乎永远无法触发（正常波动就超过主板的滞涨阈值）。
    vol_scale = _board_vol_ratio_scale(code, market=mkt)
    max_drop = cfg.evr_max_drop * vol_scale
    max_rise = cfg.evr_max_rise * vol_scale
    confirm_days = max(int(cfg.evr_confirm_days), 0)
    for idx in _evr_candidate_indexes(confirm_days):
        # 一字板当天量能失真，EVR 的"努力无结果"判断不成立。
        if _is_frozen_board_day(series.frame.iloc[idx], market=mkt):
            continue
        vol_ratio = float(series.volume.iloc[idx] / vol_ref_avg) if vol_ref_avg > 0 else 0.0
        if vol_ratio < cfg.evr_vol_ratio:
            continue

        day_pct = series.pct_chg.iloc[idx]
        if pd.isna(day_pct):
            continue

        if float(day_pct) < -max_drop or float(day_pct) > max_rise:
            continue

        if not _evr_turnover_ok(series.frame, idx, cfg):
            continue
        if not _evr_structure_ok(series.close, float(close_last)):
            continue
        if not _evr_confirmation_ok(series, idx, confirm_days, cfg):
            continue
        return vol_ratio

    return None


class _SosSeries(NamedTuple):
    frame: pd.DataFrame
    close: pd.Series
    volume: pd.Series
    pct_chg: pd.Series
    high: pd.Series


def _sos_series(df: pd.DataFrame) -> _SosSeries | None:
    df_s = sort_by_date_if_needed(df)
    series = _SosSeries(
        frame=df_s,
        close=pd.to_numeric(df_s["close"], errors="coerce"),
        volume=pd.to_numeric(df_s["volume"], errors="coerce"),
        pct_chg=pd.to_numeric(df_s["pct_chg"], errors="coerce"),
        high=pd.to_numeric(df_s["high"], errors="coerce"),
    )
    if series.close.isna().all() or series.volume.isna().all() or series.pct_chg.isna().all():
        return None
    return series


def _sos_volume_ratio(volume: pd.Series, cfg: FunnelConfig) -> float | None:
    vol_window = getattr(cfg, "sos_vol_quantile_window", 60)
    vol_ref = volume.tail(vol_window + 1).iloc[:-1]
    if vol_ref.empty:
        return None
    vol_ref_avg = float(vol_ref.mean())
    if vol_ref_avg <= 0:
        return None
    last_vol = float(volume.iloc[-1])
    vol_ratio = last_vol / vol_ref_avg
    threshold = float(getattr(cfg, "sos_vol_ratio", 2.0))
    if vol_ratio < threshold:
        return None
    quantile = float(getattr(cfg, "sos_vol_quantile", 0.95))
    quantile_volume = float(vol_ref.quantile(min(max(quantile, 0.0), 1.0)))
    if quantile_volume > 0 and last_vol < quantile_volume:
        return None
    return vol_ratio


def _sos_breakout_or_ma_cross(series: _SosSeries, cfg: FunnelConfig) -> bool:
    close_last = series.close.iloc[-1]
    ma50 = series.close.rolling(50).mean()
    recent_highs = series.high.tail(cfg.sos_breakout_window + 1).iloc[:-1]
    max_recent_high = float(recent_highs.max()) if not recent_highs.empty else float("inf")
    breakout_tolerance = getattr(cfg, "sos_breakout_tolerance", 0.01)
    is_breakout = float(close_last) >= max_recent_high * (1.0 - breakout_tolerance)

    ma50_last = ma50.iloc[-1] if not ma50.empty else None
    ma50_prev = ma50.iloc[-2] if len(ma50) >= 2 else None
    is_ma_crossover = False
    if ma50_last is not None and pd.notna(ma50_last) and ma50_prev is not None and pd.notna(ma50_prev):
        prev_close = float(series.close.iloc[-2])
        is_ma_crossover = bool(prev_close <= float(ma50_prev) and float(close_last) > float(ma50_last))
    return bool(is_breakout or is_ma_crossover)


def _detect_sos(df: pd.DataFrame, cfg: FunnelConfig, max_bias_200: float | None = None, code: str = "") -> float | None:
    """
    Sign of Strength (SOS) / Jump Across the Creek (JAC):
    点火标志。特征为低位脱盘、放量大阳线，破除重要阻力或近期高点。
    返回 score（量比）或 None。
    """
    if len(df) < max(cfg.sos_vol_window, cfg.sos_breakout_window) + 2:
        return None

    series = _sos_series(df)
    if series is None:
        return None

    if _bias_200_exceeds_limit(series.close, cfg, max_bias_200):
        return None

    mkt = _detect_market(code)

    # 一字涨停板的 SOS 没有真实换手，不能视为有效 Sign of Strength。
    if _is_frozen_board_day(series.frame.iloc[-1], market=mkt):
        return None

    # 注：SOS 不对创业板/科创板做 vol_scale 放大。真实历史回放（627只A股，2021-2025）
    # 显示把 sos_pct_min/sos_vol_ratio 按 20% 板块整体抬高门槛后，被门槛卡掉的边际样本
    # 胜率（28.9%~38.0%）系统性高于留存样本（22.2%~29.1%），即门槛越高、留下的样本越差，
    # 说明 SOS 筛的是极端强势尾部，抬高门槛只会放大幸存者偏差，不是过滤噪声。
    day_pct = float(series.pct_chg.iloc[-1])
    if pd.isna(day_pct) or day_pct < cfg.sos_pct_min:
        return None

    vol_ratio = _sos_volume_ratio(series.volume, cfg)
    if vol_ratio is None:
        return None

    if not _sos_breakout_or_ma_cross(series, cfg):
        return None

    return vol_ratio


def _compression_ohlcv(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series] | None:
    df_s = sort_by_date_if_needed(df)
    close = pd.to_numeric(df_s["close"], errors="coerce")
    high = pd.to_numeric(df_s["high"], errors="coerce")
    low = pd.to_numeric(df_s["low"], errors="coerce")
    volume = pd.to_numeric(df_s["volume"], errors="coerce")
    if close.isna().all() or high.isna().all() or low.isna().all():
        return None
    return close, high, low, volume


def _compression_direction_ok(close: pd.Series, cfg: FunnelConfig) -> bool:
    if not cfg.compression_require_direction:
        return True
    direction_ok = False
    if len(close) >= 25:
        ma20 = close.rolling(20).mean()
        ma20_last = ma20.iloc[-1]
        ma20_prev = ma20.shift(5).iloc[-1]
        direction_ok = pd.notna(ma20_last) and pd.notna(ma20_prev) and float(ma20_last) >= float(ma20_prev)
    if len(close) >= 50:
        ma50_last = close.rolling(50).mean().iloc[-1]
        close_last = close.iloc[-1]
        ma50_ok = pd.notna(ma50_last) and pd.notna(close_last) and float(close_last) >= float(ma50_last)
        direction_ok = direction_ok or ma50_ok
    return direction_ok


def _compression_bias_ok(close: pd.Series, cfg: FunnelConfig, max_bias_200: float | None = None) -> bool:
    return not _bias_200_exceeds_limit(close, cfg, max_bias_200)


def _compression_atr_ratio(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    cfg: FunnelConfig,
) -> float | None:
    lookback = cfg.compression_lookback
    atr_w = cfg.compression_atr_window
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_pct = (tr / close) * 100.0
    hist_atr = atr_pct.iloc[-(atr_w + lookback) : -lookback]
    recent_atr = atr_pct.tail(lookback)
    if hist_atr.empty or recent_atr.empty:
        return None

    threshold = float(hist_atr.quantile(cfg.compression_atr_quantile))
    current_atr_avg = float(recent_atr.mean())
    if current_atr_avg > threshold:
        return None

    atr_vals = recent_atr.values
    violations = sum(1 for i in range(1, len(atr_vals)) if atr_vals[i] > atr_vals[i - 1])
    if violations > 1:
        return None

    hist_atr_median = float(hist_atr.median())
    return float(current_atr_avg / hist_atr_median) if hist_atr_median > 0 else None


def _detect_compression(
    df: pd.DataFrame, cfg: FunnelConfig, max_bias_200: float | None = None, code: str = ""
) -> float | None:
    """压缩蓄势：连续N日ATR收窄+缩量，爆发前夜形态。返回压缩比或None。"""
    lookback = cfg.compression_lookback
    atr_w = cfg.compression_atr_window
    if len(df) < atr_w + lookback + 5:
        return None
    ohlcv = _compression_ohlcv(df)
    if ohlcv is None:
        return None
    close, high, low, volume = ohlcv
    if not _compression_direction_ok(close, cfg) or not _compression_bias_ok(close, cfg, max_bias_200):
        return None
    mkt = _detect_market(code)
    vol_scale = _board_vol_ratio_scale(code, market=mkt)
    vol_ref = float(volume.iloc[-(atr_w + lookback) : -lookback].mean())
    vol_recent = float(volume.tail(lookback).mean())
    if vol_ref <= 0 or vol_recent / vol_ref > cfg.compression_vol_decline_ratio * vol_scale:
        return None
    return _compression_atr_ratio(close, high, low, volume, cfg)


def _trend_pullback_peak_idx(close: pd.Series, cfg: FunnelConfig) -> int | None:
    lookback = cfg.trend_pb_lookback
    ma = close.rolling(cfg.trend_pb_ma_window).mean()
    last_ma = ma.iloc[-1]
    if pd.isna(last_ma):
        return None
    ma_prev = ma.iloc[-(lookback + 1)]
    if pd.isna(ma_prev) or float(last_ma) <= float(ma_prev):
        return None

    recent = close.tail(lookback + 1)
    peak = float(recent.max())
    peak_idx = int(recent.values.argmax())
    if peak_idx < 1 or peak <= 0:
        return None
    trough = float(recent.iloc[peak_idx + 1 : -1].min()) if peak_idx + 1 < len(recent) - 1 else float(recent.iloc[-1])
    last_close = float(close.iloc[-1])
    pullback_pct = (peak - min(trough, last_close)) / peak * 100.0
    if pullback_pct < cfg.trend_pb_min_pullback_pct or pullback_pct > cfg.trend_pb_max_pullback_pct:
        return None
    if last_close <= float(close.iloc[-2]):
        return None
    return peak_idx


def _trend_pullback_vol_threshold(close: pd.Series, cfg: FunnelConfig, market_cap_yi: float) -> float:
    threshold = cfg.trend_pb_vol_shrink_ratio
    if market_cap_yi >= 200.0:
        threshold = min(threshold + 0.15, 0.85)
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    if len(ma50) < 200 or pd.isna(ma50.iloc[-1]) or pd.isna(ma200.iloc[-1]):
        return threshold

    streak = 0
    for i in range(1, min(len(ma50), 60) + 1):
        if pd.isna(ma50.iloc[-i]) or pd.isna(ma200.iloc[-i]) or float(ma50.iloc[-i]) <= float(ma200.iloc[-i]):
            break
        streak += 1
    if streak >= 20:
        threshold = min(threshold + 0.10, 0.90)
    return threshold


def _detect_trend_pullback(
    df: pd.DataFrame,
    cfg: FunnelConfig,
    market_cap_yi: float = 0.0,
    max_bias_200: float | None = None,
    code: str = "",
) -> float | None:
    """趋势回踩：上升趋势中缩量回调后企稳。返回 score (0~1, 越大越好)。"""
    lookback = cfg.trend_pb_lookback
    ma_w = cfg.trend_pb_ma_window
    if len(df) < ma_w + lookback + 5:
        return None
    df_s = sort_by_date_if_needed(df)
    close = pd.to_numeric(df_s["close"], errors="coerce")
    volume = pd.to_numeric(df_s["volume"], errors="coerce")
    if close.isna().all() or volume.isna().all():
        return None

    fallback = float(getattr(cfg, "trend_pb_max_bias_200", cfg.global_entry_max_bias_200))
    if _bias_200_exceeds_limit(close, cfg, fallback if max_bias_200 is None else max_bias_200):
        return None

    peak_idx = _trend_pullback_peak_idx(close, cfg)
    if peak_idx is None:
        return None

    # 缩量确认：回落段（排除峰值日）均量 / 上涨段均量
    vol_tail = volume.tail(lookback + 1)
    vol_up = float(vol_tail.iloc[: peak_idx + 1].mean())
    vol_down_slice = vol_tail.iloc[peak_idx + 1 :]
    if vol_down_slice.empty or vol_up <= 0:
        return None
    vol_down = float(vol_down_slice.mean())
    vol_ratio = vol_down / vol_up

    # 大市值放宽 + 饥饿模式（趋势持续久无触发）
    mkt = _detect_market(code)
    vol_scale = _board_vol_ratio_scale(code, market=mkt)
    threshold = _trend_pullback_vol_threshold(close, cfg, market_cap_yi) * vol_scale
    if vol_ratio > threshold:
        return None
    return float(1.0 - vol_ratio)


def _recent_sequence_events(df: pd.DataFrame, cfg: FunnelConfig) -> tuple[bool, bool]:
    frame = sort_by_date_if_needed(df).iloc[:-1].tail(max(int(cfg.signal_sequence_lookback), 1) + 20)
    if len(frame) < 21:
        return False, False
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    pct_chg = pd.to_numeric(frame.get("pct_chg", close.pct_change() * 100.0), errors="coerce")
    support = low.rolling(20).min().shift(1)
    resistance = high.rolling(20).max().shift(1)
    vol_ref = volume.rolling(20).mean().shift(1)
    recent = slice(-max(int(cfg.signal_sequence_lookback), 1), None)
    spring_like = ((low < support * 0.995) & (close > support * 1.005)).iloc[recent].fillna(False).any()
    sos_volume = max(float(cfg.sos_vol_ratio) * 0.65, 1.2)
    sos_like = (
        ((close >= resistance * 0.995) & (pct_chg >= float(cfg.sos_pct_min) * 0.75) & (volume >= vol_ref * sos_volume))
        .iloc[recent]
        .fillna(False)
        .any()
    )
    return bool(spring_like), bool(sos_like)


def _apply_signal_sequence_bonus(
    results: dict[str, list[tuple[str, float]]], df_map: dict[str, pd.DataFrame], cfg: FunnelConfig
) -> None:
    hit_keys: dict[str, set[str]] = {}
    for key, rows in results.items():
        for code, _score in rows:
            hit_keys.setdefault(str(code), set()).add(key)
    qualified: set[tuple[str, str]] = set()
    for code, keys in hit_keys.items():
        if not keys.intersection({"sos", "lps"}):
            continue
        spring_like, sos_like = _recent_sequence_events(df_map[code], cfg)
        if "sos" in keys and spring_like:
            qualified.add((code, "sos"))
        if "lps" in keys and (spring_like or sos_like):
            qualified.add((code, "lps"))
    bonus = float(cfg.signal_sequence_score_bonus)
    for key in ("sos", "lps"):
        results[key] = [
            (code, float(score) + bonus if (str(code), key) in qualified else float(score))
            for code, score in results[key]
        ]


def layer4_triggers(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    channel_map: dict[str, str] | None = None,
    market_cap_map: dict[str, float] | None = None,
    rps_slow_map: dict[str, float] | None = None,
) -> dict[str, list[tuple[str, float]]]:
    """在最终候选集上运行 Spring / LPS / EVR / Compression / SOS / TrendPullback 检测。"""
    results: dict[str, list[tuple[str, float]]] = {
        "sos": [],
        "spring": [],
        "lps": [],
        "evr": [],
        "compression": [],
        "trend_pullback": [],
    }
    if channel_map is None:
        channel_map = {}
    cap_map = market_cap_map or {}
    # 默认在 df_map 全宇宙上算 RPS，禁止用 L3 小集合局部排名冒充全市场。
    universe_rps = rps_slow_map if rps_slow_map is not None else _universe_rps_slow_map(df_map, cfg)

    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        channel = channel_map.get(sym, "")
        max_bias_200 = _effective_entry_max_bias_200(
            sym,
            channel,
            cfg,
            rps_slow=universe_rps.get(sym),
            ret120_pct=_ret120_pct(df, cfg),
        )
        score = _detect_spring(df, cfg, max_bias_200=max_bias_200, code=sym)
        if score is not None:
            results["spring"].append((sym, score))
        score = _detect_lps(df, cfg, max_bias_200=max_bias_200, code=sym)
        if score is not None:
            results["lps"].append((sym, score))
        if cfg.enable_evr_trigger:
            score = _detect_evr(df, cfg, max_bias_200=max_bias_200, code=sym)
            if score is not None:
                results["evr"].append((sym, score))
        if cfg.enable_compression_trigger:
            score = _detect_compression(df, cfg, max_bias_200=max_bias_200, code=sym)
            if score is not None:
                results["compression"].append((sym, score))
        score = _detect_sos(df, cfg, max_bias_200=max_bias_200, code=sym)
        if score is not None:
            results["sos"].append((sym, score))
        if cfg.enable_trend_pullback_trigger:
            if any(t in channel for t in TREND_CHANNEL_TAGS):
                cap_yi = float(cap_map.get(sym, 0.0) or 0.0)
                score = _detect_trend_pullback(df, cfg, market_cap_yi=cap_yi, max_bias_200=max_bias_200, code=sym)
                if score is not None:
                    results["trend_pullback"].append((sym, score))
    if cfg.signal_sequence_bonus_enabled:
        _apply_signal_sequence_bonus(results, df_map, cfg)
    return results


# Layer 2.5: Markup 阶段识别


def _detect_markup_entry(df: pd.DataFrame, cfg: FunnelConfig) -> float | None:
    """
    Markup 阶段：MA50 从下穿上 MA200，且在上方保持 N 日，确认进入上升趋势。
    返回 score（确认天数占比）或 None。
    """
    if len(df) < max(cfg.ma_long, cfg.markup_ma_crossover_confirm_days) + 5:
        return None

    df_s = sort_by_date_if_needed(df)
    close = df_s["close"].astype(float)
    ma_short = close.rolling(cfg.ma_short).mean()
    ma_long = close.rolling(cfg.ma_long).mean()

    if pd.isna(ma_short.iloc[-1]) or pd.isna(ma_long.iloc[-1]) or ma_short.iloc[-1] <= ma_long.iloc[-1]:
        return None

    # 检查过去 N 日内 MA50 是否从下穿上 MA200
    lookback = max(int(cfg.markup_ma_crossover_confirm_days * 2), 10)
    if len(ma_short) < lookback:
        return None

    recent_ma_short = ma_short.tail(lookback).values
    recent_ma_long = ma_long.tail(lookback).values

    # 寻找穿过点
    crossover_found = False
    for i in range(1, len(recent_ma_short)):
        if recent_ma_short[i - 1] <= recent_ma_long[i - 1] and recent_ma_short[i] > recent_ma_long[i]:
            crossover_found = True
            break

    if not crossover_found:
        return None

    # 确认最近 N 日持续在 MA200 上方
    confirm_days = max(int(cfg.markup_ma_crossover_confirm_days), 1)
    recent_above = sum(1 for j in range(-confirm_days, 0) if ma_short.iloc[j] > ma_long.iloc[j])

    if recent_above < confirm_days:
        return None

    # 计算 MA50 的角度（过去 5 日的变化率）
    ma_short_recent = ma_short.tail(6).values
    if len(ma_short_recent) < 2:
        return None

    angle = (ma_short_recent[-1] - ma_short_recent[0]) / ma_short_recent[0] * 100.0
    if angle < cfg.markup_ma_angle_min:
        return None

    return float(recent_above / confirm_days)


def detect_markup_stage(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
) -> list[str]:
    """
    返回已进入 Markup 阶段的股票。
    """
    if not cfg.enable_markup_detection:
        return []

    markup: list[str] = []
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        score = _detect_markup_entry(df, cfg)
        if score is not None:
            markup.append(sym)

    return markup


def _close_return_pct(close: pd.Series, lookback: int) -> float | None:
    s = pd.to_numeric(close, errors="coerce").dropna()
    lb = max(int(lookback), 1)
    if len(s) <= lb:
        return None
    start = float(s.iloc[-lb - 1])
    end = float(s.iloc[-1])
    return None if start <= 0 else (end / start - 1.0) * 100.0


def _leader_feature_row(
    code: str,
    df: pd.DataFrame,
    sector_map: dict[str, str],
    channel_map: dict[str, str],
    cfg: FunnelConfig,
) -> dict[str, Any] | None:
    df_s = sort_by_date_if_needed(df)
    close = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    if len(close) < 80:
        return None
    ret20 = _close_return_pct(close, 20)
    ret60 = _close_return_pct(close, 60)
    ret120 = _close_return_pct(close, 120)
    if ret20 is None or ret60 is None:
        return None
    high_window = min(max(int(cfg.leader_radar_new_high_window), 20), len(close))
    recent = close.tail(high_window)
    high_close = float(recent.max())
    last_close = float(close.iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    drawdown = ((recent / recent.cummax()) - 1.0).min() * 100.0
    vol = pd.to_numeric(df_s.get("volume"), errors="coerce").dropna()
    vol_ratio = None
    if len(vol) >= 20:
        vol20 = float(vol.tail(20).mean())
        vol_ratio = float(vol.tail(5).mean() / vol20) if vol20 > 0 else None
    return {
        "code": code,
        "sector": sector_map.get(code, ""),
        "channel": channel_map.get(code, ""),
        "ret20": float(ret20),
        "ret60": float(ret60),
        "ret120": None if ret120 is None else float(ret120),
        "new_high_count": int((recent >= high_close * 0.995).sum()),
        "near_high_pct": (last_close / high_close - 1.0) * 100.0 if high_close > 0 else None,
        "drawdown_pct": float(drawdown),
        "vol_ratio_5_20": vol_ratio,
        "above_ma20": bool(ma20 is not None and last_close >= ma20),
        "above_ma50": bool(ma50 is not None and last_close >= ma50),
        "bias200_pct": None if ma200 is None or ma200 <= 0 else (last_close / ma200 - 1.0) * 100.0,
    }


def _percentile_map(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    values = [(str(r["code"]), float(r[key])) for r in rows if r.get(key) is not None]
    if not values:
        return {}
    ranked = pd.Series({code: value for code, value in values}).rank(pct=True, method="average")
    return {code: float(value) for code, value in ranked.items()}


def _leader_risk(row: dict[str, Any]) -> str:
    bias = row.get("bias200_pct")
    ret20 = float(row.get("ret20") or 0.0)
    near_high = float(row.get("near_high_pct") or 0.0)
    if bias is not None and float(bias) >= 150.0:
        return "高乖离观察"
    if ret20 >= 60.0:
        return "短线过热"
    if near_high <= -18.0:
        return "回撤跟踪"
    return "主升跟踪"


def _leader_reason(row: dict[str, Any]) -> str:
    parts = [f"20日{float(row['ret20']):.1f}%", f"60日{float(row['ret60']):.1f}%"]
    if row.get("ret120") is not None:
        parts.append(f"120日{float(row['ret120']):.1f}%")
    if int(row.get("new_high_count") or 0) > 0:
        parts.append(f"新高密度{int(row['new_high_count'])}")
    if row.get("vol_ratio_5_20") is not None:
        parts.append(f"量比{float(row['vol_ratio_5_20']):.2f}")
    return " / ".join(parts)


def detect_leader_radar(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    channel_map: dict[str, str] | None,
    cfg: FunnelConfig,
) -> list[dict[str, Any]]:
    if not cfg.enable_leader_radar:
        return []
    rows = [
        row
        for code in symbols
        if (df := df_map.get(code)) is not None and not df.empty
        if (row := _leader_feature_row(code, df, sector_map, channel_map or {}, cfg)) is not None
    ]
    q20 = _percentile_map(rows, "ret20")
    q60 = _percentile_map(rows, "ret60")
    q120 = _percentile_map(rows, "ret120")
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["code"])
        vol_score = min(max((float(row.get("vol_ratio_5_20") or 0.0) - cfg.leader_radar_vol_ratio_min) / 1.0, 0.0), 1.0)
        high_score = min(float(row["new_high_count"]) / max(float(cfg.leader_radar_new_high_days_min), 1.0), 1.0)
        trend_score = 1.0 if row["above_ma20"] and row["above_ma50"] else 0.5 if row["above_ma50"] else 0.0
        score = 0.18 * q20.get(code, 0.0) + 0.32 * q60.get(code, 0.0) + 0.24 * q120.get(code, 0.0)
        score += 0.12 * high_score + 0.07 * vol_score + 0.07 * trend_score
        if _leader_radar_keep(row, score, q60.get(code, 0.0), q120.get(code, 0.0), cfg):
            row.update({"score": round(score, 4), "risk": _leader_risk(row), "reason": _leader_reason(row)})
            out.append(row)
    limit = max(int(cfg.leader_radar_limit), 0)
    return sorted(out, key=lambda item: (-float(item["score"]), -float(item["ret60"]), str(item["code"])))[:limit]


def _leader_radar_keep(row: dict[str, Any], score: float, q60: float, q120: float, cfg: FunnelConfig) -> bool:
    ret120 = float(row.get("ret120") or 0.0)
    momentum = (
        float(row["ret20"]) >= cfg.leader_radar_ret20_min
        or float(row["ret60"]) >= cfg.leader_radar_ret60_min
        or ret120 >= cfg.leader_radar_ret120_min
    )
    rank_ok = q60 >= 0.88 or q120 >= 0.88
    trend_ok = bool(row["above_ma20"] or row["above_ma50"])
    pullback_ok = float(row["drawdown_pct"]) >= -float(cfg.leader_radar_pullback_max_pct)
    vol_ok = row.get("vol_ratio_5_20") is None or float(row["vol_ratio_5_20"]) >= cfg.leader_radar_vol_ratio_min
    return score >= cfg.leader_radar_min_score and momentum and rank_ok and trend_ok and pullback_ok and vol_ok


def _rank_pct_map(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    values = {str(row["code"]): float(row[key]) for row in rows if row.get(key) is not None}
    if not values:
        return {}
    ranked = pd.Series(values).rank(pct=True, method="average")
    return {str(code): float(value) for code, value in ranked.items()}


def _range_pct(high: pd.Series, low: pd.Series, lookback: int) -> float | None:
    h = pd.to_numeric(high, errors="coerce").tail(lookback).dropna()
    l = pd.to_numeric(low, errors="coerce").tail(lookback).dropna()
    if h.empty or l.empty:
        return None
    low_min = float(l.min())
    return None if low_min <= 0 else (float(h.max()) / low_min - 1.0) * 100.0


def _alpha_breakout_frame(df_s: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df_s.columns]
    work = df_s[cols].copy()
    for col in cols:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    return work.dropna(subset=["high", "low", "close", "volume"]).reset_index(drop=True)


def _alpha_breakout_setup(df_s: pd.DataFrame, cfg: FunnelConfig) -> dict[str, float] | None:
    work = _alpha_breakout_frame(df_s)
    prior_window = max(int(cfg.alpha_breakout_prior_window), 20)
    recent_days = max(int(cfg.alpha_breakout_recent_days), 1)
    if len(work) < prior_window + 2:
        return None
    start = max(prior_window, len(work) - recent_days)
    for idx in range(len(work) - 1, start - 1, -1):
        setup = _alpha_breakout_setup_at(work, idx, prior_window, cfg)
        if setup is not None:
            setup["days_ago"] = float(len(work) - 1 - idx)
            return setup
    return None


def _alpha_breakout_setup_at(
    work: pd.DataFrame,
    idx: int,
    prior_window: int,
    cfg: FunnelConfig,
) -> dict[str, float] | None:
    prior_high = float(work["high"].iloc[idx - prior_window : idx].max())
    prev_close = float(work["close"].iloc[idx - 1])
    row = work.iloc[idx]
    close, high, low = float(row["close"]), float(row["high"]), float(row["low"])
    if prior_high <= 0 or prev_close <= 0 or close < prior_high or prev_close >= prior_high * 1.01:
        return None
    day_pct = (close / prev_close - 1.0) * 100.0
    bar_pos = (close - low) / (high - low) if high > low else 1.0
    vol_ref = float(work["volume"].iloc[max(0, idx - 20) : idx].mean())
    vol_ratio = float(row["volume"]) / vol_ref if vol_ref > 0 else 0.0
    if day_pct < cfg.alpha_breakout_day_pct_min or bar_pos < 0.62 or vol_ratio < cfg.alpha_breakout_vol_ratio_min:
        return None
    return _alpha_breakout_support(work, idx, close, high, low, vol_ratio, day_pct, cfg)


def _alpha_breakout_support(
    work: pd.DataFrame,
    idx: int,
    close: float,
    high: float,
    low: float,
    vol_ratio: float,
    day_pct: float,
    cfg: FunnelConfig,
) -> dict[str, float] | None:
    midpoint = (high + low) / 2.0
    after = work.iloc[idx:]
    min_low = float(after["low"].min())
    last_close = float(work.iloc[-1]["close"])
    support_gap = (min_low / midpoint - 1.0) * 100.0 if midpoint > 0 else -100.0
    close_dd = (last_close / close - 1.0) * 100.0 if close > 0 else -100.0
    if support_gap < -cfg.alpha_breakout_mid_break_allow_pct:
        return None
    if close_dd < -cfg.alpha_breakout_close_drawdown_max_pct:
        return None
    return {"day_pct": day_pct, "vol_ratio": vol_ratio, "support_gap": support_gap}


def _alpha_feature_row(
    code: str,
    df: pd.DataFrame,
    sector_map: dict[str, str],
    channel_map: dict[str, str],
    cfg: FunnelConfig,
) -> dict[str, Any] | None:
    df_s = sort_by_date_if_needed(df)
    close = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    if len(close) < 120:
        return None
    high = pd.to_numeric(df_s.get("high"), errors="coerce")
    low = pd.to_numeric(df_s.get("low"), errors="coerce")
    volume = pd.to_numeric(df_s.get("volume"), errors="coerce").dropna()
    ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
    ma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    high120 = float(close.tail(min(120, len(close))).max())
    low250 = float(close.tail(min(250, len(close))).min())
    vol20 = float(volume.tail(20).mean()) if len(volume) >= 20 else 0.0
    vol60_ref = float(volume.tail(80).iloc[:60].mean()) if len(volume) >= 80 else 0.0
    last = float(close.iloc[-1])
    ma50_slope = None
    if len(close) >= 70 and ma50 is not None:
        base = float(close.iloc[-70:-20].mean())
        ma50_slope = None if base <= 0 else (ma50 / base - 1.0) * 100.0
    return {
        "code": code,
        "sector": sector_map.get(code, ""),
        "channel": channel_map.get(code, ""),
        "ret20": _close_return_pct(close, 20),
        "ret60": _close_return_pct(close, 60),
        "ret120": _close_return_pct(close, 120),
        "last_close": last,
        "near_high120_pct": (last / high120 - 1.0) * 100.0 if high120 > 0 else None,
        "price_from_low250_pct": (last / low250 - 1.0) * 100.0 if low250 > 0 else None,
        "range20_pct": _range_pct(high, low, 20),
        "range60_pct": _range_pct(high, low, 60),
        "vol_ratio_5_20": float(volume.tail(5).mean() / vol20) if vol20 > 0 else None,
        "vol_ratio_20_60": float(vol20 / vol60_ref) if vol60_ref > 0 else None,
        "above_ma20": bool(ma20 is not None and last >= ma20),
        "above_ma50": bool(ma50 is not None and last >= ma50),
        "above_ma200": bool(ma200 is not None and last >= ma200),
        "bias200_pct": None if ma200 is None or ma200 <= 0 else (last / ma200 - 1.0) * 100.0,
        "ma50_slope20": ma50_slope,
        "breakout_setup": _alpha_breakout_setup(df_s, cfg),
    }


def _alpha_risk_score(row: dict[str, Any], cfg: FunnelConfig) -> float:
    risk = 0.0
    bias = row.get("bias200_pct")
    ret20 = float(row.get("ret20") or 0.0)
    near_high = float(row.get("near_high120_pct") or 0.0)
    vol_ratio = row.get("vol_ratio_5_20")
    if bias is not None:
        risk += max((float(bias) - float(cfg.alpha_bias200_soft_max)) / 120.0, 0.0)
    risk += max((ret20 - float(cfg.alpha_ret20_overheat)) / 80.0, 0.0)
    risk += max((-near_high - 25.0) / 50.0, 0.0)
    if vol_ratio is not None and float(vol_ratio) >= 2.8 and ret20 <= 5.0:
        risk += 0.25
    if not row.get("above_ma50"):
        risk += 0.18
    return min(max(risk, 0.0), 1.0)


def _alpha_entry_values(row: dict[str, Any]) -> dict[str, float]:
    ret20 = float(row.get("ret20") or 0.0)
    ret60 = float(row.get("ret60") or 0.0)
    ret120 = float(row.get("ret120") or 0.0)
    near_high_raw = row.get("near_high120_pct")
    return {
        "ret20": ret20,
        "ret60": ret60,
        "ret120": ret120,
        "near_high": -100.0 if near_high_raw is None else float(near_high_raw),
        "range20": float(row.get("range20_pct") or 999.0),
        "range60": float(row.get("range60_pct") or 999.0),
        "vol5": float(row.get("vol_ratio_5_20") or 1.0),
        "vol20": float(row.get("vol_ratio_20_60") or 1.0),
        "price_low": float(row.get("price_from_low250_pct") or 999.0),
        "slope": float(row.get("ma50_slope20") or 0.0),
    }


def _alpha_breakout_option(
    row: dict[str, Any], cfg: FunnelConfig, v: dict[str, float]
) -> tuple[str, str, float, list[str]] | None:
    setup = row.get("breakout_setup")
    if (
        setup
        and row["above_ma50"]
        and v["near_high"] >= -3
        and cfg.alpha_breakout_ret20_min <= v["ret20"] <= cfg.alpha_breakout_ret20_max
        and v["ret60"] >= cfg.alpha_breakout_ret60_min
    ):
        return (
            "breakout",
            "early_breakout",
            0.84,
            [
                f"突破{int(float(setup['days_ago']))}日",
                f"突破日{float(setup['day_pct']):.1f}%",
                f"量比{float(setup['vol_ratio']):.1f}",
                f"承接{float(setup['support_gap']):.1f}%",
            ],
        )
    return None


def _alpha_launchpad_option(
    row: dict[str, Any], cfg: FunnelConfig, v: dict[str, float]
) -> tuple[str, str, float, list[str]] | None:
    if (
        row["above_ma50"]
        and cfg.alpha_launchpad_ret20_min <= v["ret20"] <= cfg.alpha_launchpad_ret20_max
        and v["ret60"] >= cfg.alpha_launchpad_ret60_min
        and v["ret120"] >= cfg.alpha_launchpad_ret120_min
        and v["near_high"] >= -18
        and v["slope"] >= 0
    ):
        return (
            "future_leader",
            "launchpad",
            0.76,
            [f"60日{v['ret60']:.1f}%", f"120日{v['ret120']:.1f}%", "均线抬升"],
        )
    return None


def _alpha_tight_base_option(
    row: dict[str, Any], cfg: FunnelConfig, v: dict[str, float]
) -> tuple[str, str, float, list[str]] | None:
    if (
        row["above_ma50"]
        and v["ret60"] >= cfg.alpha_tight_base_ret60_min
        and -15 <= v["ret20"] <= 18
        and v["range20"] <= cfg.alpha_tight_base_range20_max
        and v["near_high"] >= cfg.alpha_tight_base_near_high_min
    ):
        return (
            "future_leader",
            "tight_base",
            0.72,
            [f"20日振幅{v['range20']:.1f}%", f"60日{v['ret60']:.1f}%", "强势横盘"],
        )
    return None


def _alpha_volatile_pullback_option(
    row: dict[str, Any], cfg: FunnelConfig, v: dict[str, float]
) -> tuple[str, str, float, list[str]] | None:
    if (
        row["above_ma50"]
        and v["ret20"] >= cfg.alpha_volatile_pullback_ret20_min
        and v["ret60"] >= cfg.alpha_volatile_pullback_ret60_min
        and v["range20"] >= cfg.alpha_volatile_pullback_range20_min
        and v["near_high"] >= cfg.alpha_volatile_pullback_near_high_min
        and v["vol5"] <= cfg.alpha_volatile_pullback_vol_ratio_max
        and v["slope"] >= -1.0
    ):
        return (
            "future_leader",
            "volatile_pullback",
            0.73,
            [f"20日{v['ret20']:.1f}%", f"60日{v['ret60']:.1f}%", f"20日振幅{v['range20']:.1f}%"],
        )
    return None


def _alpha_accum_ready_option(
    row: dict[str, Any], cfg: FunnelConfig, v: dict[str, float]
) -> tuple[str, str, float, list[str]] | None:
    if (
        v["price_low"] <= cfg.alpha_accum_price_from_low_max * 100
        and v["range60"] <= cfg.alpha_accum_range60_max
        and row["above_ma50"]
        and v["vol20"] <= 0.9
        and v["slope"] >= -2.0
    ):
        return (
            "accumulation",
            "accumulation_ready",
            0.68,
            [f"距年低{v['price_low']:.1f}%", f"60日振幅{v['range60']:.1f}%", "低位转强"],
        )
    return None


def _alpha_entry_options(row: dict[str, Any], cfg: FunnelConfig) -> list[tuple[str, str, float, list[str]]]:
    values = _alpha_entry_values(row)
    options = [
        _alpha_breakout_option(row, cfg, values),
        _alpha_launchpad_option(row, cfg, values),
        _alpha_tight_base_option(row, cfg, values),
        _alpha_volatile_pullback_option(row, cfg, values),
        _alpha_accum_ready_option(row, cfg, values),
    ]
    return [item for item in options if item is not None]


def _alpha_score(row: dict[str, Any], q60: float, q120: float, timing: float, risk: float) -> tuple[float, float]:
    trend = 1.0 if row.get("above_ma20") and row.get("above_ma50") else 0.65 if row.get("above_ma50") else 0.25
    near_high_raw = row.get("near_high120_pct")
    near_high = -40.0 if near_high_raw is None else float(near_high_raw)
    near = max(min((near_high + 25.0) / 25.0, 1.0), 0.0)
    slope = max(min((float(row.get("ma50_slope20") or 0.0) + 3.0) / 10.0, 1.0), 0.0)
    opportunity = 0.30 * q60 + 0.25 * q120 + 0.18 * trend + 0.14 * near + 0.13 * slope
    score = 100.0 * (0.48 * opportunity + 0.37 * timing + 0.15 * (1.0 - risk))
    return round(score, 4), round(opportunity, 4)


def _alpha_candidate_entries(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    channel_map: dict[str, str],
    cfg: FunnelConfig,
) -> list[dict[str, Any]]:
    rows = [
        row
        for code in symbols
        if (df := df_map.get(code)) is not None
        if (row := _alpha_feature_row(code, df, sector_map, channel_map, cfg)) is not None
    ]
    q60_map = _rank_pct_map(rows, "ret60")
    q120_map = _rank_pct_map(rows, "ret120")
    entries: list[dict[str, Any]] = []
    for row in rows:
        options = _alpha_entry_options(row, cfg)
        if not options:
            continue
        track, entry_type, timing, reasons = max(options, key=lambda item: item[2])
        risk = _alpha_risk_score(row, cfg)
        score, opportunity = _alpha_score(
            row, q60_map.get(row["code"], 0.0), q120_map.get(row["code"], 0.0), timing, risk
        )
        if score >= cfg.alpha_min_score:
            entries.append(
                _candidate_entry(row["code"], track, "alpha", entry_type, score, opportunity, timing, risk, reasons)
            )
    return entries


def _candidate_entry(
    code: str,
    track: str,
    state: str,
    entry_type: str,
    score: float,
    opportunity_score: float,
    timing_score: float,
    risk_score: float,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "code": str(code),
        "track": track,
        "state": state,
        "entry_type": entry_type,
        "signal_key": entry_type,
        "score": round(float(score), 4),
        "opportunity_score": round(float(opportunity_score), 4),
        "timing_score": round(float(timing_score), 4),
        "risk_score": round(float(risk_score), 4),
        "reasons": [str(x) for x in reasons if str(x).strip()],
    }


def _formal_candidate_entries(
    triggers: dict[str, list[tuple[str, float]]],
    stage_map: dict[str, str],
    exit_signals: dict[str, dict],
) -> list[dict[str, Any]]:
    track_map = {
        "spring": "accumulation",
        "lps": "accumulation",
        "compression": "accumulation",
        "trend_pullback": "trend",
        "sos": "breakout",
        "evr": "trend",
    }
    base_map = {"spring": 70.0, "lps": 64.0, "compression": 58.0, "trend_pullback": 66.0, "sos": 56.0, "evr": 52.0}
    entries: list[dict[str, Any]] = []
    for key, rows in (triggers or {}).items():
        for code, raw_score in rows or []:
            risk = 0.0
            sig = str((exit_signals.get(str(code), {}) or {}).get("signal", "")).strip()
            if sig == "stop_loss":
                risk = 1.0
            elif sig in {"distribution_warning", "upthrust_warning"}:
                risk = 0.45
            score = min(float(base_map.get(key, 50.0)) + float(raw_score or 0.0) * 5.0 - risk * 35.0, 100.0)
            entries.append(
                _candidate_entry(
                    str(code),
                    track_map.get(key, "trend"),
                    stage_map.get(str(code), "formal_l4"),
                    key,
                    score,
                    score / 100.0,
                    0.78,
                    risk,
                    [key],
                )
            )
    return entries


def _dedup_candidate_entries(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for item in entries:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        prev = best.get(code)
        if prev is None or _candidate_entry_rank(item) < _candidate_entry_rank(prev):
            best[code] = item
    ranked = sorted(best.values(), key=_candidate_entry_rank)
    return ranked if limit <= 0 else ranked[:limit]


def _candidate_entry_rank(item: dict[str, Any]) -> tuple[int, float, tuple[int, float, str]]:
    formal_rank = 1 if str(item.get("state", "")).strip().lower() == "alpha" else 0
    return formal_rank, -float(item.get("score", 0.0)), candidate_entry_sort_key(item)


def build_candidate_entries(
    *,
    alpha_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    channel_map: dict[str, str],
    triggers: dict[str, list[tuple[str, float]]],
    stage_map: dict[str, str],
    exit_signals: dict[str, dict],
    cfg: FunnelConfig,
) -> list[dict[str, Any]]:
    formal = _formal_candidate_entries(triggers, stage_map, exit_signals)
    alpha = (
        _alpha_candidate_entries(alpha_symbols, df_map, sector_map, channel_map, cfg) if cfg.alpha_board_enabled else []
    )
    return _dedup_candidate_entries(formal + alpha, max(int(cfg.alpha_board_limit), 0))


def _prepare_df_map(df_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {sym: sort_by_date_if_needed(df) for sym, df in df_map.items() if df is not None and not df.empty}


# Layer 2 增强: Accumulation ABC 细化


class _AccumSeries(NamedTuple):
    frame: pd.DataFrame
    close: pd.Series
    low: pd.Series
    volume: pd.Series


class _AccumVolume(NamedTuple):
    recent_mean: float
    ref_mean: float


def _accum_series(df: pd.DataFrame) -> _AccumSeries:
    df_s = sort_by_date_if_needed(df)
    return _AccumSeries(
        frame=df_s,
        close=pd.to_numeric(df_s["close"], errors="coerce"),
        low=pd.to_numeric(df_s["low"], errors="coerce"),
        volume=pd.to_numeric(df_s["volume"], errors="coerce"),
    )


def _accum_base_low(series: _AccumSeries, cfg: FunnelConfig) -> float | None:
    lookback_w = max(int(cfg.accum_lookback_days), 2)
    period_low = float(series.low.tail(lookback_w).min())
    last_close = series.close.iloc[-1]
    if period_low <= 0 or last_close > period_low * (1.0 + cfg.accum_price_from_low_max):
        return None
    return period_low


def _accum_ma_gap_ok(close: pd.Series, cfg: FunnelConfig) -> bool:
    ma_short = close.rolling(cfg.ma_short).mean()
    ma_long = close.rolling(cfg.ma_long).mean()
    last_ma_short = ma_short.iloc[-1]
    last_ma_long = ma_long.iloc[-1]
    if pd.isna(last_ma_short) or pd.isna(last_ma_long) or float(last_ma_long) <= 0:
        return False
    ma_gap_pct = (float(last_ma_short) - float(last_ma_long)) / float(last_ma_long) * 100.0
    ma_gap_limit = cfg.accum_ma_gap_max * 100.0
    return bool(-ma_gap_limit <= ma_gap_pct <= ma_gap_limit)


def _accum_volume(series: _AccumSeries, cfg: FunnelConfig) -> _AccumVolume | None:
    dw = max(int(cfg.accum_vol_dry_window), 2)
    rfw = max(int(cfg.accum_vol_dry_ref_window), dw + 1)
    recent_vol_mean = float(series.volume.tail(dw).mean()) if len(series.volume) >= dw else 0.0
    ref_vol_mean = float(series.volume.tail(rfw).iloc[:-dw].mean()) if len(series.volume) >= rfw else 0.0
    if ref_vol_mean <= 0 or recent_vol_mean / ref_vol_mean >= cfg.accum_vol_dry_ratio:
        return None
    return _AccumVolume(recent_mean=recent_vol_mean, ref_mean=ref_vol_mean)


def _accum_zone_low(series: _AccumSeries, cfg: FunnelConfig) -> pd.Series:
    rw = max(int(cfg.accum_range_window), 5)
    return pd.to_numeric(series.frame.tail(rw).get("low"), errors="coerce")


def _accum_b_test_count(zone_low: pd.Series, base_low: float) -> int:
    return sum(1 for low_value in zone_low.dropna() if abs(low_value - base_low) / base_low <= 0.05)


def _accum_c_ok(series: _AccumSeries, base_low: float, volume: _AccumVolume, cfg: FunnelConfig) -> bool:
    recent_lookback = min(20, len(series.frame))
    recent_low = pd.to_numeric(series.frame.tail(recent_lookback).get("low"), errors="coerce").min()
    c_stage_ok = recent_low >= base_low * (1.0 - cfg.accum_c_max_drop_ratio)
    return bool(c_stage_ok and volume.recent_mean < volume.ref_mean * cfg.accum_vol_dry_ratio)


def analyze_accum_stage(df: pd.DataFrame, cfg: FunnelConfig) -> str | None:
    """
    分析 Accumulation 内部的三个子阶段：
    - A: 下跌停止，量能萎缩
    - B: 底部区间反复测试
    - C: 小幅下跌不破 A 低，量能再度萎缩

    返回 "Accum_A"、"Accum_B"、"Accum_C" 或 None。
    """
    if len(df) < max(cfg.accum_lookback_days, cfg.accum_vol_dry_ref_window, cfg.accum_range_window):
        return None

    series = _accum_series(df)
    accum_base_low = _accum_base_low(series, cfg)
    if accum_base_low is None:
        return None

    if not _accum_ma_gap_ok(series.close, cfg):
        return None

    volume = _accum_volume(series, cfg)
    if volume is None:
        return None

    zone_low = _accum_zone_low(series, cfg)
    if zone_low.empty:
        return "Accum_A"

    if _accum_b_test_count(zone_low, accum_base_low) >= cfg.accum_b_test_count:
        return "Accum_B"

    if _accum_c_ok(series, accum_base_low, volume, cfg):
        return "Accum_C"

    return "Accum_A"


def detect_accum_stage(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
) -> dict[str, str]:
    """
    返回 symbol -> stage 的映射。
    """
    if not cfg.enable_accum_abc_detail:
        return {}

    result: dict[str, str] = {}
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        stage = analyze_accum_stage(df, cfg)
        if stage is not None:
            result[sym] = stage

    return result


# Layer 5: Exit 策略


def _detect_distribution_start(df: pd.DataFrame, cfg: FunnelConfig) -> bool:
    """
    Distribution 阶段识别：高位缩量警告。
    触发条件：
    1. 价格相对 MA200 处于高位（>30%）
    2. 连续 N 日的成交量 < 参考均量的 50%
    """
    if len(df) < max(cfg.ma_long, cfg.dist_confirm_days) + 20:
        return False

    df_s = sort_by_date_if_needed(df)
    close = df_s["close"].astype(float)
    volume = df_s["volume"].astype(float)

    ma_long = close.rolling(cfg.ma_long).mean()
    last_ma_long = ma_long.iloc[-1]
    last_close = close.iloc[-1]

    if pd.isna(last_ma_long) or pd.isna(last_close) or last_ma_long <= 0:
        return False

    bias = (last_close - last_ma_long) / last_ma_long * 100.0
    if bias < cfg.dist_high_threshold_pct:
        return False

    # 检查近 N 日的缩量
    ref_vol = volume.tail(60).mean()
    recent_vol = volume.tail(cfg.dist_confirm_days).mean()

    if ref_vol <= 0:
        return False

    return not recent_vol / ref_vol > cfg.dist_vol_dry_ratio


def _detect_upthrust_after_distribution(df: pd.DataFrame, cfg: FunnelConfig) -> dict[str, float] | None:
    """Detect a high-level false breakout that closes back below prior resistance."""
    if not cfg.dist_upthrust_enabled or len(df) < max(cfg.ma_long, cfg.dist_upthrust_lookback) + 1:
        return None
    df_s = sort_by_date_if_needed(df)
    open_ = pd.to_numeric(df_s["open"], errors="coerce")
    high = pd.to_numeric(df_s["high"], errors="coerce")
    low = pd.to_numeric(df_s["low"], errors="coerce")
    close = pd.to_numeric(df_s["close"], errors="coerce")
    volume = pd.to_numeric(df_s["volume"], errors="coerce")
    prior_high = high.iloc[-(cfg.dist_upthrust_lookback + 1) : -1].dropna()
    swing_highs = swing_values(prior_high, kind="high", window=3)
    resistance = (
        float(pd.Series(swing_highs[-5:]).median()) if len(swing_highs) >= 2 else float(prior_high.quantile(0.95))
    )
    ma200 = close.rolling(cfg.ma_long).mean().iloc[-1]
    ref_volume = volume.tail(21).iloc[:-1].mean()
    day_range = float(high.iloc[-1] - low.iloc[-1])
    shadow = float(high.iloc[-1] - max(open_.iloc[-1], close.iloc[-1]))
    if any(pd.isna(value) for value in (resistance, ma200, ref_volume)) or resistance <= 0 or ma200 <= 0:
        return None
    breakout_pct = (float(high.iloc[-1]) / resistance - 1.0) * 100.0
    close_back_pct = (resistance / float(close.iloc[-1]) - 1.0) * 100.0 if close.iloc[-1] > 0 else 0.0
    volume_ratio = float(volume.iloc[-1] / ref_volume) if ref_volume > 0 else 0.0
    bias_200 = (float(close.iloc[-1]) / float(ma200) - 1.0) * 100.0
    upper_shadow_ratio = shadow / day_range if day_range > 0 else 0.0
    if (
        breakout_pct < cfg.dist_upthrust_breakout_pct
        or close_back_pct < cfg.dist_upthrust_close_back_pct
        or upper_shadow_ratio < cfg.dist_upthrust_upper_shadow_ratio
        or volume_ratio < cfg.dist_upthrust_vol_ratio
        or bias_200 < cfg.dist_upthrust_min_bias_200_pct
    ):
        return None
    return {
        "resistance": resistance,
        "breakout_pct": breakout_pct,
        "close_back_pct": close_back_pct,
        "upper_shadow_ratio": upper_shadow_ratio,
        "volume_ratio": volume_ratio,
        "bias_200_pct": bias_200,
    }


def _is_holiday_grace(df_s: pd.DataFrame, grace_days: int) -> bool:
    """检测最近交易日是否处于节后宽限期（跨 ≥3 自然日后的 grace_days 个交易日内）。"""
    if grace_days <= 0 or "date" not in df_s.columns or len(df_s) < 2:
        return False
    dates = pd.to_datetime(df_s["date"], errors="coerce")
    n = len(dates)
    check_pairs = min(grace_days, n - 1)
    for i in range(1, check_pairs + 1):
        if dates.isna().iloc[-i] or dates.isna().iloc[-i - 1]:
            continue
        if (dates.iloc[-i] - dates.iloc[-i - 1]).days >= 3:
            return True
    return False


def _compute_stop_loss(
    close: pd.Series,
    low: pd.Series,
    high: pd.Series,
    stage: str,
    cfg: FunnelConfig,
) -> tuple[float | None, str]:
    """计算单只股票的止损价和原因。"""
    last_close = float(close.iloc[-1])
    ma_short_series = close.rolling(cfg.ma_short).mean()
    ma_short = float(ma_short_series.iloc[-1]) if not ma_short_series.isna().all() else None
    recent_high = float(high.tail(60).max())

    if stage.startswith("Accum_"):
        lookback_w = max(int(cfg.accum_lookback_days), 2)
        accum_low = float(low.tail(lookback_w).min())
        trailing_active_pct = cfg.exit_trailing_active_pct / 100.0
        if last_close >= accum_low * (1.0 + trailing_active_pct):
            drawdown_pct = cfg.exit_trailing_drawdown_pct / 100.0
            trailing_price = recent_high * (1.0 + drawdown_pct)
            price = max(trailing_price, float(ma_short) * 0.98) if ma_short else trailing_price
            return price, "已脱离底部，触发利润保护(动态跟踪止损)"
        price = accum_low * (1.0 + cfg.exit_stop_loss_pct / 100.0)
        return price, f"破位防守(跌破 {stage} 吸筹底线)"

    drawdown_pct = cfg.exit_trailing_drawdown_pct / 100.0
    trailing_price = recent_high * (1.0 + drawdown_pct)
    price = max(trailing_price, float(ma_short) * 0.98) if ma_short else trailing_price
    return price, "主升趋势破位(跌破MA50或高位回撤)"


def _stop_loss_exit_signal(df_s: pd.DataFrame, stage: str, cfg: FunnelConfig) -> dict | None:
    if _is_holiday_grace(df_s, cfg.exit_holiday_grace_days):
        return None
    close = pd.to_numeric(df_s["close"], errors="coerce")
    low = pd.to_numeric(df_s["low"], errors="coerce")
    high = pd.to_numeric(df_s["high"], errors="coerce")
    stop_price, stop_reason = _compute_stop_loss(close, low, high, stage, cfg)
    last_close = float(close.iloc[-1])
    if stop_price is None or last_close > stop_price:
        return None
    deep_breach = stop_price > 0 and (stop_price - last_close) / stop_price >= 0.05
    if deep_breach:
        return {
            "signal": "stop_loss",
            "price": stop_price,
            "current": last_close,
            "reason": stop_reason + "(深度破位)",
        }
    confirm_days = max(int(cfg.exit_confirm_days), 1)
    recent_closes = close.tail(confirm_days)
    all_below = len(close) < confirm_days or all(float(value) <= stop_price for value in recent_closes)
    volume = pd.to_numeric(df_s.get("volume"), errors="coerce")
    vol_confirmed = True
    if all_below and cfg.exit_vol_confirm_ratio > 0 and len(volume) >= 20 + confirm_days:
        vol_recent = float(volume.tail(confirm_days).mean())
        vol_ref = float(volume.tail(20 + confirm_days).iloc[:-confirm_days].mean())
        vol_confirmed = vol_ref <= 0 or (vol_recent / vol_ref) >= cfg.exit_vol_confirm_ratio
    if not all_below or not vol_confirmed:
        return None
    return {"signal": "stop_loss", "price": stop_price, "current": last_close, "reason": stop_reason}


def _distribution_exit_signal(df_s: pd.DataFrame, cfg: FunnelConfig) -> dict | None:
    upthrust = _detect_upthrust_after_distribution(df_s, cfg)
    if upthrust:
        return {
            "signal": "upthrust_warning",
            "current": float(pd.to_numeric(df_s["close"], errors="coerce").iloc[-1]),
            "resistance": upthrust["resistance"],
            "reason": (
                f"检测到 Upthrust/UTAD 假突破：盘中越过阻力 {upthrust['breakout_pct']:.1f}% 后收回，"
                f"上影占振幅 {upthrust['upper_shadow_ratio']:.0%}，量比 {upthrust['volume_ratio']:.1f}"
            ),
            "evidence": upthrust,
        }
    if _detect_distribution_start(df_s, cfg):
        return {
            "signal": "distribution_warning",
            "reason": "检测到高位 Distribution 阶段迹象（放量不涨/高位缩量），主力疑似派发",
        }
    return None


def layer5_exit_signals(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    accum_stage_map: dict[str, str],
    cfg: FunnelConfig,
) -> dict[str, dict]:
    """止损 + 派发预警。节后宽限期内跳过止损但仍检查派发。"""
    if not cfg.enable_exit_signals:
        return {}

    signals: dict[str, dict] = {}
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue

        df_s = sort_by_date_if_needed(df)
        close = pd.to_numeric(df_s["close"], errors="coerce")
        low = pd.to_numeric(df_s["low"], errors="coerce")
        high = pd.to_numeric(df_s["high"], errors="coerce")
        if close.empty or low.empty or high.empty:
            continue

        signal = _stop_loss_exit_signal(df_s, accum_stage_map.get(sym, "Markup"), cfg)
        if signal:
            signals[sym] = signal
            continue
        signal = _distribution_exit_signal(df_s, cfg)
        if signal:
            signals[sym] = signal

    return signals


# run_funnel: 串联 4 层


def run_funnel(
    all_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    cfg: FunnelConfig | None = None,
    concept_map: dict[str, list[str]] | None = None,
    concept_heat: list[dict[str, Any]] | None = None,
    hot_concepts: list[str] | None = None,
    theme_radar: dict[str, Any] | None = None,
    financial_map: dict[str, dict] | None = None,
    theme_member_index: dict[str, list[str]] | None = None,
    mainline_config: MainlineEngineConfig | None = None,
) -> FunnelResult:
    if cfg is None:
        cfg = FunnelConfig()
    layers = _run_funnel_layers(
        all_symbols,
        df_map,
        bench_df,
        name_map,
        market_cap_map,
        sector_map,
        cfg,
        concept_map=concept_map,
        hot_concepts=hot_concepts,
        financial_map=financial_map,
    )
    stages = _run_funnel_stage_context(layers, sector_map, cfg)
    candidate_entries = _candidate_entries_for_result(
        layers.l1,
        layers.l2,
        layers.top_sectors,
        layers.df_map,
        sector_map,
        layers.channel_map,
        layers.triggers,
        stages.stage_map,
        stages.exit_signals,
        cfg,
        concept_map=concept_map,
        concept_heat=concept_heat,
        theme_radar=theme_radar,
        financial_map=financial_map,
        theme_member_index=theme_member_index,
        name_map=name_map,
        mainline_config=mainline_config,
    )
    return FunnelResult(
        layer1_symbols=layers.l1,
        layer2_symbols=layers.l2,
        layer3_symbols=layers.l3,
        top_sectors=layers.top_sectors,
        triggers=layers.triggers,
        stage_map=stages.stage_map,
        markup_symbols=stages.markup_symbols,
        exit_signals=stages.exit_signals,
        channel_map=layers.channel_map,
        leader_radar_symbols=[str(row["code"]) for row in stages.leader_radar_rows],
        leader_radar_rows=stages.leader_radar_rows,
        candidate_entries=candidate_entries,
    )


_CHANNEL_HIT_LABELS = {
    "momentum": "主升",
    "ambush": "潜伏",
    "accum": "吸筹",
    "dry_vol": "地量蓄势",
    "rs_div": "暗中护盘",
    "trend_cont": "趋势延续",
    "breakout_accel": "加速突破",
    "sos": "点火破局",
}


def _log_layer2_channel_hits(channel_hits: dict[str, int], l1_count: int) -> None:
    # 诊断日志：记录每个通道的原始命中次数（含被其他通道抢先归类的重叠情况），
    # 用于观察左侧通道（潜伏/暗中护盘等）是长期真实 0 命中，还是仅样本窗口不足。
    if l1_count <= 0:
        return
    parts = [f"{_CHANNEL_HIT_LABELS.get(name, name)}={channel_hits.get(name, 0)}" for name in _CHANNEL_HIT_LABELS]
    logger.info("L2通道命中统计(基数L1=%d): %s", l1_count, ", ".join(parts))


def _run_funnel_layers(
    all_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    cfg: FunnelConfig,
    *,
    concept_map: dict[str, list[str]] | None,
    hot_concepts: list[str] | None,
    financial_map: dict[str, dict] | None,
) -> _FunnelLayerState:
    prepared_df_map = _prepare_df_map(df_map)
    l1 = layer1_filter(all_symbols, name_map, market_cap_map, prepared_df_map, cfg, financial_map=financial_map)
    channel_hits: dict[str, int] = {}
    l2, channel_map, _pre_ign = layer2_strength_detailed(
        l1,
        prepared_df_map,
        bench_df,
        cfg,
        rps_universe=list(prepared_df_map.keys()),
        channel_hit_counter=channel_hits,
    )
    _log_layer2_channel_hits(channel_hits, len(l1))
    l3, top_sectors = layer3_sector_resonance(
        l2,
        sector_map,
        cfg,
        base_symbols=l1,
        df_map=prepared_df_map,
        concept_map=concept_map,
        hot_concepts=hot_concepts,
    )
    triggers = layer4_triggers(l3, prepared_df_map, cfg, channel_map=channel_map, market_cap_map=market_cap_map)
    return _FunnelLayerState(prepared_df_map, l1, l2, l3, top_sectors, channel_map, triggers)


def _run_funnel_stage_context(
    layers: _FunnelLayerState,
    sector_map: dict[str, str],
    cfg: FunnelConfig,
) -> _FunnelStageState:
    markup_symbols = detect_markup_stage(layers.l3, layers.df_map, cfg)
    accum_stage_map = detect_accum_stage(layers.l2, layers.df_map, cfg)
    leader_radar_rows = detect_leader_radar(layers.l1, layers.df_map, sector_map, layers.channel_map, cfg)
    stage_map: dict[str, str] = accum_stage_map.copy()
    for sym in markup_symbols:
        stage_map[sym] = "Markup"
    exit_signals = layer5_exit_signals(layers.l2 + markup_symbols, layers.df_map, accum_stage_map, cfg)
    return _FunnelStageState(stage_map, markup_symbols, exit_signals, leader_radar_rows)


def _candidate_entries_for_result(
    l1: list[str],
    l2: list[str],
    top_sectors: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    channel_map: dict[str, str],
    triggers: dict[str, list[tuple[str, float]]],
    stage_map: dict[str, str],
    exit_signals: dict[str, dict],
    cfg: FunnelConfig,
    *,
    concept_map: dict[str, list[str]] | None = None,
    concept_heat: list[dict[str, Any]] | None = None,
    theme_radar: dict[str, Any] | None = None,
    financial_map: dict[str, dict] | None = None,
    theme_member_index: dict[str, list[str]] | None = None,
    name_map: dict[str, str] | None = None,
    mainline_config: MainlineEngineConfig | None = None,
) -> list[dict[str, Any]]:
    main_force_map = {code: analyze_main_force_signal(df_map.get(code)) for code in l1}
    wyckoff_entries = build_candidate_entries(
        alpha_symbols=l1,
        df_map=df_map,
        sector_map=sector_map,
        channel_map=channel_map,
        triggers=triggers,
        stage_map=stage_map,
        exit_signals=exit_signals,
        cfg=cfg,
    )
    lane_entries = build_l1_candidate_lane_entries(
        l1_symbols=l1,
        df_map=df_map,
        sector_map=sector_map,
        top_sectors=top_sectors,
        l2_symbols=l2,
        channel_map=channel_map,
        main_force_map=main_force_map,
    )
    mainline_entries = _mainline_entries_for_result(
        l1,
        l2,
        df_map,
        sector_map=sector_map,
        concept_map=concept_map,
        concept_heat=concept_heat,
        theme_radar=theme_radar,
        financial_map=financial_map,
        theme_member_index=theme_member_index,
        name_map=name_map,
        mainline_config=mainline_config,
        main_force_map=main_force_map,
    )
    merged = merge_candidate_entries(wyckoff_entries, lane_entries, mainline_entries)
    _attach_price_targets(merged, df_map)
    return merged


def _attach_price_targets(entries: list[dict[str, Any]], df_map: dict[str, pd.DataFrame]) -> None:
    """给候选条目原地补充技术位目标价（量度运动/前高/ATR倍数），仅供参考，不做盈利预测。

    同时写入独立的 price_targets 字段（方便直接消费）和 metrics 字段（复用
    candidate_metadata.py 既有的透传通道，落到下游 candidate_metrics 列）。
    """
    for entry in entries:
        df = df_map.get(str(entry.get("code", "")))
        if df is None or df.empty:
            continue
        df_s = sort_by_date_if_needed(df)
        targets = compute_price_targets(df_s["close"], df_s.get("high", df_s["close"]), df_s.get("low", df_s["close"]))
        if targets is None:
            continue
        payload = {
            "last_close": round(targets.last_close, 2),
            "measured_move": None if targets.measured_move is None else round(targets.measured_move, 2),
            "prior_high": None if targets.prior_high is None else round(targets.prior_high, 2),
            "atr_multiple": None if targets.atr_multiple is None else round(targets.atr_multiple, 2),
            "conservative": None if targets.conservative is None else round(targets.conservative, 2),
            "aggressive": None if targets.aggressive is None else round(targets.aggressive, 2),
        }
        entry["price_targets"] = payload
        metrics = dict(entry.get("metrics") or {})
        metrics.update({f"target_{k}": v for k, v in payload.items() if v is not None})
        entry["metrics"] = metrics


def _mainline_entries_for_result(
    l1: list[str],
    l2: list[str],
    df_map: dict[str, pd.DataFrame],
    *,
    sector_map: dict[str, str] | None,
    concept_map: dict[str, list[str]] | None,
    concept_heat: list[dict[str, Any]] | None,
    theme_radar: dict[str, Any] | None,
    financial_map: dict[str, dict] | None,
    theme_member_index: dict[str, list[str]] | None,
    name_map: dict[str, str] | None,
    mainline_config: MainlineEngineConfig | None,
    main_force_map: dict | None = None,
) -> list[dict[str, Any]]:
    if mainline_config is None or not mainline_config.enabled:
        return []
    theme_activity = build_theme_activity_snapshot(
        trade_date="",
        df_map=df_map,
        concept_map=concept_map or {},
        sector_map=sector_map or {},
        concept_heat=concept_heat or [],
        theme_member_index=theme_member_index,
    )
    candidates = build_mainline_candidates(
        l1_passed=l1,
        l2_passed=l2,
        concept_map=concept_map or {},
        concept_heat=concept_heat or [],
        theme_radar=theme_radar or {},
        theme_activity=theme_activity,
        df_map=df_map,
        financial_map=financial_map or {},
        name_map=name_map or {},
        config=mainline_config,
        main_force_map=main_force_map,
    )
    return mainline_candidate_entries(candidates, max_count=mainline_config.max_ai_candidates)
