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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from agents.stream_events import stream_event


class AgentCancelled(Exception):
    """Agent 运行被用户主动取消。"""


from cli.agent_loop import (
    CONTINUATION_PROMPT,
    MAX_AUTO_CONTINUATIONS,
    MAX_TOTAL_TOOL_ROUNDS,
    continuation_limit_message,
    decide_agent_loop,
    has_incomplete_tool_calls,
)
from cli.compaction import compact_messages, enforce_context_limit
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
from cli.prepare_tool_call import PrepareDecision, accept, prepare_allowed_tools, prepare_exists, reject
from cli.providers.base import LLMProvider
from cli.scratchpad import AgentScratchpad
from cli.text_repair import StreamTextRepair, repair_text
from cli.tool_results import format_tool_result_for_context
from cli.tools import ToolRegistry
from cli.usage_metrics import as_int, enrich_usage, generation_seconds
from cli.workflows.router import build_workflow_system_prompt

logger = logging.getLogger(__name__)

RuntimeEvent = dict[str, Any]

# 默认值；实际运行优先读 wyckoff.json 的 stream_chunk_timeout_seconds。
STREAM_CHUNK_TIMEOUT = 120.0
_INTERNAL_RETRY_MARKER = "_internal_retry"
_CONTINUATION_PARTIAL_MARKER = "_continuation_partial"
_STRICT_EXPECTATIONS_ENV = "WYCKOFF_STRICT_TOOL_EXPECTATIONS"
_STEER_MARKER = "_steering"
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


# 哪些流式字段是「一段正文的一部分」，会被网关拆到两个 chunk 里。
_STREAM_TEXT_FIELDS = ("text_delta", "thinking_delta")


def _repair_split_chars(stream):
    """接上被网关拆到两个 chunk 里的字符，落单的代理字符换成 U+FFFD。

    在这里做而不是各 provider 里做：三家 provider 都可能遇到，而下游 IPC、
    SQLite、JSONL 全是 strict UTF-8，漏一处就是整轮回答变一行报错。
    """
    repairs = {event_type: StreamTextRepair() for event_type in _STREAM_TEXT_FIELDS}

    def flush_all() -> Iterator[RuntimeEvent]:
        # 流结束在半个字符上时，攥着的尾巴要放出来，否则那点内容凭空消失。
        for event_type, repair in repairs.items():
            if tail := repair.flush():
                yield {"type": event_type, "text": tail}

    for chunk in stream:
        chunk_type = chunk.get("type") if isinstance(chunk, dict) else None
        if repair := repairs.get(chunk_type):
            # 攥住尾巴等下一块拼上，所以这块可能什么都不剩 —— 那就别发空事件。
            if fixed := repair.feed(str(chunk.get("text") or "")):
                yield {**chunk, "text": fixed}
            continue
        # 出现非正文事件说明这一段正文到此为止，攥着的尾巴得先放出来，才能保持
        # 「尾巴在前、本块在后」的顺序。
        yield from flush_all()
        if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
            # tool_calls 也带 text，是 provider 自己累计的原始串，没走上面的逐块修复。
            yield {**chunk, "text": repair_text(chunk["text"])}
        else:
            yield chunk
    yield from flush_all()


@dataclass
class RoundState:
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    streamed: bool = False
    finish_reason: str = ""
    stream_started: float | None = None
    first_content_at: float | None = None


@dataclass
class RunState:
    started_at: float
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    total_generation_ms: int = 0
    cache_reported: bool = False
    streamed: bool = False
    incomplete_tool_retries: int = 0
    auto_continuations: int = 0
    answer_parts: list[str] = field(default_factory=list)
    continuation_limit_hint: str = ""
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


def _merge_answer_text(parts: list[str], final: str) -> str:
    chunks = [part.strip() for part in parts if part and part.strip()]
    if final and final.strip():
        chunks.append(final.strip())
    return "\n\n".join(chunks)


def _unanswered_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return tool_calls from the latest assistant message that still lack a tool result."""

    assistant_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_idx = idx
            break
    if assistant_idx < 0:
        return []
    tool_calls = list(messages[assistant_idx].get("tool_calls") or [])
    answered = {
        str(msg.get("tool_call_id") or "")
        for msg in messages[assistant_idx + 1 :]
        if msg.get("role") == "tool" and msg.get("tool_call_id")
    }
    return [call for call in tool_calls if str(call.get("id") or "") and str(call["id"]) not in answered]


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
        steer_drain: Callable[[], list[str]] | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.scratchpad = scratchpad
        self.max_tool_rounds = max_tool_rounds
        self.cancel_check = cancel_check
        self.steer_drain = steer_drain
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
            # 取消后必须补齐未应答的 toolResult，避免历史出现裸 tool_call。
            yield from self._pair_unanswered_tool_calls(messages, error="Operation aborted")
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
        if workflow_event := self._workflow_start_event():
            yield workflow_event

        total_rounds = 0
        while True:
            segment = yield from self._run_round_segment(
                messages, system_prompt, state, expectation, model_name, total_rounds
            )
            total_rounds = int(segment["rounds"])
            if segment["kind"] == "done":
                yield segment["event"]
                return
            cont = yield from self._maybe_auto_continue(
                messages,
                state,
                finish_reason="step_limit",
                step_count=total_rounds,
                has_tool_calls=bool(state.used_tools),
                unfinished_required_work=False,
                round_state=None,
            )
            if cont:
                continue
            yield self._finish_limit_turn(state)
            return

    def _run_round_segment(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        state: RunState,
        expectation: Any,
        model_name: str,
        base_rounds: int,
    ) -> Iterator[RuntimeEvent | dict[str, Any]]:
        for offset in range(self.max_tool_rounds):
            round_number = base_rounds + offset + 1
            if round_number > MAX_TOTAL_TOOL_ROUNDS:
                return {"kind": "limit", "rounds": base_rounds + offset}
            self._ensure_not_cancelled()
            if steer_event := self._inject_steering(messages):
                yield steer_event
            messages, event = self._compact_if_needed(messages, model_name, self._provider_context_window())
            if event:
                yield event
            if offset > 0 or base_rounds > 0:
                yield {"type": "model_start", "round": round_number}
            yield stream_event("stage_start", stage="model", round=round_number, message="正在分析")
            try:
                round_state = yield from self._collect_model_round(messages, system_prompt, round_number)
            except Exception:
                yield stream_event("stage_done", stage="model", round=round_number, success=False)
                raise
            yield stream_event("stage_done", stage="model", round=round_number, success=True)
            self._accumulate_usage(state, round_state)
            if round_state.thinking:
                yield self._record_thinking_event(round_state, round_number)
            outcome = yield from self._handle_round_outcome(messages, round_state, state, expectation, round_number)
            if outcome == "continue":
                continue
            if isinstance(outcome, dict):
                return outcome
        return {"kind": "limit", "rounds": base_rounds + self.max_tool_rounds}

    def _handle_round_outcome(
        self,
        messages: list[dict[str, Any]],
        round_state: RoundState,
        state: RunState,
        expectation: Any,
        round_number: int,
    ) -> Iterator[RuntimeEvent | str | dict[str, Any]]:
        if has_incomplete_tool_calls(round_state.tool_calls):
            raise RuntimeError("模型在工具参数尚未完整生成时中断，本轮无法安全续跑。请输入「继续」补齐缺失步骤。")
        if round_state.tool_calls:
            self._append_assistant_tool_message(messages, round_state)
            completed = yield from self._run_tool_batches(messages, round_state.tool_calls, state)
            if completed:
                if retry_event := self._maybe_retry_required_tool_args(messages, round_state, state, expectation):
                    yield retry_event
                return "continue"
        active_expectation = self._active_turn_expectation(messages, state, expectation)
        if retry_event := self._maybe_retry_required_tool(messages, round_state, state, active_expectation):
            yield retry_event
            return "continue"
        if steer_event := self._inject_steering(messages, round_state=round_state):
            yield steer_event
            return "continue"
        unfinished = bool(missing_required_tool(active_expectation, state.used_tools))
        # step-limit 只由外层 segment 耗尽触发；此处 has_tool_calls 恒为 False，
        # 避免「第 N 轮自然 stop」被累计 used_tools 误判成续跑。
        continued = yield from self._maybe_auto_continue(
            messages,
            state,
            finish_reason=round_state.finish_reason or "stop",
            step_count=round_number,
            has_tool_calls=False,
            unfinished_required_work=unfinished and state.incomplete_tool_retries >= MAX_INCOMPLETE_TOOL_RETRIES,
            round_state=round_state,
        )
        if continued:
            return "continue"
        self._apply_missing_tool_warning(round_state, state, active_expectation)
        return {
            "kind": "done",
            "rounds": round_number,
            "event": self._finish_turn(messages, round_state, state, round_number),
        }

    def _maybe_auto_continue(
        self,
        messages: list[dict[str, Any]],
        state: RunState,
        *,
        finish_reason: str,
        step_count: int,
        has_tool_calls: bool,
        unfinished_required_work: bool,
        round_state: RoundState | None,
    ) -> Iterator[RuntimeEvent | bool]:
        decision = decide_agent_loop(
            finish_reason=finish_reason,
            step_count=step_count,
            max_steps=self.max_tool_rounds,
            has_tool_calls=has_tool_calls,
            unfinished_required_work=unfinished_required_work,
        )
        if decision.kind == "error":
            raise RuntimeError(decision.message)
        if decision.kind != "continue" or not decision.reason:
            return False
        if state.auto_continuations >= MAX_AUTO_CONTINUATIONS or step_count >= MAX_TOTAL_TOOL_ROUNDS:
            self._note_continuation_limit(state, round_state, decision.reason)
            return False
        state.auto_continuations += 1
        if round_state and (round_state.text or round_state.thinking):
            if round_state.text:
                state.answer_parts.append(round_state.text)
            partial: dict[str, Any] = {
                "role": "assistant",
                "content": round_state.text,
                _CONTINUATION_PARTIAL_MARKER: True,
            }
            if round_state.thinking:
                partial["reasoning_content"] = round_state.thinking
            messages.append(partial)
            round_state.text = ""
            round_state.streamed = False
        messages.append({"role": "user", "content": CONTINUATION_PROMPT, _INTERNAL_RETRY_MARKER: True})
        yield {
            "type": "continuation",
            "reason": decision.reason,
            "n": state.auto_continuations,
            "message": CONTINUATION_PROMPT,
        }
        return True

    def _note_continuation_limit(
        self,
        state: RunState,
        round_state: RoundState | None,
        reason: str,
    ) -> None:
        hint = continuation_limit_message(reason)  # type: ignore[arg-type]
        state.continuation_limit_hint = hint
        if round_state is None:
            return
        round_state.text = f"{round_state.text}\n\n{hint}".strip() if round_state.text else hint

    def _inject_steering(
        self,
        messages: list[dict[str, Any]],
        *,
        round_state: RoundState | None = None,
    ) -> RuntimeEvent | None:
        items = self.steer_drain() if self.steer_drain else []
        if not items:
            return None
        if round_state and round_state.text:
            # 打断前的正文是真实对话，必须保留（不能打 internal-retry 删除标记）。
            messages.append({"role": "assistant", "content": round_state.text})
            round_state.text = ""
            round_state.streamed = False
        joined = "\n".join(f"- {item}" for item in items)
        prompt = (
            "<steering>\n"
            "用户在本轮执行中途注入了新指令，请立即按以下要求调整后续行动"
            "（可复用已完成的只读工具结果，不要无必要重复）：\n"
            f"{joined}\n"
            "</steering>"
        )
        messages.append({"role": "user", "content": prompt, _STEER_MARKER: True})
        return {"type": "steered", "texts": list(items), "count": len(items)}

    def _prepare_tool_call(
        self,
        call: dict[str, Any],
        messages: list[dict[str, Any]],
        state: RunState,
    ) -> PrepareDecision:
        name = str(call.get("name") or "")
        args = dict(call.get("args") or {})
        if blocked := prepare_allowed_tools(name, args, self.allowed_tools):
            return blocked
        known = True
        if hasattr(self.tools, "has_tool"):
            known = bool(self.tools.has_tool(name))
        elif hasattr(self.tools, "schemas"):
            known = any(schema.get("name") == name for schema in self.tools.schemas())
        if blocked := prepare_exists(name, args, known=known):
            return blocked
        if hasattr(self.tools, "prepare"):
            prepared = self.tools.prepare(name, args)
            if isinstance(prepared, PrepareDecision) and prepared.action != "accept":
                return prepared
            if isinstance(prepared, PrepareDecision):
                args = prepared.args
        if expectation := self._premature_question_expectation(name, messages, state):
            return reject(
                "premature_question",
                (
                    "先不要向用户提问。"
                    f"{expectation.reason} 请先调用 `{expectation.required_tool}` 获取真实数据；"
                    "如果工具结果仍不足，再向用户澄清。"
                ),
                args=args,
            )
        return accept(args)

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
        # 压缩可能未生效（摘要失败）或不足（tail 本身超窗），兜底硬截断，
        # 否则请求会被网关以 400 拒绝。
        compacted_messages, overflow = enforce_context_limit(compacted_messages, model_name, context_window)
        if overflow:
            messages[:] = compacted_messages
            if self.scratchpad:
                self.scratchpad.record_compaction(
                    before_messages=prev_len,
                    after_messages=len(compacted_messages),
                    metadata={"contextOverflow": overflow},
                )
            return messages, {
                "type": "context_overflow",
                "before_messages": prev_len,
                "after_messages": len(compacted_messages),
                "dropped_messages": overflow["dropped_messages"],
                "limit": overflow["limit"],
            }
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
        round_state = RoundState(stream_started=time.monotonic())
        stream = self.provider.chat_stream(messages, self._tool_schemas(), system_prompt)
        guarded = _repair_split_chars(_iter_with_timeout(stream, self.stream_chunk_timeout, self.cancel_check))
        for chunk in guarded:
            event = self._consume_model_chunk(round_state, chunk, round_number)
            if event:
                yield event
        self._finalize_round_usage(round_state)
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
            self._mark_first_content(round_state)
            return {"type": "thinking_delta", "text": chunk["text"], "round": round_number}
        if chunk_type == "text_delta":
            round_state.text += chunk["text"]
            round_state.streamed = True
            self._mark_first_content(round_state)
            return {"type": "text_delta", "text": chunk["text"], "round": round_number}
        if chunk_type == "tool_calls":
            round_state.tool_calls = chunk["tool_calls"]
            partial = chunk.get("text", "")
            if partial and not round_state.text:
                round_state.text = partial
            return {"type": "tool_calls", "tool_calls": round_state.tool_calls, "text": partial, "round": round_number}
        if chunk_type == "usage":
            round_state.usage = dict(chunk)
            self._finalize_round_usage(round_state)
            return {"type": "usage", "usage": dict(round_state.usage), "round": round_number}
        if chunk_type == "finish":
            round_state.finish_reason = str(chunk.get("reason") or "")
            return None
        return None

    @staticmethod
    def _mark_first_content(round_state: RoundState) -> None:
        if round_state.first_content_at is None:
            round_state.first_content_at = time.monotonic()

    def _finalize_round_usage(self, round_state: RoundState) -> None:
        if not round_state.usage:
            return
        if "generation_ms" in round_state.usage and "output_tok_per_s" in round_state.usage:
            return
        gen_s = generation_seconds(
            stream_started=round_state.stream_started,
            first_content_at=round_state.first_content_at,
            ended_at=time.monotonic(),
        )
        round_state.usage = enrich_usage(
            round_state.usage,
            generation_ms=int(round(gen_s * 1000)),
            cache_reported="cache_read_tokens" in round_state.usage,
        )

    def _accumulate_usage(self, state: RunState, round_state: RoundState) -> None:
        usage = round_state.usage
        state.total_input += as_int(usage.get("input_tokens"))
        state.total_output += as_int(usage.get("output_tokens"))
        state.total_cache_read += as_int(usage.get("cache_read_tokens"))
        state.total_cache_write += as_int(usage.get("cache_write_tokens"))
        state.total_generation_ms += as_int(usage.get("generation_ms"))
        state.cache_reported = state.cache_reported or bool(usage.get("cache_reported"))
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
        if round_state.text or round_state.thinking:
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
        messages[:] = [m for m in messages if not m.get(_CONTINUATION_PARTIAL_MARKER)]
        full_text = _merge_answer_text(state.answer_parts, round_state.text)
        final_msg: dict[str, Any] = {"role": "assistant", "content": full_text}
        if round_state.thinking and not state.answer_parts:
            final_msg["reasoning_content"] = round_state.thinking
        messages.append(final_msg)
        return self._done_event(full_text, state, rounds)

    def _finish_limit_turn(self, state: RunState) -> RuntimeEvent:
        text = state.continuation_limit_hint or "(Agent 工具调用轮次超限，已停止)"
        return self._done_event(text, state, self.max_tool_rounds)

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
        usage_payload: dict[str, Any] = {
            "input_tokens": state.total_input,
            "output_tokens": state.total_output,
            "generation_ms": state.total_generation_ms,
        }
        if state.cache_reported:
            usage_payload["cache_read_tokens"] = state.total_cache_read
            usage_payload["cache_write_tokens"] = state.total_cache_write
        usage = enrich_usage(usage_payload, cache_reported=state.cache_reported)
        return {
            "type": "done",
            "text": text,
            "streamed": state.streamed,
            "usage": usage,
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
        """Execute a concurrent-safe batch. Returns True on doom-loop break.

        Results are appended in the original tool_call order (not completion order).
        """

        resumed_ids = self._resumed_tool_ids()
        to_run: list[dict] = []
        for call in calls:
            call_id = call["id"]
            state.used_tools.append((call["name"], call["args"]))
            if call_id in resumed_ids:
                yield from self._append_skipped_resumed_tool(messages, call)
                answered_call_ids.add(call_id)
                continue
            prepared = self._prepare_tool_call(call, messages, state)
            if prepared.action == "reject":
                yield from self._append_tool_result(
                    messages,
                    call["name"],
                    call["args"],
                    call_id,
                    prepared.error_result(),
                    elapsed_ms=0,
                    status="error",
                )
                answered_call_ids.add(call_id)
                continue
            to_run.append({**call, "args": prepared.args})
        if not to_run:
            return False

        for call in to_run:
            if self.scratchpad:
                self.scratchpad.record_tool_start(call["name"], call.get("args") or {}, tool_call_id=call["id"])
            yield self._tool_start_event(call, concurrent=True)

        completed: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(len(to_run), 5)) as pool:
            futures = {pool.submit(self._execute_tool_call_raw, c, messages): c for c in to_run}
            pending = set(futures)
            while pending:
                self._ensure_not_cancelled()
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    call = futures[future]
                    call_id = call["id"]
                    try:
                        res = future.result()
                        completed[call_id] = {
                            "result": res["result"],
                            "status": res["status"],
                            "elapsed_ms": res["elapsed_ms"],
                        }
                    except Exception as exc:
                        completed[call_id] = {
                            "result": {"error": str(exc)},
                            "status": "error",
                            "elapsed_ms": 0,
                        }

        for call in to_run:
            call_id = call["id"]
            if call_id in answered_call_ids:
                continue
            name = call["name"]
            args = call["args"]
            if self._is_doom_loop(name, args, state):
                yield self._append_doom_loop_result(messages, name, args, call_id)
                answered_call_ids.add(call_id)
                return True
            payload = completed.get(call_id) or {
                "result": {"error": "Operation aborted"},
                "status": "error",
                "elapsed_ms": 0,
            }
            yield from self._append_tool_result(
                messages,
                name,
                args,
                call_id,
                payload["result"],
                elapsed_ms=int(payload["elapsed_ms"]),
                status=str(payload["status"]),
            )
            answered_call_ids.add(call_id)
        return False

    def _pair_unanswered_tool_calls(
        self,
        messages: list[dict[str, Any]],
        *,
        error: str,
    ) -> Iterator[RuntimeEvent]:
        unanswered = _unanswered_tool_calls(messages)
        if not unanswered:
            return
        answered: set[str] = set()
        yield from self._append_aborted_tool_results(messages, unanswered, answered, error=error)

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

        prepared = self._prepare_tool_call(call, messages, state)
        if prepared.action == "reject":
            yield from self._append_tool_result(
                messages,
                name,
                args,
                call_id,
                prepared.error_result(),
                elapsed_ms=0,
                status="error",
            )
            answered_call_ids.add(call_id)
            return None
        call = {**call, "args": prepared.args}
        args = prepared.args

        # 先落意图再执行。顺序很关键：反过来的话，工具执行中途被 kill
        # 就查不到「这次调用发生过」——而这些工具会真的改持仓、设止损。
        if self.scratchpad:
            self.scratchpad.record_tool_start(name, args, tool_call_id=call_id)
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
            self.scratchpad.record_tool_result(
                name, args, result, duration_ms=elapsed_ms, status=status, tool_call_id=call_id
            )

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
