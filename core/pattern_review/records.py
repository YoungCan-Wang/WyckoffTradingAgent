from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

AI_RECOMMENDATION_ROLE = "AI推荐"
PATTERN_REVIEW_ROLE = "观察/信号复盘"


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y", "ai", "ai推荐"}
    return bool(value)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    """
    按顺序取第一个有值的列。

    存储层的列名与本记录的字段名不一致（表里是 initial_price / change_pct /
    mfe_pct / candidate_status），历史上这里按字段名直接 get，于是四个字段
    恒为 None —— 数据是满的，只是取错了列。

    保留旧名作为后备：万一别的写入路径或历史行用的是旧名，不要因为改名丢数据。
    全都没有时返回 None 而不是 0 —— 「涨跌 0%」和「没有数据」是两件事，
    填 0 就是编数字。
    """
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


@dataclass(frozen=True)
class PatternReviewRecord:
    code: str
    name: str
    recommend_date: str
    recommend_price: Any
    current_price: Any
    pnl_pct: Any
    max_pnl_pct: Any
    # 区间最大不利偏移（mae_pct）。有了它才能看出这只票的波动区间，
    # 只给最高值会让一只大起大落的票看起来和平稳上涨的一样。
    min_pnl_pct: Any
    camp: str
    status: str
    is_ai_recommended: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PatternReviewRecord:
        return cls(
            code=_as_text(row.get("code")),
            name=_as_text(row.get("name")),
            recommend_date=_as_text(row.get("recommend_date")),
            recommend_price=_first(row, "initial_price", "recommend_price"),
            current_price=row.get("current_price"),
            pnl_pct=_first(row, "change_pct", "pnl_pct"),
            # mfe/mae = maximum favorable/adverse excursion，即区间内最高/最低点。
            max_pnl_pct=_first(row, "mfe_pct", "max_pnl_pct"),
            min_pnl_pct=_first(row, "mae_pct", "min_pnl_pct"),
            camp=_as_text(row.get("camp")),
            status=_as_text(_first(row, "candidate_status", "status")),
            is_ai_recommended=_as_bool(row.get("is_ai_recommended")),
        )

    @property
    def entry_role(self) -> str:
        return AI_RECOMMENDATION_ROLE if self.is_ai_recommended else PATTERN_REVIEW_ROLE

    def to_tool_record(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "recommend_date": self.recommend_date,
            "recommend_price": self.recommend_price,
            "current_price": self.current_price,
            "pnl_pct": self.pnl_pct,
            "max_pnl_pct": self.max_pnl_pct,
            "min_pnl_pct": self.min_pnl_pct,
            "camp": self.camp,
            "status": self.status,
            "is_ai_recommended": self.is_ai_recommended,
            "entry_role": self.entry_role,
        }


def pattern_review_tool_records(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [PatternReviewRecord.from_row(row).to_tool_record() for row in rows]
