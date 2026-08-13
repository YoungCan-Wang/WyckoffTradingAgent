"""
Supabase 投资组合读写（脚本侧）
用途：
1) 读取 USER_LIVE 持仓状态给 Step4 使用
2) 记录 AI 订单建议与每日净值快照
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from supabase import Client

from core.buy_dt import (
    POSITION_EXISTS_ERROR,
    POSITION_MISSING_ERROR,
    buy_dt_error,
)
from core.constants import (
    TABLE_DAILY_NAV,
    TABLE_PORTFOLIO_POSITIONS,
    TABLE_PORTFOLIOS,
    TABLE_TRADE_ORDERS,
    TABLE_USER_SETTINGS,
)
from core.trade_fill import Fill, Holding, apply_fill
from integrations.supabase_base import create_admin_client as _get_supabase_admin_client
from integrations.supabase_base import is_admin_configured as is_supabase_configured
from integrations.supabase_base import require_server_write_context

logger = logging.getLogger(__name__)

# Partial commit after position write. Must not embed upstream auth/JWT text — callers
# that auth-retry on keyword match would otherwise re-apply the same fill.
PARTIAL_FILL_WRITE_MSG = "持仓已更新但现金写入失败，请手动核对可用现金，勿重复回填同一笔成交"


@dataclass(frozen=True)
class FillWriteResult:
    ok: bool
    message: str
    position_committed: bool = False


@dataclass(frozen=True)
class EquityRefreshResult:
    ok: bool
    total_equity: float | None
    message: str


def load_user_settings_admin(user_id: str) -> dict[str, Any] | None:
    user_id = str(user_id or "").strip()
    if not user_id or not is_supabase_configured():
        return None
    try:
        client = _get_supabase_admin_client()
        resp = client.table(TABLE_USER_SETTINGS).select("*").eq("user_id", user_id).limit(1).execute()
        if not resp.data:
            return None
        row = resp.data[0] or {}
        if not isinstance(row, dict):
            return None
        return row
    except Exception as e:
        logger.warning("[supabase_portfolio] load_user_settings_admin failed: %s", e)
        return None


def _normalize_buy_dt_text(raw: Any) -> str:
    text = str(raw or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return text


def compute_portfolio_state_signature(
    free_cash: float | int | None,
    positions: list[dict[str, Any]] | None,
) -> str:
    from core.portfolio_symbol import normalize_portfolio_code

    normalized_positions: list[dict[str, Any]] = []
    for row in positions or []:
        code = normalize_portfolio_code(str(row.get("code", "") or ""))
        if not code:
            continue
        normalized_positions.append(
            {
                "code": code,
                "shares": int(row.get("shares", 0) or 0),
                "cost_price": round(float(row.get("cost_price", row.get("cost", 0.0)) or 0.0), 4),
                "buy_dt": _normalize_buy_dt_text(row.get("buy_dt")),
            }
        )
    normalized_positions.sort(key=lambda x: x["code"])
    payload = {
        "free_cash": round(float(free_cash or 0.0), 2),
        "positions": normalized_positions,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def extract_state_signature_from_run_id(run_id: Any) -> str:
    text = str(run_id or "").strip()
    m = re.search(r"_sig([0-9a-fA-F]{8,40})$", text)
    return m.group(1).lower() if m else ""


def _is_active_trade_order_status(status: Any) -> bool:
    return str(status or "").strip().upper() not in {"", "CANCELLED", "CANCELED"}


def load_portfolio_state(portfolio_id: str = "USER_LIVE", client: Client | None = None) -> dict[str, Any] | None:
    """
    返回格式：
    {
      "portfolio_id": "...",
      "free_cash": 12345.6,
      "total_equity": 23456.7 | None,
      "positions": [{"code","name","cost","buy_dt","shares"}, ...]
    }
    """
    if client is None and not is_supabase_configured():
        return None
    try:
        client = client or _get_supabase_admin_client()
        p_resp = (
            client.table(TABLE_PORTFOLIOS)
            .select("portfolio_id,free_cash,total_equity,updated_at")
            .eq("portfolio_id", portfolio_id)
            .limit(1)
            .execute()
        )
        if not p_resp.data:
            return None
        p = p_resp.data[0]
        pos_resp = (
            client.table(TABLE_PORTFOLIO_POSITIONS)
            .select("code,name,shares,cost_price,buy_dt,stop_loss,updated_at")
            .eq("portfolio_id", portfolio_id)
            .order("code")
            .execute()
        )
        positions: list[dict[str, Any]] = []
        latest_updates: list[str] = [str(p.get("updated_at", "") or "").strip()]
        for row in pos_resp.data or []:
            row_updated_at = str(row.get("updated_at", "") or "").strip()
            if row_updated_at:
                latest_updates.append(row_updated_at)
            positions.append(
                {
                    "code": str(row.get("code", "")).strip(),
                    "name": str(row.get("name", "")).strip(),
                    "cost": float(row.get("cost_price", 0.0) or 0.0),
                    "buy_dt": str(row.get("buy_dt", "") or "").strip(),
                    "shares": int(row.get("shares", 0) or 0),
                    "stop_loss": (float(row["stop_loss"]) if row.get("stop_loss") is not None else None),
                    "updated_at": row_updated_at,
                }
            )
        state_updated_at = max((x for x in latest_updates if x), default="")
        return {
            "portfolio_id": str(p.get("portfolio_id")),
            "free_cash": float(p.get("free_cash", 0.0) or 0.0),
            "total_equity": (float(p["total_equity"]) if p.get("total_equity") is not None else None),
            "updated_at": str(p.get("updated_at", "") or "").strip(),
            "state_updated_at": state_updated_at,
            "state_signature": compute_portfolio_state_signature(p.get("free_cash", 0.0), positions),
            "positions": positions,
        }
    except Exception as e:
        logger.warning("[supabase_portfolio] load_portfolio_state failed: %s", e)
        return None


def build_user_live_portfolio_id(user_id: str) -> str:
    user_id = str(user_id or "").strip()
    return f"USER_LIVE:{user_id}"


def list_step4_targets(target_user_id: str | None = None) -> list[dict[str, Any]]:
    """
    自动发现可执行 Step4 的用户目标：
    - 来自 user_settings（必须有 user_id / tg_bot_token / tg_chat_id）
    - 自动映射 portfolio_id=USER_LIVE:<user_id>
    - 仅返回 Supabase 中已存在且结构可用的 portfolio
    """
    if not is_supabase_configured():
        return []
    try:
        client = _get_supabase_admin_client()
        query = client.table(TABLE_USER_SETTINGS).select("user_id,tg_bot_token,tg_chat_id,gemini_api_key,gemini_model")
        target_user_id = str(target_user_id or "").strip()
        if target_user_id:
            query = query.eq("user_id", target_user_id).limit(1)
        resp = query.execute()
        targets: list[dict[str, Any]] = []
        for row in resp.data or []:
            user_id = str(row.get("user_id", "") or "").strip()
            if target_user_id and user_id != target_user_id:
                continue
            tg_bot_token = str(row.get("tg_bot_token", "") or "").strip()
            tg_chat_id = str(row.get("tg_chat_id", "") or "").strip()
            if not user_id or not tg_bot_token or not tg_chat_id:
                continue
            portfolio_id = build_user_live_portfolio_id(user_id)
            p = load_portfolio_state(portfolio_id)
            if not isinstance(p, dict):
                continue
            if p.get("free_cash") is None or not isinstance(p.get("positions"), list):
                continue
            targets.append(
                {
                    "user_id": user_id,
                    "portfolio_id": portfolio_id,
                    "tg_bot_token": tg_bot_token,
                    "tg_chat_id": tg_chat_id,
                    "gemini_api_key": str(row.get("gemini_api_key", "") or "").strip(),
                    "gemini_model": str(row.get("gemini_model", "") or "").strip(),
                }
            )
        return targets
    except Exception as e:
        logger.warning("[supabase_portfolio] list_step4_targets failed: %s", e)
        return []


def check_daily_run_exists(
    portfolio_id: str,
    trade_date: str,
    state_signature: str | None = None,
) -> bool:
    """
    检查当日是否已存在同一持仓快照下的有效交易订单（幂等性检查）。
    返回 True 表示当前快照已运行过。
    """
    if not is_supabase_configured():
        return False
    try:
        client = _get_supabase_admin_client()
        resp = (
            client.table(TABLE_TRADE_ORDERS)
            .select("run_id,status,created_at")
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        rows = resp.data or []
        active_rows = [row for row in rows if _is_active_trade_order_status(row.get("status"))]
        if not active_rows:
            return False
        expected_sig = str(state_signature or "").strip().lower()
        if not expected_sig:
            return True
        return any(extract_state_signature_from_run_id(row.get("run_id")) == expected_sig for row in active_rows)
    except Exception as e:
        logger.warning("[supabase_portfolio] check_daily_run_exists failed: %s", e)
        return False


def set_position_stops(
    portfolio_id: str,
    updates: list[dict[str, Any]],
    client: Client | None = None,
) -> tuple[bool, int]:
    """按用户 JWT 写止损价，只碰 stop_loss 列。返回 (成功, 写入条数)。

    与 update_position_stops 的区别：那个走 server_job admin 上下文（Step4 用），
    这个走用户自己的 client，供 CLI/桌面端的 set_stop_loss 工具调用。
    """
    if not updates:
        return True, 0
    try:
        write_client = _resolve_write_client(client, "set portfolio stop losses")
        written = 0
        for item in updates:
            code = str(item.get("code") or "").strip()
            if not code or "stop_loss" not in item:
                continue
            (
                write_client.table(TABLE_PORTFOLIO_POSITIONS)
                .update({"stop_loss": item.get("stop_loss")})
                .eq("portfolio_id", portfolio_id)
                .eq("code", code)
                .execute()
            )
            written += 1
        return True, written
    except Exception as e:
        logger.warning("[supabase_portfolio] set_position_stops failed: %s", e)
        return False, 0


def update_position_stops(portfolio_id: str, updates: list[dict[str, Any]]) -> bool:
    """
    批量更新持仓止损价。
    updates: [{"code": "000001", "stop_loss": 12.34}, ...]
    """
    if not is_supabase_configured() or not updates:
        return False
    require_server_write_context("update portfolio stop losses")
    try:
        client = _get_supabase_admin_client()
        # Supabase 不支持批量 update 不同值，需逐个 update
        # 若量大可考虑其它方式，目前持仓数不多，循环即可
        for item in updates:
            code = item.get("code")
            if not code or "stop_loss" not in item:
                continue
            (
                client.table(TABLE_PORTFOLIO_POSITIONS)
                .update({"stop_loss": item.get("stop_loss")})
                .eq("portfolio_id", portfolio_id)
                .eq("code", code)
                .execute()
            )
        return True
    except Exception as e:
        logger.warning("[supabase_portfolio] update_position_stops failed: %s", e)
        return False


def _ensure_portfolio_exists(portfolio_id: str, client: Client) -> None:
    """确保 portfolios 行存在，不存在则创建。"""
    resp = client.table(TABLE_PORTFOLIOS).select("portfolio_id").eq("portfolio_id", portfolio_id).limit(1).execute()
    if not resp.data:
        client.table(TABLE_PORTFOLIOS).upsert(
            {"portfolio_id": portfolio_id, "free_cash": 0, "name": "我的持仓"},
            on_conflict="portfolio_id",
        ).execute()


def _resolve_write_client(client: Client | None, operation: str) -> Client:
    if client is not None:
        return client
    require_server_write_context(operation)
    return _get_supabase_admin_client()


def refresh_portfolio_total_equity(
    portfolio_id: str,
    client: Client | None = None,
) -> EquityRefreshResult:
    """Revalue every holding at the latest quote and persist the CNY total."""
    from core.portfolio_valuation import calculate_portfolio_valuation
    from integrations.portfolio_market_value import load_portfolio_marks

    try:
        client = _resolve_write_client(client, "refresh portfolio total equity")
        state = load_portfolio_state(portfolio_id, client=client)
        if state is None:
            return EquityRefreshResult(False, None, f"未找到组合 {portfolio_id}")
        positions = list(state.get("positions") or [])
        if positions:
            api_key = _portfolio_tickflow_key(portfolio_id, client)
            if not api_key:
                return EquityRefreshResult(False, None, "未配置 TickFlow API Key")
            prices, rates = load_portfolio_marks(positions, api_key)
        else:
            prices, rates = {}, {"CNY": 1.0}
        valuation = calculate_portfolio_valuation(float(state.get("free_cash", 0.0) or 0.0), positions, prices, rates)
        payload = {"total_equity": valuation.total_equity, "updated_at": datetime.now(UTC).isoformat()}
        client.table(TABLE_PORTFOLIOS).update(payload).eq("portfolio_id", portfolio_id).execute()
        return EquityRefreshResult(True, valuation.total_equity, f"总权益已刷新为 {valuation.total_equity:,.2f}")
    except Exception as exc:
        logger.warning("[supabase_portfolio] refresh total_equity failed: %s", exc)
        return EquityRefreshResult(False, None, str(exc))


def _portfolio_tickflow_key(portfolio_id: str, client: Client) -> str:
    fallback = os.getenv("TICKFLOW_API_KEY", "").strip()
    prefix, separator, user_id = str(portfolio_id or "").partition(":")
    if prefix != "USER_LIVE" or not separator or not user_id:
        return fallback
    response = client.table(TABLE_USER_SETTINGS).select("tickflow_api_key").eq("user_id", user_id).limit(1).execute()
    rows = response.data or []
    return str(rows[0].get("tickflow_api_key", "") or "").strip() if rows else fallback


def _mutation_message(message: str, portfolio_id: str, client: Client, refresh_equity: bool) -> str:
    if not refresh_equity:
        return message
    refreshed = refresh_portfolio_total_equity(portfolio_id, client=client)
    suffix = refreshed.message if refreshed.ok else f"总权益刷新失败：{refreshed.message}"
    return f"{message}；{suffix}"


def _base_position_row(portfolio_id: str, position: dict[str, Any]) -> tuple[str, dict[str, Any]] | tuple[None, str]:
    from core.portfolio_symbol import normalize_portfolio_code

    code = normalize_portfolio_code(str(position.get("code", "")))
    if not code:
        return None, f"无效的股票代码: {position.get('code', '')}"
    row = {
        "portfolio_id": portfolio_id,
        "code": code,
        "name": str(position.get("name", "") or "").strip(),
        "shares": int(position.get("shares", 0) or 0),
        "cost_price": float(position.get("cost_price", 0) or 0),
    }
    return code, row


def _with_validated_buy_dt(
    row: dict[str, Any], position: dict[str, Any], *, required: bool
) -> tuple[dict[str, Any], str]:
    buy_dt = str(position.get("buy_dt", "") or "").strip()
    error = buy_dt_error(buy_dt, required=required)
    if error:
        return row, error
    if buy_dt:
        row["buy_dt"] = buy_dt
    return row, ""


def _is_unique_violation(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "duplicate" in text or "23505" in text or "unique constraint" in text


def upsert_position(
    portfolio_id: str,
    position: dict[str, Any],
    client: Client | None = None,
    *,
    refresh_equity: bool = True,
) -> tuple[bool, str]:
    """成交回填用的 upsert：空 buy_dt 不覆盖原日期。add/update 不走这里。"""
    code, row = _base_position_row(portfolio_id, position)
    if code is None:
        return False, str(row)
    try:
        client = _resolve_write_client(client, "upsert portfolio position")
        _ensure_portfolio_exists(portfolio_id, client)
        buy_dt = str(position.get("buy_dt", "") or "").strip()
        if buy_dt:
            row["buy_dt"] = buy_dt
        client.table(TABLE_PORTFOLIO_POSITIONS).upsert(row, on_conflict="portfolio_id,code").execute()
        return True, _mutation_message(f"{code} 已更新", portfolio_id, client, refresh_equity)
    except Exception as e:
        logger.warning("[supabase_portfolio] upsert_position failed: %s", e)
        return False, str(e)


def insert_position(
    portfolio_id: str,
    position: dict[str, Any],
    client: Client | None = None,
    *,
    refresh_equity: bool = True,
) -> tuple[bool, str]:
    """Insert-only. Missing or invalid buy_dt fails without writing."""
    code, row = _base_position_row(portfolio_id, position)
    if code is None:
        return False, str(row)
    row, error = _with_validated_buy_dt(row, position, required=True)
    if error:
        return False, error
    try:
        client = _resolve_write_client(client, "insert portfolio position")
        _ensure_portfolio_exists(portfolio_id, client)
        client.table(TABLE_PORTFOLIO_POSITIONS).insert(row).execute()
        return True, _mutation_message(f"{code} 已新增", portfolio_id, client, refresh_equity)
    except Exception as e:
        logger.warning("[supabase_portfolio] insert_position failed: %s", e)
        if _is_unique_violation(e):
            return False, POSITION_EXISTS_ERROR
        return False, str(e)


def update_position(
    portfolio_id: str,
    position: dict[str, Any],
    client: Client | None = None,
    *,
    refresh_equity: bool = True,
) -> tuple[bool, str]:
    """Update-only. Does not insert a missing row. Empty buy_dt leaves the original date."""
    code, row = _base_position_row(portfolio_id, position)
    if code is None:
        return False, str(row)
    row, error = _with_validated_buy_dt(row, position, required=False)
    if error:
        return False, error
    try:
        client = _resolve_write_client(client, "update portfolio position")
        response = (
            client.table(TABLE_PORTFOLIO_POSITIONS)
            .update(row)
            .eq("portfolio_id", portfolio_id)
            .eq("code", code)
            .execute()
        )
        if not getattr(response, "data", None):
            return False, POSITION_MISSING_ERROR
        return True, _mutation_message(f"{code} 已更新", portfolio_id, client, refresh_equity)
    except Exception as e:
        logger.warning("[supabase_portfolio] update_position failed: %s", e)
        return False, str(e)


def delete_position(
    portfolio_id: str,
    code: str,
    client: Client | None = None,
    *,
    refresh_equity: bool = True,
) -> tuple[bool, str]:
    """删除单个持仓。"""
    from core.portfolio_symbol import normalize_portfolio_code

    code = normalize_portfolio_code(code) or str(code or "").strip().upper()
    try:
        client = _resolve_write_client(client, "delete portfolio position")
        client.table(TABLE_PORTFOLIO_POSITIONS).delete().eq("portfolio_id", portfolio_id).eq("code", code).execute()
        return True, _mutation_message(f"{code} 已删除", portfolio_id, client, refresh_equity)
    except Exception as e:
        logger.warning("[supabase_portfolio] delete_position failed: %s", e)
        return False, str(e)


def _canonicalize_fill(fill: Fill) -> Fill | None:
    """CLI 可能传入大小写/补零不一致的港美代码；写路径必须先收成账本规范码。"""
    from core.portfolio_symbol import normalize_portfolio_code

    code = normalize_portfolio_code(fill.code)
    if not code:
        return None
    if code == fill.code:
        return fill
    return Fill(
        code=code,
        side=fill.side,
        shares=fill.shares,
        price=fill.price,
        trade_date=fill.trade_date,
        name=fill.name,
    )


def _find_position_for_fill(positions: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    from core.portfolio_symbol import normalize_portfolio_code

    for row in positions:
        raw = str(row.get("code", "") or "").strip()
        if (normalize_portfolio_code(raw) or raw.upper()) == code:
            return row
    return None


def record_fill(portfolio_id: str, fill: Fill, client: Client | None = None) -> FillWriteResult:
    """把一笔真实成交写回持仓与现金。

    先读当前状态再算，所以必须串行调用；同一账户并发回填会互相覆盖。人工录入的
    使用场景下这个约束是成立的，换成自动对接券商前需要改成带版本号的乐观锁。
    """
    fill = _canonicalize_fill(fill)
    if fill is None:
        return FillWriteResult(False, "无效的股票代码：A股用6位数字，港股用00700.HK，美股用AAPL.US")
    try:
        client = _resolve_write_client(client, "record trade fill")
        state = load_portfolio_state(portfolio_id, client=client)
        if state is None:
            return FillWriteResult(False, f"未找到组合 {portfolio_id}")
        row = _find_position_for_fill(state["positions"], fill.code)
        holding = (
            Holding(
                code=fill.code,
                name=str(row.get("name", "") or ""),
                shares=int(row.get("shares", 0) or 0),
                cost_price=float(row.get("cost", 0) or 0),
                buy_dt=str(row.get("buy_dt", "") or ""),
            )
            if row
            else None
        )
        result = apply_fill(holding, float(state["free_cash"]), fill)
    except ValueError as exc:
        return FillWriteResult(False, str(exc))
    except Exception as exc:
        logger.warning("[supabase_portfolio] record_fill failed: %s", exc)
        return FillWriteResult(False, str(exc))

    if result.holding is None:
        ok, msg = delete_position(portfolio_id, fill.code, client=client, refresh_equity=False)
    else:
        ok, msg = upsert_position(
            portfolio_id,
            {
                "code": result.holding.code,
                "name": result.holding.name,
                "shares": result.holding.shares,
                "cost_price": result.holding.cost_price,
                "buy_dt": result.holding.buy_dt,
            },
            client=client,
            refresh_equity=False,
        )
    if not ok:
        return FillWriteResult(False, f"持仓写入失败：{msg}")
    ok, msg = update_free_cash(portfolio_id, result.cash, client=client, refresh_equity=False)
    if not ok:
        logger.warning("[supabase_portfolio] cash write failed after position update: %s", msg)
        return FillWriteResult(False, PARTIAL_FILL_WRITE_MSG, position_committed=True)
    refresh = refresh_portfolio_total_equity(portfolio_id, client=client)
    refresh_msg = refresh.message if refresh.ok else f"总权益刷新失败：{refresh.message}"
    return FillWriteResult(
        True,
        f"{result.note}；可用现金 {result.cash:,.2f}；{refresh_msg}",
        position_committed=True,
    )


def update_free_cash(
    portfolio_id: str,
    free_cash: float,
    client: Client | None = None,
    *,
    refresh_equity: bool = True,
) -> tuple[bool, str]:
    """更新可用资金。"""
    try:
        client = _resolve_write_client(client, "update portfolio cash")
        _ensure_portfolio_exists(portfolio_id, client)
        client.table(TABLE_PORTFOLIOS).update({"free_cash": free_cash}).eq("portfolio_id", portfolio_id).execute()
        message = f"可用资金已更新为 {free_cash:,.2f}"
        return True, _mutation_message(message, portfolio_id, client, refresh_equity)
    except Exception as e:
        logger.warning("[supabase_portfolio] update_free_cash failed: %s", e)
        return False, str(e)


def save_ai_trade_orders(
    *,
    run_id: str,
    portfolio_id: str,
    model: str,
    trade_date: str,
    market_view: str,
    orders: list[dict[str, Any]],
) -> bool:
    if not is_supabase_configured():
        return False
    if not orders:
        return True
    require_server_write_context("insert trade_orders")
    try:
        client = _get_supabase_admin_client()
        payload: list[dict[str, Any]] = []
        for o in orders:
            payload.append(
                {
                    "run_id": run_id,
                    "portfolio_id": portfolio_id,
                    "trade_date": trade_date,
                    "model": model,
                    "market_view": market_view or "",
                    "code": str(o.get("code", "")).strip(),
                    "name": str(o.get("name", "")).strip(),
                    "action": str(o.get("action", "")).strip(),
                    "status": str(o.get("status", "")).strip(),
                    "shares": int(o.get("shares", 0) or 0),
                    "price_hint": (float(o["price_hint"]) if o.get("price_hint") is not None else None),
                    "amount": float(o.get("amount", 0.0) or 0.0),
                    "stop_loss": (float(o["stop_loss"]) if o.get("stop_loss") is not None else None),
                    "max_loss": float(o.get("max_loss", 0.0) or 0.0),
                    "drawdown_ratio": float(o.get("drawdown_ratio", 0.0) or 0.0),
                    "reason": str(o.get("reason", "") or ""),
                    "tape_condition": str(o.get("tape_condition", "") or ""),
                    "invalidate_condition": str(o.get("invalidate_condition", "") or ""),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        client.table(TABLE_TRADE_ORDERS).insert(payload).execute()
        return True
    except Exception as e:
        logger.warning("[supabase_portfolio] save_ai_trade_orders failed: %s", e)
        return False


def cancel_trade_orders(
    *,
    portfolio_id: str,
    trade_date: str,
    exclude_run_id: str | None = None,
    only_run_id: str | None = None,
    raise_on_error: bool = False,
) -> int:
    if not is_supabase_configured():
        return 0
    require_server_write_context("cancel trade_orders")
    try:
        client = _get_supabase_admin_client()
        query = (
            client.table(TABLE_TRADE_ORDERS)
            .select("id,status,run_id")
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .limit(500)
        )
        if only_run_id:
            query = query.eq("run_id", only_run_id)
        elif exclude_run_id:
            query = query.neq("run_id", exclude_run_id)
        rows = query.execute().data or []
        active_ids = [row["id"] for row in rows if _is_active_trade_order_status(row.get("status"))]
        if active_ids:
            client.table(TABLE_TRADE_ORDERS).update({"status": "CANCELLED"}).in_("id", active_ids).execute()
        return len(active_ids)
    except Exception as e:
        logger.warning("[supabase_portfolio] cancel_trade_orders failed: %s", e)
        if raise_on_error:
            raise
        return 0


def load_recent_trade_orders(portfolio_id: str, *, limit: int = 200) -> list[dict]:
    """近期工单，用于识别反复发出却没被执行的离场建议。"""
    if not is_supabase_configured():
        return []
    try:
        client = _get_supabase_admin_client()
        resp = (
            client.table(TABLE_TRADE_ORDERS)
            .select("code,name,action,status,trade_date")
            .eq("portfolio_id", portfolio_id)
            .order("trade_date", desc=True)
            .limit(limit)
            .execute()
        )
        return list(resp.data or [])
    except Exception as e:
        logger.warning("[supabase_portfolio] load_recent_trade_orders failed: %s", e)
        return []


def upsert_daily_nav(
    *,
    portfolio_id: str,
    trade_date: str,
    free_cash: float,
    total_equity: float,
    positions_value: float,
) -> bool:
    if not is_supabase_configured():
        return False
    require_server_write_context("upsert daily_nav")
    try:
        client = _get_supabase_admin_client()
        payload = {
            "portfolio_id": portfolio_id,
            "trade_date": trade_date,
            "free_cash": float(free_cash),
            "positions_value": float(positions_value),
            "total_equity": float(total_equity),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        client.table(TABLE_DAILY_NAV).upsert(
            payload,
            on_conflict="portfolio_id,trade_date",
        ).execute()
        return True
    except Exception as e:
        logger.warning("[supabase_portfolio] upsert_daily_nav failed: %s", e)
        return False
