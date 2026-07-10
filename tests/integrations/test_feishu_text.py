from __future__ import annotations

from utils.feishu_text import normalize_lark_md


def test_normalize_lark_md_converts_table_to_bullet_lines():
    content = "\n".join(
        [
            "## 筛选概览",
            "| 环节 | 数量 |",
            "| --- | ---: |",
            "| 股票池 | 2,708 |",
            "| 买点确认 | 15 |",
        ]
    )
    result = normalize_lark_md(content)
    assert "| --- | ---: |" not in result
    assert "| 股票池 | 2,708 |" not in result
    assert "- 环节: 股票池，数量: 2,708" in result
    assert "- 环节: 买点确认，数量: 15" in result


def test_normalize_lark_md_handles_multiple_tables_and_plain_text():
    content = "\n".join(
        [
            "## Top 候选",
            "| # | 代码 | 分数 |",
            "| ---: | --- | ---: |",
            "| 1 | 01336.HK | 0.71 |",
            "",
            "普通说明文字保持不变",
            "",
            "## 触发分布",
            "| 触发 | 数量 |",
            "| --- | ---: |",
            "| LPS（缩量回踩） | 11 |",
        ]
    )
    result = normalize_lark_md(content)
    assert "- #: 1，代码: 01336.HK，分数: 0.71" in result
    assert "普通说明文字保持不变" in result
    assert "- 触发: LPS（缩量回踩），数量: 11" in result


def test_normalize_lark_md_ignores_non_table_pipe_lines():
    content = "命令: `a | b`"
    result = normalize_lark_md(content)
    assert result == content
