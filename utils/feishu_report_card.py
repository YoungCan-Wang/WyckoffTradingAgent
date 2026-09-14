"""Generic Feishu card layout for Markdown reports without a dedicated renderer."""

from __future__ import annotations

import re

from utils.feishu_text import lark_md_div, lark_note

_BOLD_HEADING = re.compile(r"^\*\*(.+)\*\*$")
_TODAY_CONCLUSION = re.compile(r"今日结论\*{0,2}\s*[:：]\s*([^|\n]+)")
_WARNING_PREFIXES = ("⚠️", "风险提醒", "风险：", "注意：")

# 市场风险词：无论出现在标题还是正文，都代表一个真实的风险结论，判红。
_MARKET_RISK_WORDS = ("禁止", "BLACK_SWAN", "CRASH", "RISK_OFF")
# 运维异常词：只能说明"某个环节降级了"，不等于市场有风险，所以按位置区分权重（见下）。
_OPS_ALERT_WORDS = ("失败", "异常")
_WARN_WORDS = ("警告", "跳过", "WATCH", "复核", "PANIC_REPAIR")
_DONE_WORDS = ("完成", "成功", "通过", "BUY-APPROVED")
_ANALYSIS_WORDS = ("研报", "复盘", "雷达", "诊断", "分析")


def report_card_template(title: str, content: str) -> str:
    """按语义选卡片头部色。

    运维异常词按位置分权重，这是这个函数唯一不直观的地方：
    出现在**标题**里，说明这张卡本身就是失败告警（"定时任务失败"），判红；
    只出现在**正文**里，说明报告正常发出来了、只是内部有环节降级，判橙。

    原实现对全文一视同仁地判红，于是 `step3_reporting._build_final_content` 末尾
    一句 `**获取失败**: 600000(timeout)`，或 `_compact_rag_preview` 按 keep 规则
    保留的一行「拉取异常」，就足以把一份行情完全正常的研报刷成红卡。30 只标的里
    有一只取数超时是常态，结果是红色不再意味着风险——颜色这条信号直接废掉。
    降级只影响"正文含异常、且无市场风险词、标题也没说失败"这一类卡（红→橙），
    不会削弱真正的失败告警：仓内没有任何通知把 失败/异常 只写在正文里。
    """
    title_text = str(title or "").upper()
    text = f"{title}\n{content}".upper()
    conclusion = _TODAY_CONCLUSION.search(str(content or ""))
    if conclusion:
        # 显式写出的「今日结论」是作者下的判断，不是散落的字眼，此处仍按原语义全权重处理。
        decision = conclusion.group(1).upper()
        if any(word in decision for word in ("禁止", "失败", "异常")):
            return "red"
        if any(word in decision for word in ("观察", "复核", "待审")):
            return "orange"
        if any(word in decision for word in ("开放", "可执行", "通过")):
            return "green"
    if any(word in text for word in _MARKET_RISK_WORDS):
        return "red"
    if any(word in title_text for word in _OPS_ALERT_WORDS):
        return "red"
    if any(word in text for word in _OPS_ALERT_WORDS + _WARN_WORDS):
        return "orange"
    if any(word in text for word in _DONE_WORDS):
        return "green"
    if any(word in text for word in _ANALYSIS_WORDS):
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
    if "一眼结论" in text:
        return "🚦"
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
