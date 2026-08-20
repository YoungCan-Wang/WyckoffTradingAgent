"""飞书卡片不支持表格，必须拍平；拍平后要可读，且不能破坏其他报告的表形。"""

from __future__ import annotations

from utils.feishu_text import normalize_lark_md

CANDIDATE_TABLE = """| # | 代码 | 名称 | 分数 | 最新收盘 | 触发 |
| ---: | --- | --- | ---: | ---: | --- |
| 1 | RDAC.US | Rising Dragon | 1737.67 | 8.780 | SOS（量价点火） |
| 2 | AZI.US | Autozi | 7.89 | 1.660 | SOS（量价点火） |
"""


def test_index_and_name_become_bold_title() -> None:
    """回归：原先每行都重复表头，读起来是 `#: 1，代码: X，名称: Y，分数: Z` 一大坨。"""
    out = normalize_lark_md(CANDIDATE_TABLE)

    assert "- **1. RDAC.US · Rising Dragon**" in out
    assert "#: 1" not in out
    assert "代码: RDAC.US" not in out


def test_metrics_move_to_indented_second_line() -> None:
    out = normalize_lark_md(CANDIDATE_TABLE)

    assert "  分数: 1737.67 | 最新收盘: 8.780 | 触发: SOS（量价点火）" in out


def test_placeholder_row_collapses_to_single_note() -> None:
    """空表占位行 `| - | - | - | 本次无候选 |` 不该渲染成一串 '-'。"""
    md = """| # | 代码 | 名称 | 分数 | 最新收盘 | 触发 |
| ---: | --- | --- | ---: | ---: | --- |
| - | - | - | - | - | 本次无买点确认候选 |
"""

    out = normalize_lark_md(md)

    assert "- 本次无买点确认候选" in out
    assert "- - -" not in out


def test_wide_table_keeps_all_columns() -> None:
    """funnel_delivery 有 11 列，拍平不能丢字段。"""
    md = """| Rank | Code | Name | Action | AI | Funnel | Quality | Shadow | Entry | Label | Risks |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 002648 | 卫星化学 | PROBE | yes | 88 | A | pass | 25.3 | 主线 | 无 |
"""

    out = normalize_lark_md(md)

    assert "**1. 002648 · 卫星化学**" in out
    for field in ("Action: PROBE", "AI: yes", "Funnel: 88", "Quality: A", "Entry: 25.3", "Risks: 无"):
        assert field in out, field


def test_table_without_index_or_identity_keeps_legacy_single_line() -> None:
    """无序号无标识列的表保持旧格式不动。

    这类表（signal_feedback 的 Grade 表、筛选概览的环节/数量、市场闸门表）通常 2~4 列、
    本来一行就读完，改它没有收益；硬造空标题行还会多出 "- -" 噪音。
    """
    md = """| Grade | Ready | Hit rate | Payoff |
| --- | ---: | ---: | ---: |
| A | 120 | 34.2% | 0.85 |
"""

    out = normalize_lark_md(md)

    assert "- Grade: A，Ready: 120，Hit rate: 34.2%，Payoff: 0.85" in out
    assert "**" not in out


def test_trend_watch_table_has_no_name_column() -> None:
    """只有代码没有名称时，标题就是代码本身。"""
    md = """| # | 代码 | 分数 | 20日 | 60日 | 120日 | 风险 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | TEM.US | 3.78 | 12.50% | 30.10% | 55.20% | 高波动 |
"""

    out = normalize_lark_md(md)

    assert "- **1. TEM.US**" in out
    assert "风险: 高波动" in out


def test_missing_trailing_cells_do_not_crash() -> None:
    md = """| # | 代码 | 名称 | 分数 |
| ---: | --- | --- | ---: |
| 1 | AAA.US |
"""

    out = normalize_lark_md(md)

    assert "AAA.US" in out


def test_non_table_content_is_untouched() -> None:
    md = "## 概览\n- 命中: 87\n\n正文一行。\n"

    out = normalize_lark_md(md)

    assert "命中: 87" in out
    assert "正文一行。" in out
