from __future__ import annotations

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.data_source import fetch_stock_hist
from integrations.fetch_a_share_csv import get_stocks_by_board, normalize_symbols
from integrations.index_data_source import fetch_index_akshare, fetch_index_hist
from integrations.market_metadata import fetch_concept_heat, fetch_concept_map, fetch_market_cap_map, fetch_sector_map
from integrations.ths_hot_concept import fetch_ths_hot_events, merge_concept_heat, ths_hot_events_to_concept_heat
from utils.env import env_bool, env_flag


@dataclass(frozen=True)
class SnapshotRange:
    start: str
    end: str
    prefetch_start: str


def _as_yyyymmdd(text: str) -> str:
    return str(text or "").strip().replace("-", "")


def _normalize_board(board: str) -> str:
    b = str(board or "").strip().lower()
    if b in {"", "all"}:
        return "all"
    if b == "main_chinext":
        return "main_chinext_star"
    return b


#: PIT 股票池的实际启用状态，落进 metadata.json 供报告读取。
#: 报告此前硬编码「仍存在幸存者偏差」，PIT 上线后该结论已过时且会误导读日志的人
#: （2026-08-10 排查时我据此误判 PIT 未生效）。故改为按实际状态输出。
_PIT_STATE: dict = {"pit_universe": False}


def _load_symbols(board: str, sample_size: int, as_of: str = "") -> tuple[list[str], list[dict]]:
    board_norm = _normalize_board(board)
    if as_of and env_bool("BACKTEST_PIT_UNIVERSE", True):
        pit = _load_pit_symbols(board_norm, as_of)
        if pit is not None:
            symbols, pool = pit
            return _apply_sample(symbols, pool, sample_size)

    raw_pool = get_stocks_by_board(board_norm)
    pool: list[dict] = []
    for item in raw_pool:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if not code:
            continue
        pool.append({"code": code, "name": name})

    name_map = {
        str(x.get("code", "")).strip(): str(x.get("name", "")).strip() for x in pool if str(x.get("code", "")).strip()
    }
    symbols = [
        s for s in sorted(set(normalize_symbols(list(name_map.keys())))) if "ST" not in name_map.get(s, "").upper()
    ]
    return _apply_sample(symbols, name_map, sample_size)


def _apply_sample(
    symbols: list[str],
    names: dict[str, str],
    sample_size: int,
) -> tuple[list[str], list[dict]]:
    if sample_size > 0 and sample_size < len(symbols):
        random.seed(42)
        symbols = random.sample(symbols, sample_size)
    return symbols, [{"code": s, "name": names.get(s, "")} for s in symbols]


def _load_pit_symbols(board_norm: str, as_of: str) -> tuple[list[str], dict[str, str]] | None:
    """按窗口起点取 point-in-time 股票池：含当时 ST 与此后才退市的标的。

    拉取失败时返回 None，由调用方回落到存续名单——回放宁可带偏差也不能空跑，
    但偏差必须在日志里说清楚。
    """
    try:
        from integrations.pit_universe import fetch_pit_symbols, tradable_on

        include_bse = board_norm in {"all", "bse"}
        tradable = tradable_on(fetch_pit_symbols(), as_of, include_bse=include_bse)
        if not tradable:
            print(f"[snapshot] PIT 股票池为空 (as_of={as_of})，回落到存续名单")
            return None
        names = _pit_names(tradable, as_of)
        delisted = sum(1 for s in tradable if s.delisted)
        st_now = sum(1 for s in tradable if s.is_st)
        st_then = sum(1 for n in names.values() if "ST" in n.upper())
        print(
            f"[snapshot] PIT 股票池 as_of={as_of}: {len(names)} 只"
            f"（其中此后退市 {delisted}；ST 按当时名 {st_then} 只，按今日名会误判为 {st_now} 只）"
        )
        _PIT_STATE.update(
            {
                "pit_universe": True,
                "as_of": as_of,
                "symbols": len(names),
                "delisted": delisted,
                "st_then": st_then,
                "st_today": st_now,
            }
        )
        return sorted(names), names
    except Exception as exc:
        print(f"[snapshot] PIT 股票池加载失败，回落到存续名单（结果带幸存者偏差）: {exc}")
        return None


def _pit_names(tradable: list, as_of: str) -> dict[str, str]:
    """把 name_map 换成 as-of 当时的名称。

    今日名称会把「当年正常、后来变 ST」的股票误判为 ST，而下游 layer1_filter 与
    backtest_data 都按名称剔除 ST——于是这批当时可交易的标的被静默排除。实测 2020-01-01
    有 224 只属于此类。改名记录拉取失败时回落到今日名称，并明说结果仍带该偏差。
    """
    from integrations.pit_universe import fetch_name_spans, name_on

    fallback = {s.code: s.name for s in tradable}
    try:
        spans = fetch_name_spans()
    except Exception as exc:
        print(f"[snapshot] 改名记录拉取失败，name_map 沿用今日名称（仍会误滤当年正常后变 ST 的标的）: {exc}")
        return fallback
    if not spans:
        print("[snapshot] 改名记录为空，name_map 沿用今日名称")
        return fallback
    return {code: name_on(spans, code, as_of, fallback=today) for code, today in fallback.items()}


def _fetch_one(
    symbol: str,
    prefetch_start: str,
    end_s: str,
) -> tuple[str, pd.DataFrame | None, str | None, float]:
    t0 = time.monotonic()
    try:
        raw = fetch_stock_hist(symbol, prefetch_start, end_s, adjust="qfq")
        if raw is None or raw.empty:
            return (symbol, None, "no_data", time.monotonic() - t0)
        df = normalize_hist_from_fetch(raw)
        if df is None or df.empty:
            return (symbol, None, "normalized_empty", time.monotonic() - t0)
        df["symbol"] = symbol
        return (symbol, df, None, time.monotonic() - t0)
    except Exception as e:
        return (symbol, None, str(e), time.monotonic() - t0)


def _tickflow_window(prefetch_start: str, end_s: str) -> tuple[int, int, int, str, str]:
    start_d = datetime.strptime(prefetch_start, "%Y%m%d").date()
    end_d = datetime.strptime(end_s, "%Y%m%d").date()
    cn_tz = timezone(timedelta(hours=8))
    start_ms = int(datetime.combine(start_d, datetime.min.time(), tzinfo=cn_tz).timestamp() * 1000)
    end_ms = int(
        (
            datetime.combine(end_d + timedelta(days=1), datetime.min.time(), tzinfo=cn_tz) - timedelta(milliseconds=1)
        ).timestamp()
        * 1000
    )
    day_span = (end_d - start_d).days + 1
    count = min(max(day_span * 2 + 16, 64), 5000)
    return start_ms, end_ms, count, start_d.isoformat(), end_d.isoformat()


def _frame_from_tickflow_batch(
    sym: str,
    raw_df: pd.DataFrame | None,
    start_iso: str,
    end_iso: str,
) -> tuple[pd.DataFrame | None, str | None]:
    if raw_df is None or raw_df.empty:
        return None, "no_data_in_batch"
    out = raw_df[(raw_df["date"] >= start_iso) & (raw_df["date"] <= end_iso)].copy()
    if out.empty:
        return None, "empty_in_range"
    close = pd.to_numeric(out.get("close"), errors="coerce")
    prev_close = pd.to_numeric(out.get("prev_close"), errors="coerce")
    prev_ref = prev_close.where(prev_close > 0)
    if prev_ref.notna().sum() == 0:
        prev_ref = close.shift(1)
    pct = (close / prev_ref - 1.0) * 100.0
    high_s = pd.to_numeric(out.get("high"), errors="coerce")
    low_s = pd.to_numeric(out.get("low"), errors="coerce")
    amp = (high_s - low_s) / prev_ref * 100.0
    result = pd.DataFrame(
        {
            "日期": out["date"].values,
            "开盘": out["open"].values,
            "最高": out["high"].values,
            "最低": out["low"].values,
            "收盘": out["close"].values,
            "成交量": out["volume"].values,
            "成交额": out["amount"].values,
            "涨跌幅": pct.values,
            "换手率": 0.0,
            "振幅": amp.values,
        }
    )
    df = normalize_hist_from_fetch(result)
    if df is None or df.empty:
        return None, "normalized_empty"
    df["symbol"] = sym
    return df, None


def _fetch_batch_tickflow(
    symbols: list[str], prefetch_start: str, end_s: str
) -> tuple[list[pd.DataFrame], int, int, list[str]]:
    """用 TickFlow batch API 批量拉取，减少 API 调用次数。"""
    from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol

    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TICKFLOW_API_KEY 未配置")
    client = TickFlowClient(api_key=api_key)
    start_ms, end_ms, count, start_iso, end_iso = _tickflow_window(prefetch_start, end_s)
    batch_result = client.get_klines_batch(
        symbols,
        period="1d",
        count=count,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        adjust="forward",
    )

    all_frames: list[pd.DataFrame] = []
    fail_samples: list[str] = []
    fail = 0
    for sym in symbols:
        df, error = _frame_from_tickflow_batch(sym, batch_result.get(normalize_cn_symbol(sym)), start_iso, end_iso)
        if error:
            fail += 1
            if len(fail_samples) < 10:
                fail_samples.append(f"{sym}: {error}")
            continue
        all_frames.append(df)
    ok = len(all_frames)
    return all_frames, ok, fail, fail_samples


def _snapshot_range(args) -> SnapshotRange:
    start_s = _as_yyyymmdd(args.start)
    end_s = _as_yyyymmdd(args.end)
    start_dt = datetime.strptime(start_s, "%Y%m%d").date()
    prefetch_start = (start_dt - timedelta(days=int(args.trading_days * 2))).strftime("%Y%m%d")
    return SnapshotRange(start=start_s, end=end_s, prefetch_start=prefetch_start)


def _tickflow_batch_enabled() -> bool:
    tf_disabled = env_flag("DATA_SOURCE_DISABLE_TICKFLOW")
    return bool(os.getenv("TICKFLOW_API_KEY", "").strip()) and not tf_disabled


def _fetch_concurrent(
    symbols: list[str],
    prefetch_start: str,
    end_s: str,
    max_workers: int,
) -> tuple[list[pd.DataFrame], int, int, list[str]]:
    all_frames: list[pd.DataFrame] = []
    ok = 0
    fail = 0
    fail_samples: list[str] = []
    workers = max(int(max_workers), 1)
    print(f"[snapshot] fetch模式: 逐只并发, workers={workers}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, sym, prefetch_start, end_s): sym for sym in symbols}
        for done, ft in enumerate(as_completed(futs), 1):
            sym, df, err, elapsed = ft.result()
            if df is not None:
                all_frames.append(df)
                ok += 1
            else:
                fail += 1
                if len(fail_samples) < 10:
                    fail_samples.append(f"{sym}: {str(err)[:200]} (elapsed={elapsed:.1f}s)")
            if done % 500 == 0 or done == len(futs):
                print(f"[snapshot] {done}/{len(futs)} (ok={ok}, fail={fail})")
    return all_frames, ok, fail, fail_samples


def _fetch_snapshot_frames(
    symbols: list[str],
    prefetch_start: str,
    end_s: str,
    max_workers: int,
) -> tuple[list[pd.DataFrame], int, int, list[str]]:
    if _tickflow_batch_enabled():
        print("[snapshot] fetch模式: TickFlow batch API (每批200只，顺序请求避免限流)")
        try:
            return _fetch_batch_tickflow(symbols, prefetch_start, end_s)
        except Exception as exc:
            print(f"[snapshot] batch 模式失败: {exc}，回退逐只并发模式")
    return _fetch_concurrent(symbols, prefetch_start, end_s, max_workers)


def _print_fail_samples(fail_samples: list[str]) -> None:
    if fail_samples:
        print("[snapshot] 失败样本:")
        for item in fail_samples[:10]:
            print(f"  - {item}")


def _fetch_benchmark(prefetch_start: str, end_s: str) -> pd.DataFrame | None:
    bench_main = None
    try:
        bench_main = fetch_index_akshare("000001", prefetch_start, end_s)
        print(f"[snapshot] 大盘指数 via akshare: {len(bench_main)} rows")
    except Exception as e1:
        print(f"[snapshot] akshare 大盘失败: {e1}, fallback fetch_index_hist")
        try:
            bench_main = fetch_index_hist("000001", prefetch_start, end_s)
        except Exception as e2:
            print(f"[snapshot] 大盘指数全部失败（不阻塞）: {e2}")
    return bench_main


def _name_map(raw_pool: list[dict]) -> dict[str, str]:
    name_map: dict[str, str] = {}
    for item in raw_pool:
        if isinstance(item, dict):
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
            if code:
                name_map[code] = name
    return name_map


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_optional_maps(out_dir: Path) -> None:
    try:
        sector_map = fetch_sector_map()
        _write_json(out_dir / "sector_map.json", sector_map)
        print(f"[snapshot] sector_map.json: {len(sector_map)} entries")
    except Exception as exc:
        print(f"[snapshot] sector_map 拉取失败（不阻塞）: {exc}")

    try:
        market_cap_map = fetch_market_cap_map()
        _write_json(out_dir / "market_cap_map.json", market_cap_map)
        print(f"[snapshot] market_cap_map.json: {len(market_cap_map)} entries")
    except Exception as exc:
        print(f"[snapshot] market_cap_map 拉取失败（不阻塞）: {exc}")

    try:
        concept_map = fetch_concept_map()
        _write_json(out_dir / "concept_map.json", concept_map)
        print(f"[snapshot] concept_map.json: {len(concept_map)} entries")
    except Exception as exc:
        print(f"[snapshot] concept_map 拉取失败（不阻塞）: {exc}")

    try:
        concept_heat = fetch_concept_heat()
        hot_events, event_heat = _fetch_snapshot_hot_events()
        merged_heat = merge_concept_heat(concept_heat, event_heat)
        _write_json(out_dir / "ths_hot_events.json", hot_events)
        _write_json(out_dir / "concept_heat.json", merged_heat)
        print(f"[snapshot] ths_hot_events.json: {len(hot_events.get('events') or [])} events")
        print(f"[snapshot] concept_heat.json: {len(merged_heat)} entries")
    except Exception as exc:
        print(f"[snapshot] concept_heat 拉取失败（不阻塞）: {exc}")


def _fetch_snapshot_hot_events() -> tuple[dict, list[dict]]:
    try:
        hot_events = fetch_ths_hot_events()
        return hot_events, ths_hot_events_to_concept_heat(hot_events)
    except Exception as exc:
        print(f"[snapshot] 同花顺事件主线拉取失败（不阻塞）: {exc}")
        return {}, []


def _write_snapshot_outputs(
    out_dir: Path,
    all_frames: list[pd.DataFrame],
    raw_pool: list[dict],
    bench_main: pd.DataFrame | None,
    meta: dict[str, int | str],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    full_df = pd.concat(all_frames, ignore_index=True)
    full_df.to_csv(out_dir / "hist_full.csv.gz", index=False, compression="gzip")
    print(f"[snapshot] hist_full.csv.gz: {len(full_df)} rows")

    if bench_main is not None and not bench_main.empty:
        bench_main.to_csv(out_dir / "benchmark_main.csv", index=False)
        print(f"[snapshot] benchmark_main.csv: {len(bench_main)} rows")

    names = _name_map(raw_pool)
    _write_json(out_dir / "name_map.json", names)
    print(f"[snapshot] name_map.json: {len(names)} entries")
    _write_optional_maps(out_dir)
    _write_json(out_dir / "metadata.json", meta)


def run_snapshot_fetch(args) -> int:
    date_range = _snapshot_range(args)
    print(f"[snapshot] 数据区间: {date_range.prefetch_start} -> {date_range.end}")

    # 用预取起点作为 PIT as-of：窗口开始前已上市、且当时尚未摘牌的才算可交易
    symbols, raw_pool = _load_symbols(args.board, int(args.sample_size), as_of=date_range.prefetch_start)
    if not symbols:
        print("[snapshot] 严重错误: 股票池为空，请检查 board 参数或行情源可用性")
        return 1
    pit_on = bool(date_range.prefetch_start) and env_bool("BACKTEST_PIT_UNIVERSE", True)
    print(
        f"[snapshot] 股票池: {len(symbols)} symbols, sample={symbols[:5]}, "
        f"board={_normalize_board(args.board)}, pit_universe={pit_on}, exclude_st={not pit_on}"
    )

    all_frames, ok, fail, fail_samples = _fetch_snapshot_frames(
        symbols,
        date_range.prefetch_start,
        date_range.end,
        int(args.max_workers),
    )
    _print_fail_samples(fail_samples)
    if not all_frames:
        print("[snapshot] 严重错误: 没有成功拉取任何股票数据!")
        return 1
    if ok < len(symbols) * 0.1:
        print(f"[snapshot] 严重错误: 成功率仅 {ok}/{len(symbols)} ({100 * ok / len(symbols):.1f}%)，低于 10% 阈值")
        return 1

    meta = {
        "symbols": len(symbols),
        "ok": ok,
        "fail": fail,
        "start": date_range.prefetch_start,
        "end": date_range.end,
        **{f"pit_{k}" if k != "pit_universe" else k: v for k, v in _PIT_STATE.items()},
    }
    out_dir = Path(args.output_dir)
    bench_main = _fetch_benchmark(date_range.prefetch_start, date_range.end)
    _write_snapshot_outputs(out_dir, all_frames, raw_pool, bench_main, meta)
    print(f"[snapshot] Done! 成功率: {ok}/{len(symbols)} ({100 * ok / len(symbols):.1f}%)")
    return 0
