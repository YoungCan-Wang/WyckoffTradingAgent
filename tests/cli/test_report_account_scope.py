"""报告库按账号隔离。

审查指出、实测证实：所有产物落在同一个 `~/.wyckoff/reports`，`tool_context`
身份完全没被使用。Alice 存的持仓分析，Bob 能直接列出并读出全文。

这和之前修过的「持仓缓存按账号分区」是同一类问题 —— 那次的教训是：
**看起来隔离好了比压根没隔离更危险**，因为没人会再去查。

分区策略沿用 portfolioCache 的做法：按 user_id 建子目录，未登录用
`__anon__`。不做迁移 —— 旧文件留在根目录下仍可读（报告是用户资产，
不能因为加了隔离就让人找不到），但新写入一律进分区。
"""

from __future__ import annotations

import pytest

from integrations import report_store as _store


@pytest.fixture
def reports(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    monkeypatch.setattr(_store, "REPORTS_DIR", root)
    return root


class _Ctx:
    """最小的 tool_context 替身 —— 只需要 user_id。"""

    def __init__(self, user_id):
        self.user_id = user_id


def test_reports_dir_is_partitioned_by_account(reports):
    alice = _store.reports_dir_for("alice-uuid")
    bob = _store.reports_dir_for("bob-uuid")
    assert alice != bob, "两个账号必须落在不同目录"
    assert alice.is_relative_to(reports) and bob.is_relative_to(reports)


def test_anonymous_gets_its_own_partition(reports):
    anon = _store.reports_dir_for("")
    assert anon != _store.reports_dir_for("alice-uuid")
    assert "__anon__" in str(anon)


def test_alice_report_is_not_visible_to_bob(reports):
    """核心断言：这就是实测复现过的泄漏。"""
    from agents.report_artifact_tools import save_report

    out = save_report("我的持仓", "600519 持有 1000 股，成本 1580", tool_context=_Ctx("alice-uuid"))
    assert out["saved"] is True

    from cli.ipc.artifacts import list_artifacts

    bob_sees = list_artifacts(user_id="bob-uuid")
    assert bob_sees == [], f"Bob 看到了 Alice 的报告: {[a.name for a in bob_sees]}"

    alice_sees = list_artifacts(user_id="alice-uuid")
    assert len(alice_sees) == 1, "Alice 自己必须还能看到"


def test_bob_cannot_read_alice_report_by_path(reports):
    """列表隔离不够 —— 按路径直读也必须挡住。"""
    from agents.report_artifact_tools import save_report
    from cli.ipc.artifacts import ArtifactError, read_artifact

    out = save_report("私密", "敏感内容", tool_context=_Ctx("alice-uuid"))
    with pytest.raises(ArtifactError):
        read_artifact(out["path"], user_id="bob-uuid")


def test_alice_can_still_read_her_own(reports):
    from agents.report_artifact_tools import save_report
    from cli.ipc.artifacts import read_artifact

    out = save_report("我的", "正文内容", tool_context=_Ctx("alice-uuid"))
    payload = read_artifact(out["path"], user_id="alice-uuid")
    assert payload["content"] == "正文内容"


def test_dashboard_is_partitioned_too(reports):
    from agents.dashboard_tools import render_dashboard
    from cli.ipc.artifacts import list_artifacts

    render_dashboard("面板", "<p>x</p>", tool_context=_Ctx("alice-uuid"))
    assert list_artifacts(user_id="bob-uuid") == []
    assert len(list_artifacts(user_id="alice-uuid")) == 1


def test_legacy_files_at_root_stay_readable(reports):
    """旧文件不迁移，但仍要能读到 —— 报告是用户资产。"""
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "old-report.md").write_text("历史报告", encoding="utf-8")
    from cli.ipc.artifacts import list_artifacts

    names = [a.name for a in list_artifacts(user_id="alice-uuid")]
    assert "old-report.md" in names, "加了隔离不能让旧报告消失"
