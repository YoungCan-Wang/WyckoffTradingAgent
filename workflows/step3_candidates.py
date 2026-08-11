"""Step3 market context and candidate feature assembly."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial

import pandas as pd

from core.candidate_metadata import CANDIDATE_ATTRIBUTION_COLUMNS
from core.hist_dates import latest_trade_date_from_hist
from core.market_trade_mode import normalize_regime
from core.sector_rotation import SECTOR_STATE_LABELS
from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.fetch_a_share_csv import fetch_hist, resolve_trading_window
from integrations.index_data_source import fetch_index_hist
from integrations.market_metadata import fetch_market_cap_map, fetch_sector_map
from tools.spot_patch import append_spot_bar_if_needed
from utils.trading_clock import resolve_end_calendar_day
from workflows.step3_models import Step3CandidateBundle, Step3MarketContext
from workflows.step3_runtime_config import Step3RuntimeConfig
from workflows.step3_selection import normalize_step3_candidates
from workflows.step3_text import coerce_bool_like

_append_spot_bar_if_needed = partial(
    append_spot_bar_if_needed,
    env_prefix="STEP3",
    zero_fallback=True,
)


@dataclass(frozen=True)
class _HistoryLoadResult:
    item_order: int
    item: dict
    code: str
    df: pd.DataFrame | None
    failed_reason: str


def load_step3_market_context(
    items: list[dict],
    benchmark_context: dict | None,
    runtime_config: Step3RuntimeConfig,
) -> Step3MarketContext:
    ctx = benchmark_context or {}
    end_day = resolve_end_calendar_day()
    window = resolve_trading_window(end_calendar_day=end_day, trading_days=runtime_config.trading_days)
    sector_rotation_ctx = ctx.get("sector_rotation", {}) or {}
    sector_map = fetch_sector_map()
    market_cap_map = fetch_market_cap_map()
    return Step3MarketContext(
        window=window,
        benchmark_context=ctx,
        regime=normalize_regime(ctx.get("regime")),
        sector_rotation_ctx=sector_rotation_ctx,
        sector_rotation_map=sector_rotation_ctx.get("state_map", {}) or {},
        sector_map=sector_map,
        market_cap_map=market_cap_map,
        financial_map=_load_step3_financial_map(items),
        benchmark_ret_10=_benchmark_return(window),
    )


def build_step3_candidate_bundle(
    items: list[dict],
    context: Step3MarketContext,
    runtime_config: Step3RuntimeConfig,
) -> Step3CandidateBundle:
    results = _load_step3_histories(items, context, runtime_config)
    return _step3_candidate_bundle_from_history(results, context)


def _load_step3_histories(
    items: list[dict],
    context: Step3MarketContext,
    runtime_config: Step3RuntimeConfig,
) -> list[_HistoryLoadResult]:
    max_workers = min(max(runtime_config.history_max_workers, 1), len(items) or 1)
    if max_workers <= 1 or len(items) <= 1:
        return [
            _load_step3_history_result(
                item_order,
                item,
                context.window,
                runtime_config.enforce_target_trade_date,
            )
            for item_order, item in enumerate(items)
        ]
    return _load_step3_histories_parallel(items, context, runtime_config, max_workers)


def _load_step3_histories_parallel(
    items: list[dict],
    context: Step3MarketContext,
    runtime_config: Step3RuntimeConfig,
    max_workers: int,
) -> list[_HistoryLoadResult]:
    ordered: list[_HistoryLoadResult | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="step3-history") as pool:
        futures = [
            pool.submit(
                _load_step3_history_result,
                item_order,
                item,
                context.window,
                runtime_config.enforce_target_trade_date,
            )
            for item_order, item in enumerate(items)
        ]
        for future in as_completed(futures):
            result = future.result()
            ordered[result.item_order] = result
    return [result for result in ordered if result is not None]


def _load_step3_history_result(
    item_order: int,
    item: dict,
    window: object,
    enforce_target_trade_date: bool,
) -> _HistoryLoadResult:
    code = str(item.get("code") or "").strip()
    if not code:
        return _HistoryLoadResult(item_order, item, "", None, "missing code")
    try:
        df, failed_reason = _load_step3_history(
            code,
            window,
            enforce_target_trade_date=enforce_target_trade_date,
        )
        return _HistoryLoadResult(item_order, item, code, df, failed_reason or "")
    except Exception as e:
        return _HistoryLoadResult(item_order, item, code, None, str(e))


def _step3_candidate_bundle_from_history(
    results: list[_HistoryLoadResult],
    context: Step3MarketContext,
) -> Step3CandidateBundle:
    failed: list[tuple[str, str]] = []
    candidate_rows: list[dict] = []
    code_to_df: dict[str, pd.DataFrame] = {}
    for result in results:
        if result.failed_reason or result.df is None:
            failed.append((result.code, result.failed_reason or "empty dataframe"))
            continue
        code_to_df[result.code] = result.df
        candidate_rows.append(_build_step3_candidate_row(result.item_order, result.item, result.df, context))
    return Step3CandidateBundle(
        candidates_df=normalize_step3_candidates(candidate_rows),
        code_to_df=code_to_df,
        failed=failed,
    )


def _safe_return(series: pd.Series, lookback: int = 10) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= lookback:
        return None
    start = float(s.iloc[-lookback - 1])
    end = float(s.iloc[-1])
    if start == 0:
        return None
    return (end - start) / start * 100.0


def _benchmark_return(window: object) -> float | None:
    try:
        bench_df = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        return _safe_return(bench_df["close"], lookback=10)
    except Exception:
        return None


def _load_step3_financial_map(items: list[dict]) -> dict[str, dict]:
    financial_map: dict[str, dict] = {}
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not tickflow_api_key:
        return financial_map
    try:
        return _fetch_tickflow_financial_map(items, tickflow_api_key)
    except Exception as e:
        print(f"[step3] TickFlow 财务指标加载失败: {e}")
        return financial_map


def _fetch_tickflow_financial_map(items: list[dict], api_key: str) -> dict[str, dict]:
    from integrations.tickflow_client import fetch_financial_metric_map

    codes = [str(i["code"]) for i in items if i.get("code")]
    print(f"[step3] TickFlow 财务指标请求: symbols={len(codes)}")
    financial_map = fetch_financial_metric_map(api_key, codes)
    missing_codes = sorted(set(codes) - set(financial_map))
    print(
        f"[step3] TickFlow 财务指标: {len(codes) - len(missing_codes)}/{len(codes)}, "
        f"missing={len(missing_codes)}, sample_missing={','.join(missing_codes[:8]) or '-'}"
    )
    return financial_map


def _step3_sector_fields(item: dict, code: str, context: Step3MarketContext) -> tuple[str, str, str, str]:
    industry = str(item.get("industry") or context.sector_map.get(code, "未知行业") or "未知行业").strip()
    rotation_info = context.sector_rotation_map.get(industry, {}) or {}
    state_code = str(item.get("sector_state_code") or rotation_info.get("state", "") or "NEUTRAL_MIXED").strip()
    state = str(
        item.get("sector_state")
        or rotation_info.get("label", "")
        or SECTOR_STATE_LABELS.get("NEUTRAL_MIXED", "中性混沌")
    ).strip()
    note = str(item.get("sector_note") or rotation_info.get("note", "") or "").strip()
    return industry, state, state_code, note


def _load_step3_history(
    code: str,
    window: object,
    *,
    enforce_target_trade_date: bool,
) -> tuple[pd.DataFrame | None, str | None]:
    df = normalize_hist_from_fetch(fetch_hist(code, window, "qfq"))
    if not enforce_target_trade_date:
        return df, None
    latest_trade_date = latest_trade_date_from_hist(df)
    if latest_trade_date != window.end_trade_date:
        df, patched = _append_spot_bar_if_needed(code, df, window.end_trade_date)
        if patched:
            latest_trade_date = latest_trade_date_from_hist(df)
            print(f"[step3] {code} 实时快照补偿成功")
    if latest_trade_date != window.end_trade_date:
        return None, f"latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}"
    return df, None


def _build_step3_candidate_row(
    item_order: int,
    item: dict,
    df: pd.DataFrame,
    context: Step3MarketContext,
) -> dict:
    amount, close, volume = _price_volume_series(df)
    avg_amount_20_yi, min_vol_ratio_5d = _liquidity_features(amount, volume)
    stock_ret_10 = _safe_return(close, lookback=10)
    industry, sector_state, sector_state_code, sector_note = _step3_sector_fields(item, item["code"], context)
    return {
        **_base_candidate_fields(item_order, item),
        "industry": industry,
        "sector_state": sector_state,
        "sector_state_code": sector_state_code,
        "sector_note": sector_note,
        "market_cap_yi": pd.to_numeric(context.market_cap_map.get(item["code"]), errors="coerce"),
        "avg_amount_20_yi": avg_amount_20_yi,
        "bias_200": _bias_200(close),
        "rs_10": _relative_strength(stock_ret_10, context.benchmark_ret_10),
        "min_vol_ratio_5d": min_vol_ratio_5d,
        "springboard_grade": str(item.get("springboard_grade", "") or ""),
    }


def _price_volume_series(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    amount = pd.to_numeric(df["amount"], errors="coerce") if "amount" in df.columns else close * volume
    if amount.isna().all():
        amount = pd.Series(close * volume, index=df.index, dtype=float)
    return amount, close, volume


def _liquidity_features(amount: pd.Series, volume: pd.Series) -> tuple[float | object, float | object]:
    vol_ma20 = volume.rolling(20).mean()
    amount_ma20 = amount.rolling(20).mean()
    vol_ratio = volume / vol_ma20.replace(0, pd.NA)
    avg_amount_20_yi = (
        float(amount_ma20.iloc[-1]) / 1e8 if len(amount_ma20) and pd.notna(amount_ma20.iloc[-1]) else pd.NA
    )
    return avg_amount_20_yi, pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()


def _bias_200(close: pd.Series) -> float | object:
    ma200 = close.rolling(200).mean()
    latest_close = close.iloc[-1] if len(close) else pd.NA
    latest_ma200 = ma200.iloc[-1] if len(ma200) else pd.NA
    if pd.notna(latest_close) and pd.notna(latest_ma200) and float(latest_ma200) != 0:
        return (float(latest_close) - float(latest_ma200)) / float(latest_ma200) * 100.0
    return pd.NA


def _relative_strength(stock_ret_10: float | None, benchmark_ret_10: float | None) -> float | None:
    if stock_ret_10 is not None and benchmark_ret_10 is not None:
        return stock_ret_10 - benchmark_ret_10
    return stock_ret_10


def _base_candidate_fields(item_order: int, item: dict) -> dict:
    code = item["code"]
    return {
        "code": code,
        "name": item.get("name", code),
        "input_order": item_order,
        "tag": item.get("tag", ""),
        "track": str(item.get("track", "")).strip(),
        "stage": str(item.get("stage", "")).strip(),
        "funnel_score": pd.to_numeric(item.get("score"), errors="coerce"),
        "priority_score": pd.to_numeric(item.get("priority_score"), errors="coerce"),
        "priority_rank": pd.to_numeric(item.get("priority_rank"), errors="coerce"),
        "selection_source": str(item.get("selection_source", "") or "").strip(),
        "selection_is_fill": coerce_bool_like(item.get("selection_is_fill")),
        "source_type": str(item.get("source_type", "") or "").strip(),
        "signal_status": str(item.get("signal_status", "") or "").strip(),
        "signal_type": str(item.get("signal_type", "") or "").strip(),
        "signal_date": str(item.get("signal_date", "") or "").strip(),
        "confirm_date": str(item.get("confirm_date", "") or "").strip(),
        "confirm_reason": str(item.get("confirm_reason", "") or "").strip(),
        "exit_signal": str(item.get("exit_signal", "")).strip(),
        "exit_price": pd.to_numeric(item.get("exit_price"), errors="coerce"),
        "exit_reason": str(item.get("exit_reason", "")).strip(),
        **_candidate_attribution_fields(item),
    }


def _candidate_attribution_fields(item: dict) -> dict:
    return {key: item[key] for key in CANDIDATE_ATTRIBUTION_COLUMNS if item.get(key) not in (None, "", [], {})}
