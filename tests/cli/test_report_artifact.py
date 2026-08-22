"""save_report：把报告落盘并作为产物事件送到桌面端。

为什么要这个工具：桌面端原来靠 `looksLikeReport()` 猜（>400 字且含标题或表格）。
「这是不是一份报告」该由产出它的人声明，不该由读者事后猜。

落盘同时解决了「产物跨会话持久化」的一半 —— 报告本来就该是文件。
"""

from __future__ import annotations

import pytest

from cli.ipc.session import _ARTIFACT_TOOLS, _chat_artifact
from integrations import report_store as _store


@pytest.fixture
def reports(tmp_path, monkeypatch):
    """报告目录的 owner 是存储层。

    不打 `cli.ipc.artifacts.REPORTS_DIR`：那里已经不再持有常量。两处各持一份时
    monkeypatch 只改到一份，于是「测试看起来在改路径、实际读的还是真实家目录」。
    而且 agents/ 不允许依赖 cli/（架构边界测试守着），工具必须从库层拿路径。
    """
    root = tmp_path / "reports"
    monkeypatch.setattr(_store, "REPORTS_DIR", root)
    return root


def _event(**overrides):
    event = {
        "type": "tool_result",
        "name": "save_report",
        "args": {"title": "600519 结构解读", "markdown": "# 标题\n\n正文"},
        "result": {"saved": True, "path": "20260822-1-x.md"},
        "tool_call_id": "c3",
        "status": "ok",
    }
    event.update(overrides)
    return event


def test_save_report_is_a_report_artifact():
    assert _ARTIFACT_TOOLS["save_report"] == "report"
    assert _chat_artifact(_event())["kind"] == "report"


def test_body_comes_from_args_not_result():
    """工具返回值刻意不含正文 —— 那会把刚写的报告回灌进模型上下文。"""
    out = _chat_artifact(_event())
    assert out["payload"]["body"] == "# 标题\n\n正文"


def test_path_comes_from_result_so_the_report_can_be_reopened():
    """path 是落盘后才知道的；带上它，关掉页签后能从报告库找回同一份。"""
    assert _chat_artifact(_event())["payload"]["path"] == "20260822-1-x.md"


def test_missing_title_or_body_is_not_an_artifact():
    assert _chat_artifact(_event(args={"title": "x"})) is None
    assert _chat_artifact(_event(args={"markdown": "y"})) is None


def test_write_failure_becomes_a_failed_artifact():
    out = _chat_artifact(_event(status="error", result={"error": "写入失败"}))
    assert out["status"] == "failed"


def test_report_is_written_to_disk(reports):
    from agents.report_artifact_tools import save_report

    out = save_report("测试 报告", "# 内容\n\n正文")
    assert out["saved"] is True
    written = reports / out["path"]
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "# 内容\n\n正文"


def test_unsafe_characters_do_not_escape_the_directory(reports):
    """标题来自模型，可能带路径分隔符。"""
    from agents.report_artifact_tools import save_report

    out = save_report("../../etc/passwd", "正文")
    assert out["saved"] is True
    assert "/" not in out["path"] and ".." not in out["path"]
    assert (reports / out["path"]).resolve().parent == reports.resolve()


def test_same_title_twice_does_not_overwrite(reports):
    """报告是某个时点的判断；覆盖旧的等于把判断历史抹掉。

    秒级时间戳不够：同一轮里连着存两份（模型分开写多只票）会落在同一秒。
    """
    from agents.report_artifact_tools import save_report

    first = save_report("同名", "第一版")
    second = save_report("同名", "第二版")
    assert first["path"] != second["path"]
    assert (reports / first["path"]).read_text(encoding="utf-8") == "第一版"
    assert (reports / second["path"]).read_text(encoding="utf-8") == "第二版"


def test_oversized_report_is_rejected_with_actionable_advice(reports):
    from agents.report_artifact_tools import MAX_REPORT_BYTES, save_report

    out = save_report("大", "x" * (MAX_REPORT_BYTES + 1))
    assert "error" in out
    # 错误信息要能让下一次尝试成功，而不只是说「失败了」
    assert "只写结论" in out["error"]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_inputs_are_rejected(blank, reports):
    from agents.report_artifact_tools import save_report

    assert "error" in save_report(blank, "正文")
    assert "error" in save_report("标题", blank)
