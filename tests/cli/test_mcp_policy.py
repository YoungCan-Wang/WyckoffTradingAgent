from __future__ import annotations

import pytest

from cli.mcp_policy import is_write_tool


class _Ann:
    def __init__(self, **kwargs):
        self.readOnlyHint = kwargs.get("readOnlyHint")
        self.destructiveHint = kwargs.get("destructiveHint")


class TestAnnotationsWin:
    def test_read_only_hint_beats_write_verb(self):
        """server 明说只读就信它，即使名字里有 update。"""
        assert is_write_tool("update_cache", _Ann(readOnlyHint=True)) is False

    def test_destructive_hint_beats_read_verb(self):
        assert is_write_tool("get_thing", _Ann(destructiveHint=True)) is True

    def test_dict_annotations_accepted(self):
        assert is_write_tool("update_cache", {"readOnlyHint": True}) is False

    def test_none_hints_fall_through_to_name(self):
        assert is_write_tool("list_issues", _Ann()) is False
        assert is_write_tool("create_issue", _Ann()) is True


class TestUnknownDefaultsToWrite:
    """这是整个启发式的安全依据：认不出就按写。"""

    @pytest.mark.parametrize("name", ["frobnicate", "xyzzy", "do_thing", "handle", "process_batch"])
    def test_unrecognized_verbs_are_write(self, name):
        assert is_write_tool(name) is True

    def test_no_annotations_and_no_verb_is_write(self):
        assert is_write_tool("mcp__weird__blorp", None) is True


class TestWriteVerbs:
    @pytest.mark.parametrize(
        "name",
        [
            "create_issue",
            "delete_file",
            "update_record",
            "send_message",
            "write_file",
            "post_comment",
            "deploy_service",
            "revoke_token",
            "merge_pull_request",
            "upload_artifact",
            "run_command",
            "kill_process",
        ],
    )
    def test_write_verbs_detected(self, name):
        assert is_write_tool(name) is True

    def test_hyphenated_name(self):
        assert is_write_tool("create-issue") is True

    def test_dotted_name(self):
        assert is_write_tool("repo.delete") is True


class TestReadVerbs:
    @pytest.mark.parametrize(
        "name",
        ["get_user", "list_repos", "read_file", "search_code", "query_db", "fetch_url", "describe_table"],
    )
    def test_read_verbs_detected(self, name):
        assert is_write_tool(name) is False

    def test_write_verb_wins_in_mixed_name(self):
        """get_and_delete 含两类动词，写必须优先。"""
        assert is_write_tool("get_and_delete_branch") is True


class TestPrefixStripping:
    def test_server_prefix_ignored(self):
        assert is_write_tool("mcp__github__list_issues") is False
        assert is_write_tool("mcp__github__create_issue") is True

    def test_server_name_does_not_leak_into_match(self):
        """server 名叫 deploy 不该让它所有只读工具变成写。"""
        assert is_write_tool("mcp__deploy__list_targets") is False


class TestExternalNeverAuto:
    def test_external_write_maps_to_review(self):
        from cli.approval_policy import REVIEW
        from cli.mcp_policy import classify_external

        assert classify_external("create_issue") == REVIEW

    def test_external_tools_are_not_in_auto_set(self):
        from cli.approval_policy import AUTO_TOOLS

        assert not any(name.startswith("mcp__") for name in AUTO_TOOLS)
