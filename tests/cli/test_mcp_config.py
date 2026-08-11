from __future__ import annotations

import json
import stat

import pytest

from cli.mcp_config import (
    Server,
    enabled_servers,
    find_server,
    is_builtin_duplicate,
    load_servers,
    remove_server,
    save_servers,
    set_enabled,
    upsert_server,
)


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "mcp_servers.json"


def _server(name="github", **kwargs):
    base = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
    base.update(kwargs)
    return Server(name=name, **base)


class TestRoundTrip:
    def test_save_then_load(self, cfg):
        save_servers([_server(env={"GITHUB_TOKEN": "t"}, enabled=True)], cfg)
        loaded = load_servers(cfg)
        assert len(loaded) == 1
        assert loaded[0].name == "github"
        assert loaded[0].env == {"GITHUB_TOKEN": "t"}
        assert loaded[0].enabled is True

    def test_missing_file_is_empty(self, cfg):
        assert load_servers(cfg) == []

    def test_corrupt_file_is_empty_not_crash(self, cfg):
        cfg.write_text("{not json", encoding="utf-8")
        assert load_servers(cfg) == []

    def test_name_not_duplicated_into_body(self, cfg):
        save_servers([_server()], cfg)
        body = json.loads(cfg.read_text(encoding="utf-8"))["servers"]["github"]
        assert "name" not in body

    def test_saved_config_is_user_only(self, cfg):
        save_servers([_server(env={"GITHUB_TOKEN": "secret"})], cfg)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_entry_without_command_skipped(self, cfg):
        cfg.write_text(json.dumps({"servers": {"bad": {"args": ["x"]}}}), encoding="utf-8")
        assert load_servers(cfg) == []


class TestDefaultDisabled:
    def test_new_server_defaults_disabled(self):
        """接入外部 server 等于允许 spawn 进程，必须显式启用。"""
        assert _server().enabled is False

    def test_loaded_entry_without_enabled_is_disabled(self, cfg):
        cfg.write_text(json.dumps({"servers": {"x": {"command": "foo"}}}), encoding="utf-8")
        assert load_servers(cfg)[0].enabled is False

    def test_enabled_servers_excludes_disabled(self, cfg):
        save_servers([_server("a", enabled=True), _server("b", enabled=False)], cfg)
        assert [s.name for s in enabled_servers(cfg)] == ["a"]


class TestBuiltinDuplicate:
    def test_detects_mcp_server_py_in_args(self):
        assert is_builtin_duplicate(Server(name="w", command="python", args=["mcp_server.py"]))

    def test_detects_command_inside_repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".venv" / "bin").mkdir(parents=True)
        target = root / ".venv" / "bin" / "python"
        target.touch()
        server = Server(name="w", command=str(target))
        assert is_builtin_duplicate(server, repo_root=root) is True

    def test_third_party_not_flagged(self, tmp_path):
        server = Server(name="github", command="npx", args=["-y", "@modelcontextprotocol/server-github"])
        assert is_builtin_duplicate(server, repo_root=tmp_path) is False

    @pytest.mark.parametrize("command", ["npx", "uvx", "node", "docker"])
    def test_bare_command_not_resolved_against_cwd(self, command):
        """裸命令由 PATH 解析。按 cwd 展开会让每个裸命令都像是仓库内路径。"""
        assert is_builtin_duplicate(Server(name="x", command=command)) is False

    def test_package_spec_arg_not_treated_as_path(self):
        """@scope/pkg 含斜杠但不是本地路径，不能按 cwd 展开成仓库内文件。"""
        server = Server(name="g", command="npx", args=["-y", "@modelcontextprotocol/server-github"])
        assert is_builtin_duplicate(server) is False

    def test_repo_script_in_args_detected(self, tmp_path):
        """自建 server 的脚本路径在 args 里，不在 command 里。"""
        root = tmp_path / "repo"
        root.mkdir()
        script = root / "mcp_server.py"
        script.touch()
        server = Server(name="w", command="python", args=[str(script)])
        assert is_builtin_duplicate(server, repo_root=root) is True

    def test_third_party_script_outside_repo(self, tmp_path):
        outside = tmp_path / "other" / "server.py"
        outside.parent.mkdir(parents=True)
        outside.touch()
        server = Server(name="p", command="python", args=[str(outside)])
        assert is_builtin_duplicate(server, repo_root=tmp_path / "repo") is False

    def test_enabled_servers_excludes_builtin_even_if_enabled(self, cfg):
        """自建 server 已内置；接第二遍会出现同名工具且绕过审批。"""
        save_servers([Server(name="wyckoff", command="python", args=["mcp_server.py"], enabled=True)], cfg)
        assert enabled_servers(cfg) == []


class TestMutations:
    def test_upsert_replaces_same_name(self, cfg):
        upsert_server(_server(command="old"), cfg)
        upsert_server(_server(command="new"), cfg)
        loaded = load_servers(cfg)
        assert len(loaded) == 1 and loaded[0].command == "new"

    def test_upsert_sorted(self, cfg):
        upsert_server(_server("zeta"), cfg)
        upsert_server(_server("alpha"), cfg)
        assert [s.name for s in load_servers(cfg)] == ["alpha", "zeta"]

    def test_remove_reports_missing(self, cfg):
        assert remove_server("nope", cfg) is False

    def test_remove_deletes(self, cfg):
        upsert_server(_server(), cfg)
        assert remove_server("github", cfg) is True
        assert load_servers(cfg) == []

    def test_set_enabled_toggles(self, cfg):
        upsert_server(_server(), cfg)
        assert set_enabled("github", True, cfg).enabled is True
        assert find_server("github", cfg).enabled is True

    def test_set_enabled_unknown_returns_none(self, cfg):
        assert set_enabled("nope", True, cfg) is None


class TestToolPrefix:
    def test_prefix_namespaces_by_server(self):
        assert _server("github").tool_prefix() == "mcp__github__"

    def test_prefix_avoids_native_collision(self):
        from cli.tools import TOOL_SCHEMAS

        native = {s["name"] for s in TOOL_SCHEMAS}
        prefixed = _server("github").tool_prefix() + "portfolio"
        assert prefixed not in native

    def test_timeout_defaults_and_rejects_garbage(self, cfg):
        cfg.write_text(json.dumps({"servers": {"x": {"command": "foo", "timeout_seconds": "abc"}}}), encoding="utf-8")
        assert load_servers(cfg)[0].timeout_seconds == 30
