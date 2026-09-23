from __future__ import annotations

from types import SimpleNamespace

import pytest

from integrations.public_mcp.backend import DomainBackend
from integrations.public_mcp.contracts import TOOL_BY_NAME


@pytest.mark.parametrize("name", ["portfolio", "generate_ai_report", "generate_strategy_decision", "analyze_stock"])
def test_previously_direct_calls_use_shared_surface_and_context(monkeypatch, name):
    from integrations.public_mcp import backend as module
    from tools.tool_surface import ToolSurface

    captured = {}

    def handler(tool_context=None, **kwargs):
        captured["context"] = tool_context
        captured["args"] = kwargs
        return {"ok": True}

    actual_import = module.importlib.import_module
    spec = TOOL_BY_NAME[name]
    target_module, target_function = spec.handler.split(":")

    def loader(path, *args):
        if path == target_module:
            return SimpleNamespace(**{target_function: handler})
        return actual_import(path, *args)

    monkeypatch.setattr(module.importlib, "import_module", loader)
    backend = DomainBackend.__new__(DomainBackend)
    backend._context = SimpleNamespace(state={"user_id": "test-principal"})
    backend._surface = ToolSurface()
    result = backend(spec, {"sample": 1})
    assert result == {"ok": True}
    assert captured["context"] is backend._context
    assert captured["args"] == {"sample": 1}
    assert backend._surface.resolve(name).policy.read_only == spec.read_only
