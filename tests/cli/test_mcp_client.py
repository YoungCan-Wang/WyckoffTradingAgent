from __future__ import annotations

import sys

import pytest

from cli.mcp_client import McpClientManager, _normalize_result, mcp_available
from cli.mcp_config import Server

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

# 真实 stdio server，跑在本机子进程里，不发网络请求。
_PROBE = """
from mcp.server.fastmcp import FastMCP
m = FastMCP("probe")

@m.tool()
def list_items() -> dict:
    return {"items": ["a", "b"]}

@m.tool()
def create_item(name: str) -> dict:
    return {"created": name}

@m.tool()
def boom() -> dict:
    raise RuntimeError("intentional failure")

m.run()
"""


def _probe_server(name="probe", **kwargs):
    return Server(
        name=name,
        command=sys.executable,
        args=["-c", _PROBE],
        enabled=True,
        timeout_seconds=kwargs.pop("timeout_seconds", 25),
        **kwargs,
    )


@pytest.fixture
def manager():
    mgr = McpClientManager([_probe_server()])
    mgr.start()
    yield mgr
    mgr.stop()


class TestDiscovery:
    def test_tools_discovered_with_prefix(self, manager):
        names = {t.name for t in manager.tools()}
        assert "mcp__probe__list_items" in names
        assert "mcp__probe__create_item" in names

    def test_schema_shape_matches_native(self, manager):
        schema = next(s for s in manager.schemas() if s["name"] == "mcp__probe__create_item")
        assert set(schema) == {"name", "description", "parameters"}
        assert "name" in schema["parameters"]["properties"]

    def test_description_marks_external_origin(self, manager):
        tool = manager.find("mcp__probe__list_items")
        assert tool.description.startswith("[外部 MCP: probe]")

    def test_write_heuristic_applied_at_discovery(self, manager):
        assert manager.find("mcp__probe__create_item").is_write is True
        assert manager.find("mcp__probe__list_items").is_write is False

    def test_status_reports_available(self, manager):
        status = manager.status()
        assert status[0]["available"] is True
        assert status[0]["tool_count"] == 3


class TestInvocation:
    def test_read_call_returns_structured(self, manager):
        result = manager.call("mcp__probe__list_items", {})
        assert result.get("items") == ["a", "b"]

    def test_call_with_arguments(self, manager):
        assert manager.call("mcp__probe__create_item", {"name": "x"}).get("created") == "x"

    def test_tool_error_becomes_error_dict(self, manager):
        """server 端异常是 isError，不是 Python 异常。"""
        result = manager.call("mcp__probe__boom", {})
        assert "error" in result

    def test_unknown_tool(self, manager):
        assert "error" in manager.call("mcp__probe__nope", {})


class TestFailureIsolation:
    def test_missing_binary_does_not_raise(self):
        mgr = McpClientManager([Server(name="ghost", command="definitely-not-real-xyz", enabled=True)])
        mgr.start()
        try:
            assert mgr.tools() == []
            assert mgr.status()[0]["available"] is False
            assert mgr.status()[0]["error"]
        finally:
            mgr.stop()

    def test_server_that_exits_immediately(self):
        mgr = McpClientManager(
            [Server(name="dead", command=sys.executable, args=["-c", "raise SystemExit(1)"], enabled=True)]
        )
        mgr.start()
        try:
            assert mgr.tools() == []
            assert mgr.status()[0]["available"] is False
        finally:
            mgr.stop()

    def test_broken_server_does_not_block_healthy_one(self):
        """一个 server 挂掉不能拖累其他 server 或原生工具。"""
        mgr = McpClientManager([Server(name="ghost", command="not-real-xyz", enabled=True), _probe_server()])
        mgr.start()
        try:
            assert {t.name for t in mgr.tools()} == {
                "mcp__probe__list_items",
                "mcp__probe__create_item",
                "mcp__probe__boom",
            }
        finally:
            mgr.stop()

    def test_no_servers_is_noop(self):
        mgr = McpClientManager([])
        mgr.start()
        assert mgr.tools() == [] and mgr.schemas() == []
        mgr.stop()

    def test_stop_is_idempotent(self):
        mgr = McpClientManager([])
        mgr.start()
        mgr.stop()
        mgr.stop()


class TestNormalize:
    class _Block:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Result:
        def __init__(self, **kwargs):
            self.isError = kwargs.get("isError", False)
            self.structuredContent = kwargs.get("structuredContent")
            self.content = kwargs.get("content", [])

    def test_structured_preferred(self):
        r = self._Result(structuredContent={"a": 1}, content=[self._Block("ignored")])
        assert _normalize_result(r) == {"a": 1}

    def test_text_fallback(self):
        r = self._Result(content=[self._Block("hello"), self._Block("world")])
        assert _normalize_result(r) == {"result": "hello\nworld"}

    def test_error_flag(self):
        r = self._Result(isError=True, content=[self._Block("nope")])
        assert _normalize_result(r) == {"error": "nope"}

    def test_empty_content(self):
        assert _normalize_result(self._Result()) == {"result": "(无返回内容)"}

    def test_json_text_block_parsed_back(self):
        """多数 server 只在 text block 里放 JSON，不填 structuredContent。"""
        r = self._Result(content=[self._Block('{"items": ["a"]}')])
        assert _normalize_result(r) == {"items": ["a"]}

    def test_json_array_wrapped(self):
        r = self._Result(content=[self._Block("[1, 2]")])
        assert _normalize_result(r) == {"result": [1, 2]}

    def test_plain_text_left_alone(self):
        r = self._Result(content=[self._Block("just prose")])
        assert _normalize_result(r) == {"result": "just prose"}

    def test_malformed_json_falls_back_to_text(self):
        r = self._Result(content=[self._Block("{broken")])
        assert _normalize_result(r) == {"result": "{broken"}
