"""Lazy bridge to the existing production tool surface; no new trading logic."""

from __future__ import annotations

import importlib
import inspect
import os
from dataclasses import replace
from typing import Any

from integrations.public_mcp.contracts import ToolSpec


def build_context():
    from agents.tool_context import ToolContext

    state = {
        "user_id": os.getenv("SUPABASE_USER_ID", ""),
        "access_token": os.getenv("SUPABASE_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("SUPABASE_REFRESH_TOKEN", ""),
    }
    if not state["access_token"]:
        from integrations.local_auth import load_session

        session = load_session()
        if session:
            state.update({
                "user_id": session.get("user_id") or session.get("user", {}).get("id", ""),
                "access_token": session.get("access_token", ""),
                "refresh_token": session.get("refresh_token", ""),
            })
    return ToolContext(state=state)


class DomainBackend:
    def __init__(self) -> None:
        from integrations.local_db import init_db
        from tools.tool_surface import ToolSurface

        init_db()
        self._context = build_context()
        self._surface = ToolSurface()

    def __call__(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        from tools.tool_surface import ToolAccessContext, from_handler
        from tools.write_guard import check_write_allowed

        denied = check_write_allowed(spec.name)
        if denied is not None:
            return denied
        module, function = spec.handler.split(":", 1)
        handler = getattr(importlib.import_module(module), function)
        existing = self._surface.resolve(spec.name)
        if existing is None or existing.handler is not handler:
            definition = from_handler(handler, name=spec.name)
            definition.policy = replace(definition.policy, read_only=spec.read_only)
            self._surface.register(definition)
        if "tool_context" in inspect.signature(handler).parameters:
            arguments["tool_context"] = self._context
        access = ToolAccessContext(
            timeout_seconds=spec.timeout,
            session_id=self._context.state.get("user_id", ""),
        )
        result = self._surface.execute_tool(spec.name, arguments, access)
        if not result["ok"]:
            return {"status": "error", "code": result["error"]["code"], "error": result["error"]["message"]}
        payload = result["result"]
        if spec.name == "screen_stocks" and isinstance(payload, dict) and "research_discovery" in payload:
            from agents.screen_tools import research_discovery_agent_view

            payload = {**payload, "research_discovery": research_discovery_agent_view(payload["research_discovery"], detailed=True)}
        return payload
