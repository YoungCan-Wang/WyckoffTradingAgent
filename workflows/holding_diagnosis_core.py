"""Daily-bar holding diagnosis: portfolio loading, diagnosis, and action mapping.

Replaces the former minute-bar tail-buy holding pipeline. Uses `core.holding_diagnostic`
(daily K-line based; no TickFlow intraday dependency) as the single source of truth for
health/exit signals, and maps it onto a simple ADD/TRIM/HOLD action for reporting.
"""

from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from core.holding_diagnostic import HoldingDiagnostic, HoldingInput, diagnose_holdings
from core.holding_time_policy import holding_time_action, is_mainline_track
from core.wyckoff_engine import FunnelConfig, normalize_hist_from_fetch
from integrations.fetch_a_share_csv import fetch_hist, resolve_trading_window
from integrations.index_data_source import fetch_index_hist
from integrations.supabase_portfolio import load_portfolio_state
from utils.trading_clock import resolve_end_calendar_day

HOLDING_ACTION_ADD = "ADD"
HOLDING_ACTION_HOLD = "HOLD"
HOLDING_ACTION_TRIM = "TRIM"
TRADING_DAYS = 320


@dataclass
class HoldingActionAdvice:
    code: str
    name: str
    shares: int
    cost: float
    diagnostic: HoldingDiagnostic
    action: str = HOLDING_ACTION_HOLD
    risk_tag: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def current_price(self) -> float:
        return self.diagnostic.latest_close

    @property
    def pnl_pct(self) -> float:
        return self.diagnostic.pnl_pct

    @property
    def rule_score(self) -> float:
        # 日线诊断没有单一评分；用健康度映射一个可读分数供 LLM 提示词参考。
        return {"🟢健康": 80.0, "🟡警戒": 50.0, "🔴危险": 20.0}.get(self.diagnostic.health, 50.0)

    @property
    def features(self) -> dict[str, Any]:
        d = self.diagnostic
        return {
            "candidate_theme": "",
            "candidate_phase": d.accum_stage or "",
            "candidate_role": d.l2_channel,
            "ma_pattern": d.ma_pattern,
            "l4_triggers": "+".join(d.l4_triggers),
            "intraday_path": d.intraday_path,
            "vol_ratio_20_60": d.vol_ratio_20_60,
            "ret_10d_pct": d.ret_10d_pct,
        }


@dataclass(frozen=True)
class HoldingPortfolioContext:
    requested_portfolio_id: str
    resolved_portfolio_id: str
    state: dict[str, Any] | None
    positions: list[dict[str, Any]]
    position_stats: dict[str, int]


def default_holding_portfolio_id() -> str:
    direct = str(os.getenv("HOLDING_DIAG_PORTFOLIO_ID", "") or "").strip()
    if direct:
        return direct
    user_id = str(os.getenv("SUPABASE_USER_ID", "") or "").strip()
    if user_id:
        return f"USER_LIVE:{user_id}"
    monitor = str(os.getenv("MONITOR_PORTFOLIO_ID", "") or "").strip()
    return monitor or "USER_LIVE"


def _normalize_holding_code(raw: Any) -> str:
    from core.portfolio_symbol import normalize_portfolio_code

    return normalize_portfolio_code(str(raw or ""))


def _empty_position_stats() -> dict[str, int]:
    return {"raw": 0, "active": 0, "invalid_code": 0, "zero_shares": 0, "invalid_row": 0, "invalid_number": 0}


def _finite_number(raw: Any) -> float | None:
    """把 shares/cost 转成有限浮点数；None、非法字符串、NaN、Inf 一律返回 None。"""
    value = float(pd.to_numeric(raw, errors="coerce"))
    return value if math.isfinite(value) else None


def _append_position(row: Any, positions: list[dict[str, Any]], stats: dict[str, int]) -> None:
    stats["raw"] += 1
    if not isinstance(row, dict):
        stats["invalid_row"] += 1
        return
    code = _normalize_holding_code(row.get("code"))
    if not code:
        stats["invalid_code"] += 1
        return
    shares_value = _finite_number(row.get("shares"))
    cost_value = _finite_number(row.get("cost"))
    if shares_value is None or cost_value is None:
        stats["invalid_number"] += 1
        return
    shares = int(shares_value)
    if shares <= 0:
        stats["zero_shares"] += 1
        return
    stats["active"] += 1
    positions.append(
        {
            "code": code,
            "name": str(row.get("name", "") or code).strip() or code,
            "shares": shares,
            "cost": cost_value,
            "buy_dt": str(row.get("buy_dt") or row.get("buy_date") or "").strip(),
            "tag": str(row.get("tag") or row.get("wyckoff_tag") or "").strip(),
            "track": str(row.get("track") or row.get("wyckoff_track") or "").strip(),
        }
    )


def _normalize_effective_positions(raw_positions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    positions: list[dict[str, Any]] = []
    stats = _empty_position_stats()
    for row in raw_positions or []:
        _append_position(row, positions, stats)
    return positions, stats


def resolve_holding_portfolio_context(portfolio_id: str) -> HoldingPortfolioContext:
    """按显式 portfolio_id 加载持仓；缺少用户身份时 fail-closed，不跨用户兜底。

    未配置 ``SUPABASE_USER_ID``（或显式传入的 portfolio_id）时只能得到不带具体用户的
    `USER_LIVE` 占位符——绝不能因此去数据库里挑一个"最近更新且有仓位"的其它用户顶替，
    否则会把该用户的持仓诊断发到当前配置的 Telegram，构成跨用户数据泄露。
    """
    requested = str(portfolio_id or "").strip() or "USER_LIVE"
    state = load_portfolio_state(requested)
    positions: list[dict[str, Any]] = []
    stats = _empty_position_stats()
    if isinstance(state, dict):
        positions, stats = _normalize_effective_positions(list(state.get("positions") or []))
    return HoldingPortfolioContext(
        requested_portfolio_id=requested,
        resolved_portfolio_id=requested,
        state=state if isinstance(state, dict) else None,
        positions=positions,
        position_stats=stats,
    )


def holding_portfolio_meta(context: HoldingPortfolioContext) -> str:
    stats = context.position_stats
    return (
        f"portfolio={context.resolved_portfolio_id}, raw_positions={stats['raw']}, active_positions={stats['active']}"
    )


def holding_no_position_meta(context: HoldingPortfolioContext) -> str:
    stats = context.position_stats
    meta = (
        f"portfolio={context.resolved_portfolio_id}, "
        f"raw_positions={stats['raw']}, active_positions={stats['active']}, "
        f"invalid_code={stats['invalid_code']}, zero_shares={stats['zero_shares']}, "
        f"invalid_number={stats['invalid_number']}"
    )
    if context.requested_portfolio_id == "USER_LIVE":
        meta += "（提示：USER_LIVE 无有效仓位；请检查是否应使用 USER_LIVE:<user_id>）"
    return meta


def _hold_trade_days(buy_dt: Any, daily_history: pd.DataFrame | None, *, as_of: date | None = None) -> int | None:
    if daily_history is None or getattr(daily_history, "empty", True) or "date" not in daily_history.columns:
        return None
    buy_ts = pd.to_datetime(str(buy_dt or "").strip(), errors="coerce")
    if pd.isna(buy_ts):
        return None
    buy_date = buy_ts.date()
    end_date = as_of or date.today()
    dates = pd.to_datetime(daily_history["date"], errors="coerce").dropna().dt.date.tolist()
    dates = sorted({d for d in dates if d <= end_date})
    if not dates:
        return None
    entry_trade_date = next((d for d in dates if d >= buy_date), None)
    if entry_trade_date is None:
        return None
    return int(sum(1 for d in dates if d >= entry_trade_date))


def _time_management_action(
    position: dict[str, Any], diag: HoldingDiagnostic, daily_history: pd.DataFrame | None
) -> tuple[str, str] | None:
    hold_days = _hold_trade_days(position.get("buy_dt"), daily_history)
    if hold_days is None:
        return None
    mainline = is_mainline_track(diag.track, position.get("tag"), position.get("track"))
    below_ma20 = bool(diag.ma20 and diag.latest_close < diag.ma20 * 0.995)
    guidance = holding_time_action(hold_days, is_mainline=mainline, below_ma20=below_ma20)
    if guidance.action in {"TIME_EXIT", "REVIEW_TRIM"}:
        return "time_exit" if guidance.action == "TIME_EXIT" else "time_review", guidance.reason
    return None


def _action_from_diagnostic(
    position: dict[str, Any], diag: HoldingDiagnostic, daily_history: pd.DataFrame | None
) -> HoldingActionAdvice:
    reasons = list(diag.health_reasons)
    action, risk_tag = HOLDING_ACTION_HOLD, ""
    if diag.stop_loss_status == "已穿止损" or diag.exit_signal == "stop_loss":
        action, risk_tag = HOLDING_ACTION_TRIM, "hard_stop"
    elif diag.intraday_path == "distribution":
        action, risk_tag = HOLDING_ACTION_TRIM, "confirmed_breakdown"
        reasons = [diag.intraday_path_desc, *reasons]
    elif diag.intraday_path == "washout":
        risk_tag = "washout"
        reasons = [diag.intraday_path_desc, *reasons]
    elif diag.exit_signal in {"distribution_warning", "upthrust_warning"}:
        action, risk_tag = HOLDING_ACTION_TRIM, "distribution_warning"
    elif diag.health == "🟢健康" and diag.l4_triggers and diag.pnl_pct >= 0:
        action = HOLDING_ACTION_ADD
        reasons = [f"L4信号:{'+'.join(diag.l4_triggers)}，结构健康", *reasons]
    time_action = _time_management_action(position, diag, daily_history)
    if time_action and action == HOLDING_ACTION_HOLD:
        risk_tag, time_reason = time_action
        action = HOLDING_ACTION_TRIM
        reasons = [time_reason, *reasons]
    return HoldingActionAdvice(
        code=diag.code,
        name=diag.name,
        shares=int(position.get("shares") or 0),
        cost=diag.cost,
        diagnostic=diag,
        action=action,
        risk_tag=risk_tag,
        reasons=reasons[:3] or ["结构中性，先持有观察"],
    )


def fetch_holding_daily_frame(code: str) -> pd.DataFrame | None:
    from core.portfolio_symbol import is_cn_portfolio_code, normalize_portfolio_code
    from integrations.tickflow_client import normalize_cn_symbol

    normalized = normalize_portfolio_code(code)
    if not normalized:
        return None
    symbol = normalize_cn_symbol(normalized) if is_cn_portfolio_code(normalized) else normalized
    window = resolve_trading_window(end_calendar_day=resolve_end_calendar_day(), trading_days=TRADING_DAYS)
    try:
        raw = fetch_hist(symbol, window, adjust="qfq")
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return None
        return normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def fetch_holding_daily_frames(positions: list[dict[str, Any]]) -> dict[str, pd.DataFrame]:
    return {p["code"]: df for p in positions if (df := fetch_holding_daily_frame(p["code"])) is not None}


def fetch_holding_benchmark() -> pd.DataFrame | None:
    window = resolve_trading_window(end_calendar_day=resolve_end_calendar_day(), trading_days=TRADING_DAYS)
    try:
        bench_raw = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        if bench_raw is None or bench_raw.empty:
            return None
        return normalize_hist_from_fetch(bench_raw).sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def build_holding_advices(
    positions: list[dict[str, Any]],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None = None,
    cfg: FunnelConfig | None = None,
    intraday_df_map: dict[str, pd.DataFrame] | None = None,
) -> list[HoldingActionAdvice]:
    holding_inputs = [HoldingInput.from_position(p) for p in positions]
    diagnostics = diagnose_holdings(holding_inputs, df_map, bench_df, cfg, intraday_df_map)
    by_code = {p["code"]: p for p in positions}
    advices = [_action_from_diagnostic(by_code[d.code], d, df_map.get(d.code)) for d in diagnostics]
    advices.sort(key=lambda a: (_holding_rank(a), -a.diagnostic.pnl_pct, a.code))
    return advices


def _holding_rank(advice: HoldingActionAdvice) -> int:
    if advice.action == HOLDING_ACTION_ADD:
        return 0
    if advice.action == HOLDING_ACTION_TRIM:
        return 1
    if advice.risk_tag == "washout":
        return 2
    return 3


def _risk_items(advices: list[HoldingActionAdvice], tag: str) -> list[HoldingActionAdvice]:
    return [a for a in advices if a.action == HOLDING_ACTION_HOLD and a.risk_tag == tag]


def _append_block(lines: list[str], title: str, block: list[HoldingActionAdvice]) -> None:
    lines.append(f"### {title}")
    if not block:
        lines.append("- 无")
        lines.append("")
        return
    for item in block:
        diag = item.diagnostic
        reasons = "；".join(item.reasons)
        current = f"{diag.latest_close:.2f}" if diag.latest_close > 0 else "--"
        pnl = f"{diag.pnl_pct:+.1f}%" if diag.latest_close > 0 and item.cost > 0 else "--"
        lines.append(
            f"- {item.code} {item.name} | 持仓={item.shares}股 | 现价={current} | "
            f"浮盈={pnl} | 健康={diag.health} | {reasons}"
        )
    lines.append("")


def build_holdings_markdown(*, holdings: list[HoldingActionAdvice], portfolio_meta: str) -> str:
    lines: list[str] = ["## 持仓动作建议（日线止损/结构减仓/洗盘观察）"]
    if portfolio_meta:
        lines.append(f"- 持仓来源: {portfolio_meta}")

    if not holdings:
        lines.append("- 持仓数量: 0")
        lines.append("- 无可分析持仓")
        return "\n".join(lines)

    counter = Counter([x.action for x in holdings])
    wash_count = len(_risk_items(holdings, "washout"))
    neutral_hold_count = counter.get(HOLDING_ACTION_HOLD, 0) - wash_count
    lines.append(f"- 持仓数量: {len(holdings)}")
    lines.append(
        f"- 动作分布: ADD={counter.get(HOLDING_ACTION_ADD, 0)} / "
        f"TRIM（止损/确认破位/到期复核）={counter.get(HOLDING_ACTION_TRIM, 0)} / "
        f"洗盘观察={wash_count} / HOLD={max(neutral_hold_count, 0)}"
    )
    lines.append("- 读法: TRIM 才是需要处理的风险动作；洗盘观察先看收盘与次日确认。")
    lines.append("")

    _append_block(lines, "ADD（可考虑加仓）", [x for x in holdings if x.action == HOLDING_ACTION_ADD])
    _append_block(
        lines, "TRIM（止损/确认破位/到期复核，优先处理）", [x for x in holdings if x.action == HOLDING_ACTION_TRIM]
    )
    _append_block(lines, "WASH（疑似洗盘/回踩测试，不直接卖）", _risk_items(holdings, "washout"))
    _append_block(
        lines,
        "HOLD（结构中性持有观察）",
        [x for x in holdings if x.action == HOLDING_ACTION_HOLD and x.risk_tag != "washout"],
    )
    lines.append("说明：持仓动作仅为日线辅助建议，不自动下单；TRIM 也应结合仓位人工确认。")
    return "\n".join(lines)
