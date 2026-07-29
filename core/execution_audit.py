"""识别「反复发出但从未被执行」的离场工单。

OMS 每个交易日按当前持仓重新推导一次建议，持仓没变就会得到同一条 EXIT。正常情况下
第二天该仓位已经不在了，工单自然消失；如果同一代码连着多天出现在离场清单里，说明
建议没有落地，而这段时间恰恰是止损本该生效的时间。生产上出现过同一只票连发 12 天
EXIT、期间又跌 19% 的情况，靠人读每日推送发现不了。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

EXIT_ACTIONS = frozenset({"EXIT", "TRIM"})
INACTIVE_STATUSES = frozenset({"CANCELLED", "CANCELED", "NO_TRADE"})
DEFAULT_ALERT_DAYS = 2
# 收盘价与理论跌停价的允许误差，覆盖交易所四舍五入到分的规则。
LIMIT_PRICE_TOLERANCE = 0.011


def unsellable_dates(
    bars: Sequence[tuple[str, float, float, float]],
    *,
    limit_pct: float = 0.10,
) -> set[str]:
    """找出「一字跌停」的交易日——这些天想卖也卖不掉，不该算作拖延。

    判据是全天最高价从未离开过跌停价。仅仅收在跌停不算：盘中若有高于跌停价的成交，
    就存在卖出窗口（生产上昊华 07-20 收于跌停，但盘中最高 48.65 远高于跌停价 42.19）。

    bars 为 (date, prev_close, high, low) 序列，用前复权价即可——除权跳空已被复权抹平，
    涨跌幅比例保持连续。
    """
    out: set[str] = set()
    for day, prev_close, high, low in bars:
        if prev_close <= 0:
            continue
        limit_price = prev_close * (1.0 - limit_pct)
        if high <= limit_price + LIMIT_PRICE_TOLERANCE and low <= limit_price + LIMIT_PRICE_TOLERANCE:
            out.add(str(day))
    return out


@dataclass(frozen=True)
class StaleExit:
    code: str
    name: str
    days: int
    since: str
    action: str

    @property
    def is_severe(self) -> bool:
        return self.days >= 3


def _active(row: dict) -> bool:
    return str(row.get("status", "") or "").strip().upper() not in INACTIVE_STATUSES


def find_unexecuted_exits(
    orders: Sequence[dict],
    held_codes: Iterable[str],
    *,
    min_days: int = DEFAULT_ALERT_DAYS,
    unsellable_by_code: Mapping[str, set[str]] | None = None,
) -> list[StaleExit]:
    """返回仍在持仓、且连续多个运行日都被建议离场的标的，按拖延天数降序。

    「连续」按 OMS 实际跑过的日期算，不按自然日；跳过的日期（停机、节假日）不会
    打断计数，否则一次漏跑就会把告警清零。

    `unsellable_by_code` 给出各标的一字跌停的日期，这些天不计入拖延天数——卖不掉
    是市场机制，不是执行纪律问题。但它们也不打断连续性，否则中间夹一个跌停板就能
    把前面十天的拖延洗掉。
    """
    active = [row for row in orders if _active(row)]
    run_dates = sorted({str(row.get("trade_date", "") or "") for row in active if row.get("trade_date")})
    if not run_dates:
        return []

    held = {str(code).strip() for code in held_codes}
    blocked = unsellable_by_code or {}
    exits: dict[str, dict[str, dict]] = {}
    for row in active:
        if str(row.get("action", "") or "").strip().upper() not in EXIT_ACTIONS:
            continue
        code = str(row.get("code", "") or "").strip()
        if code in held:
            exits.setdefault(code, {})[str(row.get("trade_date", ""))] = row

    out: list[StaleExit] = []
    for code, by_date in exits.items():
        sealed = blocked.get(code, set())
        streak = 0
        span = 0
        for date in reversed(run_dates):
            if date not in by_date:
                break
            span += 1
            if date not in sealed:
                streak += 1
        if streak < min_days:
            continue
        since = run_dates[len(run_dates) - span]
        latest = by_date[run_dates[-1]]
        out.append(
            StaleExit(
                code=code,
                name=str(latest.get("name", "") or code),
                days=streak,
                since=since,
                action=str(latest.get("action", "") or "EXIT").strip().upper(),
            )
        )
    return sorted(out, key=lambda s: (-s.days, s.code))


def stop_breached_codes(
    stale: Sequence[StaleExit],
    stop_by_code: Mapping[str, float | None],
    price_by_code: Mapping[str, float],
) -> frozenset[str]:
    """从拖延清单里挑出「现价已在止损线下方」的那些。

    没落袋的止盈、酌情减仓与真正的止损击穿是两回事：前者拖着只是少赚，后者拖着是
    风控失效。只有后者才该触发买入闸门。
    """
    out = set()
    for item in stale:
        stop = stop_by_code.get(item.code)
        price = price_by_code.get(item.code)
        if stop and price and price < float(stop):
            out.add(item.code)
    return frozenset(out)


def render_stale_exit_alert(stale: Sequence[StaleExit]) -> list[str]:
    """渲染成工单里的告警段落；无拖延时返回空列表，不占版面。"""
    if not stale:
        return []
    severe = [s for s in stale if s.is_severe]
    head = "🚨 未执行的离场工单" if severe else "⚠️ 离场工单尚未执行"
    lines = ["", f"{head}（共 {len(stale)} 只）"]
    for s in stale:
        lines.append(f"- {s.name}({s.code}) {s.action} 已连续建议 {s.days} 个交易日，自 {s.since} 起未落地")
    if severe:
        lines.append("止损只有成交才算数。请核对券商账户，并用 `wyckoff portfolio fill` 回填实际成交。")
    return lines
