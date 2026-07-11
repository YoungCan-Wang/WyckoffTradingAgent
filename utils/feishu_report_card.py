"""Generic Feishu card layout for Markdown reports without a dedicated renderer."""

from __future__ import annotations

import re

from utils.feishu_text import lark_md_div, lark_note

_BOLD_HEADING = re.compile(r"^\*\*(.+)\*\*$")
_WARNING_PREFIXES = ("⚠️", "风险提醒", "风险：", "注意：")


def report_card_template(title: str, content: str) -> str:
    text = f"{title}\n{content}".upper()
    if any(word in text for word in ("失败", "异常", "禁止", "BLACK_SWAN", "CRASH", "RISK_OFF")):
        return "red"
    if any(word in text for word in ("警告", "跳过", "WATCH", "复核", "PANIC_REPAIR")):
        return "orange"
    if any(word in text for word in ("完成", "成功", "通过", "BUY-APPROVED")):
        return "green"
    if any(word in text for word in ("研报", "复盘", "雷达", "诊断", "分析")):
        return "purple"
    return "blue"


def build_report_card_elements(content: str) -> list[dict]:
    blocks = _content_blocks(content)
    elements: list[dict] = []
    for kind, text in blocks:
        if kind == "heading":
            if elements:
                elements.append({"tag": "hr"})
            elements.append(lark_md_div(f"{_section_icon(text)} **{text}**"))
        elif kind == "warning":
            elements.append(lark_note(text))
        elif kind == "intro":
            elements.append(_intro_panel(text))
        else:
            elements.append(lark_md_div(text))
    return elements or [lark_md_div("-")]


def _content_blocks(content: str) -> list[tuple[str, str]]:
    lines = str(content or "").splitlines()
    blocks: list[tuple[str, str]] = []
    body: list[str] = []
    seen_heading = False
    for raw in lines:
        text = raw.strip()
        heading = _heading_text(text)
        if heading:
            _flush_body(blocks, body, "body" if seen_heading else "intro")
            blocks.append(("heading", heading))
            seen_heading = True
        elif text.startswith(_WARNING_PREFIXES):
            _flush_body(blocks, body, "body" if seen_heading else "intro")
            blocks.append(("warning", text))
        else:
            body.append(raw)
    _flush_body(blocks, body, "body" if seen_heading else "intro")
    return blocks


def _heading_text(text: str) -> str:
    match = _BOLD_HEADING.fullmatch(text)
    return match.group(1).strip() if match else ""


def _flush_body(blocks: list[tuple[str, str]], body: list[str], kind: str) -> None:
    text = "\n".join(body).strip()
    body.clear()
    if text:
        blocks.append((kind, text))


def _intro_panel(text: str) -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [lark_md_div(text)],
            }
        ],
    }


def _section_icon(text: str) -> str:
    if any(word in text for word in ("风险", "失效", "逻辑破产", "SKIP", "禁止")):
        return "🔴"
    if any(word in text for word in ("BUY", "机会", "起跳板", "执行")):
        return "🎯"
    if any(word in text for word in ("WATCH", "观察", "储备", "待确认")):
        return "🟡"
    if any(word in text for word in ("持仓", "HOLD", "账户")):
        return "💼"
    if any(word in text for word in ("市场", "大盘", "水温", "主线")):
        return "📊"
    return "▎"
