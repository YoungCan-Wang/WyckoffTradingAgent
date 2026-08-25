"""Concept/theme filters for actionable A-share theme signals."""

from __future__ import annotations

_NOISE_EXACT = frozenset(
    {
        "昨日涨停",
        "昨日连板",
        "昨日触板",
        "注册制次新股",
        "新股与次新股",
        "科创次新股",
        "融资融券",
        "沪股通",
        "深股通",
        "北交所概念",
        "MSCI概念",
        "ST板块",
        "转债标的",
        "高管增持",
        "股权激励",
        "员工持股",
        "创业板重组松绑",
        "送转预期",
        "证金持股",
        "同花顺中特估100",
        "同花顺新质50",
        "超级品牌",
        "日经225",
        "纳指100",
        "标普500",
    }
)

_NOISE_KEYWORDS = (
    "同花顺",
    "证金",
    "沪股通",
    "深股通",
    "融资融券",
    "MSCI",
    "富时罗素",
    "标普",
    "纳指",
    "日经",
    "ETF",
)

_ETF_NAME_MARKERS = ("ETF", "黄金ETF", "粮食ETF")


def is_etf_code(code: str) -> bool:
    """A 股场内基金常见号段：159xxx / 51xxxx / 56xxxx。"""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) < 6:
        return False
    six = digits[:6]
    return six.startswith(("159", "51", "56"))


def is_etf_display_name(name: str) -> bool:
    upper = str(name or "").strip().upper()
    return any(marker.upper() in upper for marker in _ETF_NAME_MARKERS)


def is_user_facing_etf(code: str = "", name: str = "") -> bool:
    return is_etf_code(code) or is_etf_display_name(name) or is_etf_display_name(code)


def is_actionable_theme_name(name: str) -> bool:
    cleaned = str(name or "").strip()
    if not cleaned or cleaned in _NOISE_EXACT:
        return False
    if is_etf_display_name(cleaned):
        return False
    upper = cleaned.upper()
    return not any(keyword.upper() in upper for keyword in _NOISE_KEYWORDS)
