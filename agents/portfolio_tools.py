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
    tool_context: ToolContext | None = None,
) -> dict:
    try:
        normalized_action = str(action or "").strip().lower()
        if normalized_action == "delete_records":
            return _delete_tracking_records(table, codes)
        portfolio_id = _portfolio_id(tool_context)
        cloud = has_cloud(tool_context)
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

    from core.trade_fill import Fill
    from utils.trading_clock import CN_TZ

    try:
        fill = Fill(
            code=str(code).strip(),
            side=str(side or "").strip().lower(),
            shares=int(shares),
            price=float(price),
            trade_date=str(trade_date or "").strip() or datetime.now(CN_TZ).strftime("%Y%m%d"),
            name=str(name or "").strip() or code_to_name(str(code).strip()),
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
    state = None
    if has_cloud(tool_context):
        from integrations.supabase_portfolio import load_portfolio_state

        client = get_user_client(tool_context)
        state = with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=client)
        if state:
            _cache_portfolio(portfolio_id, state, "remote")
    if state is not None:
        return state
    try:
        from integrations.local_db import load_portfolio

        return load_portfolio(portfolio_id)
    except Exception:
        logger.warning("failed to load portfolio %s from local DB", portfolio_id, exc_info=True)
        return None


def _cache_portfolio(portfolio_id: str, state: dict, source: str) -> None:
    try:
        from integrations.local_db import save_portfolio

        save_portfolio(
            portfolio_id,
            float(state.get("free_cash", 0) or 0),
            [_local_position(p) for p in state.get("positions", [])],
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
        }
        for p in state.get("positions", [])
    ]
    return {
        "portfolio_id": portfolio_id,
        "free_cash": state.get("free_cash", 0),
        "position_count": len(positions),
        "positions": positions,
    }


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
    out = {
        "portfolio_id": portfolio_id,
        "free_cash": state.get("free_cash", 0),
        "position_count": len(state["positions"]),
        "successful_count": success,
        "failed_count": failed,
        "diagnostics": results,
    }
    if hints:
        out["tickflow_limit_hint"] = hints[0]
    return out


_EXTREME_DAY_CHANGE_PCT = -5.0  # 与 core.holding_diagnostic 的阈值保持一致，避免无谓拉取分时线


def _diagnose_position(position: dict, start_date: date, end_date: date, hints: list[str]) -> dict:
    from core.holding_diagnostic import diagnose_one_stock
    from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

    code = position.get("code", "") or position.get("code", "")
    name = position.get("name", code)
    cost = float(position.get("cost", position.get("cost_price", 0)) or 0)
    try:
        df = get_stock_hist(code, start_date, end_date)
        if df is None or df.empty:
            return {"code": code, "name": name, "error": "无行情数据"}
        metadata = hist_metadata(df)
        _append_unique_hints(hints, collect_tickflow_limit_hints_from_df(df))
        normalized_df = normalize_hist_df(df)
        latest_date = latest_hist_date(df, "日期") or latest_hist_date(normalized_df)
        intraday_df = _fetch_intraday_if_extreme_day(code, normalized_df)
        diagnostic = diagnose_one_stock(code, name, cost, normalized_df, intraday_df=intraday_df)
        return _diagnostic_payload(diagnostic, latest_date, metadata)
    except Exception as e:
        return {"code": code, "name": name, "error": str(e)}


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


def _diagnostic_payload(diagnostic, latest_date: str, metadata: dict) -> dict:
    from agents.diagnosis_tools import diagnosis_brief_from_diagnostic
    from core.holding_diagnostic import format_diagnostic_text

    payload = {
        "code": diagnostic.code,
        "name": diagnostic.name,
        "health": diagnostic.health,
        "pnl_pct": round(diagnostic.pnl_pct, 2),
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
) -> str | dict:
    if action in ("add", "update"):
        return _upsert_position(portfolio_id, code, name, shares, cost_price, buy_dt, cloud, tool_context)
    if action == "remove":
        return _remove_position(portfolio_id, code, cloud, tool_context)
    if action == "set_cash":
        return _set_cash(portfolio_id, free_cash, cloud, tool_context)
    return {"error": f"未知操作: {action}，支持 add/update/remove/set_cash/delete_records"}


def _validate_position_amounts(shares: int, cost_price: float) -> dict | None:
    if float(shares) < 0:
        return {"error": "shares 不能为负数"}
    if float(cost_price) < 0:
        return {"error": "cost_price 不能为负数"}
    return None


def _upsert_position(
    portfolio_id: str,
    code: str,
    name: str,
    shares: int,
    cost_price: float,
    buy_dt: str,
    cloud: bool,
    tool_context: ToolContext | None,
) -> str | dict:
    if not code:
        return {"error": "add/update 操作需要提供股票代码 code"}
    amount_error = _validate_position_amounts(shares, cost_price)
    if amount_error:
        return amount_error
    code = code.strip()
    resolved_name = code_to_name(code)
    if resolved_name and name and resolved_name != name:
        return {"error": f"代码 {code} 对应的股票是「{resolved_name}」，而非「{name}」，请确认代码或名称是否正确"}
    name = name or resolved_name
    if cloud:
        from integrations.supabase_portfolio import upsert_position

        ok, msg = with_auth_retry(
            tool_context,
            upsert_position,
            portfolio_id,
            {"code": code, "name": name, "shares": shares, "cost_price": cost_price, "buy_dt": buy_dt},
            client=get_user_client(tool_context),
        )
        if not ok:
            return {"error": msg}
    from integrations.local_db import upsert_local_position

    upsert_local_position(portfolio_id, code, name, shares, cost_price, buy_dt)
    return f"{code} 已更新"


def _remove_position(portfolio_id: str, code: str, cloud: bool, tool_context: ToolContext | None) -> str | dict:
    if not code:
        return {"error": "remove 操作需要提供股票代码 code"}
    code = code.strip()
    if cloud:
        from integrations.supabase_portfolio import delete_position

        ok, msg = with_auth_retry(
            tool_context, delete_position, portfolio_id, code, client=get_user_client(tool_context)
        )
        if not ok:
            return {"error": msg}
    from integrations.local_db import delete_local_position

    delete_local_position(portfolio_id, code)
    return f"{code} 已删除"


def _set_cash(portfolio_id: str, free_cash: float, cloud: bool, tool_context: ToolContext | None) -> str | dict:
    if float(free_cash) < 0:
        return {"error": "free_cash 不能为负数"}
    if cloud:
        from integrations.supabase_portfolio import update_free_cash

        ok, msg = with_auth_retry(
            tool_context, update_free_cash, portfolio_id, free_cash, client=get_user_client(tool_context)
        )
        if not ok:
            return {"error": msg}
    from integrations.local_db import update_local_free_cash

    update_local_free_cash(portfolio_id, free_cash)
    return f"可用资金已更新为 {free_cash:,.2f}"


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
