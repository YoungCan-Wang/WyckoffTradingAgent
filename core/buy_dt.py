"""建仓日校验：只接受完整的 YYYYMMDD 或 YYYY-MM-DD，且必须是真实日历日。"""

from __future__ import annotations

from datetime import datetime

MISSING_BUY_DT_ERROR = "缺少建仓日 buy_dt，请询问用户后再写入"
INVALID_BUY_DT_ERROR = "buy_dt 必须是合法日期 YYYYMMDD 或 YYYY-MM-DD"
POSITION_MISSING_ERROR = "持仓不存在，无法 update；请改用 add 并提供建仓日 buy_dt"
POSITION_EXISTS_ERROR = "持仓已存在，无法 add；请改用 update"


def parse_buy_dt(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.strftime(fmt) == text:
            return parsed
    return None


def buy_dt_error(raw: str, *, required: bool) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return MISSING_BUY_DT_ERROR if required else None
    if parse_buy_dt(text) is None:
        return INVALID_BUY_DT_ERROR
    return None
