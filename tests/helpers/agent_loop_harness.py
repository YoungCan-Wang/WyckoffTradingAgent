"""Deterministic harness for agent loop tests."""

from __future__ import annotations

from collections.abc import Callable, Generator
from copy import deepcopy
from typing import Any

from cli.providers.base import LLMProvider
from cli.workflows.dispatch import build_turn_runtime

Chunk = dict[str, Any]
RoundScript = list[Chunk] | Callable[[list[dict[str, Any]], list[dict[str, Any]], str], list[Chunk]]


class ScriptedProvider(LLMProvider):
    """Replay scripted stream chunks round by round."""

    def __init__(self, rounds: list[RoundScript], name: str = "ScriptedProvider"):
        self._rounds = rounds
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    def chat(self, messages, tools, system_prompt="") -> dict[str, Any]:
        raise NotImplementedError("ScriptedProvider only supports chat_stream() in loop tests")

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        round_idx = len(self.calls)
        if round_idx >= len(self._rounds):
            raise AssertionError(f"Unexpected extra provider round: {round_idx}")

        snapshot = {
            "round_idx": round_idx,
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "system_prompt": system_prompt,
        }
        self.calls.append(snapshot)

        script = self._rounds[round_idx]
        chunks = script(messages, tools, system_prompt) if callable(script) else script
        for chunk in chunks:
            yield deepcopy(chunk)


class StubToolRegistry:
    """Minimal ToolRegistry compatible stub."""

    def __init__(
        self,
        *,
        schemas: list[dict[str, Any]] | None = None,
        tool_results: dict[str, Any] | None = None,
        concurrency_safe_tools: set[str] | None = None,
    ):
        self._schemas = (
            deepcopy(schemas)
            if schemas is not None
            else [
                {
                    "name": "portfolio",
                    "description": "Mock portfolio tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        )
        self._tool_results = tool_results or {}
        self._concurrency_safe_tools = (
            concurrency_safe_tools
            if concurrency_safe_tools is not None
            else {
                "search_stock_by_name",
                "analyze_stock",
                "portfolio",
                "get_market_overview",
                "get_market_history",
                "query_history",
            }
        )
        self.calls: list[dict[str, Any]] = []

    def schemas(self, allowed_tools: set[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        schemas = deepcopy(self._schemas)
        if not allowed_tools:
            return schemas
        allowed = set(allowed_tools)
        return [schema for schema in schemas if schema["name"] in allowed]

    def execute(self, name: str, args: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Any:
        self.calls.append({"name": name, "args": deepcopy(args)})
        result = self._tool_results.get(name, {"ok": True, "name": name, "args": deepcopy(args)})
        if callable(result):
            return result(name, deepcopy(args))
        return deepcopy(result)

    def concurrency_safe(self, name: str) -> bool:
        return name in self._concurrency_safe_tools


class AgentLoopHarness:
    """Run a single turn through the canonical runtime with scripted dependencies."""

    def __init__(
        self,
        *,
        rounds: list[RoundScript],
        tool_results: dict[str, Any] | None = None,
        enforce_turn_expectations: bool | None = None,
    ):
        self.provider = ScriptedProvider(rounds)
        self.tools = StubToolRegistry(tool_results=tool_results)
        self.enforce_turn_expectations = enforce_turn_expectations

    def run_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
    ) -> dict[str, Any]:
        working_messages = deepcopy(messages)
        observed_tool_calls: list[dict[str, Any]] = []
        observed_tool_results: list[dict[str, Any]] = []
        user_text = next(
            (message.get("content", "") for message in reversed(working_messages) if message.get("role") == "user"),
            "",
        )
        runtime, _ = build_turn_runtime(
            self.provider,
            self.tools,
            session_id="",
            user_text=str(user_text),
            enforce_turn_expectations=self.enforce_turn_expectations,
            routing_messages=working_messages,
        )
        result: dict[str, Any] | None = None
        for event in runtime.run_stream(working_messages, system_prompt):
            if event["type"] == "tool_start":
                observed_tool_calls.append({"name": event["name"], "args": deepcopy(event["args"])})
            elif event["type"] in {"tool_result", "tool_error"} and "result" in event:
                observed_tool_results.append({"name": event["name"], "result": deepcopy(event["result"])})
            elif event["type"] == "done":
                result = {
                    "text": event["text"],
                    "streamed": event.get("streamed", False),
                    "usage": event.get("usage", {}),
                    "elapsed": event.get("elapsed", 0.0),
                }
        result = result or {
            "text": "(Agent 未返回内容)",
            "streamed": False,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "elapsed": 0.0,
        }
        return {
            "result": result,
            "messages": working_messages,
            "provider_calls": deepcopy(self.provider.calls),
            "tool_calls": observed_tool_calls,
            "tool_results": observed_tool_results,
            "tool_exec_calls": deepcopy(self.tools.calls),
        }
