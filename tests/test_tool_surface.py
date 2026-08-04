from __future__ import annotations

import time

from tools.tool_surface import ToolAccessContext, ToolSurface, from_handler


def _typed_tool(
    code: str,
    cost: float = 0.0,
    days: int = 30,
    include_breadth: bool = False,
    limit: int | None = None,
    financial_metrics: bool | str | None = None,
) -> dict:
    return {
        "code": code,
        "cost": cost,
        "days": days,
        "include_breadth": include_breadth,
        "limit": limit,
        "financial_metrics": financial_metrics,
    }


def test_from_handler_resolves_postponed_annotations_and_optional_none() -> None:
    surface = ToolSurface()
    surface.register(from_handler(_typed_tool, name="typed_tool"))

    params = surface.list_tools()[0]["parameters"]["properties"]
    assert params["cost"]["type"] == "number"
    assert params["days"]["type"] == "integer"
    assert params["include_breadth"]["type"] == "boolean"
    assert params["limit"]["type"] == "integer"

    result = surface.execute_tool(
        "typed_tool",
        {
            "code": "600519",
            "cost": 0.0,
            "days": 30,
            "include_breadth": False,
            "limit": None,
            "financial_metrics": False,
        },
        ToolAccessContext(timeout_seconds=0),
    )

    assert result["ok"] is True
    assert result["result"]["limit"] is None
    assert result["result"]["financial_metrics"] is False


def test_from_handler_keeps_numeric_bool_validation_strict() -> None:
    surface = ToolSurface()
    surface.register(from_handler(_typed_tool, name="typed_tool"))

    result = surface.execute_tool(
        "typed_tool",
        {"code": "600519", "days": True},
        ToolAccessContext(timeout_seconds=0),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_prepare_call_coerces_string_numbers_and_bools() -> None:
    surface = ToolSurface()
    surface.register(from_handler(_typed_tool, name="typed_tool"))

    prepared = surface.prepare_call(
        "typed_tool",
        {"code": "600519", "days": "30", "cost": "1.5", "include_breadth": "true"},
    )
    assert prepared["ok"] is True
    assert prepared["args"]["days"] == 30
    assert prepared["args"]["cost"] == 1.5
    assert prepared["args"]["include_breadth"] is True

    executed = surface.execute_tool(
        "typed_tool",
        {"code": "600519", "days": "7", "include_breadth": "false"},
        ToolAccessContext(timeout_seconds=0),
    )
    assert executed["ok"] is True
    assert executed["result"]["days"] == 7
    assert executed["result"]["include_breadth"] is False


def test_tool_surface_times_out_regular_tools() -> None:
    surface = ToolSurface()
    surface.register(from_handler(lambda: time.sleep(0.05), name="regular_tool"))

    result = surface.execute_tool("regular_tool", {}, ToolAccessContext(timeout_seconds=0.001))

    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"


def test_tool_surface_exempts_interactive_and_delegate_tools_from_timeout() -> None:
    for name in ("ask_user_question", "delegate_to_research", "delegate_to_analysis", "delegate_to_trading"):
        surface = ToolSurface()
        surface.register(from_handler(lambda: (time.sleep(0.01), "done")[1], name=name))

        result = surface.execute_tool(name, {}, ToolAccessContext(timeout_seconds=0.001))

        assert result["ok"] is True
        assert result["result"] == "done"
