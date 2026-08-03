"""Unified agent runtime event stream.

This module owns the headless agent loop: provider calls, tool execution,
loop guards, compaction, scratchpad tracing, and final answer assembly.
Callers such as TUI/Web/MCP should consume RuntimeEvent dictionaries instead
of reimplementing the loop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from agents.stream_events import stream_event


class AgentCancelled(Exception):
    """Agent 运行被用户主动取消。"""


from cli.compaction import compact_messages
from cli.loop_guard import (
    MAX_INCOMPLETE_TOOL_RETRIES,
    MAX_TOOL_ROUNDS,
    TurnExpectation,
    build_retry_exhausted_warning,
    build_retry_user_message,
    check_doom_loop,
    missing_required_tool,
    resolve_progressive_turn_expectation,
    resolve_turn_expectation,
)
from cli.providers.base import LLMProvider
from cli.scratchpad import AgentScratchpad
from cli.tool_results import format_tool_result_for_context
from cli.tools import ToolRegistry
from cli.workflows.router import build_workflow_system_prompt

logger = logging.getLogger(__name__)

RuntimeEvent = dict[str, Any]

STREAM_CHUNK_TIMEOUT = 60.0
_INTERNAL_RETRY_MARKER = "_internal_retry"
_STRICT_EXPECTATIONS_ENV = "WYCKOFF_STRICT_TOOL_EXPECTATIONS"
_DIRECT_TOOL_USE_PROMPT = """\

<tool-use>
自然语言理解和上下文恢复由模型完成，代码只限制工具、写入和高风险动作边界。
工具可用不代表必须调用；按用户真实意图选择最少必要工具，不要按关键词机械触发。
能从上下文合理推断的表述偏差、口语省略、错别字或术语混用，先按最高置信解释执行，并在回答中说明假设。
用户请求涉及持仓、股票或市场事实时，优先用可用工具验证。
公开信息、未上市标的、IPO、舆情或本地库查不到时，用 browser_research（本机 Chrome CDP 搜索并抽取正文）。
只有执行对象仍不明确，或需要写入/交易/高风险确认时，才使用 ask_user_question。
</tool-use>"""


def _strict_turn_expectations_enabled(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.getenv(_STRICT_EXPECTATIONS_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on", "strict"}


def _iter_with_timeout(stream, timeout: float, cancel_check: Callable[[], bool] | None = None):
    """包装流式迭代器，支持超时和取消。cancel_check 每 0.5s 轮询一次。"""
    import queue
    import threading

    _SENTINEL = None
    _EXCEPTION = object()
    q: queue.Queue = queue.Queue()

    def _producer():
        try:
            for chunk in stream:
                q.put(chunk)
            q.put(_SENTINEL)
        except BaseException as exc:
            q.put((_EXCEPTION, exc))

    t = threading.Thread(target=_producer, daemon=True)
    t.start()

    try:
        while True:
            deadline = time.monotonic() + timeout
            item = None
            got = False
            while not got:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"模型响应超时（{timeout:.0f}s 内无数据）") from None
                wait = min(remaining, 0.5)
                try:
                    item = q.get(timeout=wait)
                    got = True
                except queue.Empty:
                    if cancel_check and cancel_check():
                        raise AgentCancelled() from None
            if item is _SENTINEL:
                return
            if isinstance(item, tuple) and len(item) == 2 and item[0] is _EXCEPTION:
                raise item[1]
            yield item
    except BaseException:
        if hasattr(stream, "close"):
            with contextlib.suppress(Exception):
                stream.close()
        raise


@dataclass
class RoundState:
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    streamed: bool = False


@dataclass
class RunState:
    started_at: float
    total_input: int = 0
    total_output: int = 0
    streamed: bool = False
    incomplete_tool_retries: int = 0
    used_tools: list[tuple[str, dict]] = field(default_factory=list)
    recent_calls: list[tuple[str, int]] = field(default_factory=list)
    recent_args_texts: list[str] = field(default_factory=list)


def _normalize_required_tool_arg_sets(
    required_tools: tuple[str, ...],
    required_tool_args: dict[str, dict[str, Any]],
    required_tool_arg_sets: dict[str, tuple[dict[str, Any], ...]] | None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    arg_sets: dict[str, tuple[dict[str, Any], ...]] = {}
    for name in required_tools:
        cleaned_sets = _clean_required_arg_sets((required_tool_arg_sets or {}).get(name))
        if not cleaned_sets and (args := _clean_required_args(required_tool_args.get(name))):
            cleaned_sets = (args,)
        if cleaned_sets:
            arg_sets[name] = cleaned_sets
    return arg_sets


def _clean_required_arg_sets(value: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(value, dict):
        items = (value,)
    elif isinstance(value, (list, tuple)):
        items = tuple(item for item in value if isinstance(item, dict))
    else:
        items = ()
    return tuple(args for item in items if (args := _clean_required_args(item)))


def _clean_required_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if str(key).strip() and item not in (None, "")}


def _format_required_arg_set(args: dict[str, Any]) -> str:
    return ", ".join(f'{key}="{value}"' for key, value in args.items())


def _drop_internal_retry_messages(messages: list[dict[str, Any]]) -> None:
    messages[:] = [m for m in messages if not m.get(_INTERNAL_RETRY_MARKER)]


def partition_tool_calls(
    tool_calls: list[dict],
    concurrency_safe: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """将工具调用分批：连续可并行工具归入同一批次，其余串行。"""

    batches: list[dict[str, Any]] = []
    for call in tool_calls:
        is_safe = concurrency_safe(call["name"])
        if is_safe and batches and batches[-1]["concurrent"]:
            batches[-1]["calls"].append(call)
        else:
            batches.append({"concurrent": is_safe, "calls": [call]})
    return batches


class AgentRuntime:
    """Provider-agnostic agent loop that emits RuntimeEvent dictionaries."""

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        *,
        scratchpad: AgentScratchpad | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        cancel_check: Callable[[], bool] | None = None,
        stream_chunk_timeout: float = STREAM_CHUNK_TIMEOUT,
        allowed_tools: set[str] | tuple[str, ...] | None = None,
        required_tools: tuple[str, ...] | None = None,
        required_tool_args: dict[str, dict[str, str]] | None = None,
        required_tool_arg_sets: dict[str, tuple[dict[str, Any], ...]] | None = None,
        workflow: Any | None = None,
        enforce_turn_expectations: bool | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.scratchpad = scratchpad
        self.max_tool_rounds = max_tool_rounds
        self.cancel_check = cancel_check
        self.stream_chunk_timeout = stream_chunk_timeout
        workflow_tools = getattr(workflow, "allowed_tools", ()) if workflow else ()
        tool_scope = tuple(allowed_tools) if allowed_tools is not None else tuple(workflow_tools or ())
        self.allowed_tools = set(tool_scope) if allowed_tools is not None or tool_scope else None
        self.workflow = workflow
        self.enforce_turn_expectations = _strict_turn_expectations_enabled(enforce_turn_expectations)
        self.required_tools = tuple(
            name
            for name in dict.fromkeys(required_tools or ())
            if self.enforce_turn_expectations and (self.allowed_tools is None or name in self.allowed_tools)
        )
        self.required_tool_args = {
            name: dict(required_tool_args.get(name) or {})
            for name in self.required_tools
            if required_tool_args and isinstance(required_tool_args.get(name), dict)
        }
        self.required_tool_arg_sets = _normalize_required_tool_arg_sets(
            self.required_tools,
            self.required_tool_args,
            required_tool_arg_sets,
        )

    def run_stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str = "",
        *,
        resume_from: Any | None = None,
    ) -> Iterator[RuntimeEvent]:
        """Run the agent loop and yield normalized runtime events.

        Always ends with a terminal event: ``done``, ``turn_cancelled``, or
        ``turn_failed`` (failures are then re-raised). ``resume_from`` is a
        ``TurnCheckpoint`` used to skip already-completed tool_call_ids.
        """

        self._resume_checkpoint = resume_from
        try:
            yield from self._run_stream_loop(messages, system_prompt)
        except AgentCancelled:
            yield self._turn_cancelled_event()
            return
        except Exception as exc:
            yield self._turn_failed_event(exc)
            raise
        finally:
            self._resume_checkpoint = None

    def _run_stream_loop(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> Iterator[RuntimeEvent]:
        system_prompt = self._prepare_system_prompt(system_prompt)
        if self.tools and hasattr(self.tools, "_tool_context") and self.tools._tool_context:
            self.tools._tool_context.state["session_id"] = self._session_id()
        state = RunState(started_at=time.monotonic())
        expectation = None if self.required_tools else self._natural_turn_expectation(messages)
        model_name = getattr(self.provider, "name", "")
        workflow_event = self._workflow_start_event()
        if workflow_event:
            yield workflow_event

        for round_idx in range(self.max_tool_rounds):
            self._ensure_not_cancelled()
            messages, event = self._compact_if_needed(messages, model_name, self._provider_context_window())
            if event:
                yield event

            if round_idx > 0:
                yield {"type": "model_start", "round": round_idx + 1}

            yield stream_event(
                "stage_start",
                stage="model",
                round=round_idx + 1,
                message="正在分析",
            )
            try:
                round_state = yield from self._collect_model_round(messages, system_prompt, round_idx + 1)
            except Exception:
                yield stream_event(
                    "stage_done",
                    stage="model",
                    round=round_idx + 1,
                    success=False,
                )
                raise
            yield stream_event(
                "stage_done",
                stage="model",
                round=round_idx + 1,
                success=True,
            )
            self._accumulate_usage(state, round_state)
            if round_state.thinking:
                yield self._record_thinking_event(round_state, round_idx + 1)

            if round_state.tool_calls:
                self._append_assistant_tool_message(messages, round_state)
                completed = yield from self._run_tool_batches(messages, round_state.tool_calls, state)
                if completed:
                    retry_event = self._maybe_retry_required_tool_args(messages, round_state, state, expectation)
                    if retry_event:
                        yield retry_event
                    continue

            active_expectation = self._active_turn_expectation(messages, state, expectation)
            retry_event = self._maybe_retry_required_tool(messages, round_state, state, active_expectation)
            if retry_event:
                yield retry_event
                continue

            self._apply_missing_tool_warning(round_state, state, active_expectation)
            yield self._finish_turn(messages, round_state, state, round_idx + 1)
            return

        yield self._finish_limit_turn(state)

    def _ensure_not_cancelled(self) -> None:
        if self.cancel_check and self.cancel_check():
            raise AgentCancelled()

    def _turn_cancelled_event(self) -> RuntimeEvent:
        return stream_event("turn_cancelled", message="cancelled by user")

    def _turn_failed_event(self, exc: BaseException) -> RuntimeEvent:
        from cli.conversation.failures import classify_failure

        info = classify_failure(exc)
        return stream_event(
            "turn_failed",
            message=info.message,
            failure={
                "kind": info.kind.value,
                "message": info.message,
                "exception_type": info.exception_type,
            },
        )

    def _resumed_tool_ids(self) -> set[str]:
        checkpoint = getattr(self, "_resume_checkpoint", None)
        if checkpoint is None:
            return set()
        ids = getattr(checkpoint, "completed_tool_call_ids", None) or []
        return {str(item) for item in ids if item}

    def _compact_if_needed(
        self,
        messages: list[dict[str, Any]],
        model_name: str,
        context_window: int | None,
    ) -> tuple[list[dict[str, Any]], RuntimeEvent | None]:
        prev_len = len(messages)
        compacted_messages, compacted, metadata = compact_messages(
            messages,
            self.provider,
            model_name,
            context_window,
            session_id=self._session_id(),
            include_metadata=True,
        )
        if not compacted:
            return compacted_messages, None
        messages[:] = compacted_messages
        if self.scratchpad:
            self.scratchpad.record_compaction(
                before_messages=prev_len,
                after_messages=len(compacted_messages),
                metadata=metadata,
            )
        return messages, {
            "type": "compaction",
            "before_messages": prev_len,
            "after_messages": len(compacted_messages),
            "archive_ref": metadata.get("archive_ref") if metadata else "",
        }

    def _provider_context_window(self) -> int | None:
        try:
            window = int(getattr(self.provider, "context_window", 0) or 0)
        except (TypeError, ValueError):
            return None
        return window if window > 0 else None

    def _session_id(self) -> str:
        if self.scratchpad and getattr(self.scratchpad, "session_id", ""):
            return str(self.scratchpad.session_id)
        return ""

    def _prepare_system_prompt(self, system_prompt: str) -> str:
        system_prompt += build_workflow_system_prompt(self.workflow)
        if not self.workflow:
            system_prompt += _DIRECT_TOOL_USE_PROMPT
        try:
            return self._append_skills_prompt(system_prompt)
        except Exception:
            logger.debug("Failed to load/inject skills into system prompt", exc_info=True)
            return system_prompt

    def _append_skills_prompt(self, system_prompt: str) -> str:
        from cli.skills import load_skills

        skills = load_skills()
        if not skills or not self._tool_allowed_for_prompt("execute_skill"):
            return system_prompt
        skills_text = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
        return (
            system_prompt
            + "\n\n<system-reminder>\n"
            + "The following skills are available for use with the execute_skill tool:\n\n"
            + f"{skills_text}\n\n"
            + "When a skill matches the user's intent, you should call the execute_skill tool first "
            + "to retrieve the detailed instructions, and then follow them to accomplish the task.\n"
            + "</system-reminder>"
        )

    def _collect_model_round(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        round_number: int,
    ) -> Iterator[RuntimeEvent | RoundState]:
        round_state = RoundState()
        stream = self.provider.chat_stream(messages, self._tool_schemas(), system_prompt)
        for chunk in _iter_with_timeout(stream, self.stream_chunk_timeout, self.cancel_check):
            event = self._consume_model_chunk(round_state, chunk, round_number)
            if event:
                yield event
        return round_state

    def _consume_model_chunk(
        self,
        round_state: RoundState,
        chunk: dict[str, Any],
        round_number: int,
    ) -> RuntimeEvent | None:
        chunk_type = chunk["type"]
        if chunk_type == "thinking_delta":
            round_state.thinking += chunk["text"]
            return {"type": "thinking_delta", "text": chunk["text"], "round": round_number}
        if chunk_type == "text_delta":
            round_state.text += chunk["text"]
            round_state.streamed = True
            return {"type": "text_delta", "text": chunk["text"], "round": round_number}
        if chunk_type == "tool_calls":
            round_state.tool_calls = chunk["tool_calls"]
            partial = chunk.get("text", "")
            if partial and not round_state.text:
                round_state.text = partial
            return {"type": "tool_calls", "tool_calls": round_state.tool_calls, "text": partial, "round": round_number}
        if chunk_type == "usage":
            round_state.usage = chunk
            return {"type": "usage", "usage": dict(round_state.usage), "round": round_number}
        return None

    def _accumulate_usage(self, state: RunState, round_state: RoundState) -> None:
        state.total_input += round_state.usage.get("input_tokens", 0)
        state.total_output += round_state.usage.get("output_tokens", 0)
        state.streamed = state.streamed or round_state.streamed

    def _record_thinking_event(self, round_state: RoundState, round_number: int) -> RuntimeEvent:
        if self.scratchpad:
            self.scratchpad.record_thinking(round_state.thinking)
        return {"type": "thinking", "text": round_state.thinking, "round": round_number}

    def _append_assistant_tool_message(self, messages: list[dict[str, Any]], round_state: RoundState) -> None:
        assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": round_state.tool_calls}
        if round_state.text:
            assistant_msg["content"] = round_state.text
        if round_state.thinking:
            assistant_msg["reasoning_content"] = round_state.thinking
        messages.append(assistant_msg)

    def _run_tool_batches(
        self,
        messages: list[dict[str, Any]],
        tool_calls: list[dict],
        state: RunState,
    ) -> Iterator[RuntimeEvent | bool]:
        answered_call_ids: set[str] = set()
        if order_error := self._required_tool_order_error(tool_calls, state):
            yield from self._append_aborted_tool_results(messages, tool_calls, answered_call_ids, error=order_error)
            return False
        for batch in partition_tool_calls(tool_calls, self.tools.concurrency_safe):
            self._ensure_not_cancelled()
            if batch["concurrent"] and len(batch["calls"]) > 1:
                if (
                    yield from self._execute_concurrent_batch(
                        batch["calls"],
                        messages,
                        state,
                        answered_call_ids,
                    )
                ):
                    yield from self._append_aborted_tool_results(messages, tool_calls, answered_call_ids)
                    return False
                continue
            if (
                yield from self._execute_serial_batch(
                    batch["calls"],
                    messages,
                    state,
                    answered_call_ids,
                )
            ):
                yield from self._append_aborted_tool_results(messages, tool_calls, answered_call_ids)
                return False
        return True

    def _required_tool_order_error(self, tool_calls: list[dict], state: RunState) -> str:
        if len(self.required_tools) < 2:
            return ""
        seen_this_round: set[str] = set()
        for call in tool_calls:
            name = str(call.get("name") or "")
            if name not in self.required_tools:
                continue
            missing = self._missing_required_predecessor(name, state, seen_this_round)
            if missing:
                return f"工具调用顺序错误：`{name}` 依赖 `{missing}` 的结果，必须先调用 `{missing}`。"
            seen_this_round.add(name)
        return ""

    def _missing_required_predecessor(self, name: str, state: RunState, seen_this_round: set[str]) -> str:
        index = self.required_tools.index(name)
        for predecessor in self.required_tools[:index]:
            if self._required_tool_satisfied(predecessor, state, seen_this_round):
                continue
            return predecessor
        return ""

    def _required_tool_satisfied(self, name: str, state: RunState, seen_this_round: set[str]) -> bool:
        if name in seen_this_round:
            return True
        return not self._missing_required_arg_sets(name, state)

    def _maybe_retry_required_tool(
        self,
        messages: list[dict[str, Any]],
        round_state: RoundState,
        state: RunState,
        expectation: Any,
    ) -> RuntimeEvent | None:
        if not missing_required_tool(expectation, state.used_tools):
            return None
        if state.incomplete_tool_retries >= MAX_INCOMPLETE_TOOL_RETRIES:
            return None
        retry_prompt = build_retry_user_message(expectation, round_state.text)
        state.incomplete_tool_retries += 1
        logger.info("loop_guard retry=%d required_tool=%s", state.incomplete_tool_retries, expectation.required_tool)
        self._append_retry_messages(messages, round_state, retry_prompt)
        return {
            "type": "retry",
            "message": retry_prompt,
            "retry": state.incomplete_tool_retries,
            "required_tool": expectation.required_tool if expectation else "",
        }

    def _maybe_retry_required_tool_args(
        self,
        messages: list[dict[str, Any]],
        round_state: RoundState,
        state: RunState,
        expectation: Any,
    ) -> RuntimeEvent | None:
        scoped_expectation = self._required_tools_expectation(state)
        active_expectation = scoped_expectation or self._available_expectation(expectation)
        if not active_expectation or not active_expectation.required_args:
            return None
        return self._maybe_retry_required_tool(messages, round_state, state, active_expectation)

    def _active_turn_expectation(
        self,
        messages: list[dict[str, Any]],
        state: RunState,
        expectation: Any,
    ) -> Any:
        if scoped_expectation := self._required_tools_expectation(state):
            return scoped_expectation
        if missing_required_tool(expectation, state.used_tools):
            return self._available_expectation(expectation)
        if not self.enforce_turn_expectations:
            return None
        return self._available_expectation(resolve_progressive_turn_expectation(messages, state.used_tools))

    def _natural_turn_expectation(self, messages: list[dict[str, Any]]) -> Any:
        if not self.enforce_turn_expectations:
            return None
        return self._available_expectation(resolve_turn_expectation(messages))

    def _required_tools_expectation(self, state: RunState) -> TurnExpectation | None:
        if not self.required_tools:
            return None
        for name in self.required_tools:
            missing_arg_sets = self._missing_required_arg_sets(name, state)
            if missing_arg_sets:
                return TurnExpectation(
                    required_tool=name,
                    reason=self._required_tool_reason(missing_arg_sets),
                    required_args=missing_arg_sets[0],
                )
        return None

    def _missing_required_arg_sets(self, name: str, state: RunState) -> list[dict[str, Any]]:
        arg_sets = self.required_tool_arg_sets.get(name)
        if arg_sets:
            return [
                required_args
                for required_args in arg_sets
                if missing_required_tool(TurnExpectation(name, "", required_args=required_args), state.used_tools)
            ]
        required_args = self.required_tool_args.get(name, {})
        expectation = TurnExpectation(name, "", required_args=required_args)
        return [required_args] if missing_required_tool(expectation, state.used_tools) else []

    def _required_tool_reason(self, missing_arg_sets: list[dict[str, Any]]) -> str:
        if not missing_arg_sets or not missing_arg_sets[0]:
            return "当前 workflow step 声明了必需工具，必须先运行对应工具获取真实数据。"
        reason = "当前 workflow step 声明了必需工具参数，必须按指定参数逐个运行工具获取真实数据。"
        if len(missing_arg_sets) > 1:
            targets = "；".join(_format_required_arg_set(args) for args in missing_arg_sets[:6])
            reason += f" 还缺少 {len(missing_arg_sets)} 组调用：{targets}。下一轮请一次性逐个调用这些参数组。"
        return reason

    def _available_expectation(self, expectation: Any) -> Any:
        if expectation is None:
            return None
        if self.allowed_tools is not None and expectation.required_tool not in self.allowed_tools:
            return None
        return expectation

    def _append_retry_messages(
        self,
        messages: list[dict[str, Any]],
        round_state: RoundState,
        retry_prompt: str,
    ) -> None:
        if round_state.text:
            retry_msg: dict[str, Any] = {
                "role": "assistant",
                "content": round_state.text,
                _INTERNAL_RETRY_MARKER: True,
            }
            if round_state.thinking:
                retry_msg["reasoning_content"] = round_state.thinking
            messages.append(retry_msg)
        messages.append({"role": "user", "content": retry_prompt, _INTERNAL_RETRY_MARKER: True})

    def _apply_missing_tool_warning(self, round_state: RoundState, state: RunState, expectation: Any) -> None:
        if missing_required_tool(expectation, state.used_tools):
            warning = build_retry_exhausted_warning(expectation, state.incomplete_tool_retries)
            round_state.text = f"{warning}\n\n{round_state.text}".strip()

    def _finish_turn(
        self,
        messages: list[dict[str, Any]],
        round_state: RoundState,
        state: RunState,
        rounds: int,
    ) -> RuntimeEvent:
        _drop_internal_retry_messages(messages)
        final_msg: dict[str, Any] = {"role": "assistant", "content": round_state.text}
        if round_state.thinking:
            final_msg["reasoning_content"] = round_state.thinking
        messages.append(final_msg)
        return self._done_event(round_state.text, state, rounds)

    def _finish_limit_turn(self, state: RunState) -> RuntimeEvent:
        return self._done_event("(Agent 工具调用轮次超限，已停止)", state, self.max_tool_rounds)

    def _done_event(self, text: str, state: RunState, rounds: int) -> RuntimeEvent:
        elapsed = time.monotonic() - state.started_at
        if self.scratchpad:
            self.scratchpad.record_final(
                text,
                input_tokens=state.total_input,
                output_tokens=state.total_output,
                elapsed_s=elapsed,
                provider=str(getattr(self.provider, "name", "")),
                model=str(getattr(self.provider, "model", "")),
            )
        return {
            "type": "done",
            "text": text,
            "streamed": state.streamed,
            "usage": {"input_tokens": state.total_input, "output_tokens": state.total_output},
            "elapsed": elapsed,
            "rounds": rounds,
        }

    def _execute_concurrent_batch(
        self,
        calls: list[dict],
        messages: list[dict[str, Any]],
        state: RunState,
        answered_call_ids: set[str],
    ) -> Iterator[RuntimeEvent | bool]:
        """Execute a concurrent-safe batch. Returns True on doom-loop break."""

        resumed_ids = self._resumed_tool_ids()
        to_run: list[dict] = []
        for call in calls:
            call_id = call["id"]
            state.used_tools.append((call["name"], call["args"]))
            if call_id in resumed_ids:
                yield from self._append_skipped_resumed_tool(messages, call)
                answered_call_ids.add(call_id)
                continue
            to_run.append(call)
        if not to_run:
            return False

        for call in to_run:
            yield self._tool_start_event(call, concurrent=True)

        with ThreadPoolExecutor(max_workers=min(len(to_run), 5)) as pool:
            futures = {pool.submit(self._execute_tool_call_raw, c, messages): c for c in to_run}
            for future in as_completed(futures):
                self._ensure_not_cancelled()
                call = futures[future]
                name = call["name"]
                args = call["args"]
                call_id = call["id"]

                if self._is_doom_loop(name, args, state):
                    yield self._append_doom_loop_result(messages, name, args, call_id)
                    answered_call_ids.add(call_id)
                    return True

                try:
                    res = future.result()
                    result = res["result"]
                    status = res["status"]
                    elapsed_ms = res["elapsed_ms"]
                except Exception as exc:
                    result = {"error": str(exc)}
                    status = "error"
                    elapsed_ms = 0

                yield from self._append_tool_result(
                    messages,
                    name,
                    args,
                    call_id,
                    result,
                    elapsed_ms=elapsed_ms,
                    status=status,
                )
                answered_call_ids.add(call_id)
        return False

    def _execute_serial_batch(
        self,
        calls: list[dict],
        messages: list[dict[str, Any]],
        state: RunState,
        answered_call_ids: set[str],
    ) -> Iterator[RuntimeEvent | bool]:
        for call in calls:
            self._ensure_not_cancelled()
            tool_event = yield from self._execute_single_tool(
                call,
                messages,
                state,
                answered_call_ids,
            )
            if tool_event == "doom":
                return True
        return False

    def _execute_single_tool(
        self,
        call: dict,
        messages: list[dict[str, Any]],
        state: RunState,
        answered_call_ids: set[str],
    ) -> Iterator[RuntimeEvent | str | None]:
        name = call["name"]
        args = call["args"]
        call_id = call["id"]
        state.used_tools.append((name, args))

        if call_id in self._resumed_tool_ids():
            yield from self._append_skipped_resumed_tool(messages, call)
            answered_call_ids.add(call_id)
            return None

        if self._is_doom_loop(name, args, state):
            yield self._append_doom_loop_result(messages, name, args, call_id)
            answered_call_ids.add(call_id)
            return "doom"

        if expectation := self._premature_question_expectation(name, messages, state):
            yield from self._append_premature_question_result(messages, name, args, call_id, expectation)
            answered_call_ids.add(call_id)
            return None

        yield self._tool_start_event(call)
        raw = self._execute_tool_call_raw(call, messages)
        yield from self._append_tool_result(
            messages,
            name,
            args,
            call_id,
            raw["result"],
            elapsed_ms=raw["elapsed_ms"],
            status=raw["status"],
        )
        answered_call_ids.add(call_id)
        return None

    def _append_skipped_resumed_tool(self, messages: list[dict[str, Any]], call: dict) -> Iterator[RuntimeEvent]:
        """Skip re-executing a tool_call_id already completed before hard resume."""

        result = {
            "skipped": True,
            "reason": "already_completed_before_resume",
            "note": "写操作不可盲目 skip；若需重新确认请明确要求用户。",
        }
        yield from self._append_tool_result(
            messages,
            call["name"],
            call.get("args") or {},
            call["id"],
            result,
            elapsed_ms=0,
            status="skipped",
        )

    def _tool_start_event(self, call: dict[str, Any], *, concurrent: bool = False) -> RuntimeEvent:
        display = call["name"]
        if self.tools and hasattr(self.tools, "display_name"):
            display = self.tools.display_name(call["name"])
        event = {
            "type": "tool_start",
            "name": call["name"],
            "args": call["args"],
            "tool_call_id": call["id"],
            # Progress contract fields
            "step": call["id"],
            "tool": call["name"],
            "display_name": display,
        }
        if concurrent:
            event["concurrent"] = True
        return event

    def _execute_tool_call_raw(
        self, call: dict[str, Any], messages: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        t_tool = time.monotonic()
        status = "ok"
        if self.allowed_tools is not None and call["name"] not in self.allowed_tools:
            return {
                "call": call,
                "result": {"error": f"工具 {call['name']} 不在当前 workflow 允许范围内"},
                "status": "error",
                "elapsed_ms": 0,
            }
        try:
            result = self.tools.execute(call["name"], call["args"], messages=messages)
            if isinstance(result, dict) and result.get("error"):
                status = "error"
        except Exception as exc:
            status = "error"
            result = {"error": str(exc)}
            logger.exception("tool execution failed: name=%s args=%s", call["name"], call["args"])
        return {
            "call": call,
            "result": result,
            "status": status,
            "elapsed_ms": int((time.monotonic() - t_tool) * 1000),
        }

    def _tool_schemas(self) -> list[dict[str, Any]]:
        try:
            return self.tools.schemas(self.allowed_tools)
        except TypeError:
            schemas = self.tools.schemas()
            if self.allowed_tools is None:
                return schemas
            return [schema for schema in schemas if schema.get("name") in self.allowed_tools]

    def _tool_allowed_for_prompt(self, name: str) -> bool:
        return self.allowed_tools is None or name in self.allowed_tools

    def _workflow_start_event(self) -> RuntimeEvent | None:
        workflow = self.workflow
        if not workflow or getattr(workflow, "is_general", False):
            return None
        return {
            "type": "workflow_start",
            "workflow": getattr(workflow, "name", ""),
            "label": getattr(workflow, "label", ""),
            "route": workflow.route_payload() if hasattr(workflow, "route_payload") else {},
            "allowed_tools": sorted(self.allowed_tools or []),
        }

    def _is_doom_loop(self, name: str, args: dict[str, Any], state: RunState) -> bool:
        return check_doom_loop(state.recent_calls, name, args, recent_args_texts=state.recent_args_texts)

    def _append_doom_loop_result(
        self,
        messages: list[dict[str, Any]],
        name: str,
        args: dict[str, Any],
        call_id: str,
    ) -> RuntimeEvent:
        logger.warning("doom-loop detected: %s", name)
        result = {"error": "doom-loop: 同参数重复调用3次，已中止"}
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
        return {"type": "tool_error", "name": name, "args": args, "tool_call_id": call_id, "error": result["error"]}

    def _append_aborted_tool_results(
        self,
        messages: list[dict[str, Any]],
        tool_calls: list[dict],
        answered_call_ids: set[str],
        *,
        error: str = "工具调用已因 doom-loop 中止",
    ) -> Iterator[RuntimeEvent]:
        result = {"error": error}
        for call in tool_calls:
            call_id = call["id"]
            if call_id in answered_call_ids:
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": call["name"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            answered_call_ids.add(call_id)
            yield {
                "type": "tool_error",
                "name": call["name"],
                "args": call["args"],
                "result": result,
                "tool_call_id": call_id,
                "error": result["error"],
                "status": "error",
                "elapsed_ms": 0,
                "content": json.dumps(result, ensure_ascii=False),
            }

    def _append_tool_result(
        self,
        messages: list[dict[str, Any]],
        name: str,
        args: dict[str, Any],
        call_id: str,
        result: Any,
        *,
        elapsed_ms: int,
        status: str,
    ) -> Iterator[RuntimeEvent]:
        if self.scratchpad:
            self.scratchpad.record_tool_result(name, args, result, duration_ms=elapsed_ms, status=status)

        content = format_tool_result_for_context(name, call_id, result)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": content,
            }
        )
        event_type = "tool_error" if status == "error" else "tool_result"
        display = name
        if self.tools and hasattr(self.tools, "display_name"):
            display = self.tools.display_name(name)
        event: RuntimeEvent = {
            "type": event_type,
            "name": name,
            "args": args,
            "result": result,
            "tool_call_id": call_id,
            "elapsed_ms": elapsed_ms,
            "status": status,
            "content": content,
            # Progress contract fields
            "step": call_id,
            "tool": name,
            "success": status != "error",
            "duration": elapsed_ms / 1000.0,
            "display_name": display,
        }
        if event_type == "tool_error":
            event["error"] = str(result.get("error", result)) if isinstance(result, dict) else str(result)
        yield event

    def _premature_question_expectation(
        self,
        name: str,
        messages: list[dict[str, Any]],
        state: RunState,
    ) -> Any:
        if name != "ask_user_question" or not self.enforce_turn_expectations:
            return None
        expectation = self._required_tools_expectation(state)
        return expectation if missing_required_tool(expectation, state.used_tools) else None

    def _append_premature_question_result(
        self,
        messages: list[dict[str, Any]],
        name: str,
        args: dict[str, Any],
        call_id: str,
        expectation: Any,
    ) -> Iterator[RuntimeEvent]:
        result = {
            "error": (
                "先不要向用户提问。"
                f"{expectation.reason} 请先调用 `{expectation.required_tool}` 获取真实数据；"
                "如果工具结果仍不足，再向用户澄清。"
            )
        }
        yield from self._append_tool_result(messages, name, args, call_id, result, elapsed_ms=0, status="error")
