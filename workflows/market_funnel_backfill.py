"""按历史交易日回刷美股/港股推荐表（as-of 口径）。

与 A 股 ``recommendation_backfill`` 同样的安全语义：默认 dry-run 只出 artifact，
``--apply`` 才写库；写前把目标日期的旧行备份进 artifact，便于回滚。

一次取数、多日重放：历史日 K 拉一次，按每个目标日分别截断，避免重复打满限流。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from integrations.market_universe import load_hk_symbols, load_us_symbols
from integrations.recommendation_global import resolve_global_table, upsert_global_recommendations
from integrations.recommendation_tracking_common import chunked, fetch_records_from_table
from integrations.supabase_base import create_admin_client, is_admin_configured, require_server_write_context
from integrations.tickflow_client import TickFlowClient
from workflows.market_funnel_asof import build_asof_pool, truncate_history
from workflows.market_funnel_data import (
    fetch_benchmark_history,
    fetch_daily_histories,
    fetch_quotes,
    quote_name,
)
from workflows.market_funnel_job import run_funnel_for_ranked
from workflows.market_funnel_runtime import RuntimeConfig, runtime_config_from_env

_LOADERS = {"us": load_us_symbols, "hk": load_hk_symbols}


@dataclass(frozen=True)
class MarketBackfillRequest:
    market: str
    dates: tuple[date, ...]
    output_dir: str
    apply: bool = False


def run_market_funnel_backfill(request: MarketBackfillRequest) -> int:
    market = request.market.lower()
    if market not in _LOADERS:
        raise ValueError(f"unsupported market: {market}, must be 'us' or 'hk'")
    target_dates = tuple(sorted(set(request.dates)))
    if not target_dates:
        raise ValueError("dates 不能为空")
    if not is_admin_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    if request.apply:
        require_server_write_context(f"backfill {resolve_global_table(market)}")

    output_dir = Path(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime = runtime_config_from_env(market, None)
    print(f"[funnel-backfill] market={market} dates={','.join(d.isoformat() for d in target_dates)}")

    day_rows = _replay_dates(market, runtime, target_dates)
    client = create_admin_client()
    old_rows = _fetch_target_rows(client, market, target_dates)
    _write_artifacts(output_dir, market, target_dates, day_rows, old_rows)

    if not request.apply:
        total = sum(len(rows) for rows in day_rows.values())
        print(f"[funnel-backfill] dry-run 完成，未写库。候选合计 {total} 行，核对 artifact 后加 --apply。")
        return 0
    return _apply(client, market, day_rows, old_rows)


def _replay_dates(
    market: str,
    runtime: RuntimeConfig,
    target_dates: tuple[date, ...],
) -> dict[int, list[dict[str, Any]]]:
    symbols, name_map = _LOADERS[market]()
    client = TickFlowClient(api_key=_require_api_key())
    name_map = _augment_names(client, symbols, name_map, runtime)
    bench_df, bench_symbol = fetch_benchmark_history(client, runtime)
    print(f"[funnel-backfill] 取历史日K symbols={len(symbols)} count={runtime.kline_count}")
    df_map, _ = fetch_daily_histories(client, symbols, runtime)
    print(f"[funnel-backfill] 历史可用 {len(df_map)}/{len(symbols)}")

    out: dict[int, list[dict[str, Any]]] = {}
    for as_of in target_dates:
        pool = build_asof_pool(df_map, name_map, runtime, as_of)
        bench_asof = truncate_history(bench_df, as_of)
        _, candidates = run_funnel_for_ranked(pool.ranked, pool.df_map, runtime, bench_asof, bench_symbol)
        rows = [_tracking_row(item) for item in candidates]
        out[_date_int(as_of)] = rows
        print(f"[funnel-backfill] {as_of.isoformat()} pool={len(pool.ranked)} candidates={len(rows)}")
    return out


def _augment_names(
    client: TickFlowClient,
    symbols: list[str],
    name_map: dict[str, str],
    runtime: RuntimeConfig,
) -> dict[str, str]:
    """用实时报价补名称。

    美股/港股 universe meta 的 name 字段为空，若直接回落成代码，回刷后线上表的
    名称会从「爱彼迎」退化成「ABNB.US」。名称不参与 as-of 判定（只是展示字段），
    所以用今天的报价补名不会引入未来信息；标的改名属极少数，可接受。
    """
    out = {code: name for code, name in name_map.items() if str(name or "").strip()}
    quotes = fetch_quotes(client, symbols, runtime)
    for code, row in quotes.items():
        name = quote_name(row or {}, code)
        if name and name.upper() != code.upper():
            out[code] = name
    print(f"[funnel-backfill] 名称补齐 {len(out)}/{len(symbols)}（meta 为空，取自实时报价）")
    return out


def _tracking_row(candidate: dict[str, Any]) -> dict[str, Any]:
    """与生产 ``market_funnel_tracking._tracking_row`` 同构；日期由 upsert 的参数决定。"""
    return {
        "code": str(candidate.get("symbol", "")).strip(),
        "name": str(candidate.get("name", "")).strip(),
        "tag": ",".join(candidate.get("triggers") or []),
        "score": float(candidate.get("score") or 0),
        "latest_close": float(candidate.get("latest_close") or 0),
    }


def _apply(
    client,
    market: str,
    day_rows: dict[int, list[dict[str, Any]]],
    old_rows: list[dict[str, Any]],
) -> int:
    table = resolve_global_table(market)
    upserted = 0
    for recommend_date, rows in sorted(day_rows.items()):
        if not rows:
            print(f"[funnel-backfill] {recommend_date} 无候选，跳过写入（旧行保留）")
            continue
        if not upsert_global_recommendations(recommend_date, rows, market):
            raise RuntimeError(f"DB write failed: market={market} date={recommend_date}")
        upserted += len(rows)
        print(f"[funnel-backfill] wrote date={recommend_date} rows={len(rows)}")
    stale = _stale_row_ids(day_rows, old_rows)
    for batch in chunked(stale, 500):
        client.table(table).delete().in_("id", batch).execute()
    print(f"[funnel-backfill] done upserted={upserted} stale_deleted={len(stale)}")
    return 0


def _stale_row_ids(day_rows: dict[int, list[dict[str, Any]]], old_rows: list[dict[str, Any]]) -> list[Any]:
    """目标日旧行里、新结果不再包含的代码：删掉，避免旧口径残留。

    某目标日新结果为空时不删该日旧行——空结果更可能是取数异常而非真的无候选。
    """
    fresh = {date_int: {str(row["code"]).strip() for row in rows} for date_int, rows in day_rows.items() if rows}
    stale: list[Any] = []
    for row in old_rows:
        try:
            recommend_date = int(row.get("recommend_date"))
        except (TypeError, ValueError):
            continue
        codes = fresh.get(recommend_date)
        if codes is not None and str(row.get("code") or "").strip() not in codes and row.get("id") is not None:
            stale.append(row["id"])
    return stale


def _fetch_target_rows(client, market: str, target_dates: tuple[date, ...]) -> list[dict[str, Any]]:
    wanted = {_date_int(day) for day in target_dates}
    rows = fetch_records_from_table(client, resolve_global_table(market), "*")
    return [row for row in rows if _safe_int(row.get("recommend_date")) in wanted]


def _write_artifacts(
    output_dir: Path,
    market: str,
    target_dates: tuple[date, ...],
    day_rows: dict[int, list[dict[str, Any]]],
    old_rows: list[dict[str, Any]],
) -> None:
    stamp = datetime.now(UTC).isoformat()
    summary = {
        "generated_at": stamp,
        "market": market,
        "table": resolve_global_table(market),
        "target_dates": [day.isoformat() for day in target_dates],
        "new_counts": {str(k): len(v) for k, v in sorted(day_rows.items())},
        "old_counts": _counts_by_date(old_rows),
        "stale_to_delete": len(_stale_row_ids(day_rows, old_rows)),
    }
    (output_dir / f"{market}_backfill_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / f"{market}_backfill_new_rows.json").write_text(
        json.dumps({str(k): v for k, v in sorted(day_rows.items())}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / f"{market}_backfill_old_rows_backup.json").write_text(
        json.dumps(old_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[funnel-backfill] artifacts -> {output_dir}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _counts_by_date(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(_safe_int(row.get("recommend_date")))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _require_api_key() -> str:
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")
    return api_key


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _date_int(day: date) -> int:
    return int(day.strftime("%Y%m%d"))


__all__ = ["MarketBackfillRequest", "run_market_funnel_backfill"]
