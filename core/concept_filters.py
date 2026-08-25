"""Concept/theme filters for actionable A-share theme signals."""

from __future__ import annotations

from core.cn_boards import is_supported_cn_board

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
    """非 A 股交易板段的 6 位代码即视为场内基金（ETF/LOF）。

    原实现枚举 ETF 号段（159/51/56），漏了科创 50 ETF 的 588 段——实测 588000 会被判成
    普通股票混进用户面卡片。号段黑名单每新增一类基金就得补一次，因此改成复用
    ``is_supported_cn_board`` 的白名单取反：A 股板段（600/601/603/605/000/001/002/003/
    300/301/688/689 与北交所）之外的 6 位代码，在展示层一律当基金处理。
    """
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) < 6:
        return False
    return not is_supported_cn_board(digits[:6])


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
