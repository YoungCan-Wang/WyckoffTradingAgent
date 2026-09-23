"""MCP transport and discovery. Importing this module does not import the domain or SDK."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout

from integrations.public_mcp.contracts import TOOLS
from integrations.public_mcp.runtime import Runtime


def create_server(runtime: Runtime | None = None):
    import anyio
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, TextContent, Tool

    server = Server("wyckoff")
    runtime = runtime or Runtime()
    limiter = anyio.CapacityLimiter(1)

    @server.list_tools()
    async def list_tools():
        return [Tool(**spec.descriptor()) for spec in TOOLS]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict | None):
        outcome = await anyio.to_thread.run_sync(runtime.call, name, arguments, limiter=limiter)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(outcome.data, ensure_ascii=False, allow_nan=False))],
            structuredContent=outcome.data,
            isError=outcome.is_error,
        )

    return server


async def serve_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = create_server()
    # stdio_server captures the real stdout first. Legacy Python print calls made
    # by domain libraries afterwards must never be mixed with JSON-RPC messages.
    async with stdio_server() as (read, write):
        with redirect_stdout(sys.stderr):
            await server.run(read, write, server.create_initialization_options())


def main() -> None:
    try:
        import anyio

        anyio.run(serve_stdio)
    except ImportError as exc:
        raise SystemExit("MCP dependencies are missing. Install: uv pip install 'youngcan-wyckoff-analysis[mcp]'") from exc
    except KeyboardInterrupt:
        pass
