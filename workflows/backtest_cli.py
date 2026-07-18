"""CLI parser helpers for the daily backtest runner."""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from workflows.backtest_defaults import (
    DEFAULT_ATR_HARD_STOP_PCT,
    DEFAULT_ATR_MULTIPLIER,
    DEFAULT_ATR_PERIOD,
    DEFAULT_BUY_FRICTION_PCT,
    DEFAULT_CASH_PORTFOLIO_COMMISSION_RATE,
    DEFAULT_CASH_PORTFOLIO_INITIAL_CASH,
    DEFAULT_CASH_PORTFOLIO_LOT_SIZE,
    DEFAULT_CASH_PORTFOLIO_MAX_POSITIONS,
    DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_FEE,
    DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_THRESHOLD,
    DEFAULT_CASH_PORTFOLIO_STYLES,
    DEFAULT_ENTRY_PRICE_FALLBACK,
    DEFAULT_ENTRY_PRICE_TIME,
    DEFAULT_EXIT_MODE,
    DEFAULT_HOLD_DAYS,
    DEFAULT_METRICS_ENGINE,
    DEFAULT_SELL_FRICTION_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_TRAILING_ACTIVATE_PCT,
    DEFAULT_TRAILING_STOP_PCT,
    DEFAULT_USE_CURRENT_META,
    DEFAULT_WBT_FEE_RATE,
    DEFAULT_WBT_N_JOBS,
)


def build_backtest_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wyckoff Funnel 日线轻量回测器")
    _add_window_args(parser)
    _add_universe_args(parser)
    _add_exit_args(parser)
    _add_metadata_and_cost_args(parser)
    _add_signal_args(parser)
    _add_entry_args(parser)
    _add_cash_args(parser)
    parser.add_argument(
        "--grid-cells",
        default="",
        help="共享一次信号计算的参数格，格式 hold:stop:take:trail，多个格用逗号分隔",
    )
    parser.add_argument("--grid-prefix", default="backtest-grid", help="参数格输出目录名前缀")
    return parser


def parse_hold_days_list(raw: str) -> list[int]:
    vals: list[int] = []
    for token in str(raw or "").replace("，", ",").replace(" ", ",").split(","):
        t = str(token).strip()
        if not t:
            continue
        n = int(t)
        if n <= 0:
            raise ValueError(f"hold_days_list 中存在非法值: {n}")
        vals.append(n)
    dedup = sorted(set(vals))
    if not dedup:
        raise ValueError("hold_days_list 为空")
    return dedup


def _add_window_args(parser: argparse.ArgumentParser) -> None:
    default_end = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    default_start = (date.today() - timedelta(days=548)).strftime("%Y-%m-%d")
    parser.add_argument("--start", default=default_start, help=f"起始日期 (default: {default_start})")
    parser.add_argument("--end", default=default_end, help=f"结束日期 (default: {default_end})")
    parser.add_argument(
        "--hold-days",
        type=int,
        default=DEFAULT_HOLD_DAYS,
        help=f"持有交易日数 (default: {DEFAULT_HOLD_DAYS})",
    )
    parser.add_argument(
        "--hold-days-list",
        default="",
        help="逗号分隔的持有周期列表，例如 10,15,20,30。设置后会依次回测并输出汇总。",
    )


def _add_universe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top-n", type=int, default=0, help="每日候选上限；0 表示不截断（回测全量 AI 输入，默认 0）")
    parser.add_argument(
        "--board",
        choices=["all", "main_chinext", "main_chinext_star", "main", "chinext", "star", "bse", "us"],
        default="all",
    )
    parser.add_argument("--benchmark", default="000001")
    parser.add_argument("--sample-size", type=int, default=0, help="股票池采样数量；0 表示不采样（默认全量，贴近线上）")
    parser.add_argument("--trading-days", type=int, default=320, help="单次筛选回看交易日数")
    parser.add_argument("--workers", type=int, default=8, help="历史拉取并发数")
    parser.add_argument(
        "--snapshot-dir", default="", help="CI 专用：GitHub Actions Phase 1 导出的快照目录（留空则直接从数据源取数）"
    )
    parser.add_argument("--output-dir", default="analysis/backtest", help="输出目录（会写 summary.md 与 trades.csv）")


def _add_exit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exit-mode",
        choices=["close_only", "sltp", "atr"],
        default=DEFAULT_EXIT_MODE,
        help=f"离场模式：close_only=收盘离场；sltp=固定止盈止损；atr=ATR动态止损(对齐实盘) (default: {DEFAULT_EXIT_MODE})",
    )
    parser.add_argument(
        "--stop-loss", type=float, default=DEFAULT_STOP_LOSS_PCT, help="止损线(%%), 如 -9.0 表示跌破 9%% 止损"
    )
    parser.add_argument("--take-profit", type=float, default=DEFAULT_TAKE_PROFIT_PCT, help="止盈线(%%), 0 表示不设止盈")
    parser.add_argument(
        "--trailing-stop",
        type=float,
        default=DEFAULT_TRAILING_STOP_PCT,
        help=f"移动止盈(%%), 如 -5.0 表示从最高点回撤 5%% 卖出. 0 表示不启用 (default: {DEFAULT_TRAILING_STOP_PCT})",
    )
    parser.add_argument(
        "--trailing-activate",
        type=float,
        default=DEFAULT_TRAILING_ACTIVATE_PCT,
        help=f"移动止盈激活门槛(%%), 浮盈达到此值后才启用移动止盈. 0 表示立即启用 (default: {DEFAULT_TRAILING_ACTIVATE_PCT})",
    )
    parser.add_argument("--atr-period", type=int, default=DEFAULT_ATR_PERIOD, help="ATR 周期（仅 atr 模式生效）")
    parser.add_argument(
        "--atr-multiplier", type=float, default=DEFAULT_ATR_MULTIPLIER, help="ATR 乘数（仅 atr 模式生效）"
    )
    parser.add_argument(
        "--atr-hard-stop", type=float, default=DEFAULT_ATR_HARD_STOP_PCT, help="ATR 模式极限止损地板(%%)"
    )
    parser.add_argument(
        "--sltp-priority",
        choices=["stop_first", "take_first"],
        default="stop_first",
        help="同一交易日同时触及止损/止盈时的判定顺序",
    )


def _add_metadata_and_cost_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--use-current-meta",
        dest="use_current_meta",
        action="store_true",
        default=DEFAULT_USE_CURRENT_META,
        help="显式使用当前截面市值/行业映射过滤（会引入 look-ahead bias，仅限探索）",
    )
    parser.add_argument(
        "--no-use-current-meta",
        dest="use_current_meta",
        action="store_false",
        help="关闭当前截面市值/行业映射过滤（降低 look-ahead bias）",
    )
    parser.add_argument("--buy-friction-pct", type=float, default=DEFAULT_BUY_FRICTION_PCT, help="买入端摩擦成本(%%)")
    parser.add_argument("--sell-friction-pct", type=float, default=DEFAULT_SELL_FRICTION_PCT, help="卖出端摩擦成本(%%)")


def _add_signal_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--strategy-variant",
        choices=["live", "A", "B", "C", "D", "E", "F", "G", "H", "I"],
        default="live",
        help="策略消融组：live=生产配置；A=基线；B-E=经典形态研究；F-I=A股实证入场研究",
    )
    parser.add_argument(
        "--regime-filter",
        action="store_true",
        default=False,
        help="兼容旧参数；已废弃且不再生效，回测候选口径跟随实盘漏斗。",
    )
    parser.add_argument(
        "--pending-mode",
        choices=["off", "only", "both"],
        default="only",
        help="信号确认模式: only=仅用跨日确认后信号（与实盘 Step4 confirmed 口径一致，默认）, "
        "off=跳过确认、直接用当日 L4 信号次日开盘价买入（仅作研究对照，不代表实盘表现）, both=两者合并",
    )
    parser.add_argument(
        "--execution-regime-gate",
        choices=["live", "off", "neutral_only"],
        default="live",
        help="新开仓水温闸门: live=跟随实盘；off=仅研究对照；neutral_only=只允许NEUTRAL",
    )
    parser.add_argument(
        "--pending-merge-order",
        choices=["funnel_first", "confirmed_first"],
        default="confirmed_first",
        help="pending_mode=both 时合并顺序",
    )
    parser.add_argument(
        "--metrics-engine",
        choices=["legacy", "auto", "both", "wbt"],
        default=DEFAULT_METRICS_ENGINE if DEFAULT_METRICS_ENGINE in {"legacy", "auto", "both", "wbt"} else "legacy",
        help="绩效统计引擎",
    )
    parser.add_argument("--wbt-fee-rate", type=float, default=DEFAULT_WBT_FEE_RATE, help="wbt 合成 NAV 评估的费率")
    parser.add_argument("--wbt-n-jobs", type=int, default=DEFAULT_WBT_N_JOBS, help="wbt Rust 后端并行线程数")
    parser.add_argument("--abc-filter", action="store_true", default=False, help="启用 ABC 起跳板过滤")


def _add_entry_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--entry-price-mode",
        choices=["open", "close", "tail_1455"],
        default="open",
        help="入场成交价: open=T+1开盘；close=T+1收盘；tail_1455=T+1 14:55 分钟线价",
    )
    parser.add_argument(
        "--entry-price-time",
        default=DEFAULT_ENTRY_PRICE_TIME,
        help=f"tail_1455 模式下的目标分钟时间 (default: {DEFAULT_ENTRY_PRICE_TIME})",
    )
    parser.add_argument(
        "--entry-price-fallback",
        choices=["close", "skip", "error"],
        default=DEFAULT_ENTRY_PRICE_FALLBACK,
        help="tail_1455 缺分钟线时的处理：close=日收盘回退，skip=跳过，error=失败",
    )


def _add_cash_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cash-portfolio", action="store_true", default=False, help="启用真实现金账户模拟")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_CASH_PORTFOLIO_INITIAL_CASH)
    parser.add_argument("--max-positions", type=int, default=DEFAULT_CASH_PORTFOLIO_MAX_POSITIONS)
    parser.add_argument("--commission-rate", type=float, default=DEFAULT_CASH_PORTFOLIO_COMMISSION_RATE)
    parser.add_argument(
        "--small-trade-threshold",
        type=float,
        default=DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_THRESHOLD,
        help="现金账户手续费小额成交阈值",
    )
    parser.add_argument("--small-trade-fee", type=float, default=DEFAULT_CASH_PORTFOLIO_SMALL_TRADE_FEE)
    parser.add_argument("--lot-size", type=int, default=DEFAULT_CASH_PORTFOLIO_LOT_SIZE)
    parser.add_argument(
        "--portfolio-styles",
        default=DEFAULT_CASH_PORTFOLIO_STYLES,
        help="现金账户交易风格，逗号分隔；支持 slot_equal_4/probe_add/confirmation_only/trend_pyramid/concentrated_swap/all_core",
    )
