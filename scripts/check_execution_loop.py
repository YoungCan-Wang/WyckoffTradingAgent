"""只读体检两条运维验收项：执行环闭合与市场信号就绪。

不写库、不发通知、不改配置。用于回答「这两条 P0 现在到底达标了没有」，
避免每次都手写临时脚本重踩同样的坑。

用法：
    python scripts/check_execution_loop.py
    python scripts/check_execution_loop.py --days 30 --portfolio-id USER_LIVE:<uuid>
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401

from core.execution_audit import find_unexecuted_exits
from integrations.supabase_base import create_admin_client

# premarket_regime 非空并不等于就绪：缺失值与真实判定都会被压成 UNKNOWN，
# 而 UNKNOWN 本身是禁买状态。判定必须排除它，否则「两字段非空」会给出假通过。
INVALID_PREMARKET = frozenset({"", "UNKNOWN"})
REQUIRED_STREAK = 10


def market_signal_row_ready(row: dict) -> bool:
    """当日行是否真的可用于 Step4 放行判定。

    与 `integrations.supabase_market_signal.market_signal_readiness` 的 ready 条件对齐，
    但只看本地已取到的两个字段，不做 trade_date 新鲜度校验（调用方已按日期取行）。
    """
    benchmark = str(row.get("benchmark_regime") or "").strip().upper()
    premarket = str(row.get("premarket_regime") or "").strip().upper()
    return bool(benchmark) and premarket not in INVALID_PREMARKET


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读体检执行环与市场信号就绪度")
    parser.add_argument("--days", type=int, default=30, help="market_signal_daily 回看天数")
    parser.add_argument("--portfolio-id", default="", help="留空则自动选用工单最多的组合")
    return parser.parse_args()


def _resolve_portfolio_id(client, explicit: str) -> str:
    if explicit:
        return explicit
    rows = client.table("trade_orders").select("portfolio_id").order("trade_date", desc=True).limit(200).execute()
    counts: dict[str, int] = {}
    for row in rows.data or []:
        pid = str(row.get("portfolio_id") or "").strip()
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    if not counts:
        return ""
    # 组合主键形如 USER_LIVE:<uuid>，裸 USER_LIVE 是另一条早期遗留记录，不能混用
    return max(counts.items(), key=lambda kv: kv[1])[0]


def check_market_signal(client, days: int) -> bool:
    print("=" * 78)
    print(f"[1/2] market_signal_daily 就绪度（近 {days} 天）")
    print("=" * 78)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = (
        client.table("market_signal_daily")
        .select("trade_date,premarket_regime,benchmark_regime")
        .gte("trade_date", cutoff)
        .order("trade_date")
        .execute()
    )
    data = list(rows.data or [])
    if not data:
        print("无记录，无法判定")
        return False

    weekdays = "一二三四五六日"
    print(f"{'trade_date':12s}{'周':>4s}{'premarket':>12s}{'benchmark':>14s}{'判定':>8s}")
    streak = 0
    for row in data:
        day = date.fromisoformat(str(row.get("trade_date"))[:10])
        pre = str(row.get("premarket_regime") or "").strip().upper()
        bench = str(row.get("benchmark_regime") or "").strip().upper()
        ready = market_signal_row_ready(row)
        streak = streak + 1 if ready else 0
        print(
            f"{day.isoformat():12s}{weekdays[day.weekday()]:>4s}{pre or '-':>12s}"
            f"{bench or '-':>14s}{'OK' if ready else '未就绪':>8s}"
        )

    print(f"\n当前连续就绪 = {streak} 个交易日（门槛 {REQUIRED_STREAK}）")
    print("判定口径：benchmark_regime 非空，且 premarket_regime 非空且不为 UNKNOWN")
    _report_latest_gap(data)
    return streak >= REQUIRED_STREAK


def _report_latest_gap(data: list[dict]) -> None:
    """最后一行未就绪时，区分「排期未到点」与「真实缺失」。

    漏斗 cron 为 '17 9 * * 0-4'（周日至周四，北京 17:17），而 resolve_trading_window 取
    ≤ 触发日的最近交易日——周日触发写的是上一个周五。五个交易日都有对应运行，不存在
    按周几的结构性缺口；最新一行为空通常只是写它的那次运行还没跑。
    """
    latest = data[-1]
    if market_signal_row_ready(latest):
        return
    day = date.fromisoformat(str(latest.get("trade_date"))[:10])
    writer = "当日" if day.weekday() != 4 else "随后那个周日"
    print(f"最新一行 {day.isoformat()} 未就绪；写入它的是{writer} 17:17（北京）的漏斗运行。")
    print("  先确认该次运行是否已到点或已成功，再判定为故障。")


def check_execution_loop(client, portfolio_id: str) -> bool:
    print("\n" + "=" * 78)
    print(f"[2/2] 执行环闭合（portfolio_id={portfolio_id or '未找到'}）")
    print("=" * 78)
    if not portfolio_id:
        print("无工单记录，无法判定")
        return False
    orders = list(
        client.table("trade_orders")
        .select("code,name,action,status,trade_date")
        .eq("portfolio_id", portfolio_id)
        .order("trade_date", desc=True)
        .limit(400)
        .execute()
        .data
        or []
    )
    positions = list(
        client.table("portfolio_positions")
        .select("code,name,shares")
        .eq("portfolio_id", portfolio_id)
        .limit(200)
        .execute()
        .data
        or []
    )
    held = [str(p.get("code") or "").strip() for p in positions if str(p.get("code") or "").strip()]
    run_dates = sorted({str(o.get("trade_date"))[:10] for o in orders if o.get("trade_date")})
    print(f"持仓 {len(held)} 只: {', '.join(sorted(held)) or '-'}")
    print(
        f"工单 {len(orders)} 条，覆盖运行日 {len(run_dates)} 个"
        + (f": {run_dates[0]} .. {run_dates[-1]}" if run_dates else "")
    )

    stale = find_unexecuted_exits(orders, held)
    if stale:
        print(f"\n发现 {len(stale)} 只连续被建议离场但仍在持仓:")
        for item in stale:
            severe = " 【严重 ≥3日】" if item.is_severe else ""
            print(f"  {item.code} {item.name}: {item.action} 连续 {item.days} 运行日，自 {item.since}{severe}")
        return False
    print("\n无连续未执行的离场工单 → 当前无告警")
    _report_closed_orders(orders, set(held))
    return True


def _report_closed_orders(orders: list[dict], held: set[str]) -> None:
    """已离场代码仅供人工核对，不是告警：不在持仓即视为工单已执行。"""
    closed: dict[str, list[str]] = {}
    for row in orders:
        code = str(row.get("code") or "").strip()
        if code in held or str(row.get("action") or "").strip().upper() not in {"EXIT", "TRIM"}:
            continue
        closed.setdefault(code, []).append(str(row.get("trade_date"))[:10])
    if not closed:
        return
    print("\n历史离场单已不在持仓（视为已执行，仅供核对）:")
    for code, days in sorted(closed.items()):
        print(f"  {code}: {len(days)} 条，{min(days)} .. {max(days)}")


def main() -> int:
    args = _parse_args()
    client = create_admin_client()
    print(f"体检时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    signal_ok = check_market_signal(client, args.days)
    loop_ok = check_execution_loop(client, _resolve_portfolio_id(client, args.portfolio_id))
    print("\n" + "=" * 78)
    print(f"市场信号就绪: {'达标' if signal_ok else '未达标'}    执行环闭合: {'无告警' if loop_ok else '有告警'}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
