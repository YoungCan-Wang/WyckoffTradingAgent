"""Small text helpers shared by Feishu delivery and rich-card builders."""

from __future__ import annotations

import re

_TERM_GLOSSARY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bBLACK_SWAN\b(?!\s*[（(])"), "BLACK_SWAN（黑天鹅高风险）"),
    (re.compile(r"\bRISK_OFF\b(?!\s*[（(])"), "RISK_OFF（风险收缩）"),
    (re.compile(r"\bRISK_ON\b(?!\s*[（(])"), "RISK_ON（短线过热禁追）"),
    (re.compile(r"\bNORMAL\b(?!\s*[（(])"), "NORMAL（常态）"),
    (re.compile(r"\bPANIC_REPAIR_CONFIRMED\b(?!\s*[（(])"), "PANIC_REPAIR_CONFIRMED（修复成立）"),
    (re.compile(r"\bPANIC_REPAIR\b(?![_\s]*CONFIRMED)(?!\s*[（(])"), "PANIC_REPAIR（修复候选）"),
    (re.compile(r"\bVIX\b(?!\s*[（(])"), "VIX（波动率恐慌指数）"),
    (re.compile(r"\bA50\b(?!\s*[（(])"), "A50（富时中国A50期货）"),
    (re.compile(r"\bATR\b(?!\s*[（(])"), "ATR（真实波动幅度）"),
    (re.compile(r"\bRPS\b(?!\s*[（(])"), "RPS（相对强弱百分位）"),
    (re.compile(r"\bQPS\b(?!\s*[（(])"), "QPS（每秒请求量）"),
    (re.compile(r"\bATTACK\b(?!\s*[（(])"), "ATTACK（进攻建仓）"),
    (re.compile(r"\bPROBE\b(?!\s*[（(])"), "PROBE（试探建仓）"),
    (re.compile(r"\bTRIM\b(?!\s*[（(])"), "TRIM（减仓）"),
    (re.compile(r"\bHOLD\b(?!\s*[（(])"), "HOLD（持有观察）"),
    (re.compile(r"\bEXIT\b(?!\s*[（(])"), "EXIT（清仓离场）"),
    (re.compile(r"\bNO_TRADE\b(?!\s*[（(])"), "NO_TRADE（拒单）"),
    (re.compile(r"\bAPPROVED\b(?!\s*[（(])"), "APPROVED（核准执行）"),
    (re.compile(r"\bComposite Man\b(?!\s*[（(])"), "Composite Man（综合人/主力）"),
    (re.compile(r"\bTape Reading\b(?!\s*[（(])"), "Tape Reading（盘面解读）"),
    (re.compile(r"\bSpring\b(?!\s*[（(])"), "Spring（弹簧/假跌破）"),
    (re.compile(r"\bLPS\b(?!\s*[（(])"), "LPS（最后支撑点）"),
    (re.compile(r"\bSOS\b(?!\s*[（(])"), "SOS（强势信号）"),
    (re.compile(r"\bUTAD\b(?!\s*[（(])"), "UTAD（上冲诱多）"),
    (re.compile(r"\bEVR\b(?!\s*[（(])"), "EVR（放量不跌）"),
    (re.compile(r"\bJAC\b(?!\s*[（(])"), "JAC（跃过小溪）"),
    (re.compile(r"\bBUEC\b(?!\s*[（(])"), "BUEC（回踩小溪边缘）"),
    (re.compile(r"\bStop[- ]?Loss\b(?!\s*[（(])", re.IGNORECASE), "Stop-Loss（止损位）"),
    (re.compile(r"\bEntry\b(?!\s*[（(])", re.IGNORECASE), "Entry（入场区）"),
    (re.compile(r"\bTarget\b(?!\s*[（(])", re.IGNORECASE), "Target（目标位）"),
]


def annotate_financial_terms(content: str) -> str:
    """Add Chinese glosses for common trading terms when they are still bare."""
    if not content:
        return content
    out = content
    for pattern, replacement in _TERM_GLOSSARY_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _is_table_row(stripped: str) -> bool:
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _split_table_row(stripped: str) -> list[str]:
    return [cell.strip() for cell in stripped.strip("|").split("|")]


# 序号类列名：它们的值直接做行首标号，不重复打印列名（"#: 1" 这种噪音占了整行开头）。
_INDEX_HEADERS = frozenset({"#", "序号", "Rank", "rank", "No", "no", "No.", "排名"})
# 标识类列名：值本身自解释（代码/名称），做行首标题，不加"代码: "前缀。
_IDENTITY_HEADERS = frozenset(
    {"代码", "名称", "Code", "code", "Name", "name", "symbol", "Symbol", "标的", "概念", "Strategy", "策略"}
)


def _table_block_to_lines(header: list[str], rows: list[list[str]]) -> list[str]:
    """把 markdown 表格拍平成飞书卡片可读的行。

    飞书 lark_md 不支持表格，必须拍平。原实现把每格都写成 `列名: 值` 再用「，」连起来，
    于是 30 行候选每行都重复一遍表头，读起来是一大坨 `#: 1，代码: X，名称: Y，分数: Z`，
    在手机上会折成三四行。

    这里改成「标题行 + 指标尾」：序号与标识列（代码/名称）直接做行首并加粗，其余列才带列名。
    列宽差异很大（4~11 列，横跨 7 份报告），所以规则必须是通用的，不能对某张表硬编码。
    """
    out: list[str] = []
    index_cols = [i for i, name in enumerate(header) if name in _INDEX_HEADERS]
    identity_cols = [i for i, name in enumerate(header) if name in _IDENTITY_HEADERS]
    for row in rows:
        if _is_placeholder_row(row):
            out.append(f"- {_placeholder_text(row)}")
            continue
        lead = [row[i] for i in index_cols if i < len(row) and row[i] not in ("", "-")]
        names = [row[i] for i in identity_cols if i < len(row) and row[i] not in ("", "-")]
        rest = [
            f"{name}: {row[i]}"
            for i, name in enumerate(header)
            if i not in index_cols and i not in identity_cols and i < len(row) and row[i] != ""
        ]
        prefix = f"{lead[0]}. " if lead else ""
        title = " · ".join(names)
        if not title and not prefix:
            # 没有序号也没有标识列（如筛选概览的「环节/数量」、signal_feedback 的 Grade 表、
            # 市场闸门表）：保持原来的 `列名: 值，列名: 值` 单行格式不动。
            # 这类表通常只有 2~4 列、本来就一行读完，改它没有收益，只会让 diff 变大。
            # 硬造一个空标题行还会多出一条 "- -" 噪音。
            cells = [f"{name}: {row[idx]}" if idx < len(row) else f"{name}: -" for idx, name in enumerate(header)]
            out.append("- " + "，".join(cells))
            continue
        head = f"**{prefix}{title}**" if title else f"**{prefix.rstrip('. ')}**"
        out.append(f"- {head}" + (f"\n  {' | '.join(rest)}" if rest else ""))
    return out


def _is_placeholder_row(row: list[str]) -> bool:
    """空表占位行形如 `| - | - | - | 本次无候选 |`：全是 '-' 只剩一句说明。"""
    meaningful = [cell for cell in row if cell not in ("", "-")]
    return len(meaningful) <= 1 and len(row) > 1


def _placeholder_text(row: list[str]) -> str:
    meaningful = [cell for cell in row if cell not in ("", "-")]
    return meaningful[0] if meaningful else "-"


def _consume_table(lines: list[str], start: int) -> tuple[list[str], int]:
    header = _split_table_row(lines[start].strip())
    rows: list[list[str]] = []
    idx = start + 2
    while idx < len(lines) and _is_table_row(lines[idx].strip()):
        rows.append(_split_table_row(lines[idx].strip()))
        idx += 1
    return _table_block_to_lines(header, rows), idx


def normalize_lark_md(content: str) -> str:
    safe_content = content.replace("<", "&lt;").replace(">", "&gt;")
    lines = safe_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            out.append("")
            i += 1
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            out.append(f"**{title}**" if title else "")
            i += 1
            continue
        if stripped in {"---", "***", "___"}:
            out.append("")
            i += 1
            continue
        if _is_table_row(stripped) and i + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(lines[i + 1].strip()):
            table_lines, i = _consume_table(lines, i)
            out.extend(table_lines)
            continue
        out.append(line)
        i += 1
    return "\n".join(out).strip()


def split_lark_md(content: str, max_len: int = 2800) -> list[str]:
    if len(content) <= max_len:
        return [content]

    paragraphs = content.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= max_len:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + max_len])
            start += max_len
    if current:
        chunks.append(current)
    return chunks


def lark_md_div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def lark_note(content: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}
