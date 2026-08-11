"""持仓代码规范化：A 股 6 位、港股 NNNNN.HK、美股 TICKER.US。"""

from __future__ import annotations

import re

_CN_RE = re.compile(r"^\d{6}$")
_HK_RE = re.compile(r"^(\d{1,5})\.HK$")
_US_BASE_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")


def normalize_portfolio_code(raw: str) -> str:
    """把用户/Agent 输入收成持仓账本规范码；无法识别时返回空串。

    不接受裸 1–5 位数字（会与残缺 A 股码混淆）；港股必须带 ``.HK``。
    裸美股 ticker（如 ``AAPL``）会补成 ``AAPL.US``。
    """
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if _CN_RE.fullmatch(text):
        return text
    hk = _HK_RE.fullmatch(text)
    if hk:
        return f"{hk.group(1).zfill(5)}.HK"
    if text.endswith(".US"):
        base = text[:-3]
        return text if _US_BASE_RE.fullmatch(base) else ""
    if _US_BASE_RE.fullmatch(text):
        return f"{text}.US"
    return ""


def is_supported_portfolio_code(code: str) -> bool:
    return bool(normalize_portfolio_code(code))


def is_cn_portfolio_code(code: str) -> bool:
    return bool(_CN_RE.fullmatch(str(code or "").strip().upper()))


def portfolio_name_conflict(code: str, provided_name: str, resolved_name: str) -> str | None:
    """仅当名册真正解析出不同于 code 的中文名，且与用户提供名不一致时才报错。

    港美代码通常不在 A 股名册里，``code_to_name`` 会回退成 code 本身；
    若把这种回退当成「真实名称」再比对，会误拒 ``06881.HK`` +「中国银河」。
    """
    provided = str(provided_name or "").strip()
    resolved = str(resolved_name or "").strip()
    if not provided or not resolved or resolved == code:
        return None
    if resolved == provided:
        return None
    return f"代码 {code} 对应的股票是「{resolved}」，而非「{provided}」，请确认代码或名称是否正确"
