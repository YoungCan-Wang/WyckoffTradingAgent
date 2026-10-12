"""星球会员状态判定与统一的「需要会员」提示文案。

判定口径与 web/packages/shared/src/planet-membership.ts 一致：`expires_on` 为空即长期有效，
否则按 Asia/Shanghai 的当天比较。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TABLE_PLANET_MEMBERS = "planet_members"
WEB_BASE = os.environ.get("WYCKOFF_WEB_BASE", "https://wyckoff-analysis.pages.dev").rstrip("/")
PLANET_MEMBERSHIP_URL = f"{WEB_BASE}/membership#capability-boundary"

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def membership_required_message(feature: str) -> str:
    return f"{feature}仅对有效星球会员开放（账号未开通或已到期），这不是暂无数据。了解并加入：{PLANET_MEMBERSHIP_URL}"


def is_membership_active(expires_on: Any, today: date | None = None) -> bool:
    text = str(expires_on or "").strip()
    if not text:
        return True
    if not _ISO_DATE.fullmatch(text):
        return False
    try:
        expires = date.fromisoformat(text)
    except ValueError:
        return False
    return expires >= (today or datetime.now(_SHANGHAI).date())


def is_active_planet_member(client: Any, user_id: str) -> bool | None:
    """True / False；查询本身失败返回 None。

    不确定时调用方应保持原有提示：RLS 对非会员返回空行，和「真的没有数据」同形，
    只有明确查到「不是有效会员」才能换成会员提示。
    """
    if not user_id:
        return None
    try:
        rows = client.table(TABLE_PLANET_MEMBERS).select("expires_on").eq("user_id", user_id).limit(1).execute().data
    except Exception:
        logger.warning("planet_members lookup failed", exc_info=True)
        return None
    if not isinstance(rows, list):
        return None
    if not rows:
        return False
    return is_membership_active(rows[0].get("expires_on"))
