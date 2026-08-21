"""卖出建议归因：卖出后价格怎么走，以及分组对照。

背景（2026-08-17 首轮跑数，10 只票 / 20 次建议）：

- 全部 10 只票卖出后平均 +14.19%，同期上证 +2.01%，即**卖错约 13.7 个百分点**。
- 分组检验否掉了两个机制假设：
  - 「陈旧止损是主因」不成立：偏离 <5%（正常止损）卖后 +17.36%，偏离 >20%（陈旧）
    +23.84%，两档都严重卖错，陈旧只加剧约 6pct。
  - 「重复触发是主因」不成立：首次 +14.74%、重复 +17.71%，差仅 2.97pct，而首次本身
    就已经错了 14.74pct。
- 20 次建议里有 4 次集中在 2026-07-30 同一天，而那天是阶段底部（同期 CRASH 判定后
  T+5 +2.51%）。故更像**同一个市场时机误判的多次表现**，而非多次独立的判断失误。

因此本模块只做归因，不给改动建议：样本仅 10 只票、全部落在同一段 V 型行情，
撑不起修改风控代码。判据由持续积累的样本决定，而不是一次跑数的结论。

口径约定：``after_pct`` 为**卖出后**的价格变化，所以**负值表示卖对了**（避开了下跌），
正值表示卖早了。``excess_pct`` 扣掉同期基准，剔除市场整体涨跌。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

STALE_DEVIATION_PCT = 20.0
NEAR_TRIGGER_PCT = 5.0
_STOP_PATTERN = re.compile(r"inherit_pos_stop\(([\d.]+)\)")


@dataclass(frozen=True)
class ExitRecord:
    code: str
    name: str
    action: str
    trade_date: str
    price: float
    sequence: int
    origin: str = "模型判断"
    stop_loss: float | None = None
    after_pct: float | None = None
    benchmark_pct: float | None = None

    @property
    def is_first(self) -> bool:
        return self.sequence == 1

    @property
    def stop_deviation_pct(self) -> float | None:
        """止损位相对建议价的偏离。正值=倒挂（止损高于现价），负值=正常。"""
        if self.stop_loss is None or self.price <= 0:
            return None
        return (self.stop_loss / self.price - 1.0) * 100.0

    @property
    def stop_band(self) -> str:
        deviation = self.stop_deviation_pct
        if deviation is None:
            return "无止损信息"
        if deviation < 0:
            return "止损低于现价"
        if deviation < NEAR_TRIGGER_PCT:
            return f"倒挂 0~{NEAR_TRIGGER_PCT:.0f}%"
        if deviation < STALE_DEVIATION_PCT:
            return f"倒挂 {NEAR_TRIGGER_PCT:.0f}~{STALE_DEVIATION_PCT:.0f}%"
        return f"陈旧 >{STALE_DEVIATION_PCT:.0f}%"

    @property
    def excess_pct(self) -> float | None:
        if self.after_pct is None or self.benchmark_pct is None:
            return None
        return self.after_pct - self.benchmark_pct


@dataclass
class GroupStat:
    label: str
    count: int
    after_pct: float | None
    excess_pct: float | None
    sold_correctly: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "n": self.count,
            "after_pct": None if self.after_pct is None else round(self.after_pct, 4),
            "excess_pct": None if self.excess_pct is None else round(self.excess_pct, 4),
            "sold_correctly": self.sold_correctly,
        }


@dataclass
class ExitAttribution:
    records: list[ExitRecord] = field(default_factory=list)
    overall: GroupStat | None = None
    by_origin: list[GroupStat] = field(default_factory=list)
    by_sequence: list[GroupStat] = field(default_factory=list)
    by_stop_band: list[GroupStat] = field(default_factory=list)


def parse_stop_loss(reason: str) -> float | None:
    match = _STOP_PATTERN.search(str(reason or ""))
    return float(match.group(1)) if match else None


def classify_origin(reason: str) -> str:
    text = str(reason or "")
    if "系统强制止损" in text or "system_stop_breach_override" in text:
        return "系统强制止损"
    if "已穿止损" in text or "inherit_pos_stop" in text:
        return "止损驱动"
    return "模型判断"


def _stat(label: str, records: list[ExitRecord]) -> GroupStat:
    after = [r.after_pct for r in records if r.after_pct is not None]
    excess = [r.excess_pct for r in records if r.excess_pct is not None]
    return GroupStat(
        label=label,
        count=len(records),
        after_pct=mean(after) if after else None,
        excess_pct=mean(excess) if excess else None,
        # after_pct < 0 表示卖出后继续下跌，即这一笔卖对了。
        sold_correctly=sum(1 for value in after if value < 0),
    )


def build_attribution(records: list[ExitRecord]) -> ExitAttribution:
    report = ExitAttribution(records=list(records))
    if not records:
        return report
    report.overall = _stat("全部", records)
    report.by_origin = [
        _stat(origin, [r for r in records if r.origin == origin]) for origin in sorted({r.origin for r in records})
    ]
    first = [r for r in records if r.is_first]
    repeat = [r for r in records if not r.is_first]
    report.by_sequence = [stat for stat in (_stat("首次触发", first), _stat("重复触发", repeat)) if stat.count]
    report.by_stop_band = [
        _stat(band, [r for r in records if r.stop_band == band]) for band in sorted({r.stop_band for r in records})
    ]
    return report


def as_report_dict(report: ExitAttribution) -> dict[str, Any]:
    """归因结果的可序列化形式，供落盘与飞书卡片使用。"""
    return {
        "records": len(report.records),
        "codes": len({r.code for r in report.records}),
        "overall": None if report.overall is None else report.overall.as_dict(),
        "by_origin": [stat.as_dict() for stat in report.by_origin],
        "by_sequence": [stat.as_dict() for stat in report.by_sequence],
        "by_stop_band": [stat.as_dict() for stat in report.by_stop_band],
        "reading": (
            "after_pct 为卖出后价格变化：负值=卖对了（避开下跌），正值=卖早了。"
            "excess_pct 已扣同期基准。样本不足时不要据此改风控。"
        ),
    }
