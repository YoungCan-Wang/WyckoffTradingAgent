from __future__ import annotations

from utils.feishu_text import annotate_financial_terms, normalize_lark_md, split_lark_md


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
    # 带序号/代码列的表改成「标题行 + 指标尾」，不再逐格重复表头（见 test_feishu_table_flatten）。
    assert "- **1. 01336.HK**" in result
    assert "分数: 0.71" in result
    assert "普通说明文字保持不变" in result
    # 无序号无标识列的表（触发分布）保持原单行格式。
    assert "- 触发: LPS（缩量回踩），数量: 11" in result


def test_normalize_lark_md_ignores_non_table_pipe_lines():
    content = "命令: `a | b`"
    result = normalize_lark_md(content)
    assert result == content


def test_split_lark_md_keeps_every_tracking_name_across_chunks():
    names = [f"{idx:06d} 形态股{idx:02d}" for idx in range(1, 33)]
    content = "**【🧾 今日形态入表观察】32 只**\n\n" + "\n".join(f"  {name}  A+C  分80.00" for name in names)
    chunks = split_lark_md(content, max_len=280)
    assert len(chunks) > 1
    combined = "".join(chunks)
    assert all(name in combined for name in names)


def test_split_lark_md_never_cuts_a_line_in_half():
    """漏斗名单是 30+ 行无空行的一个段落，切分必须落在行边界上。

    只断言"数据没丢"是不够的：硬切同样不丢数据，但会在卡片上留下
    「…现价12.」/「34 起跳板结构…」这种断头行。
    """
    lines = [f"  {idx:06d} 形态股{idx:02d}  A+C  分80.00  现价12.34  起跳板结构:A+C(2/3)" for idx in range(1, 33)]
    content = "**【🧾 今日形态入表观察】32 只**\n\n" + "\n".join(lines)
    chunks = split_lark_md(content, max_len=280)
    assert len(chunks) > 1
    emitted = [line for chunk in chunks for line in chunk.split("\n")]
    assert set(emitted) <= {"**【🧾 今日形态入表观察】32 只**", "", *lines}


def test_split_lark_md_falls_back_to_hard_cut_for_a_single_giant_line():
    """单行自身就超过上限时无处可切，只能硬切——但不能丢字符。"""
    chunks = split_lark_md("x" * 700, max_len=280)
    assert [len(chunk) for chunk in chunks] == [280, 280, 140]
    assert "".join(chunks) == "x" * 700


def test_annotate_financial_terms_only_glosses_capitalized_entry_and_target():
    """小写 entry/target 是英文普通词，注解它们纯属误伤；Stop-Loss 不受影响。"""
    assert annotate_financial_terms("- Target: 8.0%") == "- Target（目标位）: 8.0%"
    assert annotate_financial_terms("| Entry | Label |") == "| Entry（入场区） | Label |"
    assert annotate_financial_terms("skip: no log entry for target file") == "skip: no log entry for target file"
    assert annotate_financial_terms("stop-loss 已触发") == "Stop-Loss（止损位） 已触发"
