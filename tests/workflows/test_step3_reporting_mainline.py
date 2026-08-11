from workflows.step3_reporting import _compact_rag_preview


def test_rag_preview_keeps_only_execution_summary():
    preview = _compact_rag_preview(
        "## RAG 防雷\n- 扫描股票: 10\n- 新闻拉取成功: 10/10\n- 相关新闻覆盖: 10/10\n- veto 剔除: 1"
    )

    assert "扫描股票" in preview
    assert "新闻拉取成功" in preview
    assert "veto 剔除" in preview
    assert "相关新闻覆盖" not in preview
