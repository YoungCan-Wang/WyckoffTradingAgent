from __future__ import annotations

import pytest

from cli.mcp_client import ExternalTool
from cli.tools import TOOL_SCHEMAS, ToolRegistry


class _FakeManager:
    def __init__(self, tools=None, result=None):
        self._tools = tools or []
        self._result = result if result is not None else {"ok": True}
        self.calls: list[tuple[str, dict]] = []

    def tools(self):
        return self._tools

    def schemas(self):
        return [t.schema() for t in self._tools]

    def find(self, name):
        return next((t for t in self._tools if t.name == name), None)

    def call(self, name, args):
        self.calls.append((name, args))
        return self._result


def _tool(name="mcp__probe__create_item", is_write=True):
    return ExternalTool(
        name=name,
        server="probe",
        description="[外部 MCP: probe] test",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        is_write=is_write,
    )


@pytest.fixture
def registry():
    return ToolRegistry()


class TestSchemaMerge:
    def test_external_schemas_appended(self, registry):
        registry.set_mcp_manager(_FakeManager([_tool()]))
        names = {s["name"] for s in registry.schemas()}
        assert "mcp__probe__create_item" in names
        assert "portfolio" in names

    def test_module_level_schemas_not_mutated(self, registry):
        """合并必须在实例上；改模块级列表会污染其他实例。"""
        before = len(TOOL_SCHEMAS)
        registry.set_mcp_manager(_FakeManager([_tool()]))
        registry.schemas()
        assert len(TOOL_SCHEMAS) == before

    def test_other_instance_unaffected(self, registry):
        registry.set_mcp_manager(_FakeManager([_tool()]))
        registry.schemas()
        clean = ToolRegistry()
        assert not any(s["name"].startswith("mcp__") for s in clean.schemas())

    def test_no_manager_means_native_only(self, registry):
        assert not any(s["name"].startswith("mcp__") for s in registry.schemas())

    def test_allowed_tools_filter_applies_to_external(self, registry):
        registry.set_mcp_manager(_FakeManager([_tool()]))
        scoped = registry.schemas({"mcp__probe__create_item"})
        assert [s["name"] for s in scoped] == ["mcp__probe__create_item"]

    def test_broken_manager_does_not_break_native(self, registry):
        class _Broken:
            def schemas(self):
                raise RuntimeError("server died")

            def find(self, _name):
                return None

        registry.set_mcp_manager(_Broken())
        assert any(s["name"] == "portfolio" for s in registry.schemas())


class TestApprovalGate:
    def test_external_write_requires_approval(self, registry):
        registry.set_mcp_manager(_FakeManager([_tool(is_write=True)]))
        assert registry.requires_approval("mcp__probe__create_item") is True

    def test_external_read_does_not(self, registry):
        registry.set_mcp_manager(_FakeManager([_tool("mcp__probe__list_items", is_write=False)]))
        assert registry.requires_approval("mcp__probe__list_items") is False

    def test_write_blocked_without_callback(self, registry):
        """无确认回调时外部写工具必须被拦，不能默认放行。"""
        manager = _FakeManager([_tool(is_write=True)])
        registry.set_mcp_manager(manager)
        result = registry.execute("mcp__probe__create_item", {"x": "1"})
        assert "error" in result
        assert manager.calls == []

    def test_write_queued_by_daemon_guard(self, registry, tmp_path):
        from cli.headless import DaemonGuard

        guard = DaemonGuard(source="daemon", db_path=tmp_path / "a.db")
        manager = _FakeManager([_tool(is_write=True)])
        registry.set_mcp_manager(manager)
        registry.set_confirm_callback(guard.confirm)
        result = registry.execute("mcp__probe__create_item", {"x": "1"})
        assert "待批准队列" in result["error"]
        assert manager.calls == []
        assert len(guard.queued) == 1

    def test_read_passes_through(self, registry):
        manager = _FakeManager([_tool("mcp__probe__list_items", is_write=False)], result={"items": [1]})
        registry.set_mcp_manager(manager)
        assert registry.execute("mcp__probe__list_items", {}) == {"items": [1]}
        assert manager.calls == [("mcp__probe__list_items", {})]

    def test_approved_write_executes(self, registry):
        manager = _FakeManager([_tool(is_write=True)])
        registry.set_mcp_manager(manager)
        registry.set_confirm_callback(lambda _n, _a: {"action": "allow"})
        assert registry.execute("mcp__probe__create_item", {"x": "1"}) == {"ok": True}
        assert len(manager.calls) == 1

    def test_denied_write_not_executed(self, registry):
        manager = _FakeManager([_tool(is_write=True)])
        registry.set_mcp_manager(manager)
        registry.set_confirm_callback(lambda _n, _a: {"action": "deny"})
        assert "error" in registry.execute("mcp__probe__create_item", {"x": "1"})
        assert manager.calls == []


class TestNameSpacing:
    def test_prefix_prevents_native_shadowing(self, registry):
        """外部 server 有同名工具时不能顶掉原生实现。"""
        registry.set_mcp_manager(_FakeManager([_tool("mcp__evil__portfolio", is_write=False)]))
        assert registry.has_tool("portfolio") is True
        assert registry._tools["portfolio"].__module__.startswith("agents.")

    def test_prepare_accepts_external(self, registry):
        registry.set_mcp_manager(_FakeManager([_tool()]))
        assert registry.prepare("mcp__probe__create_item", {"x": "1"}).action == "accept"

    def test_prepare_still_rejects_unknown(self, registry):
        decision = registry.prepare("totally_unknown_tool", {})
        assert decision.action == "reject"
        assert decision.code == "tool_not_found"
