from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

from agents.stock_data_helpers import (
    code_to_name,
    collect_tickflow_limit_hints_from_df,
    hist_metadata,
    latest_hist_date,
)
from agents.tool_context import (
    ToolContext,
    ensure_tushare_token,
    get_user_client,
    get_user_id,
    has_cloud,
    is_auth_failure_result,
    with_auth_retry,
)
from core.buy_dt import POSITION_EXISTS_ERROR, POSITION_MISSING_ERROR, buy_dt_error

logger = logging.getLogger(__name__)


def portfolio(mode: str = "view", tool_context: ToolContext | None = None) -> dict:
    try:
        portfolio_id = _portfolio_id(tool_context)
        state = _load_portfolio_state(portfolio_id, tool_context)
        if state is None:
            return {"message": "未找到持仓记录，可通过 update_portfolio 添加", "positions": [], "free_cash": 0}
        normalized_mode = (mode or "view").strip().lower()
        if normalized_mode not in ("view", "diagnose"):
            return {"error": f"mode 参数无效: '{mode}'，可选值: view, diagnose"}
        if normalized_mode == "view":
            return _portfolio_view(portfolio_id, state)
        return _portfolio_diagnosis(portfolio_id, state, tool_context)
    except Exception as e:
        logger.exception("portfolio error")
        return {"error": str(e)}


def update_portfolio(
    action: str,
    code: str = "",
    name: str = "",
    shares: int = 0,
    cost_price: float = 0,
    buy_dt: str = "",
    free_cash: float = 0,
    table: str = "",
    codes: list[str] | None = None,
    items: list[dict[str, Any]] | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    try:
        normalized_action = str(action or "").strip().lower()
        if normalized_action == "delete_records":
            return _delete_tracking_records(table, codes)
        portfolio_id = _portfolio_id(tool_context)
        cloud = has_cloud(tool_context)
        if items is not None:
            return _update_portfolio_batch(normalized_action, portfolio_id, items, free_cash, cloud, tool_context)
        msg = _apply_portfolio_action(
            normalized_action, portfolio_id, code, name, shares, cost_price, buy_dt, free_cash, cloud, tool_context
        )
        if isinstance(msg, dict):
            return msg
        if cloud:
            _sync_remote_portfolio_to_local(portfolio_id, tool_context)
        return _local_update_summary(portfolio_id, msg, cloud)
    except Exception as e:
        logger.exception("update_portfolio error")
        return {"error": str(e)}


_BATCH_ACTIONS = frozenset({"add", "update", "remove"})
_BATCH_MAX_ITEMS = 30
_STOPS_MAX_ITEMS = 200


def set_stop_loss(
    code: str = "",
    stop_loss: float | None = 0,
    items: list[dict[str, Any]] | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    """只设置持仓止损价，不能改股数、成本或现金。

    安全性来自这个工具能做的事很窄，而不是来自调用方检查了参数。

    传 stop_loss=None 表示清除该持仓的止损；0 或负数仍视为无效价格而报错。
    """
    try:
        rows, error = _normalize_stop_rows(code, stop_loss, items)
        if error:
            return error

        portfolio_id = _portfolio_id(tool_context)
        cloud = has_cloud(tool_context)
        if cloud:
            from integrations.supabase_portfolio import set_position_stops

            ok, _written = with_auth_retry(
                tool_context,
                set_position_stops,
                portfolio_id,
                rows,
                client=get_user_client(tool_context),
            )
            if not ok:
                return {"error": "云端止损写入失败"}

        from integrations.local_db import set_local_position_stop

        updated = 0
        missing: list[str] = []
        for row in rows:
            if set_local_position_stop(portfolio_id, row["code"], row["stop_loss"]):
                updated += 1
            else:
                missing.append(row["code"])
        result: dict[str, Any] = {"updated_count": updated, "cloud": cloud}
        if missing:
            result["not_in_portfolio"] = missing
        return result
    except Exception as e:
        logger.exception("set_stop_loss error")
        return {"error": str(e)}


def _normalize_stop_rows(
    code: str,
    stop_loss: float,
    items: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], dict | None]:
    from core.portfolio_symbol import normalize_portfolio_code

    raw = items if items is not None else [{"code": code, "stop_loss": stop_loss}]
    if not isinstance(raw, list) or not raw:
        return [], {"error": "items 必须是非空数组"}
    if len(raw) > _STOPS_MAX_ITEMS:
        return [], {"error": f"单次最多 {_STOPS_MAX_ITEMS} 条，请拆分后重试"}

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], {"error": f"第 {index} 项必须是对象"}
        normalized = normalize_portfolio_code(str(item.get("code") or ""))
        if not normalized:
            return [], {"error": f"第 {index} 项股票代码无效: {item.get('code')}"}
        # 显式的 None 表示「清除止损」——存储层一直支持 null，只是这里以前
        # 一律 float() 把它堵成了「无效」，于是止损填错了没法单独去掉。
        # 注意 0 和负数仍然是错误：那不是「清除」，是无效价格。
        raw_stop = item.get("stop_loss")
        if raw_stop is None:
            rows.append({"code": normalized, "stop_loss": None})
            continue
        try:
            price = float(raw_stop)
        except (TypeError, ValueError):
            return [], {"error": f"{normalized} 的 stop_loss 无效"}
        if price <= 0:
            return [], {"error": f"{normalized} 的 stop_loss 必须大于 0"}
        rows.append({"code": normalized, "stop_loss": price})
    return rows, None


def _update_portfolio_batch(
    action: str,
    portfolio_id: str,
    items: list[dict[str, Any]],
    free_cash: float,
    cloud: bool,
    tool_context: ToolContext | None,
) -> dict:
    """一次工具调用改多只：减少 Agent 多轮 LLM，利于 prompt cache。"""
    if action == "set_cash":
        return {"error": "批量 items 不支持 set_cash，请单独调用"}
    if action not in _BATCH_ACTIONS:
        return {"error": f"批量 items 仅支持 add/update/remove，收到: {action}"}
    if not isinstance(items, list) or not items:
        return {"error": "items 必须是非空数组"}
    if len(items) > _BATCH_MAX_ITEMS:
        return {"error": f"单次最多 {_BATCH_MAX_ITEMS} 条，请拆分后重试"}

    ok_messages: list[str] = []
    failures: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        row = _batch_item_row(index, raw, failures, action=action)
        if row is None:
            continue
        msg = _apply_portfolio_action(
            action,
            portfolio_id,
            row["code"],
            row["name"],
            row["shares"],
            row["cost_price"],
            row["buy_dt"],
            free_cash,
            cloud,
            tool_context,
            refresh_equity=False,
        )
        if isinstance(msg, dict) and msg.get("error"):
            failures.append({"index": index, "code": row["code"], "error": msg["error"]})
        else:
            ok_messages.append(str(msg))

    if not ok_messages:
        first = failures[0]["error"] if failures else "批量操作失败"
        return {"error": first, "failures": failures, "updated_count": 0, "failed_count": len(failures)}
    if cloud:
        valuation_message = _refresh_cloud_equity(portfolio_id, tool_context)
        _sync_remote_portfolio_to_local(portfolio_id, tool_context)
    summary = _local_update_summary(portfolio_id, f"批量{action}成功 {len(ok_messages)} 只", cloud)
    summary["updated_count"] = len(ok_messages)
    summary["failed_count"] = len(failures)
    summary["item_messages"] = ok_messages
    if cloud:
        summary["valuation_message"] = valuation_message
    if failures:
        summary["failures"] = failures
    return summary


def _batch_item_row(
    index: int, raw: Any, failures: list[dict[str, Any]], *, action: str = "add"
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        failures.append({"index": index, "error": "item 必须是对象"})
        return None
    # remove 只按 code 清仓，股数与成本无意义；只有 add/update 才要求显式股数，
    # 否则省略会被当成 0 静默清零仓位。
    if action == "remove":
        return _batch_row(raw, shares=0, cost_price=0.0)
    if "shares" not in raw or "cost_price" not in raw:
        failures.append({"index": index, "code": raw.get("code"), "error": "shares/cost_price 不能省略"})
        return None
    try:
        shares = int(raw["shares"])
        cost_price = float(raw["cost_price"])
    except (TypeError, ValueError):
        failures.append({"index": index, "code": raw.get("code"), "error": "shares/cost_price 无效"})
        return None
    return _batch_row(raw, shares=shares, cost_price=cost_price)


def _batch_row(raw: dict[str, Any], *, shares: int, cost_price: float) -> dict[str, Any]:
    return {
        "code": str(raw.get("code") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "shares": shares,
        "cost_price": cost_price,
        "buy_dt": str(raw.get("buy_dt") or "").strip(),
    }


def record_trade_fill(
    code: str,
    side: str,
    shares: int,
    price: float,
    trade_date: str = "",
    name: str = "",
    tool_context: ToolContext | None = None,
) -> dict:
    """把一笔真实成交回填进持仓与现金。

    与 update_portfolio 的区别：那个是覆盖式录入快照，这个是按成交增量算账，会自动
    摊薄成本价、扣减佣金印花税、卖光时清仓，并给出已实现盈亏。
    """
    from datetime import datetime

    from core.portfolio_symbol import normalize_portfolio_code
    from core.trade_fill import Fill
    from utils.trading_clock import CN_TZ

    try:
        normalized = normalize_portfolio_code(str(code))
        if not normalized:
            return {"error": "无效的股票代码：A股用6位数字，港股用00700.HK，美股用AAPL.US"}
        fill = Fill(
            code=normalized,
            side=str(side or "").strip().lower(),
            shares=int(shares),
            price=float(price),
            trade_date=str(trade_date or "").strip() or datetime.now(CN_TZ).strftime("%Y%m%d"),
            name=str(name or "").strip() or code_to_name(normalized),
        )
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}

    portfolio_id = _portfolio_id(tool_context)
    if not has_cloud(tool_context):
        return {"error": "成交回填需要登录云端账户"}
    result = _record_fill_with_safe_auth_retry(portfolio_id, fill, tool_context)
    if not result.ok:
        return {"error": result.message}
    _sync_remote_portfolio_to_local(portfolio_id, tool_context)
    return {"message": result.message}


def _record_fill_with_safe_auth_retry(portfolio_id: str, fill: Any, tool_context: ToolContext | None):
    """Auth-retry only when the fill did not already mutate positions.

    `record_fill` writes position then cash. If cash fails with a JWT-looking error,
    blanket `with_auth_retry` would replay the buy/sell against the already-updated
    holding and corrupt shares/cash.
    """
    from integrations.supabase_portfolio import FillWriteResult, record_fill

    def _call(*, client):
        return record_fill(portfolio_id, fill, client=client)

    result = _call(client=get_user_client(tool_context))
    if not isinstance(result, FillWriteResult):
        return FillWriteResult(False, "成交回填失败")
    if result.ok or result.position_committed or not is_auth_failure_result(result):
        return result
    retried = with_auth_retry(tool_context, _call, client=get_user_client(tool_context))
    return retried if isinstance(retried, FillWriteResult) else result


def _portfolio_id(tool_context: ToolContext | None) -> str:
    from integrations.supabase_portfolio import build_user_live_portfolio_id

    return build_user_live_portfolio_id(get_user_id(tool_context))


def _load_portfolio_state(portfolio_id: str, tool_context: ToolContext | None) -> dict | None:
    """读持仓：云端优先，云端不行就用本地库。

    云端那段**必须**包在 try 里。原来没包，于是 `get_user_client()` 的网络异常
    （实测是 TLS 握手超时）直接穿透出去，下面写好的本地兜底根本没机会执行 ——
    界面收到一个装着 error 的对象，显示成「暂无持仓数据」，而本地库里 8 只
    持仓一直都在，7ms 就能读出来。

    「云端挂了」和「你没有持仓」是两件完全不同的事，不能长成一样。
    """
    state = None
    cloud_error = ""
    if has_cloud(tool_context):
        try:
            from integrations.supabase_portfolio import load_portfolio_state

            client = get_user_client(tool_context)
            state = with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=client)
            if state:
                _cache_portfolio(portfolio_id, state, "remote")
        except Exception as exc:  # noqa: BLE001 —— 云端任何失败都该落到本地库
            cloud_error = str(exc) or exc.__class__.__name__
            logger.warning(
                "cloud portfolio read failed for %s, falling back to local DB: %s",
                portfolio_id,
                cloud_error,
            )
    if state is not None:
        return state
    try:
        from integrations.local_db import load_portfolio

        local = load_portfolio(portfolio_id)
    except Exception:
        logger.warning("failed to load portfolio %s from local DB", portfolio_id, exc_info=True)
        return None
    # 标注数据来源：界面要能说清「这是本地数据，云端没连上」。不标的话用户会
    # 以为看到的是最新的云端持仓，而它可能落后于另一台设备上的改动。
    if local is not None and cloud_error:
        local = {**local, "source": "local", "cloud_error": cloud_error}
    return local


def _cache_portfolio(portfolio_id: str, state: dict, source: str) -> None:
    try:
        from integrations.local_db import save_portfolio

        # 顺手把云端算好的总估值存进本地。
        #
        # 估值原来只存在云端,所以「云端连不上就用本地库」那条兜底虽然能给出
        # 持仓,总资产却必然是「—/未估值」。云端每次读成功都把估值缓存一份,
        # 断网时就还有个带时间戳的旧估值可看。
        #
        # 拿不到就传 None —— save_portfolio 会保留库里已有的那份,而不是覆盖成 NULL。
        equity = state.get("total_equity")
        save_portfolio(
            portfolio_id,
            float(state.get("free_cash", 0) or 0),
            [_local_position(p) for p in state.get("positions", [])],
            total_equity=float(equity) if equity is not None else None,
        )
    except Exception:
        logger.warning("failed to cache %s portfolio %s locally", source, portfolio_id, exc_info=True)


def _local_position(position: dict) -> dict:
    return {
        "code": position.get("code", ""),
        "name": position.get("name", ""),
        "shares": position.get("shares", 0),
        "cost_price": position.get("cost", position.get("cost_price", 0)),
        "buy_dt": position.get("buy_dt", ""),
        "stop_loss": position.get("stop_loss"),
    }


def _portfolio_view(portfolio_id: str, state: dict) -> dict:
    positions = [
        {
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "shares": p.get("shares", 0),
            "cost_price": p.get("cost", p.get("cost_price", 0)),
            "buy_dt": p.get("buy_dt", ""),
            # stop_loss 是存储列，view 里漏掉会让模型把每一只都报成「未设止损」
            # —— 这是个风控数字，不能靠字段缺失去推断。
            "stop_loss": p.get("stop_loss"),
        }
        for p in state.get("positions", [])
    ]
    result = {
        "portfolio_id": portfolio_id,
        "free_cash": state.get("free_cash", 0),
        "position_count": len(positions),
        "positions": positions,
    }
    if state.get("total_equity") is not None:
        result["total_equity"] = state["total_equity"]
        # 云端那份的时间在 updated_at，本地缓存那份在 valued_at（单独一列，
        # 因为估值可能比持仓行更旧）。两者都要认，否则本地兜底给出的估值
        # 会显示成「没有时间」—— 一个不标时间的旧估值比不显示更容易误导。
        result["valuation_updated_at"] = state.get("updated_at") or state.get("valued_at") or ""
    return result


def _portfolio_diagnosis(portfolio_id: str, state: dict, tool_context: ToolContext | None) -> dict:
    ensure_tushare_token(tool_context)
    if not state.get("positions"):
        return {
            "message": "持仓记录存在但无头寸",
            "portfolio_id": portfolio_id,
            "free_cash": state.get("free_cash", 0),
            "positions": [],
        }
    start_date = date.today() - timedelta(days=500)
    end_date = date.today()
    results, hints, success, failed = [], [], 0, 0
    for position in state["positions"]:
        diagnostic = _diagnose_position(position, start_date, end_date, hints)
        results.append(diagnostic)
        if diagnostic.get("error"):
            failed += 1
        else:
            success += 1
    free_cash = float(state.get("free_cash", 0) or 0)
    total_market_value = _fill_position_weights(results)
    stored_total = state.get("total_equity")
    computed_total = total_market_value + free_cash
    out = {
        "portfolio_id": portfolio_id,
        "free_cash": state.get("free_cash", 0),
        "position_count": len(state["positions"]),
        "successful_count": success,
        "failed_count": failed,
        "total_market_value": round(total_market_value, 2),
        "total_assets": round(float(stored_total) if stored_total is not None else computed_total, 2),
        "diagnostics": results,
    }
    if stored_total is not None:
        out["total_equity"] = stored_total
        out["valuation_updated_at"] = state.get("updated_at", "")
    if failed:
        out["market_value_note"] = f"{failed} 只持仓行情缺失，总市值与仓位占比仅覆盖成功诊断的部分"
    if hints:
        out["tickflow_limit_hint"] = hints[0]
    return out


def _fill_position_weights(results: list[dict]) -> float:
    """按市值回填 weight_pct，返回可计算的总市值（行情缺失的持仓不计入）。"""
    total = sum(float(item.get("market_value") or 0) for item in results)
    if total > 0:
        for item in results:
            market_value = float(item.get("market_value") or 0)
            if market_value > 0:
                item["weight_pct"] = round(market_value / total * 100.0, 2)
    return total


_EXTREME_DAY_CHANGE_PCT = -5.0  # 与 core.holding_diagnostic 的阈值保持一致，避免无谓拉取分时线


def _diagnose_position(position: dict, start_date: date, end_date: date, hints: list[str]) -> dict:
    from core.holding_diagnostic import diagnose_one_stock
    from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

    code = position.get("code", "") or position.get("code", "")
    name = position.get("name", code)
    cost = float(position.get("cost", position.get("cost_price", 0)) or 0)
    shares = _position_shares(position)
    try:
        df = get_stock_hist(code, start_date, end_date)
        if df is None or df.empty:
            return {"code": code, "name": name, "shares": shares, "error": "无行情数据"}
        metadata = hist_metadata(df)
        _append_unique_hints(hints, collect_tickflow_limit_hints_from_df(df))
        normalized_df = normalize_hist_df(df)
        latest_date = latest_hist_date(df, "日期") or latest_hist_date(normalized_df)
        intraday_df = _fetch_intraday_if_extreme_day(code, normalized_df)
        diagnostic = diagnose_one_stock(
            code,
            name,
            cost,
            normalized_df,
            intraday_df=intraday_df,
            buy_dt=str(position.get("buy_dt", "") or "").strip(),
        )
        return _diagnostic_payload(diagnostic, latest_date, metadata, shares)
    except Exception as e:
        return {"code": code, "name": name, "shares": shares, "error": str(e)}


def _position_shares(position: dict) -> int:
    try:
        return int(float(position.get("shares", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _fetch_intraday_if_extreme_day(code: str, normalized_df) -> Any:
    """仅当日跌幅显著时才按需拉取当日分钟线，用于洗盘/出货识别，避免无谓 API 消耗。"""
    close = normalized_df.get("close") if normalized_df is not None else None
    if close is None or len(close) < 2:
        return None
    try:
        prev_close = float(close.iloc[-2])
        latest_close = float(close.iloc[-1])
    except (TypeError, ValueError, IndexError):
        return None
    if prev_close <= 0 or (latest_close / prev_close - 1.0) * 100.0 > _EXTREME_DAY_CHANGE_PCT:
        return None

    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from integrations.tickflow_client import TickFlowClient

        return TickFlowClient(api_key=api_key).get_intraday(code, period="1m", count=500)
    except Exception:
        logger.warning("failed to fetch intraday data for extreme-day diagnosis: %s", code, exc_info=True)
        return None


def _append_unique_hints(target: list[str], hints: list[str]) -> None:
    for hint in hints:
        if hint not in target:
            target.append(hint)


def _diagnostic_payload(diagnostic, latest_date: str, metadata: dict, shares: int = 0) -> dict:
    from agents.diagnosis_tools import diagnosis_brief_from_diagnostic
    from core.holding_diagnostic import format_diagnostic_text

    payload = {
        "code": diagnostic.code,
        "name": diagnostic.name,
        "shares": shares,
        "cost": round(diagnostic.cost, 3),
        "market_value": round(shares * diagnostic.latest_close, 2) if shares else 0,
        "health": diagnostic.health,
        "pnl_pct": round(diagnostic.pnl_pct, 2),
        "pnl_amount": round(shares * (diagnostic.latest_close - diagnostic.cost), 2) if shares else 0,
        "latest_close": diagnostic.latest_close,
        "ma_pattern": diagnostic.ma_pattern,
        "l2_channel": diagnostic.l2_channel,
        "track": diagnostic.track,
        "l4_triggers": diagnostic.l4_triggers,
        "candidate_lane": diagnostic.candidate_lane,
        "candidate_entry_type": diagnostic.candidate_entry_type,
        "candidate_score": diagnostic.candidate_score,
        "exit_signal": diagnostic.exit_signal,
        "health_reasons": diagnostic.health_reasons,
        "diagnosis_brief": diagnosis_brief_from_diagnostic(diagnostic),
        "formatted_text": format_diagnostic_text(diagnostic),
        "data_status": "ok",
        "latest_date": latest_date,
        **metadata,
    }
    if diagnostic.intraday_path_desc:
        payload["day_change_pct"] = round(diagnostic.day_change_pct, 2)
        payload["limit_move_desc"] = diagnostic.limit_move_desc
        payload["intraday_path"] = diagnostic.intraday_path
        payload["intraday_path_desc"] = diagnostic.intraday_path_desc
    return payload


def _delete_tracking_records(table: str, codes: list[str] | None) -> dict:
    if not codes:
        return {"error": "请指定要删除的股票代码 codes"}
    clean_codes = [str(code).strip() for code in codes if str(code).strip()]
    if table == "recommendation":
        from integrations.local_db import delete_recommendations

        return {
            "deleted": delete_recommendations(clean_codes),
            "table": "recommendation_tracking",
            "codes": clean_codes,
        }
    if table == "signal":
        from integrations.local_db import delete_signals

        return {"deleted": delete_signals(clean_codes), "table": "signal_pending", "codes": clean_codes}
    return {"error": f"不支持的表：{table}，请用 'recommendation' 或 'signal'"}


def _apply_portfolio_action(
    action: str,
    portfolio_id: str,
    code: str,
    name: str,
    shares: int,
    cost_price: float,
    buy_dt: str,
    free_cash: float,
    cloud: bool,
    tool_context: ToolContext | None,
    *,
    refresh_equity: bool = True,
) -> str | dict:
    if action in ("add", "update"):
        return _write_position(
            action,
            portfolio_id,
            code,
            name,
            shares,
            cost_price,
            buy_dt,
            cloud,
            tool_context,
            refresh_equity=refresh_equity,
        )
    if action == "remove":
        return _remove_position(portfolio_id, code, cloud, tool_context, refresh_equity=refresh_equity)
    if action == "set_cash":
        return _set_cash(portfolio_id, free_cash, cloud, tool_context, refresh_equity=refresh_equity)
    return {"error": f"未知操作: {action}，支持 add/update/remove/set_cash/delete_records"}


def _validate_position_amounts(shares: int, cost_price: float) -> dict | None:
    """只在 add/update 写入路径调用；remove 走 _remove_position，不经过这里。"""
    if float(shares) <= 0:
        return {"error": "shares 必须大于 0"}
    if float(cost_price) <= 0:
        return {"error": "cost_price 必须大于 0"}
    return None


def _write_position(
    action: str,
    portfolio_id: str,
    code: str,
    name: str,
    shares: int,
    cost_price: float,
    buy_dt: str,
    cloud: bool,
    tool_context: ToolContext | None,
    *,
    refresh_equity: bool = True,
) -> str | dict:
    prepared = _prepare_position_write(action, code, name, shares, cost_price, buy_dt)
    if isinstance(prepared, dict):
        return prepared
    code, name, shares, cost_price, buy_dt = prepared
    payload = {"code": code, "name": name, "shares": shares, "cost_price": cost_price, "buy_dt": buy_dt}
    if cloud:
        return _write_cloud_position(action, portfolio_id, payload, tool_context, refresh_equity=refresh_equity)
    return _write_local_position(action, portfolio_id, payload)


def _prepare_position_write(
    action: str, code: str, name: str, shares: int, cost_price: float, buy_dt: str
) -> tuple[str, str, int, float, str] | dict:
    from core.portfolio_symbol import normalize_portfolio_code, portfolio_name_conflict

    if not code:
        return {"error": "add/update 操作需要提供股票代码 code"}
    amount_error = _validate_position_amounts(shares, cost_price)
    if amount_error:
        return amount_error
    code = normalize_portfolio_code(code)
    if not code:
        return {"error": "无效的股票代码：A股用6位数字，港股用00700.HK，美股用AAPL.US"}
    date_error = buy_dt_error(buy_dt, required=action == "add")
    if date_error:
        return {"error": date_error}
    resolved_name = code_to_name(code)
    conflict = portfolio_name_conflict(code, name, resolved_name)
    if conflict:
        return {"error": conflict}
    name = name or (resolved_name if resolved_name != code else "")
    return code, name, shares, cost_price, str(buy_dt or "").strip()


def _write_cloud_position(
    action: str,
    portfolio_id: str,
    payload: dict,
    tool_context: ToolContext | None,
    *,
    refresh_equity: bool,
) -> str | dict:
    from integrations.local_db import insert_local_position, update_local_position
    from integrations.supabase_portfolio import insert_position, update_position

    writer = insert_position if action == "add" else update_position
    ok, msg = with_auth_retry(
        tool_context,
        writer,
        portfolio_id,
        payload,
        client=get_user_client(tool_context),
        refresh_equity=refresh_equity,
    )
    if not ok:
        return {"error": msg}
    args = (
        portfolio_id,
        payload["code"],
        payload["name"],
        payload["shares"],
        payload["cost_price"],
        payload["buy_dt"],
    )
    if action == "add":
        insert_local_position(*args)
    else:
        update_local_position(*args)
    return f"{payload['code']} 已更新"


def _write_local_position(action: str, portfolio_id: str, payload: dict) -> str | dict:
    from integrations.local_db import insert_local_position, update_local_position

    code = payload["code"]
    args = (portfolio_id, code, payload["name"], payload["shares"], payload["cost_price"], payload["buy_dt"])
    if action == "add":
        if not insert_local_position(*args):
            return {"error": POSITION_EXISTS_ERROR}
        return f"{code} 已更新"
    if not update_local_position(*args):
        return {"error": POSITION_MISSING_ERROR}
    return f"{code} 已更新"


def _remove_position(
    portfolio_id: str,
    code: str,
    cloud: bool,
    tool_context: ToolContext | None,
    *,
    refresh_equity: bool = True,
) -> str | dict:
    from core.portfolio_symbol import normalize_portfolio_code

    if not code:
        return {"error": "remove 操作需要提供股票代码 code"}
    code = normalize_portfolio_code(code) or str(code).strip().upper()
    if cloud:
        from integrations.supabase_portfolio import delete_position

        ok, msg = with_auth_retry(
            tool_context,
            delete_position,
            portfolio_id,
            code,
            client=get_user_client(tool_context),
            refresh_equity=refresh_equity,
        )
        if not ok:
            return {"error": msg}
    from integrations.local_db import delete_local_position

    delete_local_position(portfolio_id, code)
    return f"{code} 已删除"


def _set_cash(
    portfolio_id: str,
    free_cash: float,
    cloud: bool,
    tool_context: ToolContext | None,
    *,
    refresh_equity: bool = True,
) -> str | dict:
    if float(free_cash) < 0:
        return {"error": "free_cash 不能为负数"}
    if cloud:
        from integrations.supabase_portfolio import update_free_cash

        ok, msg = with_auth_retry(
            tool_context,
            update_free_cash,
            portfolio_id,
            free_cash,
            client=get_user_client(tool_context),
            refresh_equity=refresh_equity,
        )
        if not ok:
            return {"error": msg}
    from integrations.local_db import update_local_free_cash

    update_local_free_cash(portfolio_id, free_cash)
    return f"可用资金已更新为 {free_cash:,.2f}"


def _refresh_cloud_equity(portfolio_id: str, tool_context: ToolContext | None) -> str:
    from integrations.supabase_portfolio import refresh_portfolio_total_equity

    result = with_auth_retry(
        tool_context,
        refresh_portfolio_total_equity,
        portfolio_id,
        client=get_user_client(tool_context),
    )
    return result.message if result.ok else f"总权益刷新失败：{result.message}"


def _sync_remote_portfolio_to_local(portfolio_id: str, tool_context: ToolContext | None) -> None:
    try:
        from integrations.supabase_portfolio import load_portfolio_state

        state = with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=get_user_client(tool_context))
        if state:
            _cache_portfolio(portfolio_id, state, "remote")
    except Exception:
        logger.warning("failed to cache portfolio %s locally after update", portfolio_id, exc_info=True)


def _local_update_summary(portfolio_id: str, msg: str, cloud: bool) -> dict:
    from integrations.local_db import load_portfolio

    state = load_portfolio(portfolio_id)
    if not state:
        return {"success": True, "message": msg, "positions": []}
    summary = [
        f"{p['code']} {p.get('name', '')} {p.get('shares', 0)}股 成本{p.get('cost_price', 0)}"
        for p in state.get("positions", [])
    ]
    result = {
        "success": True,
        "message": msg,
        "free_cash": state.get("free_cash", 0),
        "position_count": len(state.get("positions", [])),
        "positions_summary": summary,
    }
    if not cloud:
        result["storage"] = "local"
    return result
