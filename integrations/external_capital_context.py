"""On-demand A-share external capital context for signal review."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.candidate_metadata import code6 as _code
from utils.safe import drop_empty as _clean
from utils.safe import parse_cn_num as _num

CAPITAL_CONTEXT_VERSION = "external_capital_context_v2"
_CONTEXT_KEYS = (
    "lhb",
    "margin",
    "block_trade",
    "stock_moneyflow",
    "northbound_market",
    "hsgt_top10",
    "tick_large_order",
)


def _akshare_module(ak_module: Any | None) -> Any:
    if ak_module is not None:
        return ak_module
    import akshare as ak

    return ak


def _unique_codes(codes: list[str]) -> list[str]:
    out: list[str] = []
    for raw in codes or []:
        code = _code(raw)
        if code and code not in out:
            out.append(code)
    return out


def _yyyymmdd(trade_date: str) -> str:
    digits = "".join(ch for ch in str(trade_date or "") if ch.isdigit())
    return digits[:8]


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None or bool(getattr(df, "empty", False)):
        return []
    if hasattr(df, "to_dict"):
        return [dict(row) for row in df.to_dict("records")]
    return [dict(row) for row in df or []]


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row.get(name)
    return None


def _status(contexts: dict[str, dict[str, Any]], source: str, value: str) -> None:
    for ctx in contexts.values():
        ctx.setdefault("source_status", {})[source] = value


def _err(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:160]


def _new_contexts(codes: list[str], trade_date: str) -> dict[str, dict[str, Any]]:
    fetched_at = datetime.now(UTC).isoformat()
    return {
        code: {
            "version": CAPITAL_CONTEXT_VERSION,
            "trade_date": trade_date,
            "fetched_at": fetched_at,
            "source_status": {},
        }
        for code in codes
    }


def _attach_lhb(contexts: dict[str, dict[str, Any]], ak: Any, ymd: str) -> None:
    rows = _records(ak.stock_lhb_detail_em(start_date=ymd, end_date=ymd))
    matches = 0
    for row in rows:
        code = _code(_first(row, "代码", "股票代码", "证券代码"))
        if code not in contexts:
            continue
        contexts[code]["lhb"] = _clean(
            {
                "source": "akshare:eastmoney_lhb",
                "name": _first(row, "名称", "股票简称", "证券简称"),
                "reason": _first(row, "解读", "上榜原因"),
                "net_buy": _num(_first(row, "龙虎榜净买额")),
                "buy_amount": _num(_first(row, "龙虎榜买入额")),
                "sell_amount": _num(_first(row, "龙虎榜卖出额")),
                "turnover": _num(_first(row, "龙虎榜成交额")),
                "pct": _num(_first(row, "涨跌幅")),
            }
        )
        matches += 1
    _status(contexts, "lhb", f"ok rows={len(rows)} matches={matches}")


def _attach_margin_market(contexts: dict[str, dict[str, Any]], ak: Any, ymd: str, market: str, func_name: str) -> None:
    try:
        rows = _records(getattr(ak, func_name)(date=ymd))
    except Exception as exc:
        _status(contexts, f"margin_{market}", f"error:{_err(exc)}")
        return
    matches = 0
    for row in rows:
        code = _code(_first(row, "标的证券代码", "证券代码", "代码"))
        if code not in contexts:
            continue
        contexts[code]["margin"] = _clean(
            {
                "source": f"akshare:{market}_margin",
                "name": _first(row, "标的证券简称", "证券简称", "名称"),
                "margin_balance": _num(_first(row, "融资余额")),
                "margin_buy": _num(_first(row, "融资买入额")),
                "margin_repay": _num(_first(row, "融资偿还额")),
                "short_balance": _num(_first(row, "融券余量", "融券余额")),
                "short_sell": _num(_first(row, "融券卖出量")),
                "short_repay": _num(_first(row, "融券偿还量")),
            }
        )
        matches += 1
    _status(contexts, f"margin_{market}", f"ok rows={len(rows)} matches={matches}")


def _attach_margin(contexts: dict[str, dict[str, Any]], ak: Any, ymd: str) -> None:
    _attach_margin_market(contexts, ak, ymd, "sse", "stock_margin_detail_sse")
    _attach_margin_market(contexts, ak, ymd, "szse", "stock_margin_detail_szse")


def _block_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    amounts = [_num(trade.get("amount")) or 0.0 for trade in trades]
    discounts = [_num(trade.get("discount_pct")) for trade in trades]
    discounts = [value for value in discounts if value is not None]
    ranked = sorted(trades, key=lambda trade: _num(trade.get("amount")) or 0.0, reverse=True)
    return _clean(
        {
            "source": "akshare:eastmoney_block_trade",
            "trade_count": len(trades),
            "total_amount": round(sum(amounts), 2),
            "avg_discount_pct": round(sum(discounts) / len(discounts), 2) if discounts else None,
            "top_trades": ranked[:3],
        }
    )


def _attach_block_trade(contexts: dict[str, dict[str, Any]], ak: Any, ymd: str) -> None:
    rows = _records(ak.stock_dzjy_mrmx(symbol="A股", start_date=ymd, end_date=ymd))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = _code(_first(row, "证券代码", "股票代码", "代码"))
        if code not in contexts:
            continue
        grouped.setdefault(code, []).append(
            _clean(
                {
                    "price": _num(_first(row, "成交价")),
                    "discount_pct": _num(_first(row, "折溢率")),
                    "volume": _num(_first(row, "成交量")),
                    "amount": _num(_first(row, "成交额")),
                    "buyer": _first(row, "买方营业部"),
                    "seller": _first(row, "卖方营业部"),
                }
            )
        )
    for code, trades in grouped.items():
        contexts[code]["block_trade"] = _block_summary(trades)
    _status(contexts, "block_trade", f"ok rows={len(rows)} matches={sum(len(v) for v in grouped.values())}")


def _tick_symbol(code: str) -> str:
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return ""


def _side(raw: Any) -> str:
    text = str(raw or "").strip()
    if "买" in text:
        return "buy"
    if "卖" in text:
        return "sell"
    return "neutral"


def _tick_payload(rows: list[dict[str, Any]], min_amount_yuan: float) -> dict[str, Any]:
    totals = {"buy": 0.0, "sell": 0.0, "neutral": 0.0}
    events: list[dict[str, Any]] = []
    for row in rows:
        amount = _num(_first(row, "成交金额", "成交额"))
        if amount is None or amount < min_amount_yuan:
            continue
        side = _side(_first(row, "性质", "大单性质"))
        totals[side] += amount
        events.append(
            _clean(
                {
                    "time": _first(row, "成交时间", "时间"),
                    "price": _num(_first(row, "成交价格", "成交价")),
                    "amount_yuan": round(amount, 2),
                    "side": side,
                }
            )
        )
    return _clean(
        {
            "source": "akshare:tencent_tick",
            "large_trade_count": len(events),
            "large_buy_amount_yuan": round(totals["buy"], 2),
            "large_sell_amount_yuan": round(totals["sell"], 2),
            "large_net_amount_yuan": round(totals["buy"] - totals["sell"], 2),
            "top_trades": sorted(events, key=lambda item: item.get("amount_yuan", 0), reverse=True)[:5],
        }
    )


def _attach_tick_large_order(
    contexts: dict[str, dict[str, Any]],
    ak: Any,
    codes: list[str],
    min_amount_yuan: float,
) -> None:
    for code in codes:
        symbol = _tick_symbol(code)
        if not symbol:
            contexts[code].setdefault("source_status", {})["tick_large_order"] = "skipped_unsupported_market"
            continue
        try:
            payload = _tick_payload(_records(ak.stock_zh_a_tick_tx_js(symbol=symbol)), min_amount_yuan)
        except Exception as exc:
            contexts[code].setdefault("source_status", {})["tick_large_order"] = f"error:{_err(exc)}"
            continue
        contexts[code].setdefault("source_status", {})["tick_large_order"] = "ok"
        if payload.get("large_trade_count"):
            contexts[code]["tick_large_order"] = payload


def _safe_attach(contexts: dict[str, dict[str, Any]], source: str, attach_fn) -> None:
    try:
        attach_fn()
    except Exception as exc:
        _status(contexts, source, f"error:{_err(exc)}")


def _has_context(ctx: dict[str, Any]) -> bool:
    return any(ctx.get(key) for key in _CONTEXT_KEYS)


def build_external_capital_context(
    codes: list[str],
    trade_date: str,
    *,
    include_tick: bool = False,
    tick_max_symbols: int = 0,
    tick_min_amount_yuan: float = 1_000_000.0,
    ak_module: Any | None = None,
) -> dict[str, dict[str, Any]]:
    code_order = _unique_codes(codes)
    if not code_order:
        return {}
    ak = _akshare_module(ak_module)
    ymd = _yyyymmdd(trade_date)
    contexts = _new_contexts(code_order, trade_date)
    if ak_module is None and _attach_tushare(contexts, trade_date):
        _fallback_failed_tushare_sources(contexts, ak, ymd)
    else:
        _safe_attach(contexts, "lhb", lambda: _attach_lhb(contexts, ak, ymd))
        _attach_margin(contexts, ak, ymd)
        _safe_attach(contexts, "block_trade", lambda: _attach_block_trade(contexts, ak, ymd))
    if include_tick and tick_max_symbols > 0:
        tick_codes = code_order[: max(int(tick_max_symbols), 1)]
        _attach_tick_large_order(contexts, ak, tick_codes, max(float(tick_min_amount_yuan), 0.0))
    return {code: ctx for code, ctx in contexts.items() if _has_context(ctx)}


def _attach_tushare(contexts: dict[str, dict[str, Any]], trade_date: str) -> bool:
    try:
        from integrations.tushare_capital_context import attach_tushare_capital_context
        from integrations.tushare_client import get_pro

        pro = get_pro()
        if pro is None:
            return False
        attach_tushare_capital_context(contexts, pro, trade_date)
        return True
    except Exception as exc:
        _status(contexts, "tushare", f"error:{_err(exc)}")
        return False


def _fallback_failed_tushare_sources(contexts: dict[str, dict[str, Any]], ak: Any, ymd: str) -> None:
    status = next(iter(contexts.values())).get("source_status", {}) if contexts else {}
    if str(status.get("lhb", "")).startswith("error:"):
        _safe_attach(contexts, "lhb", lambda: _attach_lhb(contexts, ak, ymd))
    if str(status.get("margin", "")).startswith("error:"):
        _attach_margin(contexts, ak, ymd)
    if str(status.get("block_trade", "")).startswith("error:"):
        _safe_attach(contexts, "block_trade", lambda: _attach_block_trade(contexts, ak, ymd))
