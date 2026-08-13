"""Tushare-backed external capital context for A-share candidates."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from core.candidate_metadata import code6
from utils.safe import drop_empty, safe_float


def attach_tushare_capital_context(contexts: dict[str, dict[str, Any]], pro: Any, trade_date: str) -> None:
    ymd = _ymd(trade_date)
    _attach_lhb(contexts, pro, ymd)
    _attach_margin(contexts, pro, ymd)
    _attach_block_trade(contexts, pro, ymd)
    _attach_stock_moneyflow(contexts, pro, ymd)
    _attach_connect_market(contexts, pro, ymd)
    _attach_connect_top10(contexts, pro, ymd)


def _attach_lhb(contexts: dict[str, dict[str, Any]], pro: Any, ymd: str) -> None:
    try:
        summary = _records(pro.top_list(trade_date=ymd))
        seats = _records(pro.top_inst(trade_date=ymd))
    except Exception as exc:
        _status(contexts, "lhb", f"error:{_err(exc)}")
        return
    seat_map = _lhb_seat_map(seats)
    best: dict[str, dict[str, Any]] = {}
    for row in summary:
        code = code6(row.get("ts_code"))
        if code not in contexts:
            continue
        if safe_float(row.get("l_amount")) >= safe_float(best.get(code, {}).get("l_amount")):
            best[code] = row
    for code, row in best.items():
        contexts[code]["lhb"] = drop_empty(
            {
                "source": "tushare:top_list+top_inst",
                "name": row.get("name"),
                "reason": row.get("reason"),
                "net_buy": safe_float(row.get("net_amount")),
                "buy_amount": safe_float(row.get("l_buy")),
                "sell_amount": safe_float(row.get("l_sell")),
                "turnover": safe_float(row.get("l_amount")),
                "pct": safe_float(row.get("pct_change")),
                **seat_map.get(code, {}),
            }
        )
    _status(contexts, "lhb", f"ok rows={len(summary)} seats={len(seats)} matches={len(best)}")


def _lhb_seat_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        code = code6(row.get("ts_code"))
        if not code:
            continue
        name = str(row.get("exalter") or "")
        net = safe_float(row.get("net_buy"))
        bucket = result.setdefault(code, {})
        if "机构专用" in name:
            bucket["institution_net_buy"] = bucket.get("institution_net_buy", 0.0) + net
        if "沪股通专用" in name or "深股通专用" in name:
            bucket["connect_seat_net_buy"] = bucket.get("connect_seat_net_buy", 0.0) + net
    return result


def _attach_margin(contexts: dict[str, dict[str, Any]], pro: Any, ymd: str) -> None:
    rows, data_date, error = _latest_rows(pro.margin_detail, ymd)
    if error:
        _status(contexts, "margin", f"error:{error}")
        return
    matches = 0
    for row in rows:
        code = code6(row.get("ts_code"))
        if code not in contexts:
            continue
        contexts[code]["margin"] = drop_empty(
            {
                "source": "tushare:margin_detail",
                "data_date": data_date,
                "margin_balance": safe_float(row.get("rzye")),
                "margin_buy": safe_float(row.get("rzmre")),
                "margin_repay": safe_float(row.get("rzche")),
                "short_balance": safe_float(row.get("rqye")),
                "short_sell": safe_float(row.get("rqmcl")),
                "short_repay": safe_float(row.get("rqchl")),
            }
        )
        matches += 1
    _status(contexts, "margin", f"ok data_date={data_date} rows={len(rows)} matches={matches}")


def _latest_rows(fetch, ymd: str) -> tuple[list[dict[str, Any]], str, str]:
    day = date.fromisoformat(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
    last_error = ""
    for offset in range(6):
        target = (day - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            rows = _records(fetch(trade_date=target))
        except Exception as exc:
            last_error = _err(exc)
            continue
        if rows:
            return rows, target, ""
    return [], ymd, last_error


def _attach_block_trade(contexts: dict[str, dict[str, Any]], pro: Any, ymd: str) -> None:
    try:
        rows = _records(pro.block_trade(trade_date=ymd))
    except Exception as exc:
        _status(contexts, "block_trade", f"error:{_err(exc)}")
        return
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = code6(row.get("ts_code"))
        if code in contexts:
            grouped.setdefault(code, []).append(row)
    for code, trades in grouped.items():
        contexts[code]["block_trade"] = {
            "source": "tushare:block_trade",
            "trade_count": len(trades),
            "total_amount": round(sum(safe_float(row.get("amount")) for row in trades), 2),
            "top_trades": sorted(trades, key=lambda row: safe_float(row.get("amount")), reverse=True)[:3],
        }
    _status(contexts, "block_trade", f"ok rows={len(rows)} matches={sum(map(len, grouped.values()))}")


def _attach_stock_moneyflow(contexts: dict[str, dict[str, Any]], pro: Any, ymd: str) -> None:
    try:
        rows = _records(pro.moneyflow(trade_date=ymd))
    except Exception as exc:
        _status(contexts, "stock_moneyflow", f"error:{_err(exc)}")
        return
    matches = 0
    for row in rows:
        code = code6(row.get("ts_code"))
        if code not in contexts:
            continue
        contexts[code]["stock_moneyflow"] = {
            "source": "tushare:moneyflow",
            "net_amount_wan": safe_float(row.get("net_mf_amount")),
            "large_net_amount_wan": _net(row, "buy_lg_amount", "sell_lg_amount"),
            "extra_large_net_amount_wan": _net(row, "buy_elg_amount", "sell_elg_amount"),
        }
        matches += 1
    _status(contexts, "stock_moneyflow", f"ok rows={len(rows)} matches={matches}")


def _attach_connect_market(contexts: dict[str, dict[str, Any]], pro: Any, ymd: str) -> None:
    try:
        day = date.fromisoformat(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
        rows = _records(
            pro.moneyflow_hsgt(
                start_date=(day - timedelta(days=6)).strftime("%Y%m%d"),
                end_date=ymd,
            )
        )
    except Exception as exc:
        _status(contexts, "connect_market", f"error:{_err(exc)}")
        return
    if rows:
        row = max(rows, key=lambda item: str(item.get("trade_date") or ""))
        payload = {
            "source": "tushare:moneyflow_hsgt",
            "data_date": row.get("trade_date"),
            "published_north_amount_million": safe_float(row.get("north_money")),
            "sh_connect_amount_million": safe_float(row.get("hgt")),
            "sz_connect_amount_million": safe_float(row.get("sgt")),
            "semantic": "published_connect_amount_not_net_inflow",
        }
        for context in contexts.values():
            context["northbound_market"] = payload
    _status(contexts, "connect_market", f"ok rows={len(rows)}")


def _attach_connect_top10(contexts: dict[str, dict[str, Any]], pro: Any, ymd: str) -> None:
    try:
        rows = _records(pro.hsgt_top10(trade_date=ymd))
    except Exception as exc:
        _status(contexts, "connect_top10", f"error:{_err(exc)}")
        return
    matches = 0
    for row in rows:
        code = code6(row.get("ts_code"))
        if code not in contexts:
            continue
        contexts[code]["hsgt_top10"] = drop_empty(
            {
                "source": "tushare:hsgt_top10",
                "rank": row.get("rank"),
                "amount": safe_float(row.get("amount")),
                "net_amount": row.get("net_amount"),
                "buy": row.get("buy"),
                "sell": row.get("sell"),
            }
        )
        matches += 1
    _status(contexts, "connect_top10", f"ok rows={len(rows)} matches={matches}")


def _net(row: dict[str, Any], buy: str, sell: str) -> float:
    return round(safe_float(row.get(buy)) - safe_float(row.get(sell)), 2)


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or bool(getattr(frame, "empty", False)):
        return []
    return [dict(row) for row in frame.to_dict("records")]


def _status(contexts: dict[str, dict[str, Any]], source: str, value: str) -> None:
    for context in contexts.values():
        context.setdefault("source_status", {})[source] = value


def _ymd(value: str) -> str:
    return "".join(char for char in str(value) if char.isdigit())[:8]


def _err(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:160]
