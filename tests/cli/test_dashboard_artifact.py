"""可交互面板：落盘、读回、以及「不能当静态 html 打开」这条约束。

报告库的 `.html` 走 `sandbox=""` 的 iframe（不给 allow-scripts）。一个可交互面板
用那条路径打开会变成死页面 —— 按钮点不动、图表不画，而且**没有任何提示**。
静默降级比明确不支持更糟，所以面板用自己的后缀和 kind。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.ipc.artifacts import kind_for, list_artifacts, read_artifact
from cli.ipc.session import _ARTIFACT_TOOLS, _chat_artifact
from integrations import report_store as _store

INTERACTIVE = '<div id="v">a</div><button id="b">go</button><script>window.x=1</script>'


@pytest.fixture
def reports(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    monkeypatch.setattr(_store, "REPORTS_DIR", root)
    return root


def test_dashboard_is_registered_as_an_artifact_tool():
    assert _ARTIFACT_TOOLS["render_dashboard"] == "dashboard"


def test_html_comes_from_args_not_result():
    """工具返回值刻意不含 html —— 几百 KB 回灌进模型上下文纯属浪费。"""
    event = {
        "type": "tool_result",
        "name": "render_dashboard",
        "args": {"title": "行业分布", "html": INTERACTIVE},
        "result": {"rendered": True, "bytes": 80, "path": "x.dash.html"},
        "tool_call_id": "c1",
        "status": "ok",
    }
    out = _chat_artifact(event, "turn-1")
    assert out["kind"] == "dashboard"
    assert out["payload"]["html"] == INTERACTIVE


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.dash.html", "dashboard"),
        ("a.html", "html"),
        ("a.htm", "html"),
        ("a.md", "markdown"),
        ("DASH.DASH.HTML", "dashboard"),
    ],
)
def test_dash_suffix_is_not_treated_as_static_html(name, expected):
    """这是本文件最要紧的一条：认成 html 就会静默失去交互。"""
    assert kind_for(Path(name)) == expected


def test_panel_is_persisted_and_readable(reports):
    from agents.dashboard_tools import render_dashboard

    out = render_dashboard("行业分布", INTERACTIVE)
    assert out["rendered"] is True
    assert out["path"].endswith(".dash.html")
    assert (reports / out["path"]).read_text(encoding="utf-8") == INTERACTIVE

    # 报告库要能列出它，且 kind 是 dashboard 而不是 html
    listed = list_artifacts()
    assert [a.kind for a in listed] == ["dashboard"]

    # 读回来的内容必须**保留 script** —— 面板的交互全在里面
    payload = read_artifact(out["path"])
    assert payload["kind"] == "dashboard"
    assert payload["content"] == INTERACTIVE
    assert "<script>" in payload["content"]


def test_same_title_twice_does_not_overwrite(reports):
    from agents.dashboard_tools import render_dashboard

    first = render_dashboard("同名", "<p>1</p>")
    second = render_dashboard("同名", "<p>2</p>")
    assert first["path"] != second["path"]
    assert (reports / first["path"]).read_text(encoding="utf-8") == "<p>1</p>"


def test_persist_failure_still_opens_the_panel(reports, monkeypatch):
    """落盘失败不该让面板打不开。

    「能用但关掉就没了」远好过「压根没有」—— 持久化是加分项,不是前提。
    """
    from agents import dashboard_tools

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(_store, "ensure_reports_dir", boom)
    out = dashboard_tools.render_dashboard("行业分布", INTERACTIVE)
    assert out["rendered"] is True
    assert out["path"] == "", "落盘失败时 path 应为空，而不是编一个"


def test_oversized_html_is_rejected_with_actionable_advice(reports):
    from agents.dashboard_tools import MAX_HTML_BYTES, render_dashboard

    out = render_dashboard("大", "x" * (MAX_HTML_BYTES + 1))
    assert "error" in out
    # 错误信息要能让下一次尝试成功
    assert "canvas/svg" in out["error"]


def test_unsafe_title_does_not_escape_the_directory(reports):
    from agents.dashboard_tools import render_dashboard

    out = render_dashboard("../../etc/evil", INTERACTIVE)
    assert "/" not in out["path"] and ".." not in out["path"]
    assert (reports / out["path"]).resolve().parent == reports.resolve()
