"""Runtime configuration for market funnel jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from utils.package_resources import market_universe_path


@dataclass(frozen=True)
class MarketSpec:
    key: str
    label: str
    universe: str
    symbol_file: str
    benchmark_symbols: tuple[str, ...]
    default_max_symbols: int
    default_min_quote_amount: float
    default_min_quote_price: float


@dataclass(frozen=True)
class RuntimeConfig:
    spec: MarketSpec
    max_symbols: int
    quote_batch_size: int
    quote_batch_sleep: float
    kline_count: int
    kline_batch_size: int
    kline_batch_sleep: float
    min_quote_amount: float
    min_quote_price: float
    min_avg_amount: float
    min_history_rows: int
    output_path: Path | None
    symbol_path: Path
    benchmark_symbols: tuple[str, ...]


MARKET_SPECS = {
    "hk": MarketSpec(
        key="hk",
        label="港股",
        universe="HK_Equity",
        symbol_file="hk.txt",
        # TickFlow 不支持任何形式的恒生指数代码（800000.HK/HSI.HK/^HSI/HSI 均无数据），
        # 用 02800.HK（盈富基金）代理恒指、03033.HK（南方恒生科技）代理科技股行情。
        #
        # 2026-08 实测：TickFlow 的港股 ETF 自 2026-03-20 起停止更新，02800/02828/03033/
        # 03067 全部只剩一根 2026-08-12 的孤立 K 线，中间缺 145 天；与 count、复权方式无关。
        # 而港股个股（00700/00939/01299）同期数据完整。因此在 ETF 之后补几只高流动性权重股
        # 作兜底——它们不是理想的指数代理，但一份连续的近似基准远好过一份有 5 个月空洞的
        # "精确"基准：水温分类要算 MA50/MA200，空洞会让 regime 判断静默失真。
        benchmark_symbols=("02800.HK", "03033.HK", "00939.HK", "01299.HK", "00700.HK"),
        default_max_symbols=600,
        default_min_quote_amount=2_000_000.0,
        default_min_quote_price=1.0,
    ),
    "us": MarketSpec(
        key="us",
        label="美股",
        universe="US_Equity",
        symbol_file="us.txt",
        benchmark_symbols=("SPY.US", "QQQ.US"),
        # 放开低价股：候选池按成交额降序截断，1500 的容量本身就是隐含流动性门槛
        # （美股第 1500 名日成交额约千万美元级），低价股几乎排不进来。要真正纳入
        # 仙股，必须同时抬高容量并下调成交额门槛，单调价格门无效。
        default_max_symbols=3500,
        default_min_quote_amount=300_000.0,
        # $0.5 以下多为流动性枯竭或退市途中的报价噪音，仍然拦掉。
        default_min_quote_price=0.5,
    ),
}


def runtime_config_from_env(market: str, output: str | None) -> RuntimeConfig:
    spec = MARKET_SPECS[market]
    return RuntimeConfig(
        spec=spec,
        max_symbols=_int_env("MARKET_FUNNEL_MAX_SYMBOLS", spec.default_max_symbols, minimum=1),
        quote_batch_size=_int_env("MARKET_FUNNEL_QUOTE_BATCH_SIZE", 500, minimum=1),
        quote_batch_sleep=_float_env("MARKET_FUNNEL_QUOTE_BATCH_SLEEP", 0.25),
        kline_count=_int_env("MARKET_FUNNEL_KLINE_COUNT", 320, minimum=220),
        kline_batch_size=_int_env("MARKET_FUNNEL_KLINE_BATCH_SIZE", 200, minimum=1),
        kline_batch_sleep=_float_env("MARKET_FUNNEL_KLINE_BATCH_SLEEP", 0.55),
        min_quote_amount=_float_env("MARKET_FUNNEL_MIN_QUOTE_AMOUNT", spec.default_min_quote_amount),
        min_quote_price=_float_env("MARKET_FUNNEL_MIN_QUOTE_PRICE", spec.default_min_quote_price),
        min_avg_amount=_float_env("MARKET_FUNNEL_MIN_AVG_AMOUNT", 0.0),
        min_history_rows=_int_env("MARKET_FUNNEL_MIN_HISTORY_ROWS", 220, minimum=80),
        output_path=Path(output) if output else None,
        symbol_path=market_symbol_path(market, spec),
        benchmark_symbols=benchmark_symbols_for_market(spec),
    )


def market_symbol_path(market: str, spec: MarketSpec) -> Path:
    symbol_file = (
        os.getenv(f"MARKET_FUNNEL_{market.upper()}_SYMBOL_FILE", "").strip()
        or os.getenv("MARKET_FUNNEL_SYMBOL_FILE", "").strip()
    )
    if symbol_file:
        return Path(symbol_file)
    return market_universe_path(spec.symbol_file)


def benchmark_symbols_for_market(spec: MarketSpec) -> tuple[str, ...]:
    raw = (
        os.getenv(f"MARKET_FUNNEL_{spec.key.upper()}_BENCHMARK_SYMBOLS", "").strip()
        or os.getenv("MARKET_FUNNEL_BENCHMARK_SYMBOLS", "").strip()
    )
    if not raw:
        return spec.benchmark_symbols
    return tuple(symbol.strip().upper() for symbol in raw.replace(";", ",").split(",") if symbol.strip())


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return max(default, minimum)
    try:
        return max(int(raw), minimum)
    except ValueError:
        return max(default, minimum)


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return max(default, minimum)
    try:
        return max(float(raw), minimum)
    except ValueError:
        return max(default, minimum)
