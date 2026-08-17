"""独立的每日净值快照：不依赖漏斗或 Step4 是否跑成功。

为什么要解耦：``daily_nav`` 只有 10 行，缺 2026-08-07、08-10、08-17 三个交易日
（覆盖率 77%）。根因不是写入逻辑坏了，而是 ``_save_step4_nav_snapshot`` 挂在
Step4 的工单写入之后（workflows/step4_results.py:54-59）——只要漏斗没跑或 Step4
中断，当天净值就永久缺失。实测 08-10 是 ``FUNNEL_DATA_FRESHNESS_HARD_FAIL`` 拦下
（那是**故意的护栏**，数据不新鲜宁可整条失败），08-07 与 08-17 压根没触发。

但净值是**账户事实**，与「今天有没有生成工单」无关。信号链路可以失败，账户估值不该
跟着消失——否则你永远无法自证盈亏，而信号级统计既不含仓位也不含现金拖累与交易成本。

沿用 Step4 里那条已想清楚的原则：净值记账户**当前真实状态**，不记 OMS「假设你照单
执行后」的模拟现金。工单未被执行时两者会持续背离。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NavSnapshotResult:
    ok: bool
    portfolio_id: str
    trade_date: str
    total_equity: float | None = None
    free_cash: float | None = None
    positions_value: float | None = None
    message: str = ""
    written: bool = False


def build_nav_snapshot(portfolio_id: str, trade_date: str) -> NavSnapshotResult:
    """按最新行情重估持仓，返回当日净值快照（不写库）。"""
    from core.portfolio_valuation import PortfolioValuationError, calculate_portfolio_valuation
    from integrations.portfolio_market_value import load_portfolio_marks
    from integrations.supabase_base import create_admin_client
    from integrations.supabase_portfolio import load_portfolio_state, portfolio_tickflow_key

    if not trade_date:
        return NavSnapshotResult(False, portfolio_id, trade_date, message="缺少 trade_date")
    try:
        client = create_admin_client()
        state = load_portfolio_state(portfolio_id, client=client)
    except Exception as exc:
        return NavSnapshotResult(False, portfolio_id, trade_date, message=f"读取组合失败: {exc}")
    if state is None:
        return NavSnapshotResult(False, portfolio_id, trade_date, message=f"未找到组合 {portfolio_id}")

    positions = [row for row in (state.get("positions") or []) if int(row.get("shares", 0) or 0) > 0]
    free_cash = float(state.get("free_cash", 0.0) or 0.0)
    if not positions:
        # 空仓也要记：净值曲线不能因为清仓而断档。
        return NavSnapshotResult(True, portfolio_id, trade_date, free_cash, free_cash, 0.0, "空仓，净值等于现金")
    api_key = portfolio_tickflow_key(portfolio_id, client)
    if not api_key:
        return NavSnapshotResult(False, portfolio_id, trade_date, message="未配置 TickFlow API Key")
    try:
        prices, rates = load_portfolio_marks(positions, api_key)
        valuation = calculate_portfolio_valuation(free_cash, positions, prices, rates)
    except PortfolioValuationError as exc:
        # 行情缺失时**不写**部分估值：一个偏低的净值比没有净值更有害。
        return NavSnapshotResult(False, portfolio_id, trade_date, message=f"估值不完整: {exc}")
    except Exception as exc:
        return NavSnapshotResult(False, portfolio_id, trade_date, message=f"估值失败: {exc}")
    return NavSnapshotResult(
        True,
        portfolio_id,
        trade_date,
        valuation.total_equity,
        free_cash,
        valuation.positions_value,
        f"总权益 {valuation.total_equity:,.2f}（现金 {free_cash:,.2f}）",
    )


def persist_nav_snapshot(snapshot: NavSnapshotResult) -> NavSnapshotResult:
    """把快照写入 daily_nav；写入失败不抛异常，由调用方决定是否致命。"""
    from integrations.supabase_portfolio import upsert_daily_nav

    if not snapshot.ok or snapshot.total_equity is None:
        return snapshot
    written = upsert_daily_nav(
        portfolio_id=snapshot.portfolio_id,
        trade_date=snapshot.trade_date,
        free_cash=float(snapshot.free_cash or 0.0),
        total_equity=float(snapshot.total_equity),
        positions_value=float(snapshot.positions_value or 0.0),
    )
    if not written:
        logger.warning("[nav] 写入失败 portfolio=%s date=%s", snapshot.portfolio_id, snapshot.trade_date)
    return NavSnapshotResult(
        snapshot.ok,
        snapshot.portfolio_id,
        snapshot.trade_date,
        snapshot.total_equity,
        snapshot.free_cash,
        snapshot.positions_value,
        snapshot.message,
        written=written,
    )


def missing_nav_dates(portfolio_id: str, start: str, end: str) -> list[str]:
    """区间内缺失净值的交易日。用于发现空洞，不做回补。"""
    from integrations.fetch_a_share_csv import cached_trade_dates
    from integrations.supabase_base import create_admin_client

    client = create_admin_client()
    rows = (
        client.table("daily_nav")
        .select("trade_date")
        .eq("portfolio_id", portfolio_id)
        .gte("trade_date", start)
        .lte("trade_date", end)
        .execute()
        .data
        or []
    )
    have = {str(row.get("trade_date")) for row in rows}
    sessions = [str(day) for day in cached_trade_dates() if start <= str(day) <= end]
    return [day for day in sessions if day not in have]
