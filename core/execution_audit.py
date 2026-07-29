"""识别「反复发出但从未被执行」的离场工单。

OMS 每个交易日按当前持仓重新推导一次建议，持仓没变就会得到同一条 EXIT。正常情况下
第二天该仓位已经不在了，工单自然消失；如果同一代码连着多天出现在离场清单里，说明
建议没有落地，而这段时间恰恰是止损本该生效的时间。生产上出现过同一只票连发 12 天
EXIT、期间又跌 19% 的情况，靠人读每日推送发现不了。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

EXIT_ACTIONS = frozenset({"EXIT", "TRIM"})
INACTIVE_STATUSES = frozenset({"CANCELLED", "CANCELED", "NO_TRADE"})
DEFAULT_ALERT_DAYS = 2


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
) -> list[StaleExit]:
    """返回仍在持仓、且连续多个运行日都被建议离场的标的，按拖延天数降序。

    「连续」按 OMS 实际跑过的日期算，不按自然日；跳过的日期（停机、节假日）不会
    打断计数，否则一次漏跑就会把告警清零。
    """
    active = [row for row in orders if _active(row)]
    run_dates = sorted({str(row.get("trade_date", "") or "") for row in active if row.get("trade_date")})
    if not run_dates:
        return []

    held = {str(code).strip() for code in held_codes}
    exits: dict[str, dict[str, dict]] = {}
    for row in active:
        if str(row.get("action", "") or "").strip().upper() not in EXIT_ACTIONS:
            continue
        code = str(row.get("code", "") or "").strip()
        if code in held:
            exits.setdefault(code, {})[str(row.get("trade_date", ""))] = row

    out: list[StaleExit] = []
    for code, by_date in exits.items():
        streak = 0
        for date in reversed(run_dates):
            if date not in by_date:
                break
            streak += 1
        if streak < min_days:
            continue
        since = run_dates[len(run_dates) - streak]
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
