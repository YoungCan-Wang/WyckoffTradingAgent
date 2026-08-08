"""Today review-pool discovery for strong-move replay jobs."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from core.cn_boards import is_supported_cn_board
from core.wyckoff_engine import sort_by_date_if_needed

TODAY_REVIEW_MIN_PCT = 7.0
PREVIOUS_REVIEW_MAX_PCT = 3.0
EXECUTABLE_OPEN_GAP_MAX_PCT = 4.0


@dataclass(frozen=True)
class ReviewPool:
    codes: list[str]
    frames: dict[str, pd.DataFrame]


def is_target_cn_board(code: str) -> bool:
    return is_supported_cn_board(code)


def find_big_gainers(
    df_map: dict[str, pd.DataFrame],
    name_map: dict[str, str],
    today_threshold: float = TODAY_REVIEW_MIN_PCT,
    previous_max: float = PREVIOUS_REVIEW_MAX_PCT,
) -> list[str]:
    codes: list[str] = []
    for code, df in df_map.items():
        if _skip_daily_candidate(code, df, name_map):
            continue
        latest_pct, previous_pct = latest_and_previous_pct(df)
        if _daily_candidate_matches(latest_pct, previous_pct, today_threshold, previous_max):
            codes.append(code)
    return sorted(codes)


def find_big_gainers_from_spot(
    spot_map: dict[str, dict],
    name_map: dict[str, str],
    threshold: float = TODAY_REVIEW_MIN_PCT,
) -> tuple[list[str], int]:
    codes: list[str] = []
    usable = 0
    for code, snap in (spot_map or {}).items():
        code = str(code).strip()
        if _skip_spot_candidate(code, snap, name_map):
            continue
        try:
            pct_f = float(snap.get("pct_chg"))
            usable += 1
            if pct_f > threshold:
                codes.append(code)
        except Exception:
            continue
    return sorted(codes), usable


def load_today_review_codes(
    all_codes: list[str],
    name_map_today: dict[str, str],
    today_window,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    return load_today_review_pool(all_codes, name_map_today, today_window, log=log).codes


def load_today_review_pool(
    all_codes: list[str],
    name_map_today: dict[str, str],
    today_window,
    log: Callable[[str], None] | None = None,
    previous_trade_date=None,
) -> ReviewPool:
    logger = log or (lambda _msg: None)
    tushare_pool = _load_tushare_review_pool(
        all_codes,
        name_map_today,
        today_window,
        previous_trade_date,
        logger,
    )
    if tushare_pool is not None:
        return tushare_pool
    spot_codes, spot_usable = _load_spot_candidates(name_map_today, logger)
    spot_min_coverage = review_spot_min_coverage()
    spot_coverage = spot_usable / max(len(all_codes), 1)
    if spot_usable > 0 and spot_coverage >= spot_min_coverage:
        return _load_pool_from_sufficient_spot(spot_codes, all_codes, name_map_today, today_window, logger)
    _log_spot_fallback(spot_usable, spot_coverage, spot_min_coverage, logger)
    return fetch_review_pool(all_codes, name_map_today, today_window, logger)


def fetch_review_pool(
    codes: list[str],
    name_map: dict[str, str],
    window,
    log: Callable[[str], None] | None = None,
) -> ReviewPool:
    from tools.data_fetcher import fetch_all_ohlcv
    from workflows.fetch_runtime_config import fetch_runtime_config_from_env

    df_map, stats = fetch_all_ohlcv(
        symbols=codes,
        window=window,
        enforce_target_trade_date=True,
        direct_source=True,
        runtime_config=fetch_runtime_config_from_env(),
    )
    _log_fetch_stats(stats, df_map, window, log or (lambda _msg: None))
    return ReviewPool(find_big_gainers(df_map, name_map), df_map)


def review_spot_min_coverage() -> float:
    try:
        value = float(os.getenv("REVIEW_SPOT_MIN_COVERAGE", "0.8"))
    except ValueError:
        value = 0.8
    return min(max(value, 0.0), 1.0)


def review_tushare_min_coverage() -> float:
    try:
        value = float(os.getenv("REVIEW_TUSHARE_MIN_COVERAGE", "0.8"))
    except ValueError:
        value = 0.8
    return min(max(value, 0.0), 1.0)


def latest_and_previous_pct(df: pd.DataFrame) -> tuple[float | None, float | None]:
    series = sort_by_date_if_needed(df)
    close = pd.to_numeric(series.get("close"), errors="coerce").dropna()
    latest_pct = _latest_close_pct(close)
    previous_pct = _previous_close_pct(close)
    pct = pd.to_numeric(series.get("pct_chg", pd.Series(dtype=float)), errors="coerce")
    if latest_pct is None and len(pct) >= 1 and pd.notna(pct.iloc[-1]):
        latest_pct = float(pct.iloc[-1])
    if previous_pct is None and len(pct) >= 2 and pd.notna(pct.iloc[-2]):
        previous_pct = float(pct.iloc[-2])
    return latest_pct, previous_pct


def execution_snapshot(frame: pd.DataFrame | None) -> dict[str, object]:
    if frame is None or frame.empty:
        return {"available": False, "executable": False, "reason": "缺少当日行情"}
    rows = sort_by_date_if_needed(frame)
    if len(rows) < 2:
        return {"available": False, "executable": False, "reason": "当日行情长度不足"}
    prev_close = _number(rows["close"].iloc[-2]) if "close" in rows.columns else None
    today_open = _last_number(rows, "open")
    today_high = _last_number(rows, "high")
    today_low = _last_number(rows, "low")
    if prev_close is None or prev_close <= 0 or today_open is None or today_open <= 0:
        return {"available": False, "executable": False, "reason": "缺少前收盘或当日开盘"}
    open_gap = (today_open / prev_close - 1.0) * 100.0
    one_price = today_high is not None and today_low is not None and abs(today_high - today_low) <= 1e-8
    executable = open_gap <= EXECUTABLE_OPEN_GAP_MAX_PCT and not one_price
    reason = "开盘可交易" if executable else "一字板不可成交" if one_price else "开盘跳空超过4%"
    return {"available": True, "executable": executable, "open_gap_pct": open_gap, "reason": reason}


def _skip_daily_candidate(code: str, df: pd.DataFrame, name_map: dict[str, str]) -> bool:
    return not is_target_cn_board(code) or "ST" in str(name_map.get(code, "")).upper() or df is None or df.empty


def _daily_candidate_matches(
    latest_pct: float | None,
    previous_pct: float | None,
    today_threshold: float,
    previous_max: float,
) -> bool:
    epsilon = 1e-9
    return (
        latest_pct is not None
        and previous_pct is not None
        and latest_pct > today_threshold + epsilon
        and previous_pct < previous_max - epsilon
    )


def _skip_spot_candidate(code: str, snap: dict, name_map: dict[str, str]) -> bool:
    return (
        code not in name_map
        or not is_target_cn_board(code)
        or "ST" in str(name_map.get(code, "")).upper()
        or not isinstance(snap, dict)
        or snap.get("pct_chg") is None
    )


def _latest_close_pct(close: pd.Series) -> float | None:
    if len(close) < 2:
        return None
    prev_close = float(close.iloc[-2])
    if prev_close <= 0:
        return None
    return (float(close.iloc[-1]) / prev_close - 1.0) * 100.0


def _previous_close_pct(close: pd.Series) -> float | None:
    if len(close) < 3:
        return None
    prev_prev_close = float(close.iloc[-3])
    if prev_prev_close <= 0:
        return None
    return (float(close.iloc[-2]) / prev_prev_close - 1.0) * 100.0


def _number(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if pd.notna(value) else None


def _last_number(frame: pd.DataFrame, column: str) -> float | None:
    return _number(frame[column].iloc[-1]) if column in frame.columns else None


def _load_spot_candidates(name_map_today: dict[str, str], log: Callable[[str], None]) -> tuple[list[str], int]:
    try:
        from integrations.spot_snapshot import load_spot_snapshot_map

        spot_map = load_spot_snapshot_map(force_refresh=True)
        spot_codes, spot_usable = find_big_gainers_from_spot(spot_map=spot_map, name_map=name_map_today)
        log(
            "[review] 实时快照加载完成: "
            f"symbols={len(spot_map or {})}, usable_pct={spot_usable}, "
            f"today_gainers={len(spot_codes)}"
        )
        return spot_codes, spot_usable
    except Exception as exc:
        log(f"[review] 实时快照加载失败，准备回退日线拉取: {exc}")
        return [], 0


def _load_tushare_review_pool(
    all_codes: list[str],
    name_map: dict[str, str],
    today_window,
    previous_trade_date,
    log: Callable[[str], None],
) -> ReviewPool | None:
    try:
        from integrations.tushare_client import get_pro, has_tushare_token

        if not has_tushare_token() or not hasattr(today_window, "end_trade_date"):
            return None
        today = today_window.end_trade_date
        previous = previous_trade_date or _previous_trade_date(today)
        pro = get_pro()
        if pro is None:
            return None
        today_rows = _tushare_rows_by_code(pro.daily(trade_date=today.strftime("%Y%m%d")))
        previous_rows = _tushare_rows_by_code(pro.daily(trade_date=previous.strftime("%Y%m%d")))
        today_coverage = len(today_rows) / max(len(all_codes), 1)
        previous_coverage = len(previous_rows) / max(len(all_codes), 1)
        minimum = review_tushare_min_coverage()
        if min(today_coverage, previous_coverage) < minimum:
            log(
                "[review] Tushare 日截面覆盖不足: "
                f"today={today_coverage:.1%}, previous={previous_coverage:.1%}, min={minimum:.1%}"
            )
            return None
        pool = _review_pool_from_tushare(today_rows, previous_rows, all_codes, name_map, today, previous)
        log(
            "[review] Tushare 双日截面加载完成: "
            f"today_rows={len(today_rows)}, previous_rows={len(previous_rows)}, candidates={len(pool.codes)}"
        )
        return pool
    except Exception as exc:
        log(f"[review] Tushare 日截面加载失败，准备回退实时快照: {exc}")
        return None


def _previous_trade_date(today):
    from integrations.fetch_a_share_csv import resolve_trading_window

    return resolve_trading_window(end_calendar_day=today - timedelta(days=1), trading_days=1).end_trade_date


def _tushare_rows_by_code(frame: pd.DataFrame | None) -> dict[str, dict]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    rows: dict[str, dict] = {}
    for raw in frame.to_dict("records"):
        code = str(raw.get("ts_code") or "").split(".", 1)[0].zfill(6)
        if code.isdigit() and len(code) == 6:
            rows[code] = raw
    return rows


def _review_pool_from_tushare(
    today_rows: dict[str, dict],
    previous_rows: dict[str, dict],
    all_codes: list[str],
    name_map: dict[str, str],
    today,
    previous,
) -> ReviewPool:
    allowed = set(all_codes)
    codes: list[str] = []
    frames: dict[str, pd.DataFrame] = {}
    for code, today_row in today_rows.items():
        previous_row = previous_rows.get(code) or {}
        if _skip_cross_section_code(code, allowed, name_map):
            continue
        if not _daily_candidate_matches(
            _number(today_row.get("pct_chg")),
            _number(previous_row.get("pct_chg")),
            TODAY_REVIEW_MIN_PCT,
            PREVIOUS_REVIEW_MAX_PCT,
        ):
            continue
        frame = _tushare_execution_frame(today_row, previous_row, today, previous)
        if not frame.empty:
            codes.append(code)
            frames[code] = frame
    return ReviewPool(sorted(codes), frames)


def _skip_cross_section_code(code: str, allowed: set[str], name_map: dict[str, str]) -> bool:
    return code not in allowed or not is_target_cn_board(code) or "ST" in str(name_map.get(code, "")).upper()


def _tushare_execution_frame(today_row: dict, previous_row: dict, today, previous) -> pd.DataFrame:
    previous_close = _number(today_row.get("pre_close")) or _number(previous_row.get("close"))
    today_close = _number(today_row.get("close"))
    if previous_close is None or today_close is None:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "date": previous.isoformat(),
                "open": previous_close,
                "high": previous_close,
                "low": previous_close,
                "close": previous_close,
            },
            {
                "date": today.isoformat(),
                "open": _number(today_row.get("open")),
                "high": _number(today_row.get("high")),
                "low": _number(today_row.get("low")),
                "close": today_close,
            },
        ]
    )


def _load_pool_from_sufficient_spot(
    spot_codes: list[str],
    all_codes: list[str],
    name_map_today: dict[str, str],
    today_window,
    log: Callable[[str], None],
) -> ReviewPool:
    if spot_codes:
        pool = fetch_review_pool(spot_codes, name_map_today, today_window, log)
        if pool.codes:
            return pool
        log("[review] 实时快照候选经三日校验为空，回退到全量 OHLCV 校验")
    else:
        log("[review] 实时快照未发现今日候选，回退到全量 OHLCV 校验")
    return fetch_review_pool(all_codes, name_map_today, today_window, log)


def _log_spot_fallback(
    spot_usable: int,
    spot_coverage: float,
    spot_min_coverage: float,
    log: Callable[[str], None],
) -> None:
    if spot_usable <= 0:
        log("[review] 实时快照不可用，回退到三日 OHLCV 拉取")
    else:
        log(
            "[review] 实时快照覆盖不足，回退到三日 OHLCV 拉取: "
            f"coverage={spot_coverage:.1%}, min={spot_min_coverage:.1%}"
        )


def _log_fetch_stats(stats: dict, df_map: dict[str, pd.DataFrame], window, log: Callable[[str], None]) -> None:
    log(
        "[review] 三日数据拉取完成: "
        f"ok={stats.get('fetch_ok', len(df_map))}, "
        f"fail={stats.get('fetch_fail', 0)}, "
        f"target_trade_date={window.end_trade_date}"
    )
