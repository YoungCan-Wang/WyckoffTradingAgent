"""Data loading and quote prefiltering for market funnel jobs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.tickflow_client import TickFlowClient
from workflows.market_funnel_runtime import RuntimeConfig


def load_market_symbols(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"market symbol file not found: {path}")
    symbols = _dedupe_symbols(path.read_text(encoding="utf-8").splitlines())
    if not symbols:
        raise ValueError(f"market symbol file is empty: {path}")
    return symbols


def fetch_market_inputs(
    client: TickFlowClient,
    universe_symbols: list[str],
    runtime: RuntimeConfig,
) -> tuple[
    dict[str, dict[str, Any]], list[dict[str, Any]], pd.DataFrame | None, str, dict[str, pd.DataFrame], dict[str, Any]
]:
    quotes = fetch_quotes(client, universe_symbols, runtime)
    ranked = rank_quotes(
        quotes,
        max_symbols=runtime.max_symbols,
        min_quote_amount=runtime.min_quote_amount,
        min_quote_price=runtime.min_quote_price,
    )
    if not ranked and runtime.min_quote_amount > 0:
        print("[market-funnel] quote amount filter returned empty; retry ranking without amount floor")
        ranked = rank_quotes(
            quotes,
            max_symbols=runtime.max_symbols,
            min_quote_amount=0.0,
            min_quote_price=runtime.min_quote_price,
        )
    bench_df, bench_symbol = fetch_benchmark_history(client, runtime)
    df_map, fetch_stats = fetch_daily_histories(client, [str(item["symbol"]) for item in ranked], runtime)
    return quotes, ranked, bench_df, bench_symbol, df_map, fetch_stats


def rank_quotes(
    quotes: dict[str, dict[str, Any]],
    *,
    max_symbols: int,
    min_quote_amount: float,
    min_quote_price: float,
) -> list[dict[str, Any]]:
    rows = [_quote_rank_row(symbol, row, min_quote_amount, min_quote_price) for symbol, row in quotes.items()]
    rows = [row for row in rows if row is not None]
    rows.sort(key=lambda item: (item["amount"], item["volume"]), reverse=True)
    return rows[:max_symbols]


def fetch_quotes(
    client: TickFlowClient,
    symbols: list[str],
    runtime: RuntimeConfig,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    batches = _chunks(symbols, runtime.quote_batch_size)
    for index, batch in enumerate(batches, start=1):
        print(f"[market-funnel] {runtime.spec.label} 行情批次 {index}/{len(batches)} symbols={len(batch)}")
        out.update(client.get_quotes(symbols=batch))
        _sleep_between_batches(index, len(batches), runtime.quote_batch_sleep)
    return out


def fetch_daily_histories(
    client: TickFlowClient,
    symbols: list[str],
    runtime: RuntimeConfig,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    started = time.monotonic()
    out: dict[str, pd.DataFrame] = {}
    batches = _chunks(symbols, runtime.kline_batch_size)
    for index, batch in enumerate(batches, start=1):
        print(f"[market-funnel] {runtime.spec.label} 日K批次 {index}/{len(batches)} symbols={len(batch)}")
        _merge_valid_histories(
            out, client.get_klines_batch(batch, period="1d", count=runtime.kline_count, adjust="forward"), runtime
        )
        _sleep_between_batches(index, len(batches), runtime.kline_batch_sleep)
    return out, _fetch_stats(symbols, out, batches, started)


BENCHMARK_MAX_GAP_DAYS = 21


def fetch_benchmark_history(client: TickFlowClient, runtime: RuntimeConfig) -> tuple[pd.DataFrame | None, str]:
    """按候选顺序取第一份**连续**的基准。

    只看行数会选中坏数据：TickFlow 的港股 ETF 自 2026-03-20 起停更，02800.HK 仍返回 320
    行（历史 319 行 + 一根 2026-08-12 的孤立 K 线），行数检查完全通过，但中间缺 145 天。
    水温分类要在这份序列上算 MA50/MA200 与近 3 日累计涨幅，空洞会让 regime 静默失真。
    因此这里额外要求近端无长断层，并把跳过原因打出来。
    """
    rejected: list[str] = []
    for symbol in runtime.benchmark_symbols:
        try:
            norm = _fetch_one_benchmark_history(client, symbol, runtime.kline_count)
        except Exception as exc:
            rejected.append(f"{symbol}(取数失败: {exc})")
            continue
        if norm is None or len(norm) < 60:
            rejected.append(f"{symbol}(行数不足: {0 if norm is None else len(norm)})")
            continue
        gap = _max_recent_gap_days(norm)
        if gap > BENCHMARK_MAX_GAP_DAYS:
            rejected.append(f"{symbol}(断层 {gap} 天)")
            continue
        if rejected:
            print(f"[market-funnel] benchmark 跳过: {', '.join(rejected)}")
        print(f"[market-funnel] benchmark loaded: {symbol} rows={len(norm)} max_gap={gap}d")
        return norm, symbol
    if rejected:
        print(f"[market-funnel] benchmark 全部不可用: {', '.join(rejected)}")
    return None, ""


def _max_recent_gap_days(df: pd.DataFrame) -> int:
    """最近 120 根 K 线内的最大相邻日期间隔（自然日）。

    只看近端：更早的历史断层不影响当前水温判断，收得太严会把可用基准也否掉。
    """
    if "date" not in df.columns or df.empty:
        return 0
    stamps = pd.to_datetime(df["date"], errors="coerce").dropna().sort_values().tail(120)
    if len(stamps) < 2:
        return 0
    return int(stamps.diff().dt.days.max() or 0)


def _dedupe_symbols(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    symbols: list[str] = []
    for line in lines:
        clean = line.split("#", 1)[0].replace(",", " ").strip()
        for raw in clean.split():
            symbol = raw.strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols


def _quote_rank_row(
    symbol: str,
    row: dict[str, Any],
    min_quote_amount: float,
    min_quote_price: float,
) -> dict[str, Any] | None:
    last_price = _row_float(row, "last_price", "close")
    if last_price is None or last_price <= 0 or last_price < min_quote_price:
        return None
    amount = _row_float(row, "amount") or 0.0
    if amount < min_quote_amount:
        return None
    return {
        "symbol": symbol,
        "name": quote_name(row, symbol),
        "last_price": float(last_price),
        "amount": float(amount),
        "volume": float(_row_float(row, "volume") or 0.0),
        "change_pct": float(_quote_change_pct(row)),
        "sector": _quote_sector(row),
    }


def _row_float(row: dict[str, Any], *keys: str) -> float | None:
    ext = row.get("ext") if isinstance(row.get("ext"), dict) else {}
    for key in keys:
        value = row.get(key)
        if value is None and key.startswith("ext."):
            value = ext.get(key.split(".", 1)[1])
        try:
            if value is not None and pd.notna(value):
                return float(value)
        except Exception:
            continue
    return None


def _quote_change_pct(row: dict[str, Any]) -> float:
    direct = _row_float(row, "change_pct", "ext.change_pct")
    if direct is not None:
        return direct
    last_price = _row_float(row, "last_price", "close")
    prev_close = _row_float(row, "prev_close")
    if last_price is None or prev_close is None or prev_close <= 0:
        return 0.0
    return (last_price / prev_close - 1.0) * 100.0


def quote_name(row: dict[str, Any], symbol: str) -> str:
    """报价行里的名称；美股/港股实际落在 ``ext.name``，顶层 ``name`` 为 None。"""
    ext = row.get("ext") if isinstance(row.get("ext"), dict) else {}
    for value in (row.get("name"), row.get("ext.name"), ext.get("name")):
        text = str(value or "").strip()
        if text:
            return text
    return symbol


def _quote_sector(row: dict[str, Any]) -> str:
    ext = row.get("ext") if isinstance(row.get("ext"), dict) else {}
    keys = ("sector", "industry", "sector_name", "industry_name", "gics_sector", "ext.sector", "ext.industry")
    for key in keys:
        value = row.get(key)
        if value is None and key.startswith("ext."):
            value = ext.get(key.split(".", 1)[1])
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    width = max(int(size), 1)
    return [items[i : i + width] for i in range(0, len(items), width)]


def _sleep_between_batches(index: int, total: int, seconds: float) -> None:
    if index < total and seconds > 0:
        time.sleep(seconds)


def _merge_valid_histories(out: dict[str, pd.DataFrame], batch: dict[str, Any], runtime: RuntimeConfig) -> None:
    for symbol, df in batch.items():
        norm = normalize_hist_from_fetch(df)
        if norm is not None and len(norm) >= runtime.min_history_rows:
            out[symbol] = norm


def _fetch_one_benchmark_history(client: TickFlowClient, symbol: str, kline_count: int) -> pd.DataFrame | None:
    if hasattr(client, "get_klines"):
        raw = client.get_klines(symbol, period="1d", count=kline_count, adjust="forward")
    else:
        raw = client.get_klines_batch([symbol], period="1d", count=kline_count, adjust="forward").get(symbol)
    return normalize_hist_from_fetch(raw) if raw is not None else None


def _fetch_stats(
    symbols: list[str],
    out: dict[str, pd.DataFrame],
    batches: list[list[str]],
    started: float,
) -> dict[str, Any]:
    elapsed = time.monotonic() - started
    return {
        "requested": len(symbols),
        "fetched": len(out),
        "failed": max(len(symbols) - len(out), 0),
        "batches": len(batches),
        "elapsed_s": round(elapsed, 2),
        "qps": round(len(out) / elapsed, 3) if elapsed > 0 else 0.0,
    }
