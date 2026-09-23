from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="Real MCP handshake requires the mcp extra; CI installs it explicitly")

_PROBE = """
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'agents', 'core', 'tools', 'cli', 'workflows', 'pandas', 'supabase', 'akshare'}:
            raise ImportError('domain import prohibited during discovery')
sys.meta_path.insert(0, Guard())
from integrations.public_mcp.server import main
main()
"""


def test_real_stdio_handshake_and_errors_without_domain_dependencies(tmp_path):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = str(Path(__file__).resolve().parents[1])
    params = StdioServerParameters(
        command=sys.executable, args=["-c", _PROBE],
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONPATH": root,
             "PYTHONDONTWRITEBYTECODE": "1", "WYCKOFF_MCP_ALLOW_WRITES": "0"},
        cwd=str(tmp_path),
    )

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "wyckoff"
                listed = await session.list_tools()
                assert len(listed.tools) == 19
                assert all(tool.outputSchema is not None for tool in listed.tools)
                write_result = await session.call_tool("update_portfolio", {"action": "remove", "code": "000001"})
                assert write_result.isError and write_result.structuredContent["code"] == "WRITE_DENIED"
                invalid = await session.call_tool("analyze_stock", {"code": "000001", "days": True})
                assert invalid.isError and invalid.structuredContent["code"] == "INVALID_ARGUMENTS"
                unavailable = await session.call_tool("portfolio", {})
                assert unavailable.isError and unavailable.structuredContent["code"] == "MISSING_DEPENDENCY"
                assert len((await session.list_tools()).tools) == 19

    async def bounded_check():
        with anyio.fail_after(30):
            await check()

    anyio.run(bounded_check)
    assert not (tmp_path / ".wyckoff").exists()
    assert not any(tmp_path.rglob("*.db"))
