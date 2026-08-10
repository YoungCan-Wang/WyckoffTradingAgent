"""Data loading helpers for the daily backtest runner."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pandas as pd

from core.cn_boards import cn_board, is_supported_cn_board
from core.hk_boards import is_hk_main_board
from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.data_source import fetch_stock_hist
from integrations.fetch_a_share_csv import get_stocks_by_board, normalize_symbols
from integrations.index_data_source import fetch_index_hist
from integrations.market_metadata import fetch_concept_heat, fetch_concept_map, fetch_market_cap_map, fetch_sector_map
from integrations.market_universe import load_hk_symbols, load_us_symbols

logger = logging.getLogger(__name__)

ProgressReporter = Callable[[str, str, float], None]
_HIST_CANDIDATE_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]


@dataclass(frozen=True)
class BacktestUniverse:
    symbols: list[str]
    name_map: dict[str, str]
    source: str


@dataclass(frozen=True)
class BacktestHistory:
    all_df_map: dict[str, pd.DataFrame]
    bench_df: pd.DataFrame
    failures: list[str]
    snapshot_rows_total: int = 0
    snapshot_used: bool = False


@dataclass(frozen=True)
class BacktestMetadata:
    market_cap_map: dict[str, float]
    sector_map: dict[str, str]
    concept_map: dict[str, list[str]]
    concept_heat: list[dict]
    financial_map: dict[str, dict]
    source: str


def normalize_backtest_board(board: str) -> str:
    b = str(board or "").strip().lower()
    if b == "us":
        return "us"
    if b in {"", "all"}:
        return "all"
    if b == "main_chinext":
        return "main_chinext_star"
    return b


def board_match(code: str, board: str) -> bool:
    b = normalize_backtest_board(board)
    if b == "us":
        return True
    c = str(code or "").strip()
    if b == "hk":
        return c.upper().endswith(".HK") and is_hk_main_board(c)
    if b in {"main", "chinext", "star", "bse"}:
        return cn_board(c) == b
    if b == "main_chinext_star":
        return is_supported_cn_board(c, include_bse=False)
    return is_supported_cn_board(c)


def build_universe(board: str, sample_size: int) -> tuple[list[str], dict[str, str]]:
    board_norm = normalize_backtest_board(board)
    if board_norm == "us":
        symbols, name_map = load_us_symbols()
        return symbols[:sample_size] if sample_size > 0 else symbols, name_map
    if board_norm == "hk":
        symbols, name_map = load_hk_symbols()
        return symbols[:sample_size] if sample_size > 0 else symbols, name_map

    board_arg = board_norm if board_norm in {"main", "chinext", "star", "bse", "main_chinext_star"} else "all"
    items = get_stocks_by_board(board_arg)
    name_map = {
        str(x.get("code", "")).strip(): str(x.get("name", "")).strip() for x in items if str(x.get("code", "")).strip()
    }
    symbols = _filter_symbols(list(name_map.keys()), name_map, board_norm)
    return symbols[:sample_size] if sample_size > 0 else symbols, name_map


def resolve_backtest_universe(board: str, sample_size: int, snapshot_dir: Path | None) -> BacktestUniverse:
    name_map = load_snapshot_name_map(snapshot_dir) if snapshot_dir is not None else None
    if name_map:
        board_norm = normalize_backtest_board(board)
        raw_symbols = list(name_map.keys())
        symbols = raw_symbols if board_norm in {"us", "hk"} else normalize_symbols(raw_symbols)
        symbols = _filter_symbols(symbols, name_map, board_norm)
        if sample_size > 0:
            symbols = symbols[:sample_size]
        return BacktestUniverse(symbols=symbols, name_map=name_map, source="快照 name_map")

    symbols, source_name_map = build_universe(board=board, sample_size=sample_size)
    return BacktestUniverse(symbols=symbols, name_map=source_name_map, source="网络拉取")


def _filter_symbols(symbols: list[str], name_map: dict[str, str], board: str) -> list[str]:
    return sorted(
        {
            str(symbol).strip()
            for symbol in symbols
            if board_match(str(symbol).strip(), board) and "ST" not in name_map.get(str(symbol).strip(), "").upper()
        }
    )


def process_hist_chunk(chunk: pd.DataFrame, symbols_filter: set[str] | None, out: dict[str, pd.DataFrame]) -> int:
    chunk["symbol"] = chunk["symbol"].astype(str).str.strip()
    cn_mask = ~chunk["symbol"].str.contains(".", regex=False)
    chunk.loc[cn_mask, "symbol"] = chunk.loc[cn_mask, "symbol"].str.zfill(6)
    if symbols_filter:
        chunk = chunk[chunk["symbol"].isin(symbols_filter)]
    if chunk.empty:
        return 0
    chunk = chunk.copy()
    chunk["date"] = pd.to_datetime(chunk["date"], errors="coerce").dt.date
    chunk = chunk.dropna(subset=["symbol", "date"])
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
        if col in chunk.columns:
            chunk[col] = pd.to_numeric(chunk[col], errors="coerce")
    for sym, group in chunk.groupby("symbol", sort=False):
        part = group.drop(columns=["symbol"]).reset_index(drop=True)
        out[sym] = pd.concat([out[sym], part], ignore_index=True) if sym in out else part
    return len(chunk)


def load_snapshot_hist_map(
    snapshot_dir: Path, symbols_filter: set[str] | None = None
) -> tuple[dict[str, pd.DataFrame], int]:
    full_path = snapshot_dir / "hist_full.csv.gz"
    if not full_path.exists():
        raise FileNotFoundError(f"snapshot missing file: {full_path}")
    header = pd.read_csv(full_path, compression="gzip", nrows=0).columns
    keep_cols = [col for col in _HIST_CANDIDATE_COLS if col in header]
    if "symbol" not in keep_cols:
        raise RuntimeError(f"snapshot file missing symbol column: {full_path}")

    out: dict[str, pd.DataFrame] = {}
    total_rows = 0
    reader = pd.read_csv(full_path, compression="gzip", chunksize=200_000, dtype={"symbol": str}, usecols=keep_cols)
    for chunk in reader:
        total_rows += process_hist_chunk(chunk, symbols_filter, out)
    for sym in out:
        out[sym] = out[sym].sort_values("date").reset_index(drop=True)
    return out, total_rows


def load_snapshot_benchmark(snapshot_dir: Path) -> pd.DataFrame | None:
    bench_path = snapshot_dir / "benchmark_main.csv"
    if not bench_path.exists():
        return None
    out = pd.read_csv(bench_path, low_memory=False)
    if out.empty or "date" not in out.columns:
        return None
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume", "pct_chg"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out if not out.empty else None


def load_snapshot_pit_meta(snapshot_dir: Path | None) -> dict:
    """读取快照的 PIT 状态，供报告据实描述幸存者偏差。

    报告此前硬编码「仍存在幸存者偏差：股票池来自当前在市样本」。PIT 股票池上线后
    该句已不准确——实测 bull_2020 窗口补回 231 只此后退市的标的、ST 按当时名 86 只
    （按今日名会误判为 260 只）。而这行静态文案会让读日志的人以为 PIT 没生效
    （2026-08-10 我本人就据此误判过一次）。
    """
    data = _load_json_map(snapshot_dir, "metadata.json")
    return data if isinstance(data, dict) else {}


def load_snapshot_name_map(snapshot_dir: Path | None) -> dict[str, str] | None:
    data = _load_json_map(snapshot_dir, "name_map.json")
    return {str(k): str(v) for k, v in data.items()} if data else None


def load_snapshot_sector_map(snapshot_dir: Path | None) -> dict[str, str] | None:
    data = _load_json_map(snapshot_dir, "sector_map.json")
    return {str(k): str(v) for k, v in data.items()} if data else None


def load_snapshot_market_cap_map(snapshot_dir: Path | None) -> dict[str, float] | None:
    data = _load_json_map(snapshot_dir, "market_cap_map.json")
    return {str(k): float(v) for k, v in data.items() if v is not None} if data else None


def load_snapshot_concept_map(snapshot_dir: Path | None) -> dict[str, list[str]] | None:
    data = _load_json_map(snapshot_dir, "concept_map.json")
    if not data:
        return None
    return {str(k): [str(item) for item in v or []] for k, v in data.items() if isinstance(v, list)}


def load_snapshot_concept_heat(snapshot_dir: Path | None) -> list[dict] | None:
    data = _load_json_list(snapshot_dir, "concept_heat.json")
    return [item for item in data if isinstance(item, dict)] if data else None


def load_snapshot_financial_map(snapshot_dir: Path | None) -> dict[str, dict] | None:
    data = _load_json_map(snapshot_dir, "financial_map.json")
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if data else None


def load_backtest_metadata(
    use_current_meta: bool,
    snapshot_dir: Path | None,
    *,
    allow_static_meta: bool = False,
) -> BacktestMetadata:
    """装配回测元数据。

    元数据分两类：市值/行业/概念归属是缓变的静态属性，用当前截面近似历史只带来
    轻微偏差；``concept_heat`` 是抓取当日的题材热度快照，把它喂给 N 天前的决策
    等于让回测预知哪个题材会火，是真正的前瞻偏差。

    ``allow_static_meta=True`` 时保留静态映射、单独剔除 ``concept_heat``，让
    L1 市值过滤与 L3 板块共振仍可工作，同时不引入题材前瞻。
    """
    if not use_current_meta and not allow_static_meta:
        logger.info("偏差抑制口径：关闭当前截面市值/行业/概念/财务元数据")
        return BacktestMetadata({}, {}, {}, [], {}, "disabled")

    snap = _snapshot_metadata(snapshot_dir)
    if snap is not None:
        return snap if use_current_meta else _without_concept_heat(snap)
    if not use_current_meta:
        logger.info("偏差抑制口径：无快照可用，退回全空元数据")
        return BacktestMetadata({}, {}, {}, [], {}, "disabled")

    market_cap_map = fetch_market_cap_map()
    sector_map = fetch_sector_map()
    concept_map = fetch_concept_map()
    concept_heat = fetch_concept_heat()
    logger.warning("使用当前截面市值/行业映射（会引入 look-ahead bias）")
    if not market_cap_map:
        logger.warning("当前市值映射为空，Layer1 市值过滤将被跳过")
    return BacktestMetadata(market_cap_map, sector_map, concept_map, concept_heat, {}, "current")


def _without_concept_heat(metadata: BacktestMetadata) -> BacktestMetadata:
    """剔除题材热度，保留静态映射。

    concept_heat 是抓取当日快照，喂给历史决策会让主线引擎预知未来热点。
    """
    if not metadata.concept_heat:
        return metadata
    logger.info("偏差抑制口径：保留市值/行业/概念归属，剔除 concept_heat(%d 条)", len(metadata.concept_heat))
    return replace(metadata, concept_heat=[], source=f"{metadata.source}_no_heat")


def _snapshot_metadata(snapshot_dir: Path | None) -> BacktestMetadata | None:
    snap_sector = load_snapshot_sector_map(snapshot_dir)
    snap_cap = load_snapshot_market_cap_map(snapshot_dir)
    snap_concept_map = load_snapshot_concept_map(snapshot_dir)
    snap_concept_heat = load_snapshot_concept_heat(snapshot_dir)
    snap_financial_map = load_snapshot_financial_map(snapshot_dir)
    if not any(
        item is not None for item in (snap_sector, snap_cap, snap_concept_map, snap_concept_heat, snap_financial_map)
    ):
        return None
    metadata = BacktestMetadata(
        market_cap_map=snap_cap or {},
        sector_map=snap_sector or {},
        concept_map=snap_concept_map or {},
        concept_heat=snap_concept_heat or [],
        financial_map=snap_financial_map or {},
        source="snapshot",
    )
    logger.info(
        "元数据从快照加载: sector=%d, cap=%d, concept=%d, heat=%d, financial=%d",
        len(metadata.sector_map),
        len(metadata.market_cap_map),
        len(metadata.concept_map),
        len(metadata.concept_heat),
        len(metadata.financial_map),
    )
    return metadata


def _load_json_map(snapshot_dir: Path | None, filename: str) -> dict | None:
    if snapshot_dir is None:
        return None
    path = snapshot_dir / filename
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("failed to load snapshot json: %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) and data else None


def _load_json_list(snapshot_dir: Path | None, filename: str) -> list | None:
    if snapshot_dir is None:
        return None
    path = snapshot_dir / filename
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("failed to load snapshot json: %s", path, exc_info=True)
        return None
    return data if isinstance(data, list) and data else None


def fetch_hist_norm(symbol: str, start_dt: date, end_dt: date) -> tuple[str, pd.DataFrame | None, str | None]:
    try:
        raw = fetch_stock_hist(symbol, start_dt, end_dt, adjust="qfq")
        df = normalize_hist_from_fetch(raw)
        if df is None or df.empty:
            return symbol, None, "empty"
        out = df.sort_values("date").copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
        out = out.dropna(subset=["date"]).reset_index(drop=True)
        return (symbol, out, None) if not out.empty else (symbol, None, "empty_after_date_parse")
    except Exception as exc:
        return symbol, None, str(exc)


def fetch_online_history_map(
    symbols: list[str],
    start_dt: date,
    end_dt: date,
    max_workers: int,
    progress: ProgressReporter | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    all_df_map: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(int(max_workers), 1)) as executor:
        futures = {executor.submit(fetch_hist_norm, sym, start_dt, end_dt): sym for sym in symbols}
        for done, future in enumerate(as_completed(futures), 1):
            sym = futures[future]
            code, df, err = future.result()
            if df is not None and not df.empty:
                all_df_map[code] = df
            else:
                failures.append(f"{sym}:{err or 'unknown'}")
            if done % 200 == 0 or done == len(futures):
                logger.info("拉取进度 %d/%d", done, len(futures))
                if progress is not None:
                    progress("拉取历史", f"{done}/{len(futures)}", done / len(futures) * 0.4)
    return all_df_map, failures


def fetch_benchmark_hist(benchmark: str, start_dt: date, end_dt: date) -> pd.DataFrame:
    try:
        bench_raw = fetch_index_hist(benchmark, start_dt, end_dt)
    except Exception as exc:
        raise RuntimeError(f"回测需要基准 {benchmark} 的交易日历数据。") from exc
    out = normalize_hist_from_fetch(bench_raw).sort_values("date").copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out = out.dropna(subset=["date"]).reset_index(drop=True)
    return out


def load_backtest_history(
    *,
    symbols: list[str],
    snapshot_dir: Path | None,
    benchmark: str,
    start_dt: date,
    end_dt: date,
    max_workers: int,
    progress: ProgressReporter | None = None,
) -> BacktestHistory:
    snapshot_rows_total = 0
    if snapshot_dir is not None:
        logger.info("使用本地快照: %s", snapshot_dir)
        all_df_map, snapshot_rows_total = load_snapshot_hist_map(snapshot_dir, symbols_filter=set(symbols))
        if not all_df_map:
            raise RuntimeError(f"快照无可用历史数据: {snapshot_dir}")
        bench_df = load_snapshot_benchmark(snapshot_dir)
        logger.info("快照载入完成: ok=%d, rows=%d", len(all_df_map), snapshot_rows_total)
        if bench_df is None or bench_df.empty:
            bench_df = fetch_benchmark_hist(benchmark, start_dt, end_dt)
        return BacktestHistory(all_df_map, bench_df, [], snapshot_rows_total, True)

    logger.info("开始拉取历史日线: symbols=%d, workers=%s", len(symbols), max_workers)
    if progress is not None:
        progress("拉取历史", f"共{len(symbols)}只", 0.0)
    all_df_map, failures = fetch_online_history_map(symbols, start_dt, end_dt, max_workers, progress)
    logger.info("历史拉取完成: ok=%d, fail=%d", len(all_df_map), len(failures))
    if progress is not None:
        progress("拉取完成", f"成功={len(all_df_map)}", 0.4)
    return BacktestHistory(all_df_map, fetch_benchmark_hist(benchmark, start_dt, end_dt), failures)
