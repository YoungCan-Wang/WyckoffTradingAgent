from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
try:
    DAY_SWITCH_HOUR = int(os.getenv("MARKET_DATA_READY_HOUR", "16"))
except Exception:
    DAY_SWITCH_HOUR = 16


def is_a_share_trading_day(d: date | None = None) -> bool:
    """判断给定日期是否为 A 股交易日（基于 akshare 交易日历）。"""
    if d is None:
        d = datetime.now(CN_TZ).date()
    if d.weekday() >= 5:
        return False
    try:
        from integrations.fetch_a_share_csv import cached_trade_dates

        return d in set(cached_trade_dates())
    except Exception:
        return d.weekday() < 5


def resolve_end_calendar_day(
    now: datetime | None = None,
    switch_hour: int = DAY_SWITCH_HOUR,
) -> date:
    """
    日线目标日统一口径（北京时间）：
    - END_CALENDAR_DAY: 手动回放时锁定目标自然日
    - switch_hour(默认16):00 - 23:59 -> T（当天）
    - 00:00 - switch_hour(默认16):59 -> T-1（上一自然日）
    """
    raw = os.getenv("END_CALENDAR_DAY", "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            pass
    dt = now.astimezone(CN_TZ) if now else datetime.now(CN_TZ)
    if dt.hour >= int(switch_hour):
        return dt.date()
    return (dt - timedelta(days=1)).date()
