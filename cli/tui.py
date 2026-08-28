"""
威科夫终端读盘室 — Textual TUI。

全屏布局：上方可滚动聊天区 + 下方固定输入框。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.highlighter import Highlighter
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from cli.conversation import ConversationSession, QueuedInput, TurnPhase, UserIntent
from cli.tui_followups import (
    FOLLOWUP_PLACEHOLDER,
    IDLE_PLACEHOLDER,
    FollowUpCancelRequested,
    FollowUpEditRequested,
    FollowUpsPanel,
    render_followups,
)
from cli.workflows.pending_reply import classify_pending_workflow_reply, is_pending_workflow_revision
from utils.tool_result_preview import serialize_tool_result, tool_result_brief_lines

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 禁用 kitty keyboard protocol（与 macOS 中文输入法冲突）
# CSI-u 序列格式: \x1b[ keycode ; modifiers ; text_codepoints u
# 中文 IME 产生的序列含冒号分隔的 Unicode codepoints，textual 无法解析
# 策略：输出侧阻止启用 kitty protocol + 输入侧将 CSI-u 解码为纯文本
# ---------------------------------------------------------------------------
_KITTY_ENABLE = "\x1b[>1u"
_KITTY_DISABLE = "\x1b[<u"
_CSI_U_IME_RE = re.compile(r"\x1b\[\d+(?::\d+)*;;([\d:]+)u")
_WORKFLOW_ID_RE = re.compile(r"\bwf_[A-Za-z0-9_-]+\b")
_BUSY_FORCE_EXIT_WINDOW = 1.5
_HARD_EXIT_DELAY = 1.0
_RECENT_WORKFLOW_FALLBACK_MAX_AGE_SECONDS = 24 * 60 * 60
_RECENT_WORKFLOW_FALLBACK_CLOCK_SKEW_SECONDS = 5 * 60
_WORKFLOW_HANDOFF_TOOL_ORDER = (
    ("last_screen_result", "screen_stocks"),
    ("last_recommendation_event_eval", "evaluate_recommendation_events"),
    ("last_ai_report", "generate_ai_report"),
    ("last_strategy_decision", "generate_strategy_decision"),
)
_WORKFLOW_HANDOFF_BY_TOOL = {item[1]: item for item in _WORKFLOW_HANDOFF_TOOL_ORDER}
_WORKFLOW_HANDOFF_BY_TOOL["recommendation_event_eval"] = (
    "last_recommendation_event_eval",
    "evaluate_recommendation_events",
)


def _decode_csi_u(m: re.Match[str]) -> str:
    text_field = m.group(1)
    try:
        return "".join(chr(int(cp)) for cp in text_field.split(":") if cp)
    except (ValueError, OverflowError):
        return m.group(0)


def _make_csi_u_input_thread(driver_self) -> None:
    """替换 run_input_thread：将 CSI-u 序列解码为纯文本后再交给 XTermParser。"""
    import os
    import selectors
    from codecs import getincrementaldecoder

    from textual._loop import loop_last
    from textual._xterm_parser import XTermParser

    selector = selectors.SelectSelector()
    selector.register(driver_self.fileno, selectors.EVENT_READ)
    fileno = driver_self.fileno
    EVENT_READ = selectors.EVENT_READ

    parser = XTermParser(driver_self._debug)
    feed = parser.feed
    tick = parser.tick
    utf8_decoder = getincrementaldecoder("utf-8")().decode

    def process_selector_events(selector_events, final=False):
        for last, (_selector_key, mask) in loop_last(selector_events):
            if mask & EVENT_READ:
                raw = os.read(fileno, 1024 * 4)
                unicode_data = utf8_decoder(raw, final=final and last)
                if not unicode_data:
                    break
                if "\x1b[" in unicode_data and "u" in unicode_data:
                    unicode_data = _CSI_U_IME_RE.sub(_decode_csi_u, unicode_data)
                if unicode_data:
                    for event in feed(unicode_data):
                        driver_self.process_message(event)
        for event in tick():
            driver_self.process_message(event)

    try:
        while not driver_self.exit_event.is_set():
            process_selector_events(selector.select(0.1))
        selector.unregister(driver_self.fileno)
        process_selector_events(selector.select(0.1), final=True)
    finally:
        selector.close()
        try:
            for _event in feed(""):
                pass
        except Exception:
            pass


def _patch_driver_no_kitty() -> None:
    from textual.drivers.linux_driver import LinuxDriver

    _orig_write = LinuxDriver.write

    def _filtered_write(self, data: str) -> None:
        if _KITTY_ENABLE in data or _KITTY_DISABLE in data:
            data = data.replace(_KITTY_ENABLE, "").replace(_KITTY_DISABLE, "")
            if not data:
                return
        _orig_write(self, data)

    LinuxDriver.write = _filtered_write
    LinuxDriver.run_input_thread = _make_csi_u_input_thread


_patch_driver_no_kitty()

# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------
from cli.runtime import AgentCancelled
from cli.scratchpad import AgentScratchpad
from cli.tools import TOOL_SPECS
from cli.workflows.dispatch import build_turn_runtime
from cli.workflows.executor import WorkflowExecutor
from cli.workflows.router import WORKFLOWS
from core.prompts import append_beijing_time_context, with_current_time


def _pop_lines(log_widget, n: int) -> None:
    """从 RichLog 底部移除 n 行 strips。"""
    from textual.geometry import Size

    if n > 0 and len(log_widget.lines) >= n:
        del log_widget.lines[-n:]
        if hasattr(log_widget, "_line_cache"):
            log_widget._line_cache.clear()
        log_widget.virtual_size = Size(log_widget._widest_line_width, len(log_widget.lines))
        log_widget.refresh()


def _get_agent_logger() -> logging.Logger:
    agent_log = logging.getLogger("wyckoff.agent")
    agent_log.setLevel(logging.DEBUG)
    if not agent_log.handlers:
        try:
            from core.constants import LOCAL_DB_PATH

            log_path = LOCAL_DB_PATH.parent / "agent.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(str(log_path), encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            agent_log.addHandler(fh)
            agent_log.propagate = False
        except Exception:
            logger.debug("agent log file handler setup failed", exc_info=True)
    return agent_log


def _write_counted(log_widget, renderable) -> int:
    """写入 RichLog，并返回实际新增的 visual strips 数。"""
    before = len(log_widget.lines)
    log_widget.write(renderable)
    return max(0, len(log_widget.lines) - before)


def _refresh_log_layout(log_widget) -> None:
    try:
        log_widget.refresh(layout=True)
    except TypeError:
        log_widget.refresh()


_MD_RENDER_HINT_RE = re.compile(
    r"(?m)"
    r"(^\s{0,3}#{1,6}\s)"  # headings
    r"|(^\s*([-*+]|\d+\.)\s)"  # lists
    r"|(\*\*[^*]|\[[^\]]+\]\([^)]+\))"  # bold / links
    r"|(`{1,3})"  # code
    r"|(^\|.+\|)"  # tables
)


def _needs_markdown_render(text: str) -> bool:
    """Plain streamed strips are enough when Rich Markdown would not change layout."""
    return bool(_MD_RENDER_HINT_RE.search(text))


def _replace_streamed_response(log_widget, strip_count: int, final_text: str) -> int:
    _get_agent_logger().info("TUI_STREAM_REPLACE: strip_count=%d text_len=%d", strip_count, len(final_text))
    _pop_lines(log_widget, strip_count)
    added = _write_counted(log_widget, Markdown(final_text))
    log_widget.call_after_refresh(_settle_markdown_render, log_widget)
    return added


def _settle_markdown_render(log_widget) -> None:
    _refresh_log_layout(log_widget)
    if hasattr(log_widget, "scroll_end"):
        log_widget.scroll_end(animate=False)


def _display_final_response(
    log_widget,
    final_text: str,
    *,
    streaming_started: bool,
    stream_separator_strips: int,
    stream_text_strips: int,
    write,
    call_from_thread,
) -> bool:
    _get_agent_logger().info(
        "TUI_DISPLAY_FINAL: final_text_len=%d streaming_started=%s sep_strips=%d text_strips=%d",
        len(final_text) if final_text else 0,
        streaming_started,
        stream_separator_strips,
        stream_text_strips,
    )
    if not final_text:
        return False
    if streaming_started:
        # Keep plain streamed lines when Markdown would not change visual structure.
        if not _needs_markdown_render(final_text):
            return True
        strip_count = stream_separator_strips + stream_text_strips
        call_from_thread(_replace_streamed_response, log_widget, strip_count, final_text)
    else:
        write(Text.from_markup("  [dim]───[/dim]"))
        write(Markdown(final_text) if _needs_markdown_render(final_text) else Text(final_text))
    return True


@dataclass
class _StreamViewState:
    separator_strips: int = 0
    text_strips: int = 0
    started: bool = False
    line_buf: str = ""


@dataclass
class _TurnRunState:
    turn_user_index: int
    user_text: str
    scratchpad: AgentScratchpad | None
    model_name: str
    provider_name: str
    system_notification: bool = False
    workflow_run_id: str = ""
    workflow_name: str = ""
    executed_tool_summaries: list[dict[str, object]] = field(default_factory=list)
    round_usages: dict[int, dict[str, Any]] = field(default_factory=dict)
    round_tool_names: dict[int, list[str]] = field(default_factory=dict)
    round_starts: dict[int, float] = field(default_factory=dict)
    round_thinking: dict[int, str] = field(default_factory=dict)
    final_usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    final_elapsed: float = 0.0
    final_rounds: int = 0
    last_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class _WorkflowOverride:
    source_run_id: str
    script: dict[str, Any]
    context: Any
    args: Any = None
    only_step_id: str = ""


@dataclass
class _PendingWorkflowLaunch:
    runtime: WorkflowExecutor
    messages: list[dict[str, Any]]
    system_prompt: str
    model_name: str
    provider_name: str


@dataclass
class _PendingUserQuestion:
    question: str
    options: list[str]
    allow_free_text: bool
    default_answer: str
    event: threading.Event
    result: list[str]


@dataclass
class _AgentUiOps:
    log: Any
    write: Any
    write_stream: Any
    scroll: Any
    spinner_start: Any
    spinner_stop: Any


def _flush_stream_line(stream: _StreamViewState, write_stream, scroll) -> None:
    if not stream.line_buf:
        return
    stream.text_strips += write_stream(Text(stream.line_buf))
    stream.line_buf = ""
    scroll()


def _clear_streamed_block(app, log_widget, stream: _StreamViewState, *, include_separator: bool) -> None:
    strip_count = stream.text_strips
    if include_separator:
        strip_count += stream.separator_strips
    if stream.started and strip_count > 0:
        app.call_from_thread(_pop_lines, log_widget, strip_count)
    stream.text_strips = 0
    if include_separator:
        stream.separator_strips = 0
        stream.started = False


def _append_stream_text(stream: _StreamViewState, text: str, write_stream, scroll, spinner_stop) -> None:
    stream.line_buf += text
    if not stream.started:
        spinner_stop()
        stream.separator_strips += write_stream(Text.from_markup("  [dim]───[/dim]"))
        stream.started = True
    while "\n" in stream.line_buf:
        line, stream.line_buf = stream.line_buf.split("\n", 1)
        stream.text_strips += write_stream(Text(line))
        scroll()


def _display_stream_final(app, log_widget, stream: _StreamViewState, final_text: str, write, scroll) -> None:
    displayed = _display_final_response(
        log_widget,
        final_text,
        streaming_started=stream.started,
        stream_separator_strips=stream.separator_strips,
        stream_text_strips=stream.text_strips,
        write=write,
        call_from_thread=app.call_from_thread,
    )
    if not displayed:
        return
    stream.separator_strips = 0
    stream.text_strips = 0
    stream.started = False
    scroll()
    app.call_from_thread(_settle_markdown_render, log_widget)
    app.call_from_thread(app.call_after_refresh, lambda: _settle_markdown_render(log_widget))


def _flush_and_clear_stream(app, ui: _AgentUiOps, stream: _StreamViewState) -> None:
    _flush_stream_line(stream, ui.write_stream, ui.scroll)
    _clear_streamed_block(app, ui.log, stream, include_separator=True)


def _tool_display_name(tools, name: str) -> str:
    return tools.display_name(name) if tools else name


_PERSISTED_TOOL_RESULT_MAX_CHARS = 20_000


def _persistable_tool_result(result: Any) -> tuple[Any, bool]:
    """把工具结果压成可入库的形态，过大就截断并标记，避免 chat_log 被单条撑爆。

    必须走 serialize_tool_result 再 loads 回来：工具结果里会混进 date / Timestamp /
    NaN / numpy 标量，直接塞进 summary 会让整轮 executed_tool_summaries 的 dumps 抛
    TypeError，连带把这一轮全部 span 丢掉。
    """
    if result is None:
        return None, False
    try:
        rendered = serialize_tool_result(result)
    except (TypeError, ValueError):
        return str(result)[:_PERSISTED_TOOL_RESULT_MAX_CHARS], True
    if len(rendered) > _PERSISTED_TOOL_RESULT_MAX_CHARS:
        return rendered[:_PERSISTED_TOOL_RESULT_MAX_CHARS], True
    return json.loads(rendered), False


def _tool_result_view(event: dict[str, Any], tools) -> tuple[dict[str, object], Text]:
    name = event["name"]
    args = event.get("args", {})
    display = _tool_display_name(tools, name)
    result = event.get("result")
    if result is None and event.get("error"):
        result = {"error": event["error"]}
    elapsed_s = float(event.get("elapsed_ms", 0)) / 1000
    summary: dict[str, object] = {
        "name": name,
        "display_name": display,
        "args_brief": str(args)[:100],
        "args": args,
        "elapsed_ms": event.get("elapsed_ms", 0),
    }
    if call_id := event.get("tool_call_id"):
        summary["tool_call_id"] = call_id
    persisted_result, truncated = _persistable_tool_result(result)
    if persisted_result is not None:
        summary["result"] = persisted_result
        if truncated:
            summary["result_truncated"] = True

    if isinstance(result, dict) and result.get("error"):
        summary.update({"status": "error", "error": str(result.get("error", ""))[:160]})
        renderable = Text.from_markup(
            f"  [red]✗ {display}[/red] [dim]{elapsed_s:.1f}s {str(result['error'])[:80]}[/dim]"
        )
    elif isinstance(result, dict) and result.get("status") == "background":
        summary["status"] = "background"
        renderable = Text.from_markup(f"  [cyan]↗ {display}[/cyan] [dim]已提交后台[/dim]")
    else:
        summary["status"] = event.get("status", "ok")
        renderable = Text.from_markup(f"  [green]✓[/green] {display} [dim]{elapsed_s:.1f}s[/dim]")
        if brief_lines := tool_result_brief_lines(name, result):
            summary["brief"] = brief_lines
            for line in brief_lines:
                renderable.append(f"\n    {line}", style="dim")
    return summary, renderable


def _display_tool_result_event(event: dict[str, Any], tools, write, scroll) -> dict[str, object]:
    summary, renderable = _tool_result_view(event, tools)
    write(renderable)
    scroll()
    return summary


def _workflow_verbose_plan() -> bool:
    """默认只渲染进度骨架；路由/脚本来源这些内部信息用 /workflow show 或本开关查看。"""
    return os.getenv("WYCKOFF_WORKFLOW_VERBOSE", "").strip().lower() in {"1", "true", "yes", "on"}


def _display_workflow_plan_event(
    event: dict[str, Any],
    write,
    scroll,
    *,
    launch_state: str = "running",
    verbose: bool | None = None,
) -> tuple[str, str]:
    run_id = str(event.get("run_id", ""))
    workflow_name = str(event.get("workflow", ""))
    label = str(event.get("label") or workflow_name)
    steps = event.get("plan", {}).get("steps", [])
    step_count = len(steps) if isinstance(steps, list) else 0
    count_text = _workflow_task_count_text(event.get("plan"), step_count)
    header_mark = {"pending_approval": "◇", "adapted": "↻"}.get(launch_state, "▸")
    lines = [
        f"  [bold cyan]{header_mark} workflow[/bold cyan] [bold]{escape(label)}[/bold]"
        f" [dim]{escape(run_id)}{count_text}[/dim]"
    ]
    if verbose is None:
        verbose = _workflow_verbose_plan()
    lines.extend(_workflow_plan_detail_lines(event, label, steps, step_count, launch_state, verbose))
    write(Text.from_markup("\n".join(line for line in lines if line)))
    scroll()
    return run_id, workflow_name


def _workflow_plan_detail_lines(
    event: dict[str, Any],
    label: str,
    steps: Any,
    step_count: int,
    launch_state: str,
    verbose: bool = False,
) -> list[str]:
    plan = event.get("plan", {})
    lines: list[str] = []
    if verbose:
        lines.extend(_workflow_plan_provenance_lines(event, plan, label))
    if not step_count:
        return lines
    lines.extend(_workflow_plan_tree_lines(steps))
    if verbose:
        lines.extend(_workflow_plan_execution_lines(steps))
    lines.append(_workflow_plan_footer_line(str(event.get("run_id", "")), launch_state))
    return lines


def _workflow_plan_provenance_lines(event: dict[str, Any], plan: Any, label: str) -> list[str]:
    lines: list[str] = []
    if route_line := _workflow_event_route_line(event):
        lines.append(route_line)
    script = plan.get("script") if isinstance(plan, dict) else {}
    script_title = str(script.get("title") or "") if isinstance(script, dict) else ""
    if script_title and script_title != label:
        lines.append(f"    [dim]动态脚本：{escape(script_title)}[/dim]")
    if planner_line := _workflow_planner_line(plan):
        lines.append(planner_line)
    if rationale_line := _workflow_script_rationale_line(plan):
        lines.append(rationale_line)
    if contract_line := _workflow_plan_contract_line(plan):
        lines.append(contract_line)
    return lines


def _workflow_plan_tree_lines(steps: Any, *, limit: int = 8) -> list[str]:
    """把计划渲染成带序号的阶段树，让"要跑几步、跑到哪一步"一眼可见。"""
    if not isinstance(steps, list):
        return []
    rows = [step for step in steps if isinstance(step, dict)]
    if not rows:
        return []
    visible = rows[:limit]
    lines: list[str] = []
    last_phase = ""
    for index, step in enumerate(visible, 1):
        phase = str(step.get("phase") or "").strip()
        if phase and phase != last_phase:
            lines.append(f"    [dim]· {escape(phase[:40])}[/dim]")
            last_phase = phase
        is_last = index == len(visible) and len(rows) <= limit
        glyph = "└" if is_last else "├"
        title = escape(str(step.get("title") or step.get("step_id") or step.get("id") or "task")[:48])
        tools, _label = _workflow_step_tool_values(step)
        tool_text = "、".join(_workflow_tool_display_name(tool) for tool in tools[:3])
        if len(tools) > 3:
            tool_text += f"、+{len(tools) - 3}"
        suffix = f" [dim]· {escape(tool_text)}[/dim]" if tool_text else ""
        lines.append(f"    [dim]{glyph}[/dim] [dim]{index}[/dim] {title}{suffix}")
    if len(rows) > limit:
        lines.append(f"    [dim]└ 另有 {len(rows) - limit} 个任务，详情见 /workflow show[/dim]")
    return lines


def _workflow_plan_footer_line(run_id: str, launch_state: str) -> str:
    detail = f"；详情用 /workflow show {escape(run_id)}" if run_id else ""
    if launch_state == "pending_approval":
        return f"    [dim]计划已准备好，等待批准{detail}[/dim]"
    if launch_state == "adapted":
        return f"    [dim]已根据阶段结果更新后续计划{detail}[/dim]"
    return f"    [dim]已交给 agent 动态执行，进度会按实际工具结果展开{detail}[/dim]"


def _workflow_event_route_line(event: dict[str, Any]) -> str:
    route = event.get("route")
    if not isinstance(route, dict):
        plan = event.get("plan") if isinstance(event.get("plan"), dict) else {}
        route = plan.get("route") if isinstance(plan.get("route"), dict) else {}
    return _workflow_route_line(route)


def _workflow_task_count_text(plan: Any, step_count: int) -> str:
    if step_count <= 0:
        return ""
    runtime = _workflow_plan_runtime(plan)
    original = _runtime_int(runtime, "original_step_count")
    truncated = _runtime_int(runtime, "truncated_step_count")
    if original > step_count and truncated > 0:
        return f" · {step_count}/{original} 个动态任务 · 已收敛 {truncated} 个过长任务"
    return f" · {step_count} 个动态任务"


def _workflow_plan_runtime(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    script = plan.get("script") if isinstance(plan.get("script"), dict) else {}
    runtime = script.get("runtime") if isinstance(script.get("runtime"), dict) else {}
    return runtime


def _workflow_planner_line(plan: Any) -> str:
    runtime = _workflow_plan_runtime(plan)
    planner = str(runtime.get("planner") or "").strip()
    if not planner:
        return ""
    label = _workflow_planner_label(planner, runtime)
    reason = _workflow_planner_reason(runtime, planner)
    suffix = f" · {escape(reason)}" if reason else ""
    return f"    [dim]脚本来源：{label}{suffix}[/dim]"


def _workflow_planner_label(planner: str, runtime: dict[str, Any]) -> str:
    if planner == "fallback_script" and runtime.get("fallback_kind") == "stock_selection":
        return "选股兜底"
    return {
        "model_script": "模型生成",
        "stored_script": "已保存脚本",
        "fallback_script": "回退单步",
    }.get(planner, escape(planner))


def _workflow_planner_reason(runtime: dict[str, Any], planner: str) -> str:
    if planner == "fallback_script":
        return _workflow_fallback_reason(runtime)
    reasons: list[str] = []
    if runtime.get("tool_contract_repair") == "model":
        reasons.append(_workflow_tool_contract_repair_reason(runtime))
    if runtime.get("adaptation") == "model_phase":
        reasons.append(_workflow_phase_adaptation_reason(runtime))
    if _runtime_int(runtime, "truncated_step_count") > 0:
        reasons.append("任务过长已自动收敛")
    return " · ".join(reason for reason in reasons if reason)


def _workflow_fallback_reason(runtime: dict[str, Any]) -> str:
    return str(runtime.get("fallback_reason") or "").strip()


def _workflow_tool_contract_repair_reason(runtime: dict[str, Any]) -> str:
    unscoped = _runtime_int(runtime, "unscoped_step_count_before_repair")
    if unscoped > 0:
        return f"模型已修订工具契约（修订前 {unscoped} 个任务未声明必用工具）"
    return "模型已修订工具契约"


def _workflow_phase_adaptation_reason(runtime: dict[str, Any]) -> str:
    count = _runtime_int(runtime, "adaptation_count")
    title = str(runtime.get("last_adaptation_title") or "").strip()
    suffix = f"：{escape(title[:48])}" if title else ""
    delta = _workflow_adaptation_delta_text(runtime)
    delta_suffix = f" · {escape(delta)}" if delta else ""
    if count > 0:
        return f"阶段结果已触发第 {count} 次模型改稿{suffix}{delta_suffix}"
    return f"阶段结果已触发模型改稿{suffix}{delta_suffix}"


def _workflow_adaptation_delta_text(runtime: dict[str, Any]) -> str:
    if runtime.get("adaptation_complete"):
        skipped = _runtime_int(runtime, "adapted_skipped_step_count") or _runtime_int(
            runtime, "adapted_removed_step_count"
        )
        if stopped := _workflow_adapted_step_titles(runtime.get("adapted_removed_steps")):
            return f"后续变更：停止 {'、'.join(stopped)}"
        return f"后续变更：停止 {skipped} 个任务" if skipped else "后续变更：停止剩余任务"
    parts: list[str] = []
    parts.extend(
        part
        for part in (
            _workflow_named_adaptation_part("保留", runtime, "adapted_kept_steps", "adapted_kept_step_count"),
            _workflow_named_adaptation_part("移除", runtime, "adapted_removed_steps", "adapted_removed_step_count"),
            _workflow_named_adaptation_part("新增", runtime, "adapted_added_steps", "adapted_added_step_count"),
        )
        if part
    )
    return f"后续变更：{'、'.join(parts)}" if parts else ""


def _workflow_named_adaptation_part(label: str, runtime: dict[str, Any], steps_field: str, count_field: str) -> str:
    titles = _workflow_adapted_step_titles(runtime.get(steps_field))
    if titles:
        return f"{label} {'、'.join(titles)}"
    if count := _runtime_int(runtime, count_field):
        return f"{label} {count} 个"
    return ""


def _workflow_adapted_step_titles(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    titles = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("id") or "").strip()
        if title:
            titles.append(escape(title[:32]))
    return titles


def _workflow_script_rationale_line(plan: Any) -> str:
    if not isinstance(plan, dict):
        return ""
    script = plan.get("script") if isinstance(plan.get("script"), dict) else {}
    rationale = _workflow_meta_text(script.get("rationale"), 120)
    return f"    [dim]模型拆分：{escape(rationale)}[/dim]" if rationale else ""


def _workflow_plan_contract_line(plan: Any) -> str:
    if not isinstance(plan, dict):
        return ""
    planner = str(_workflow_plan_runtime(plan).get("planner") or "").strip()
    if planner not in {"model_script", "stored_script"}:
        return ""
    steps = [step for step in plan.get("steps", []) if isinstance(step, dict)]
    count = sum(1 for step in steps if _workflow_step_uses_optional_tool_pool(step))
    if count <= 0:
        return ""
    return f"    [dim]脚本边界：{count} 个任务未声明必用工具，agent 将从可选工具中自选[/dim]"


def _workflow_step_uses_optional_tool_pool(step: dict[str, Any]) -> bool:
    return not any(str(item) for item in step.get("tool_scope", [])) and any(
        str(item) for item in step.get("effective_tool_scope", [])
    )


def _runtime_int(runtime: dict[str, Any], field: str) -> int:
    try:
        return int(runtime.get(field, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _workflow_plan_step_preview_lines(steps: Any, *, limit: int = 3) -> list[str]:
    if not isinstance(steps, list):
        return []
    rows = [step for step in steps if isinstance(step, dict)]
    if not any(_workflow_plan_step_meta(step) for step in rows[:limit]):
        return []
    lines = [_workflow_plan_step_preview_line(index, step) for index, step in enumerate(rows[:limit], 1)]
    if len(rows) > limit:
        lines.append(f"    [dim]… 另有 {len(rows) - limit} 个任务可在详情中查看[/dim]")
    return [line for line in lines if line]


def _workflow_plan_step_preview_line(index: int, step: dict[str, Any]) -> str:
    title = escape(str(step.get("title") or step.get("step_id") or step.get("id") or "task")[:60])
    detail = _workflow_plan_step_meta(step, field_limit=72)
    suffix = f" · {detail}" if detail else ""
    return f"    [dim]{index}. {title}{suffix}[/dim]"


def _workflow_plan_execution_lines(steps: Any) -> list[str]:
    if not isinstance(steps, list):
        return []
    rows = [step for step in steps if isinstance(step, dict)]
    return [line for line in (_workflow_plan_handoff_line(rows), _workflow_plan_tool_boundary_line(rows)) if line]


def _workflow_plan_handoff_line(rows: list[dict[str, Any]], *, limit: int = 3) -> str:
    titles = [
        escape(str(step.get("title") or step.get("step_id") or step.get("id") or "").strip()[:48])
        for step in rows[:limit]
        if str(step.get("title") or step.get("step_id") or step.get("id") or "").strip()
    ]
    if not titles:
        return ""
    suffix = f" → +{len(rows) - limit}" if len(rows) > limit else ""
    return f"    [dim]执行接力：{' → '.join(titles)}{suffix}[/dim]"


def _workflow_plan_tool_boundary_line(rows: list[dict[str, Any]], *, limit: int = 6) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    has_inferred = False
    for step in rows:
        tools, _label = _workflow_step_tool_values(step)
        has_inferred = has_inferred or _workflow_step_tool_scope_source(step) == "semantic_inference"
        for tool in tools:
            if tool in seen:
                continue
            seen.add(tool)
            labels.append(_workflow_tool_display_name(tool))
    if not labels:
        return ""
    visible = "、".join(labels[:limit])
    suffix = f"、+{len(labels) - limit}" if len(labels) > limit else ""
    source = "（含语义恢复）" if has_inferred else ""
    return f"    [dim]工具边界{source}：{escape(visible + suffix)}[/dim]"


def _workflow_route_line(route: dict[str, Any]) -> str:
    reason = escape(str(route.get("reason", "") or ""))
    if not reason:
        return ""
    matches = [escape(item) for item in _workflow_visible_route_matches(route)]
    confidence = route.get("confidence")
    parts = [f"    [dim]识别原因：{reason}"]
    if matches:
        parts.append(f" · 命中：{', '.join(matches)}")
    if isinstance(confidence, (int, float)) and confidence > 0:
        parts.append(f" · 置信度：{confidence:.0%}")
    parts.append("[/dim]")
    return "".join(parts)


def _workflow_visible_route_matches(route: dict[str, Any]) -> list[str]:
    return [text for item in route.get("matches", []) if (text := str(item).strip()) and _is_visible_route_match(text)]


def _is_visible_route_match(text: str) -> bool:
    return not text.startswith("model_router") and not text.endswith("_guard")


def _workflow_step_position_text(event: dict[str, Any]) -> str:
    """[2/3] 这种位置标记，让后台跑到第几步一眼可见。"""
    try:
        index = int(event.get("step_index", 0) or 0)
        total = int(event.get("step_total", 0) or 0)
    except (TypeError, ValueError):
        return ""
    return f"[{index}/{total}] " if index > 0 and total > 0 else ""


def _display_workflow_step_event(event: dict[str, Any], write, scroll) -> None:
    step = event.get("step", {})
    status = step.get("status", "")
    mark = {"running": "→", "completed": "✓", "failed": "✗", "skipped": "·"}.get(status, "·")
    color = {"running": "yellow", "completed": "green", "failed": "red", "skipped": "dim"}.get(status, "dim")
    title = escape(str(step.get("title", "")))
    summary = _workflow_visible_summary(step)
    label = {"running": "运行中", "completed": "完成", "failed": "失败", "skipped": "跳过"}.get(status, status)
    meta = _workflow_step_live_meta(step, label)
    position = _workflow_step_position_text(event)
    suffix = f" {summary}" if summary else ""
    write(Text.from_markup(f"    [{color}]{mark} [dim]{position}[/dim]{title}[/{color}] [dim]{meta}{suffix}[/dim]"))
    for line in _workflow_step_handoff_lines(event):
        write(Text.from_markup(f"      [dim]└ 证据: {escape(line)}[/dim]"))
    scroll()


def _display_retry_event(event: dict[str, Any], write, scroll) -> None:
    retry = int(event.get("retry") or 0)
    tool_name = str(event.get("required_tool") or "").strip()
    display = _workflow_tool_display_name(tool_name) if tool_name else "必需工具"
    reason = _workflow_meta_text(event.get("message"), 96)
    suffix = f" · {escape(reason)}" if reason else ""
    write(Text.from_markup(f"  [yellow]⚠ 运行时校验：第 {retry} 次要求先调用 {escape(display)}{suffix}[/yellow]"))
    scroll()


def _display_continuation_event(event: dict[str, Any], write, scroll) -> None:
    reason = str(event.get("reason") or "")
    label = {
        "output-length": "回答达到上限，自动续写",
        "step-limit": "工具轮次达上限，自动续跑",
        "unfinished-work": "仍有未完成步骤，自动续跑",
    }.get(reason, "自动续跑")
    write(Text.from_markup(f"  [cyan]↻ {label}（{event.get('n', 1)}）[/cyan]"))
    scroll()


def _display_steered_event(event: dict[str, Any], write, scroll) -> None:
    texts = event.get("texts") or []
    preview = escape(str(texts[0])) if texts else ""
    write(Text.from_markup(f"  [cyan]⇢ 已注入转向: {preview}[/cyan]"))
    scroll()


def _workflow_detail_step_line(step: dict[str, Any], agent_detail: dict[str, Any] | None = None) -> str:
    step_id = escape(str(step.get("step_id") or step.get("id") or "task"))
    title = escape(str(step.get("title") or step_id))
    summary = escape(str(step.get("summary", "")))
    status = str(step.get("status", "") or "pending")
    meta = _workflow_step_meta(step, status, include_debug=True)
    detail = _workflow_step_detail_meta(step)
    dependency = _workflow_step_dependency_meta(step)
    suffix = " ".join(part for part in (summary, dependency, detail) if part)
    suffix = f" {suffix}" if suffix else ""
    line = f"    - [dim]{step_id}[/dim] {title} [dim]{meta}{suffix}[/dim]"
    evidence = [f"      [dim]证据: {escape(item)}[/dim]" for item in _workflow_handoff_lines_from_detail(agent_detail)]
    return "\n".join([line, *evidence])


def _workflow_step_meta(step: dict[str, Any], label: str, *, include_debug: bool = False) -> str:
    parts: list[str] = []
    if include_debug:
        parts.append(escape(str(step.get("agent", "") or "agent")))
        tool_scope, tool_label = _workflow_step_tool_values(step)
        tool_scope = [escape(item) for item in tool_scope]
        if tool_scope:
            parts.append(f"{tool_label}：{', '.join(tool_scope[:4])}")
            if len(tool_scope) > 4:
                parts.append(f"+{len(tool_scope) - 4}")
    parts.append(escape(label))
    return " · ".join(parts)


def _workflow_step_live_meta(step: dict[str, Any], label: str) -> str:
    parts = [item for item in (_workflow_step_tool_meta(step), escape(label)) if item]
    return " · ".join(parts)


def _workflow_step_handoff_lines(event: dict[str, Any], *, limit: int = 4) -> list[str]:
    step = event.get("step") if isinstance(event.get("step"), dict) else {}
    if step.get("status") != "completed":
        return []
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    detail = source.get("agent_detail") if isinstance(source.get("agent_detail"), dict) else {}
    return _workflow_handoff_lines_from_detail(detail, limit=limit)


def _workflow_handoff_lines_from_detail(detail: dict[str, Any] | None, *, limit: int = 4) -> list[str]:
    if not isinstance(detail, dict):
        return []
    handoff = detail.get("handoff_state") if isinstance(detail.get("handoff_state"), dict) else {}
    tool_calls = [str(item) for item in detail.get("tool_calls", []) if str(item)]
    return _workflow_handoff_brief_lines(handoff, limit=limit, tool_calls=tool_calls)


def _workflow_handoff_brief_lines(
    handoff: dict[str, Any], *, limit: int, tool_calls: list[str] | tuple[str, ...] = ()
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for key, tool_name in _workflow_handoff_tool_order(tool_calls):
        result = handoff.get(key)
        if not isinstance(result, dict):
            continue
        for line in tool_result_brief_lines(tool_name, result, max_lines=2):
            clipped = _workflow_meta_text(line, 180)
            if clipped and clipped not in seen:
                seen.add(clipped)
                lines.append(clipped)
            if len(lines) >= limit:
                return lines
    return lines


def _workflow_handoff_tool_order(tool_calls: list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tool_name in tool_calls:
        if item := _WORKFLOW_HANDOFF_BY_TOOL.get(tool_name):
            key = item[0]
            if key not in seen:
                seen.add(key)
                ordered.append(item)
    for item in _WORKFLOW_HANDOFF_TOOL_ORDER:
        key = item[0]
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


def _workflow_step_detail_meta(step: dict[str, Any], *, field_limit: int | None = None) -> str:
    labels = (
        ("rationale", "目标"),
        ("success_criteria", "验收"),
        ("risk_guard", "边界"),
    )
    parts = [
        f"{label}: {escape(_workflow_meta_text(step.get(field), field_limit))}"
        for field, label in labels
        if _workflow_meta_text(step.get(field), field_limit)
    ]
    if args_hint := _workflow_step_args_hint(step, field_limit):
        parts.append(f"参数: {escape(args_hint)}")
    return "；".join(parts)


def _workflow_step_args_hint(step: dict[str, Any], limit: int | None = None) -> str:
    if value := _workflow_meta_text(step.get("args_hint"), limit):
        return value
    text = str(step.get("context") or "")
    marker = "tool args hint:"
    if marker not in text:
        return ""
    value = text.split(marker, 1)[1].strip().split("\n\n", 1)[0].strip()
    return _workflow_meta_text(value, limit)


def _workflow_plan_step_meta(step: dict[str, Any], *, field_limit: int | None = None) -> str:
    parts = [
        item
        for item in (
            _workflow_step_tool_meta(step),
            _workflow_step_dependency_meta(step),
            _workflow_step_detail_meta(step, field_limit=field_limit),
        )
        if item
    ]
    return "；".join(parts)


def _workflow_step_dependency_meta(step: dict[str, Any]) -> str:
    deps = [escape(str(item)) for item in step.get("depends_on", []) if str(item)]
    if not deps:
        return ""
    visible = "、".join(deps[:4])
    suffix = f"、+{len(deps) - 4}" if len(deps) > 4 else ""
    return f"依赖: {visible}{suffix}"


def _workflow_step_tool_meta(step: dict[str, Any]) -> str:
    tools, label = _workflow_step_tool_values(step)
    labels = [_workflow_tool_display_name(item) for item in tools]
    if not labels:
        return ""
    visible = "、".join(labels[:4])
    suffix = f"、+{len(labels) - 4}" if len(labels) > 4 else ""
    return f"{label}: {escape(visible + suffix)}"


def _workflow_step_tool_values(step: dict[str, Any]) -> tuple[list[str], str]:
    scoped = [str(item) for item in step.get("tool_scope", []) if str(item)]
    if scoped:
        return scoped, "恢复工具" if _workflow_step_tool_scope_source(step) == "semantic_inference" else "工具"
    effective = [str(item) for item in step.get("effective_tool_scope", []) if str(item)]
    if effective:
        return effective, "可选工具"
    return [], "工具"


def _workflow_step_tool_scope_source(step: dict[str, Any]) -> str:
    return str(step.get("tool_scope_source") or "").strip()


def _workflow_tool_display_name(name: Any) -> str:
    text = str(name or "").strip()
    spec = TOOL_SPECS.get(text)
    return spec.display_name if spec else text


def _workflow_meta_text(value: Any, limit: int | None) -> str:
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        return text[:limit] + "..."
    return text


def _workflow_visible_summary(step: dict[str, Any]) -> str:
    summary = str(step.get("summary", "") or "").strip()
    if not summary:
        return ""
    for prefix in ("research:", "analysis:", "trading:", "agent:"):
        if summary.startswith(prefix):
            summary = summary[len(prefix) :].strip()
            break
    return "" if summary in {"start", "running"} else escape(summary)


def _display_workflow_phase_event(event: dict[str, Any], write, scroll) -> None:
    phase = escape(str(event.get("phase", "") or "phase"))
    steps = event.get("steps", [])
    count = len(steps) if isinstance(steps, list) else 0
    parallel = bool(event.get("parallel"))
    if event.get("type") == "workflow_phase_start":
        mode = "并发" if parallel else "顺序"
        write(Text.from_markup(f"    [bold]阶段[/bold] {phase} [dim]{mode} · {count} task[/dim]"))
    else:
        write(Text.from_markup(f"    [green]阶段完成[/green] [dim]{phase}[/dim]"))
    scroll()


def _display_workflow_disagreement_event(event: dict[str, Any], write, scroll) -> None:
    summary = event.get("summary") if isinstance(event.get("summary"), dict) else {}
    conflict_type = str(summary.get("conflict_type") or "")
    lines = ["  [bold yellow]多视角复核[/bold yellow]"]
    labels = (("bullish_agents", "偏多"), ("bearish_agents", "偏防御"), ("neutral_agents", "中性"))
    for key, direction in labels:
        for item in summary.get(key) or []:
            if not isinstance(item, dict):
                continue
            agent = _sub_agent_progress_label(item.get("agent", "task")).removesuffix(" agent")
            lines.append(f"    {escape(agent)}：{direction}")
    for item in summary.get("degraded_steps") or []:
        if not isinstance(item, dict):
            continue
        agent = _sub_agent_progress_label(item.get("agent", "task")).removesuffix(" agent")
        status = escape(str(item.get("status") or "failed"))
        lines.append(f"    [yellow]{escape(agent)} 未完成（{status}）[/yellow]")
    if conflict_type == "mixed_directional_signals":
        lines.append("    [yellow]多空存在分歧，最终结论已要求等待确认后再行动。[/yellow]")
    else:
        lines.append("    [yellow]输入不完整，最终结论已按保守模式降级。[/yellow]")
    write(Text.from_markup("\n".join(lines)))
    scroll()


def _workflow_script(run: dict[str, Any]) -> dict[str, Any]:
    script = run.get("plan", {}).get("script", {})
    return script if isinstance(script, dict) else {}


def _workflow_script_path(run: Any) -> str:
    if not run:
        return ""
    script = getattr(run, "script", {}) if not isinstance(run, dict) else _workflow_script(run)
    runtime = script.get("runtime", {}) if isinstance(script, dict) else {}
    return str(runtime.get("script_path", "") or "") if isinstance(runtime, dict) else ""


def _workflow_run_with_events(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("events"):
        return run
    try:
        from cli.workflows.store import load_workflow_events

        enriched = dict(run)
        enriched["events"] = load_workflow_events(str(run.get("run_id", "")), limit=120)
        return enriched
    except Exception:
        logger.debug("load workflow events for recent context failed", exc_info=True)
        return run


def _workflow_has_step(run: dict[str, Any], step_id: str) -> bool:
    for step in run.get("plan", {}).get("steps", []):
        if isinstance(step, dict) and str(step.get("step_id", "")) == step_id:
            return True
    return False


def _workflow_agent_detail_from_events(rows: list[dict[str, Any]], step_id: str) -> dict[str, Any]:
    return _workflow_agent_details_from_events(rows).get(step_id, {})


def _workflow_agent_details_from_events(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for row in reversed(rows):
        payload = row.get("payload", {})
        source = payload.get("source", {}) if isinstance(payload, dict) else {}
        detail = source.get("agent_detail", {}) if isinstance(source, dict) else {}
        step_id = str(detail.get("step_id", "")) if isinstance(detail, dict) else ""
        if step_id and step_id not in details:
            details[step_id] = detail
    return details


def _workflow_context_from_run(run: dict[str, Any]) -> Any:
    from cli.workflows.models import WorkflowContext
    from cli.workflows.router import WORKFLOWS

    name = str(run.get("workflow", ""))
    plan = run.get("plan", {})
    if not isinstance(plan, dict):
        plan = {}
    route = plan.get("route", {}) if isinstance(plan, dict) else {}
    base = WORKFLOWS.get(name)
    return WorkflowContext(
        name=name,
        label=str(run.get("label") or (base.label if base else name)),
        allowed_tools=tuple(plan.get("allowed_tools") or (base.allowed_tools if base else ())),
        system_hint=base.system_hint if base else "",
        route_reason=str(route.get("reason", "") or "复用已保存 workflow script"),
        route_confidence=_workflow_route_confidence(route),
        route_matches=tuple(str(item) for item in route.get("matches", []) if str(item)),
    )


def _workflow_run_from_saved(saved: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": f"saved:{saved.get('name', '')}",
        "workflow": saved.get("workflow", ""),
        "label": saved.get("label", ""),
        "plan": {
            "allowed_tools": saved.get("allowed_tools", []),
            "route": saved.get("route", {}),
            "script": saved.get("script", {}),
        },
    }


def _workflow_route_confidence(route: dict[str, Any]) -> float:
    try:
        return float(route.get("confidence", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _workflow_control_intent(text: str) -> tuple[str, str] | None:
    lower = text.lower()
    if "workflow" not in lower and "工作流" not in text:
        return None
    match = _WORKFLOW_ID_RE.search(text)
    run_id = match.group(0) if match else ""
    if any(token in text for token in ("批准", "同意", "开始运行", "运行这个 workflow")) or "approve" in lower:
        return "approve", run_id
    if any(token in text for token in ("取消", "拒绝")) or "deny" in lower:
        return "deny", run_id
    if any(token in text for token in ("暂停", "先停一下")) or "pause" in lower:
        return "pause", run_id
    if any(token in text for token in ("恢复运行", "继续运行")) or "resume" in lower:
        return "resume_running", run_id
    if any(token in text for token in ("停止", "终止")) or "stop" in lower:
        return "stop", run_id
    if any(token in text for token in ("重新加载脚本", "刷新脚本")) or "reload" in lower:
        return "reload", run_id
    if "rerun" in lower or any(token in text for token in ("复跑", "重跑", "重新运行", "按原脚本再跑")):
        return "rerun", run_id
    if "events" in lower or any(token in text for token in ("事件", "日志")):
        return "events", run_id
    if "status" in lower or any(token in text for token in ("状态", "进度")):
        return "status", run_id
    if "script" in lower or "脚本" in text:
        return "script", run_id
    if any(token in text for token in ("查看", "显示", "打开", "看看", "详情")) or "show" in lower:
        return "show", run_id
    return None


def _pending_workflow_reply_intent(text: str) -> str:
    intent = classify_pending_workflow_reply(text)
    return intent if intent in {"approve", "deny"} else ""


def _pending_workflow_revision_intent(text: str) -> bool:
    return is_pending_workflow_revision(text)


def _workflow_run_is_recent(
    run: dict[str, Any], *, max_age_seconds: int = _RECENT_WORKFLOW_FALLBACK_MAX_AGE_SECONDS
) -> bool:
    timestamp = _workflow_run_timestamp(run)
    if timestamp is None:
        return False
    now = datetime.now(UTC) if timestamp.tzinfo else datetime.now()
    age_seconds = (now - timestamp).total_seconds()
    return -_RECENT_WORKFLOW_FALLBACK_CLOCK_SKEW_SECONDS <= age_seconds <= max_age_seconds


def _workflow_run_timestamp(run: dict[str, Any]) -> datetime | None:
    for field_name in ("updated_at", "created_at"):
        raw = str(run.get(field_name, "") or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _split_workflow_name_args(name_part: str, rest: str) -> tuple[str, str]:
    if rest:
        return name_part.strip(), rest
    pieces = name_part.strip().split(maxsplit=1)
    if not pieces:
        return "", ""
    return pieces[0], pieces[1] if len(pieces) > 1 else ""


_WORKFLOW_UI_EVENT_TYPES = frozenset(
    {
        "workflow_plan_update",
        "workflow_phase_start",
        "workflow_phase_done",
        "workflow_step_start",
        "workflow_step_done",
        "workflow_disagreement_summary",
    }
)


def _run_workflow_background(
    runtime: WorkflowExecutor,
    messages: list[dict[str, Any]],
    system_prompt: str,
    model_name: str = "",
    provider_name: str = "",
    on_ui_event=None,
) -> dict[str, Any]:
    from utils.progress import report_progress

    started_at = time.monotonic()
    final_text = ""
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    events: list[dict[str, Any]] = []
    run_id = ""
    workflow_name = ""
    try:
        for event in runtime.run_stream(messages, system_prompt):
            event_type = str(event.get("type", ""))
            events.append(_workflow_bg_event_summary(event))
            if event_type in {"workflow_plan", "workflow_plan_update", "workflow_start"}:
                run_id = str(event.get("run_id", "")) or run_id
                workflow_name = str(event.get("workflow", "")) or workflow_name
                report_progress("workflow running", run_id, -1.0)
            elif event_type == "workflow_phase_start":
                report_progress(f"phase {event.get('phase', '')}", "running", -1.0)
            elif event_type == "workflow_step_start":
                step = event.get("step", {})
                report_progress(str(step.get("agent", "agent")), str(step.get("title", ""))[:80], -1.0)
            elif event_type == "workflow_step_done":
                step = event.get("step", {})
                report_progress(str(step.get("agent", "agent")), str(step.get("summary", ""))[:80], -1.0)
            elif event_type == "done":
                final_text = str(event.get("text", ""))
                usage = event.get("usage", usage)
            if on_ui_event and event_type in _WORKFLOW_UI_EVENT_TYPES:
                try:
                    on_ui_event(event)
                except Exception:
                    logger.debug("workflow background ui event failed", exc_info=True)
    except Exception as exc:
        run_id = run_id or (runtime.run.run_id if runtime.run else "")
        return {"workflow_run_id": run_id, "error": str(exc), "events": events[-40:]}
    if runtime.run:
        run_id = run_id or runtime.run.run_id
        workflow_name = workflow_name or runtime.run.workflow
    return {
        "workflow_run_id": run_id,
        "workflow": workflow_name,
        "final_text": final_text,
        "usage": usage,
        "elapsed": time.monotonic() - started_at,
        "events": events[-40:],
        "model_name": model_name,
        "provider_name": provider_name,
    }


def _workflow_bg_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": event.get("type", "")}
    for key in ("run_id", "workflow", "phase", "status"):
        if event.get(key):
            payload[key] = event.get(key)
    step = event.get("step")
    if isinstance(step, dict):
        step_payload: dict[str, Any] = {
            "title": step.get("title", ""),
            "agent": step.get("agent", ""),
            "status": step.get("status", ""),
            "summary": step.get("summary", ""),
        }
        for key in ("step_id", "phase", "tool_scope"):
            if step.get(key) not in (None, "", []):
                step_payload[key] = step.get(key)
        # 耗时/工具/结果在 source.agent_detail 里，不在 step.to_dict() 里；
        # 不捞出来的话 workflow 轮次在库里查不到任何执行细节。
        detail = event.get("source", {}).get("agent_detail") if isinstance(event.get("source"), dict) else None
        if isinstance(detail, dict):
            if detail.get("elapsed"):
                step_payload["elapsed"] = detail["elapsed"]
            if tool_calls := detail.get("tool_calls"):
                step_payload["tool_calls"] = tool_calls
            if step_result := detail.get("result"):
                step_payload["result"] = str(step_result)[:4000]
            if step_error := detail.get("error"):
                step_payload["error"] = str(step_error)[:1000]
        if evidence := _workflow_step_handoff_lines(event):
            step_payload["evidence"] = evidence
        payload["step"] = step_payload
    return payload


def _tool_schemas_or_empty(tools: Any) -> list[dict[str, Any]]:
    try:
        return tools.schemas() if tools else []
    except Exception:
        return []


def _workflow_spans_json(events: list[dict[str, Any]]) -> str:
    """把 workflow 的每一步折成 dashboard 认得的 span，让后台轮次也能看到执行明细。"""
    spans: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "workflow_step_done":
            continue
        step = event.get("step")
        if not isinstance(step, dict):
            continue
        status = str(step.get("status", ""))
        span: dict[str, Any] = {
            "name": step.get("title", "") or step.get("agent", "step"),
            "display_name": step.get("title", ""),
            "status": "error" if status not in {"completed", "ok"} else "ok",
            "args_brief": ", ".join(str(name) for name in (step.get("tool_scope") or [])),
            "elapsed_ms": int(float(step.get("elapsed", 0) or 0) * 1000),
        }
        for key in ("tool_calls", "result", "error", "phase", "step_id"):
            if step.get(key):
                span[key] = step[key]
        spans.append(span)
    return json.dumps(spans, ensure_ascii=False) if spans else ""


def _background_task_summary(tool_name: str, task_id: str, result: Any, *, max_chars: int = 3000) -> str:
    try:
        from cli.tool_results import format_tool_result_for_context

        return format_tool_result_for_context(tool_name, task_id, result, max_chars=max_chars)
    except Exception:
        summary = json.dumps(result, ensure_ascii=False, default=str)
        return summary if len(summary) <= max_chars else summary[:max_chars] + "..."


def _system_notification_queue_item(content: str) -> dict[str, str]:
    return {"type": "system_notification", "content": content}


def _workflow_background_notification(task_id: str, result: dict[str, Any], status: str, summary: str) -> str:
    return (
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
        "This is an automated dynamic-workflow event, NOT a message from the user.\n"
        "Do NOT interpret this as user acknowledgement, confirmation, or response to any pending question.\n\n"
        "<system-reminder>\n"
        "<workflow-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<run-id>{result.get('workflow_run_id', '')}</run-id>\n"
        f"<workflow>{result.get('workflow', '')}</workflow>\n"
        f"<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n"
        "</workflow-notification>\n"
        "</system-reminder>"
    )


def _is_system_notification_message(message: dict[str, Any]) -> bool:
    return bool(message.get("_system_notification"))


def _chatlog_role_for_turn(system_notification: bool) -> str:
    return "system" if system_notification else "user"


def _compaction_panel(event: dict[str, Any]):
    from rich.panel import Panel

    before, after = event["before_messages"], event["after_messages"]
    return Panel(
        Text.assemble(
            (" 系统状态：上下文深度压缩中...\n\n", "bold yellow"),
            ("已自动提取持久偏好写入 ", "dim white"),
            ("SQLite 记忆库", "bold cyan"),
            ("；\n已将前序 ", "dim white"),
            (str(before), "bold red"),
            (" 条陈旧对话压缩为结构化摘要，仅保留最近 ", "dim white"),
            (str(after), "bold green"),
            (" 条消息以维持当前上下文连贯性。", "dim white"),
        ),
        border_style="yellow",
        title="[bold yellow]CONTEXT COMPACTION[/bold yellow]",
        title_align="left",
        padding=(1, 2),
    )


def _context_overflow_panel(event: dict[str, Any]):
    """压缩没能把上下文降到窗口内，已硬丢弃最旧消息——这是有损的，必须让用户看见。"""
    from rich.panel import Panel

    return Panel(
        Text.assemble(
            (" 系统状态：上下文超出窗口上限\n\n", "bold red"),
            ("摘要压缩未能生效或不足，已直接丢弃最旧 ", "dim white"),
            (str(event.get("dropped_messages", 0)), "bold red"),
            (" 条消息以保证请求不被拒绝；\n当前保留 ", "dim white"),
            (str(event.get("after_messages", 0)), "bold green"),
            (" 条，窗口上限 ", "dim white"),
            (f"{int(event.get('limit', 0)):,}", "bold cyan"),
            (" tokens。被丢弃的内容不进摘要，如仍需要请重新提供。", "dim white"),
        ),
        border_style="red",
        title="[bold red]CONTEXT OVERFLOW[/bold red]",
        title_align="left",
        padding=(1, 2),
    )


_PERSISTED_THINKING_MAX_CHARS = 8_000


def _build_rounds_detail(
    rounds: int,
    round_usages: dict[int, dict[str, Any]],
    round_tool_names: dict[int, list[str]],
    round_starts: dict[int, float],
    t_start: float,
    model_name: str,
    round_thinking: dict[int, str] | None = None,
) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    for round_number in range(1, rounds + 1):
        usage = round_usages.get(round_number, {})
        started = round_starts.get(round_number, t_start)
        detail: dict[str, object] = {
            "round": round_number,
            "model": model_name,
            "tokens_in": usage.get("input_tokens", 0),
            "tokens_out": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_tokens", 0),
            "cache_write": usage.get("cache_write_tokens", 0),
            "stop_reason": usage.get("stop_reason", ""),
            "duration": round(max(0.0, time.monotonic() - started), 2),
            "has_tool_calls": bool(round_tool_names.get(round_number)),
            "tool_names": round_tool_names.get(round_number, []),
        }
        if thinking := (round_thinking or {}).get(round_number, ""):
            detail["thinking"] = thinking[:_PERSISTED_THINKING_MAX_CHARS]
            # 与工具结果一致地标注截断：思维链被无声切断时，读者会把残句当成模型的真实结论。
            if len(thinking) > _PERSISTED_THINKING_MAX_CHARS:
                detail["thinking_truncated"] = True
        details.append(detail)
    return details


def _usage_footer(
    total_input: int,
    total_output: int,
    elapsed_s: float,
    *,
    output_tok_per_s: float | None = None,
    cache_hit_rate: int | None = None,
    cache_reported: bool = False,
) -> Text:
    from cli.usage_metrics import format_usage_footer

    line = format_usage_footer(
        input_tokens=total_input,
        output_tokens=total_output,
        elapsed_s=elapsed_s,
        output_tok_per_s=output_tok_per_s,
        cache_hit_rate=cache_hit_rate,
        cache_reported=cache_reported,
    )
    return Text.from_markup(f"  [dim]{line}[/dim]")


def _session_avg_tok_per_s(session_tokens: dict) -> float | None:
    from cli.usage_metrics import output_tok_per_s

    # Workflow 等无 generation_ms 的轮次计入 unrated_output，避免会话均速虚高。
    rated_output = max(0, int(session_tokens.get("output") or 0) - int(session_tokens.get("unrated_output") or 0))
    return output_tok_per_s(rated_output, int(session_tokens.get("generation_ms") or 0) / 1000.0)


def _make_sub_agent_progress_handler(tools, write, scroll, spinner_start, spinner_stop):
    sub_buf = ""

    def _on_sub_agent_progress(event):
        nonlocal sub_buf
        agent = _sub_agent_progress_label(event.get("sub_agent", ""))
        etype = event.get("type")
        if etype == "text_delta":
            sub_buf += event.get("text", "")
            while "\n" in sub_buf:
                line, sub_buf = sub_buf.split("\n", 1)
                if line.strip():
                    write(Text.from_markup(f"    [dim italic]{agent}: {line}[/dim italic]"))
                    scroll()
        elif etype == "tool_start":
            spinner_start(f"{agent} 调用 {_tool_display_name(tools, str(event.get('name', '')))}")
        elif etype in ("tool_result", "tool_error"):
            spinner_stop()
            elapsed = event.get("elapsed_ms", 0) / 1000
            mark = "[green]✓[/green]" if event.get("status") != "error" else "[red]✗[/red]"
            display = _tool_display_name(tools, str(event.get("name", "")))
            write(Text.from_markup(f"    {mark} [dim]{agent} 完成 {display} {elapsed:.1f}s[/dim]"))
            scroll()
        elif etype == "done":
            spinner_stop()
            if sub_buf.strip():
                write(Text.from_markup(f"    [dim italic]{agent}: {sub_buf.strip()}[/dim italic]"))
                sub_buf = ""
            scroll()

    return _on_sub_agent_progress


def _sub_agent_progress_label(name: Any) -> str:
    return {
        "task": "agent",
        "research": "研究 agent",
        "analysis": "分析 agent",
        "trading": "交易 agent",
    }.get(str(name or "").strip(), "agent")


def _pending_user_question_answer(text: str, pending: _PendingUserQuestion) -> str:
    answer = text.strip()
    if not pending.options:
        return answer or pending.default_answer
    if answer in pending.options:
        return answer
    if answer.isdigit():
        index = int(answer)
        if index == 0:
            return pending.options[index]
        if 1 <= index <= len(pending.options):
            return pending.options[index - 1]
    if pending.allow_free_text:
        return answer or pending.default_answer
    return ""


def _pending_user_question_lines(pending: _PendingUserQuestion) -> list[str]:
    lines = [
        "  [yellow]？ agent 需要你补充[/yellow] [dim]直接在输入框回复，会作为当前问题的回答[/dim]",
        f"    {escape(pending.question)}",
    ]
    if pending.options:
        lines.extend(f"    [dim]{index}. {escape(option)}[/dim]" for index, option in enumerate(pending.options, 1))
    elif pending.default_answer:
        lines.append(f"    [dim]直接回车默认：{escape(pending.default_answer)}[/dim]")
    return lines


# TUI 配色令牌：硬编码颜色只允许出现在这里。语义色（green 成功 / yellow 警告 / red 错误）直接内联使用。
# 品牌主色是琥珀金（ticker tape 意象），cyan 保留给交互/信息元素（workflow、链接 id、后台任务）。
_UI_PALETTES: dict[str, dict[str, str]] = {
    # transparent 主题跟随终端背景，只用明暗背景都可读的 ANSI 色
    "ansi": {
        "brand": "yellow",
        "heading": "yellow",
        "code": "cyan",
        "link": "blue",
        "quote": "bright_black",
        "rule": "bright_black",
        "bullet": "yellow",
    },
    "dark": {
        "brand": "#e6b450",
        "heading": "#e6b450",
        "code": "#d19a66",
        "link": "#e6b450",
        "quote": "#8b949e",
        "rule": "#30363d",
        "bullet": "#e6b450",
    },
    "light": {
        "brand": "#9a6700",
        "heading": "#9a6700",
        "code": "#cf222e",
        "link": "#0969da",
        "quote": "#57606a",
        "rule": "#d0d7de",
        "bullet": "#9a6700",
    },
}

_LIGHT_THEME_NAMES = {"solarized-light", "atom-one-light", "textual-light", "light", "github-light"}


def _ui_palette(theme_setting: str) -> dict[str, str]:
    name = theme_setting.strip().lower()
    if name in ("transparent", "terminal"):
        return _UI_PALETTES["ansi"]
    if name in _LIGHT_THEME_NAMES:
        return _UI_PALETTES["light"]
    return _UI_PALETTES["dark"]


def _build_thinking_preview(text: str) -> Text | None:
    preview = text.strip().replace("\n", " ")
    if len(preview) > 80:
        preview = preview[:80] + "…"
    if not preview:
        return None
    return Text.from_markup(f"  [dim italic]… {preview}  ({len(text)} 字)[/dim italic]")


class ChatLog(RichLog):
    DEFAULT_CSS = """
    ChatLog {
        background: $background;
        scrollbar-size: 1 1;
        border: none;
        margin: 1 2 0 2;
        height: 1fr;
    }
    """


class StatusBar(Static):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("markup", True)
        super().__init__(*args, **kwargs)


class _PasteHighlighter(Highlighter):
    def highlight(self, text: Text) -> None:
        m = re.match(r"^\[Pasted Text: \d+ lines\]$", text.plain)
        if m:
            text.stylize("bold magenta", m.start(), m.end())


class ChatInput(Input):
    """支持多行粘贴折叠显示的输入框。"""

    DEFAULT_CSS = """
    ChatInput {
        background: $background;
        border: none;
        color: $text;
        height: 1;
        margin: 0;
        padding: 0 1;
        width: 1fr;
    }
    ChatInput:focus {
        border: none;
    }
    """

    _pasted_text: str | None = None

    def on_paste(self, event: events.Paste) -> None:
        lines = event.text.splitlines()
        if len(lines) <= 1:
            return
        self._pasted_text = event.text
        self.value = f"[Pasted Text: {len(lines)} lines]"
        event.prevent_default()
        event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._pasted_text is None:
            return
        expected = f"[Pasted Text: {len(self._pasted_text.splitlines())} lines]"
        if event.value != expected:
            self._pasted_text = None

    def consume_pasted(self) -> str | None:
        text = self._pasted_text
        self._pasted_text = None
        return text

    def on_key(self, event: events.Key) -> None:
        if event.key == "up" and not (self.value or "").strip():
            event.stop()
            self.post_message(FollowUpEditRequested())
        elif event.key == "escape":
            event.stop()
            self.post_message(FollowUpCancelRequested())


class SelectorScreen(ModalScreen):
    """模态选择器 — 上下键选择，Enter 确认，Esc 取消。"""

    DEFAULT_CSS = """
    SelectorScreen {
        align: center middle;
    }
    #selector-box {
        width: 60;
        max-height: 16;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    #selector-options {
        height: auto;
        max-height: 12;
    }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, options: list[tuple[str, str]], callback_id: str):
        super().__init__()
        self._options = options
        self._values = [v for v, _ in options]
        self._callback_id = callback_id

    def compose(self) -> ComposeResult:
        with Vertical(id="selector-box"):
            yield OptionList(
                *[Option(label, id=val) for val, label in self._options],
                id="selector-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        value = self._values[event.option_index]
        self.dismiss(None)
        self.app._on_selector_choice(self._callback_id, value)

    def action_cancel(self) -> None:
        self.dismiss(None)
        self.app._on_selector_choice(self._callback_id, None)


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


_DEFAULT_MODEL_BY_PROVIDER = {
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o",
    "claude": "claude-sonnet-4-20250514",
}
_MODEL_PROVIDER_OPTIONS = [
    ("gemini", "Gemini (Google)"),
    ("openai", "OpenAI / 兼容接口 (LongCat, DeepSeek, Qwen...)"),
    ("claude", "Claude (Anthropic)"),
]


# ---------------------------------------------------------------------------
# 交互式输入状态机（/login, /model）
# ---------------------------------------------------------------------------


class _InputState:
    """管理多步交互式输入流程。"""

    NONE = "none"
    LOGIN_EMAIL = "login_email"
    LOGIN_PASSWORD = "login_password"
    CONFIG_KEY = "config_key"
    MODEL_ID = "model_id"
    MODEL_PROVIDER = "model_provider"
    MODEL_KEY = "model_key"
    MODEL_NAME = "model_name"
    MODEL_URL = "model_url"
    SCHED_ID = "sched_id"
    SCHED_NAME = "sched_name"
    SCHED_CRON = "sched_cron"
    SCHED_ACTION = "sched_action"


# ---------------------------------------------------------------------------
# 工具确认弹窗
# ---------------------------------------------------------------------------


class ToolConfirmScreen(ModalScreen[dict]):
    """高风险工具执行前的确认弹窗。"""

    DEFAULT_CSS = """
    ToolConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 80%;
        min-width: 64;
        max-width: 100;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #confirm-summary-scroll {
        height: auto;
        max-height: 8;
        overflow-y: auto;
        margin-bottom: 1;
    }
    #confirm-summary {
        width: 100%;
        color: $text-muted;
    }
    #confirm-options {
        height: 6;
        min-height: 6;
    }
    #confirm-edit {
        display: none;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, tool_name: str, args: dict, display_name: str):
        super().__init__()
        self.tool_name = tool_name
        self.tool_args = args
        self.display_name = display_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(
                f"⚠ [bold]{self.display_name}[/bold] 需要确认",
                id="confirm-title",
            )
            with VerticalScroll(id="confirm-summary-scroll"):
                yield Static(self._format_summary(), id="confirm-summary")
            yield OptionList(
                Option("允许一次", id="once"),
                Option("本次会话总是允许", id="always"),
                Option("修改后执行", id="edit"),
                Option("不允许", id="deny"),
                id="confirm-options",
            )
            yield Input(
                value=self._editable_value(),
                placeholder="修改后按 Enter 执行",
                id="confirm-edit",
            )

    def _format_summary(self) -> str:
        if self.tool_name == "exec_command":
            return f"  命令: {self.tool_args.get('command', '')}"
        if self.tool_name == "write_file":
            path = self.tool_args.get("path", "")
            size = len(self.tool_args.get("content", ""))
            return f"  路径: {path}\n  内容: {size} 字符"
        if self.tool_name == "update_portfolio":
            return self._format_portfolio_confirm_summary()
        return f"  {json.dumps(self.tool_args, ensure_ascii=False)}"

    def _format_portfolio_confirm_summary(self) -> str:
        action = str(self.tool_args.get("action", "") or "")
        items = self.tool_args.get("items")
        if isinstance(items, list) and items:
            lines = [f"  操作: {action} × {len(items)} 只"]
            for row in items[:8]:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code") or "").strip()
                name = str(row.get("name") or "").strip()
                shares = row.get("shares")
                cost = row.get("cost_price")
                detail = " ".join(
                    part
                    for part in (
                        name,
                        f"{shares}股" if shares not in (None, "", 0) else "",
                        f"成本{cost}" if cost not in (None, "") else "",
                    )
                    if part
                )
                lines.append(f"  · {code} {detail}".rstrip())
            if len(items) > 8:
                lines.append(f"  · …另有 {len(items) - 8} 只")
            return "\n".join(lines)
        code = self.tool_args.get("code", "")
        parts = [f"操作: {action}"]
        if code:
            parts.append(f"代码: {code}")
        shares = self.tool_args.get("shares")
        if shares:
            parts.append(f"股数: {shares}")
        cost = self.tool_args.get("cost_price")
        if cost:
            parts.append(f"成本: {cost}")
        cash = self.tool_args.get("free_cash")
        if cash is not None and action == "set_cash":
            parts.append(f"现金: {cash}")
        return "  " + "  ".join(parts)

    def _editable_value(self) -> str:
        if self.tool_name == "exec_command":
            return self.tool_args.get("command", "")
        if self.tool_name == "write_file":
            return self.tool_args.get("path", "")
        return json.dumps(self.tool_args, ensure_ascii=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "edit":
            self.query_one("#confirm-options").display = False
            edit_input = self.query_one("#confirm-edit", Input)
            edit_input.display = True
            edit_input.focus()
        else:
            self.dismiss({"action": event.option_id})

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "confirm-edit":
            return
        modified = dict(self.tool_args)
        if self.tool_name == "exec_command":
            modified["command"] = event.value
        elif self.tool_name == "write_file":
            modified["path"] = event.value
        else:
            with contextlib.suppress(json.JSONDecodeError):
                modified = json.loads(event.value)
        self.dismiss({"action": "edit", "modified_args": modified})

    def action_cancel(self) -> None:
        self.dismiss({"action": "deny"})


class BrowserCdpConsentScreen(ModalScreen[dict]):
    """授权启动独立调试 Chrome（专用 profile，本会话有效）。"""

    DEFAULT_CSS = """
    BrowserCdpConsentScreen {
        align: center middle;
    }
    #cdp-consent-box {
        width: 68;
        max-height: 16;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    #cdp-consent-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #cdp-consent-body {
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        from integrations.browser_cdp import chrome_cdp_profile_dir

        profile = chrome_cdp_profile_dir()
        with Vertical(id="cdp-consent-box"):
            yield Static("⚠ [bold]浏览器搜索需要本机调试 Chrome[/bold]", id="cdp-consent-title")
            yield Static(
                "将启动独立 Chrome（专用配置目录，不碰你日常浏览数据）：\n"
                f"  {profile}\n"
                "同意后本会话内可自动重拉，无需再确认。",
                id="cdp-consent-body",
            )
            yield OptionList(
                Option("允许并启动", id="allow"),
                Option("不允许", id="deny"),
                id="cdp-consent-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss({"action": event.option_id or "deny"})

    def action_cancel(self) -> None:
        self.dismiss({"action": "deny"})


# ---------------------------------------------------------------------------
# 错误友好化
# ---------------------------------------------------------------------------


def _friendly_error(e: Exception) -> str:
    """将常见网络/超时异常转为用户可读的中文提示。"""
    import re

    cls_name = type(e).__name__
    if isinstance(e, TimeoutError):
        detail = str(e).strip() or "模型响应超时"
        return f"{detail}，请检查网络或调大 stream_chunk_timeout_seconds"
    if "RemoteProtocolError" in cls_name or "ReadError" in cls_name:
        return "连接已断开，请检查网络后重试"
    if "APIConnectionError" in cls_name or "ConnectError" in cls_name:
        return "API 连接失败，请检查网络"
    err = str(e)
    if "<html" in err.lower():
        title = re.search(r"<title>(.*?)</title>", err, re.IGNORECASE)
        err = title.group(1) if title else "服务端返回 HTML 错误"
    if len(err) > 200:
        err = err[:200] + "..."
    return err


def _should_force_exit_busy_cancel(cancel_requested: bool, last_interrupt_at: float, now: float) -> bool:
    return cancel_requested and now - last_interrupt_at <= _BUSY_FORCE_EXIT_WINDOW


# ---------------------------------------------------------------------------
# 主应用与主题
# ---------------------------------------------------------------------------

SUPPORTED_TUI_THEMES: dict[str, str] = {
    "transparent": "终端透出模式（继承终端 App 原生底色/亚克力透明度）",
    "auto": "系统自适应模式（根据 Win/Mac 系统深浅外观自动调配）",
    "dracula": "Dracula 炫酷暗紫",
    "nord": "Nord 北欧极光",
    "monokai": "Monokai 经典暗色",
    "solarized-dark": "Solarized 深色",
    "solarized-light": "Solarized 浅色",
    "tokyo-night": "Tokyo Night 东京之夜",
    "catppuccin-mocha": "Catppuccin 摩卡暗色",
    "atom-one-dark": "Atom One Dark",
    "atom-one-light": "Atom One Light",
    "textual-dark": "Textual 默认暗色",
    "textual-light": "Textual 默认亮色",
}


def detect_os_dark_mode() -> bool:
    """感知 OS 系统的深浅色外观模式（支持 Windows 注册表和 macOS）。"""
    import sys

    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        except Exception:
            return True
    elif sys.platform == "darwin":
        try:
            import subprocess

            # 必须带 timeout：这是启动路径上的同步调用，defaults 卡住会把整个 TUI 挂在黑屏上
            res = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return "Dark" in res.stdout
        except Exception:
            return True
    return True


class WyckoffTUI(App):
    """威科夫终端读盘室。"""

    TITLE = "Wyckoff 读盘室"
    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }
    Screen.transparent {
        background: ansi_default;
    }
    #input-container {
        layout: horizontal;
        height: 3;
        border: round $border;
        background: $background;
        margin: 0 2;
        align: left middle;
    }
    Screen.transparent #input-container {
        background: ansi_default;
    }
    /* 品牌色与 _UI_PALETTES.brand 保持一致；CSS 无法引用 Python 变量，需手动同步 */
    #input-container:focus-within {
        border: round #e6b450;
    }
    Screen.light-mode #input-container:focus-within {
        border: round #9a6700;
    }
    Screen.transparent #input-container:focus-within {
        border: round yellow;
    }
    #prompt-prefix {
        width: auto;
        color: #e6b450;
        margin: 0 0 0 1;
        text-style: bold;
    }
    Screen.light-mode #prompt-prefix {
        color: #9a6700;
    }
    Screen.transparent #prompt-prefix {
        color: yellow;
    }
    #status-bar {
        dock: bottom;
        layout: horizontal;
        height: 1;
        background: $surface;
        color: $text-muted;
    }
    Screen.transparent #status-bar {
        background: ansi_default;
    }
    #status-left {
        width: 1fr;
        padding: 0 0 0 2;
    }
    #status-right {
        width: auto;
        padding: 0 2 0 0;
        text-align: right;
    }
    """

    ENABLE_COMMAND_PALETTE = True
    COMMAND_PALETTE_BINDING = "ctrl+p"
    COMMANDS = set()  # will be populated below after class definition

    BINDINGS = [
        Binding("ctrl+c", "smart_copy", show=False, priority=True),
        Binding("ctrl+q", "quit", "退出", show=False),
        Binding("ctrl+n", "new_chat", "新对话"),
        Binding("ctrl+l", "clear_chat", "清屏"),
    ]

    def __init__(
        self,
        provider: Any = None,
        tools: Any = None,
        state: dict | None = None,
        system_prompt: str = "",
        session_expired: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._provider = provider
        self._tools = tools
        self._state = state or {}
        self._system_prompt = system_prompt
        self._session_expired = session_expired
        self._messages: list[dict] = []
        self._session_tokens = {
            "input": 0,
            "output": 0,
            "unrated_output": 0,
            "rounds": 0,
            "cache_read": 0,
            "generation_ms": 0,
            "cache_reported": False,
        }
        self._conversation = ConversationSession()
        self._cancel_event = threading.Event()
        self._last_ctrl_c: float = 0.0
        self._workflow_override: _WorkflowOverride | None = None
        self._pending_workflows: dict[str, _PendingWorkflowLaunch] = {}
        self._pending_user_question: _PendingUserQuestion | None = None
        self._pending_resume_checkpoint = None
        self._session_id = uuid.uuid4().hex[:12]
        self._browser_cdp_autostart_allowed = False
        self._spinner_idx = 0
        self._spinner_label = ""
        self._agent_log = _get_agent_logger()
        # 后台任务管理
        from cli.background import BackgroundTaskManager

        self._bg_manager = BackgroundTaskManager()
        self._bg_manager.set_progress_callback(self._on_bg_progress)
        if self._tools:
            self._tools.set_background_manager(self._bg_manager, self._on_bg_complete)
            self._tools.set_confirm_callback(self._request_tool_confirm)
            self._tools.set_ask_user_question_callback(self._request_user_question)
            self._tools._tool_context.state["ensure_browser_cdp"] = self._ensure_browser_cdp_consent
        self._mcp_manager = None
        # 交互式输入状态
        self._input_mode = _InputState.NONE
        self._input_buf: dict[str, str] = {}
        # 定时调度
        from cli.scheduler import load_schedules

        self._schedules = load_schedules()
        self._last_schedule_check_at: datetime | None = None

    def _ensure_conversation(self) -> ConversationSession:
        if not hasattr(self, "_conversation") or self._conversation is None:
            self._conversation = ConversationSession()
        return self._conversation

    @property
    def _busy(self) -> bool:
        session = getattr(self, "_conversation", None)
        return bool(session and session.is_busy)

    @_busy.setter
    def _busy(self, value: bool) -> None:
        """Test/compat shim: map boolean busy onto ConversationSession phases."""

        session = self._ensure_conversation()
        if value:
            if not session.is_busy:
                session.begin_turn("")
                session.mark_running()
            return
        if session.is_busy:
            session.abandon_active_turn()

    @property
    def _queue(self):
        """Compat view of Session.input_queue (deque of QueuedInput / legacy items)."""

        return self._ensure_conversation().input_queue

    @_queue.setter
    def _queue(self, value) -> None:
        session = self._ensure_conversation()
        session.clear_queue()
        for item in value or ():
            if isinstance(item, QueuedInput):
                session.input_queue.append(item)
            elif isinstance(item, dict) and item.get("type") == "system_notification":
                session.input_queue.append(
                    QueuedInput(kind="system_notification", content=str(item.get("content", "")))
                )
            else:
                session.input_queue.append(QueuedInput(kind="user", content=str(item)))

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        yield ChatLog(id="chat-log", highlight=True, markup=True, wrap=True)
        yield FollowUpsPanel(id="followups-panel")
        with Horizontal(id="input-container"):
            yield Static("❯", id="prompt-prefix")
            yield ChatInput(
                placeholder=IDLE_PLACEHOLDER,
                id="chat-input",
                highlighter=_PasteHighlighter(),
            )
        with Horizontal(id="status-bar"):
            yield StatusBar(self._build_status_left(), id="status-left")
            yield StatusBar(self._build_status_right(), id="status-right")

    def _update_console_markdown_theme(self, name: str) -> None:
        """动态适配 Markdown 渲染样式，绝不在透明/浅色模式下硬加纯黑底色框。"""
        from rich.style import Style
        from rich.theme import Theme

        palette = _ui_palette(name)
        md_styles = {
            "markdown.h1": Style(color=palette["heading"], bold=True),
            "markdown.h2": Style(color=palette["heading"], bold=True),
            "markdown.h3": Style(color=palette["heading"]),
            "markdown.link": Style(color=palette["link"], underline=True),
            "markdown.code": Style(color=palette["code"], bold=True),
            "markdown.block_quote": Style(color=palette["quote"], italic=True),
            "markdown.hr": Style(color=palette["rule"]),
            "markdown.item.bullet": Style(color=palette["bullet"]),
        }

        with contextlib.suppress(Exception):
            # 先弹掉上一次压入的：push_theme 是压栈而非替换，每切一次主题栈就长一层，
            # 反复切主题会把这些字典一直留在 console 上。
            if getattr(self, "_md_theme_pushed", False):
                self.console.pop_theme()
                self._md_theme_pushed = False
            self.console.push_theme(Theme(md_styles))
            self._md_theme_pushed = True

    def apply_theme_setting(self, name: str) -> str:
        """根据主题名称生效对应的色彩与 CSS 类。"""
        normalized = name.strip().lower()
        self._active_theme_setting = normalized

        def _add_cls(cls_name: str) -> None:
            with contextlib.suppress(Exception):
                self.screen.add_class(cls_name)

        def _rm_cls(cls_name: str) -> None:
            with contextlib.suppress(Exception):
                self.screen.remove_class(cls_name)

        res = ""
        if normalized in ("transparent", "terminal"):
            self.theme = "textual-dark"
            _add_cls("transparent")
            res = "transparent"
        elif normalized == "auto":
            is_dark = detect_os_dark_mode()
            target = "transparent" if is_dark else "solarized-light"
            return self.apply_theme_setting(target)
        else:
            _rm_cls("transparent")
            if normalized in self.available_themes:
                self.theme = normalized
                res = normalized
            elif normalized in ("light", "github-light"):
                self.theme = "solarized-light"
                res = "solarized-light"
            elif normalized in ("dark", "black"):
                self.theme = "textual-dark"
                res = "textual-dark"
            else:
                self.theme = "textual-dark"
                res = "textual-dark"

        if res in _LIGHT_THEME_NAMES:
            _add_cls("light-mode")
        else:
            _rm_cls("light-mode")
        self._update_console_markdown_theme(res)
        self._refresh_followups_panel()
        return res

    def on_mount(self) -> None:
        # 加载保存的主题
        try:
            from cli.auth import load_config

            saved_theme = load_config().get("theme", "transparent")
            if saved_theme:
                self.apply_theme_setting(saved_theme)
        except Exception:
            logger.debug("load saved theme failed", exc_info=True)

        self.set_interval(0.1, self._tick_global_spinner)
        self._start_external_mcp()

        log = self.query_one("#chat-log", ChatLog)
        from importlib.metadata import version as _ver

        try:
            ver = _ver("youngcan-wyckoff-analysis")
        except Exception:
            ver = "dev"

        palette = _ui_palette(getattr(self, "_active_theme_setting", "transparent"))
        brand = palette["brand"]

        log.write(Text.from_markup(f"  [bold {brand}]▞▚ WYCKOFF STATION[/bold {brand}]  [dim]v{ver}[/dim]"))
        log.write(Text.from_markup("  [dim]威科夫读盘室 · Market Workstation[/dim]"))
        log.write("")
        log.write(
            Text.from_markup(
                f"  [dim]输入股票代码开始分析（如 [{brand}]600519[/{brand}]）"
                f" · [{brand}]/help[/{brand}] 查看全部命令[/dim]"
            )
        )
        log.write(Text.from_markup("  [dim]Ctrl+N 新会话 · Ctrl+L 清屏 · Ctrl+P 命令面板 · Ctrl+Q 退出[/dim]"))
        log.write("")
        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请输入 /model add 添加[/yellow]\n"))
        if self._session_expired:
            log.write(Text.from_markup("[yellow]⚠ 登录已过期，请输入 /login 重新登录[/yellow]\n"))
        self.query_one("#chat-input", Input).focus()
        if self._schedules:
            self.set_interval(60.0, self._check_schedules)
        self.call_after_refresh(self._check_auto_resume)

    def _build_status_left(self) -> str:
        parts = []
        active_tasks = self._bg_manager.active_tasks()
        if active_tasks:
            task = active_tasks[0]
            stage = task.current_stage or "准备中"
            elapsed = int(time.monotonic() - task.submitted_at)

            from cli.tools import TOOL_DISPLAY_NAMES

            display_name = (
                "dynamic workflow"
                if task.tool_name == "dynamic_workflow"
                else TOOL_DISPLAY_NAMES.get(task.tool_name, task.tool_name)
            )
            extra = f" (+{len(active_tasks) - 1})" if len(active_tasks) > 1 else ""
            parts.append(
                f"[bold cyan]{_SPINNER[self._spinner_idx]} 后台任务: {display_name}{extra} ({stage} · {elapsed}s)[/bold cyan]"
            )
        elif getattr(self, "_spinner_label", ""):
            parts.append(f"[bold cyan]{_SPINNER[self._spinner_idx]} {self._spinner_label}[/bold cyan]")
        elif phase_label := self._conversation.status_label():
            style = "yellow" if phase_label in {"failed", "cancelled", "cancelling"} else "dim"
            parts.append(f"[{style}]turn:{phase_label}[/{style}]")
        if queue_depth := len(self._conversation.input_queue):
            parts.append(f"queued:{queue_depth}")
        if steer_depth := len(self._conversation.steering_queue):
            parts.append(f"steer:{steer_depth}")
        return " · ".join(parts)

    def _build_status_right(self) -> str:
        parts = []
        prov = self._state.get("provider_name", "")
        model = self._state.get("model", "")
        if prov and model:
            parts.append(f"{prov}:{model}")
        email = self._tools.state.get("email", "") if self._tools else ""
        parts.append(email or "未登录")
        t = self._session_tokens
        if t["rounds"] > 0:
            tok_bits = [f"Token: {t['input'] + t['output']:,}"]
            avg = _session_avg_tok_per_s(t)
            if avg is not None:
                tok_bits.append(f"{avg:g}tok/s")
            if t.get("cache_reported") and t["input"] > 0:
                from cli.usage_metrics import cache_hit_rate_pct

                hit = cache_hit_rate_pct(int(t.get("cache_read") or 0), int(t["input"]))
                if hit is not None:
                    tok_bits.append(f"cache {hit}%")
            parts.append(" ".join(tok_bits))
        return " · ".join(parts)

    def _update_status(self) -> None:
        self.query_one("#status-left", StatusBar).update(self._build_status_left())
        self.query_one("#status-right", StatusBar).update(self._build_status_right())

    def _tick_global_spinner(self) -> None:
        active_tasks = self._bg_manager.active_tasks()
        has_spinner = bool(getattr(self, "_spinner_label", "")) or bool(active_tasks)
        if has_spinner:
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
            self._update_status()

    # ----- 工具确认 -----

    def _request_tool_confirm(self, name: str, args: dict) -> dict:
        """从 worker 线程调用，阻塞直到用户在弹窗中做出选择。"""
        event = threading.Event()
        result: list[dict | None] = [None]
        display = self._tools.display_name(name) if self._tools else name

        def _on_dismiss(choice: dict) -> None:
            result[0] = choice
            event.set()

        def _show() -> None:
            self.push_screen(ToolConfirmScreen(name, args, display), _on_dismiss)

        self.call_from_thread(_show)
        if not event.wait(timeout=120):
            # 沉默不是否决：超时必须和 deny 区分开，否则模型会告诉用户「你拒绝了」，
            # 而用户只是没看见弹窗。
            return {"action": "timeout"}
        return result[0] or {"action": "deny"}

    def _ensure_browser_cdp_consent(self) -> str:
        """供 browser_research 在 worker 线程调用：本会话授权后可自动拉起 CDP Chrome。"""
        if self._browser_cdp_autostart_allowed:
            return "allow"
        event = threading.Event()
        result: list[dict | None] = [None]

        def _on_dismiss(choice: dict) -> None:
            result[0] = choice
            event.set()

        def _show() -> None:
            self.push_screen(BrowserCdpConsentScreen(), _on_dismiss)

        self.call_from_thread(_show)
        if not event.wait(timeout=120):
            return "timeout"
        action = str((result[0] or {}).get("action") or "deny")
        if action == "allow":
            self._browser_cdp_autostart_allowed = True
            return "allow"
        return "deny"

    def _request_user_question(
        self,
        question: str,
        options: list[str] | None = None,
        allow_free_text: bool = True,
        default_answer: str = "",
    ) -> str:
        """从 worker 线程调用，阻塞并向用户提问，返回用户的回答。"""
        event = threading.Event()
        result: list[str] = [""]
        pending = _PendingUserQuestion(question, options or [], allow_free_text, default_answer, event, result)

        def _show() -> None:
            self._pending_user_question = pending
            log = self.query_one("#chat-log", ChatLog)
            for line in _pending_user_question_lines(pending):
                log.write(Text.from_markup(line))
            log.scroll_end(animate=False)
            with contextlib.suppress(Exception):
                self.query_one("#chat-input", ChatInput).focus()

        self.call_from_thread(_show)
        answered = event.wait(timeout=300)  # 等待最长 5 分钟
        self.call_from_thread(self._clear_pending_user_question, pending)
        if not answered and not default_answer:
            from cli.tools import ASK_USER_TIMEOUT_SENTINEL

            # 用哨兵而不是「已超时未作答」这类自然语言：后者会被当成用户真的这么回答了。
            return ASK_USER_TIMEOUT_SENTINEL
        return result[0] or default_answer

    def _clear_pending_user_question(self, pending: _PendingUserQuestion) -> None:
        if self._pending_user_question is pending:
            self._pending_user_question = None

    def _answer_pending_user_question(self, text: str, log) -> bool:
        pending = self._pending_user_question
        if not pending or pending.event.is_set():
            return False
        answer = _pending_user_question_answer(text, pending)
        if not answer:
            log.write(Text.from_markup("  [yellow]请从当前提问的选项中选择，或先取消该提问。[/yellow]"))
            return True
        pending.result[0] = answer
        pending.event.set()
        self._pending_user_question = None
        log.write(Text.from_markup(f"  [dim]↳ 已作为当前提问的回答：{escape(answer)}[/dim]"))
        return True

    # ----- 快捷键动作 -----

    def _save_memory_async(
        self, messages: list[dict] | None = None, *, wait_timeout: float | None = None, skip_layers: bool = False
    ) -> None:
        if not self._provider:
            return
        msgs = list(messages if messages is not None else self._messages)
        if not msgs:
            return
        try:
            from cli.memory import save_session_summary

            t = threading.Thread(
                target=save_session_summary,
                args=(msgs, self._provider),
                kwargs={"session_id": self._session_id, "skip_layers": skip_layers},
                daemon=True,
            )
            t.start()
            if wait_timeout is not None:
                t.join(timeout=wait_timeout)
        except Exception:
            logger.debug("save session summary failed", exc_info=True)

    def _start_external_mcp(self) -> None:
        """连接用户配置的外部 MCP server。失败只记日志，不影响原生工具。"""
        try:
            from cli.mcp_client import McpClientManager
            from cli.mcp_config import enabled_servers

            servers = enabled_servers()
            if not servers or not self._tools:
                return
            manager = McpClientManager(servers)
            manager.start()
            tools = manager.tools()
            if not tools:
                manager.stop()
                return
            self._mcp_manager = manager
            self._tools.set_mcp_manager(manager)
            log = self.query_one("#chat-log", ChatLog)
            log.write(Text.from_markup(f"[dim]◈ 外部 MCP：{len(tools)} 个工具已接入[/dim]"))
        except Exception:
            logger.warning("external mcp start failed", exc_info=True)

    def _stop_external_mcp(self) -> None:
        if self._mcp_manager is None:
            return
        try:
            self._mcp_manager.stop()
        except Exception:
            logger.debug("external mcp stop failed", exc_info=True)
        self._mcp_manager = None

    def _save_and_exit(self, *, force: bool = False) -> None:
        if force:
            self._cancel_event.set()
        self._stop_external_mcp()
        self._save_memory_async(wait_timeout=1 if force else 5, skip_layers=True)
        self.exit(return_code=130 if force else 0)
        if force:
            timer = threading.Timer(_HARD_EXIT_DELAY, self._hard_exit_after_force_quit)
            timer.daemon = True
            timer.start()

    def _hard_exit_after_force_quit(self) -> None:
        if not self._busy:
            return
        import os

        os._exit(130)

    def action_quit(self) -> None:
        if self._busy:
            self.notify("强制退出", timeout=1)
            self._save_and_exit(force=True)
            return
        self._save_and_exit()

    def action_smart_copy(self) -> None:
        """Ctrl+C: 选中文本→复制；执行中→中断；空闲双击1s内→退出。"""
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            self.screen.clear_selection()
            self.notify("已复制", timeout=1)
            return
        now = time.monotonic()
        if self._busy:
            if _should_force_exit_busy_cancel(self._cancel_event.is_set(), self._last_ctrl_c, now):
                self.notify("强制退出", timeout=1)
                self._save_and_exit(force=True)
                return
            self._cancel_event.set()
            self._last_ctrl_c = now
            self._apply_session_ui(self._conversation.request_cancel())
            self.notify("已中断，再按一次 Ctrl+C 强制退出", timeout=1)
            return
        if now - self._last_ctrl_c < 1.0:
            self._save_and_exit()
        else:
            self._last_ctrl_c = now
            self.notify("再按一次 Ctrl+C 退出", timeout=1)

    def action_switch_model(self) -> None:
        self._switch_model_selector()

    def action_list_models(self) -> None:
        self._list_models()

    def action_add_model(self) -> None:
        self._start_model_add()

    def action_start_login(self) -> None:
        self._start_login()

    def action_do_logout(self) -> None:
        self._do_logout()

    def action_show_token(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        self._show_token_usage(log)

    def action_show_prompt_templates(self) -> None:
        self._show_prompt_templates()

    def action_show_workflows(self) -> None:
        self._show_workflows()

    def action_switch_theme(self) -> None:
        """打开主题切换器并显示列表。"""
        log = self.query_one("#chat-log", ChatLog)
        self._handle_theme_cmd("/theme list", log)

    def watch_theme(self, new_theme: str) -> None:
        """主题变化时自动保存。"""
        try:
            from cli.auth import save_config_key

            save_config_key("theme", new_theme)
        except Exception:
            logger.debug("save theme preference failed", exc_info=True)

    # ----- Spinner（ChatLog 底部边框） -----

    def _start_spinner(self, label: str = "thinking") -> None:
        self._spinner_label = label
        self._spinner_idx = 0
        self._update_status()
        self._refresh_followups_panel()

    def _stop_spinner(self) -> None:
        had_label = bool(self._spinner_label)
        self._spinner_label = ""
        if had_label:
            self._update_status()
        self._refresh_followups_panel()

    # ----- 输入处理 -----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inp = self.query_one("#chat-input", ChatInput)
        text = (inp.consume_pasted() or event.value).strip()
        inp.clear()
        inp._pasted_text = None

        # 交互式多步输入
        if self._input_mode != _InputState.NONE:
            self._handle_interactive_input(text)
            return

        log = self.query_one("#chat-log", ChatLog)

        if not text:
            if self._busy and self._pending_user_question and self._pending_user_question.default_answer:
                self._answer_pending_user_question(text, log)
            return

        # 斜杠命令
        if text.startswith("/"):
            self._handle_command(text)
            return

        if self._busy and self._answer_pending_user_question(text, log):
            return

        if self._handle_workflow_control_text(text, log):
            return

        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请先输入 /model add[/yellow]"))
            return

        intent = self._conversation.resolve_text(
            text,
            has_pending_question=bool(self._pending_user_question),
            has_pending_workflow=False,
            resumable_workflow_exists=bool(self._latest_relevant_workflow_run()),
        )
        if intent.kind == UserIntent.RESUME_TURN:
            self._resume_active_turn(log)
            return
        if intent.kind == UserIntent.RESUME_WORKFLOW:
            self._resume_recent_workflow_followup(text, log)
            return
        if intent.kind == UserIntent.STEER_TURN:
            self._apply_session_ui(self._conversation.enqueue_steer(intent.text))
            log.write(Text.from_markup("  [dim]🧭 已注入转向指令（本轮下一跳生效）[/dim]"))
            return
        if intent.kind == UserIntent.ENQUEUE_INPUT:
            self._apply_session_ui(self._conversation.enqueue(text))
            return
        self._conversation.abandon_active_turn()
        self._send_message(text)

    def _user_echo_line(self, content: str) -> Text:
        brand = _ui_palette(getattr(self, "_active_theme_setting", "transparent"))["brand"]
        return Text.from_markup(f"[{brand}]▌[/{brand}] [bold]{escape(content)}[/bold]")

    def _send_message(
        self,
        text: str,
        *,
        turn_user_text: str | None = None,
        echo_user: bool = True,
    ) -> None:
        log = self.query_one("#chat-log", ChatLog)
        display = turn_user_text or text
        if echo_user:
            lines = display.splitlines()
            if len(lines) > 3:
                preview = "\n".join(lines[:3]) + f"\n... ({len(lines)} lines total)"
                log.write(self._user_echo_line(preview))
            else:
                log.write(self._user_echo_line(display))
        mem_ctx = ""
        try:
            from cli.memory import build_memory_context

            mem_ctx = build_memory_context(display)
        except Exception:
            logger.debug("memory context injection failed", exc_info=True)
        if workflow_ctx := self._recent_workflow_context(display):
            mem_ctx = "\n\n".join(item for item in (mem_ctx, workflow_ctx) if item)
        # 时间挂在 user 尾部，system 保持静态以便跨 turn 复用 prompt cache。
        user_message = {"role": "user", "content": append_beijing_time_context(text)}
        if mem_ctx:
            user_message["_memory_context"] = mem_ctx
        self._messages.append(user_message)
        if hasattr(self, "_cancel_event") and self._cancel_event is not None:
            self._cancel_event.clear()
        self._ensure_conversation().begin_turn(turn_user_text or text)
        self._start_spinner("thinking")
        self._run_agent()

    def _send_system_notification(self, text: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        log.write(Text.from_markup("  [dim]↳ 后台结果已回传给 agent[/dim]"))
        self._messages.append(
            {
                "role": "user",
                "content": append_beijing_time_context(text),
                "_system_notification": True,
            }
        )
        if hasattr(self, "_cancel_event") and self._cancel_event is not None:
            self._cancel_event.clear()
        self._ensure_conversation().begin_turn(text, system_notification=True)
        self._start_spinner("处理后台结果")
        self._run_agent()

    def _dispatch_queued_item(self, item: Any) -> None:
        self._refresh_followups_panel()
        if isinstance(item, QueuedInput):
            if item.kind == "system_notification":
                self._send_system_notification(item.content)
                return
            self._send_message(item.content)
            return
        if isinstance(item, dict) and item.get("type") == "system_notification":
            self._send_system_notification(str(item.get("content", "")))
            return
        self._send_message(str(item))

    def _apply_session_ui(self, events: list) -> None:
        log = self.query_one("#chat-log", ChatLog)
        for event in events:
            etype = getattr(event, "type", "")
            if etype == "spinner_stop":
                self._stop_spinner()
            elif etype == "status_refresh":
                self._update_status()
            elif etype == "resume_hint":
                cancelled = bool((event.payload or {}).get("cancelled"))
                if cancelled:
                    log.write(Text.from_markup("  [dim]提示: 输入「继续」可接着刚才的问题[/dim]"))
                else:
                    log.write(Text.from_markup("  [dim]提示: 切换模型后输入「继续」可重试刚才的问题[/dim]"))
            elif etype == "queued":
                self._refresh_followups_panel()
            elif etype == "steered":
                depth = int((event.payload or {}).get("depth") or 0)
                log.write(Text.from_markup(f"  [dim]🧭 转向已入队（pending:{depth}）[/dim]"))
            elif etype == "interrupted_banner":
                payload = event.payload or {}
                sid = escape(str(payload.get("session_id", "")))
                query = escape(str(payload.get("query", "")))
                log.write(Text.from_markup(f"[yellow]⚠ 检测到会话 [bold]#{sid}[/bold] 上次执行中途异常中断。[/yellow]"))
                log.write(Text.from_markup(f'[yellow]输入「继续」可恢复任务: [bold]"{query}"[/bold][/yellow]'))

    def _refresh_followups_panel(self) -> None:
        try:
            panel = self.query_one("#followups-panel", FollowUpsPanel)
            inp = self.query_one("#chat-input", ChatInput)
        except Exception:
            return
        if not isinstance(panel, FollowUpsPanel):
            return
        brand = _ui_palette(getattr(self, "_active_theme_setting", "transparent"))["brand"]
        panel.apply_brand(brand)
        items = list(self._conversation.input_queue)
        if items:
            panel.update(render_followups(items, brand=brand))
            panel.display = True
        else:
            panel.update("")
            panel.display = False
        if isinstance(inp, ChatInput) and getattr(self, "_input_mode", _InputState.NONE) == _InputState.NONE:
            inp.placeholder = FOLLOWUP_PLACEHOLDER if (self._busy or items) else IDLE_PLACEHOLDER

    def on_follow_up_edit_requested(self, _event: FollowUpEditRequested) -> None:
        if self._input_mode != _InputState.NONE:
            return
        taken = self._conversation.pop_last_user_followup()
        if taken is None:
            return
        inp = self.query_one("#chat-input", ChatInput)
        inp.value = taken.content
        inp.cursor_position = len(taken.content)
        self._refresh_followups_panel()
        self._update_status()

    def on_follow_up_cancel_requested(self, _event: FollowUpCancelRequested) -> None:
        if self._input_mode != _InputState.NONE:
            return
        inp = self.query_one("#chat-input", ChatInput)
        if (inp.value or "").strip() or inp._pasted_text:
            inp.clear()
            inp._pasted_text = None
            return
        if self._conversation.pop_last_user_followup() is None:
            return
        self._refresh_followups_panel()
        self._update_status()

    def _resume_active_turn(self, log) -> None:
        original = self._conversation.resumable_user_text
        soft = self._conversation.build_resume_user_text()
        hard = self._conversation.take_hard_checkpoint()
        self._conversation.abandon_active_turn()
        if not original or not soft:
            log.write(Text.from_markup("[yellow]没有可继续的对话轮次[/yellow]"))
            return
        if hard and hard.messages:
            self._messages[:] = [dict(item) for item in hard.messages]
            self._pending_resume_checkpoint = hard
        log.write(Text.from_markup("  [dim]↻ 重试刚才未完成的问题[/dim]"))
        self._send_message(soft, turn_user_text=original, echo_user=False)

    def _resume_recent_workflow_followup(self, text: str, log) -> None:
        from cli.workflows.resume import build_chat_resume_prompt

        run = self._latest_relevant_workflow_run()
        if not run:
            log.write(Text.from_markup("[yellow]没有可继续的 workflow[/yellow]"))
            return
        self._conversation.abandon_active_turn()
        self._send_message(build_chat_resume_prompt(_workflow_run_with_events(run), text))

    # ----- 斜杠命令 -----

    def _handle_command(self, raw: str) -> None:
        cmd = raw.lower().split()[0]
        log = self.query_one("#chat-log", ChatLog)

        if cmd == "/steer":
            self._handle_steer_command(raw, log)
            return
        if cmd in ("/quit", "/exit", "/q"):
            self._save_and_exit()
        elif cmd == "/clear":
            self.action_clear_chat()
        elif cmd == "/new":
            self.action_new_chat()
        elif cmd == "/help":
            self._show_help(log)
        elif cmd == "/token":
            self._show_token_usage(log)
        elif cmd == "/login":
            self._start_login()
        elif cmd == "/logout":
            self._do_logout()
        elif cmd == "/config":
            self._handle_config_cmd(raw, log)
        elif cmd == "/model":
            self._handle_model_cmd(raw, log)
        elif cmd == "/changelog":
            self._show_changelog(log)
        elif cmd == "/prompt":
            self._handle_prompt_cmd(raw, log)
        elif cmd in ("/workflow", "/wf"):
            self._handle_workflow_cmd(raw, log)
        elif cmd == "/resume":
            self._handle_resume_cmd(raw)
        elif cmd == "/fork":
            self.action_fork_session()
        elif cmd == "/schedule":
            self._handle_schedule_cmd(raw, log)
        elif cmd == "/doctor":
            self._show_backend_doctor(log)
        elif cmd == "/browser":
            self._handle_browser_cmd(raw, log)
        elif cmd == "/theme":
            self._handle_theme_cmd(raw, log)
        else:
            self._try_skill(raw, log)

    def _handle_theme_cmd(self, raw: str, log) -> None:
        parts = raw.strip().split(maxsplit=1)
        if len(parts) == 1 or parts[1].strip() in ("list", "show"):
            current = getattr(self, "_active_theme_setting", self.theme)
            lines = ["\n[bold]可用终端主题[/bold]"]
            for key, desc in SUPPORTED_TUI_THEMES.items():
                is_cur = key == current or (current == "transparent" and key == "transparent")
                mark = " [green]✔ (当前)[/green]" if is_cur else ""
                lines.append(f"  [cyan]{key:<16s}[/cyan] — {desc}{mark}")
            lines.append(
                "\n[dim]用法: /theme <名称> （例如: /theme transparent | /theme dracula | /theme auto）[/dim]\n"
            )
            log.write(Text.from_markup("\n".join(lines)))
        else:
            target = parts[1].strip()
            applied = self.apply_theme_setting(target)
            if applied:
                try:
                    from cli.auth import save_config_key

                    save_config_key("theme", target)
                except Exception:
                    logger.debug("save theme config failed", exc_info=True)
                log.write(Text.from_markup(f"[green]✓ 已设置主题为: {applied}[/green]"))
            else:
                log.write(Text.from_markup(f"[yellow]⚠ 未知主题: {target}，输入 /theme list 查看可用主题[/yellow]"))

    def _show_help(self, log) -> None:
        from cli.prompt_templates import load_prompt_templates
        from cli.skills import load_skills

        templates = load_prompt_templates()
        skills = load_skills()
        template_lines = "".join(f"  /{t.name:<11s}— {t.description}\n" for t in templates.values())
        skill_lines = "".join(f"  /{s.name:<11s}— {s.description}\n" for s in skills.values())
        log.write(
            Text.from_markup(
                "\n[bold]可用命令[/bold]\n"
                "  /model   — 切换模型（list/add/rm/default）\n"
                "  /theme   — 切换终端配色主题（transparent/auto/dracula/nord/monokai...）\n"
                "  /config  — 数据源配置（tushare_token, tickflow_api_key）\n"
                "  /login   — 登录\n"
                "  /logout  — 退出登录\n"
                "  /token   — Token 用量\n"
                "  /changelog— 版本更新日志\n"
                "  /prompt  — Prompt 模板（list/show/<name>）\n"
                "  /workflow— workflow（approve/reload/restart/pause/stop/save/run）\n"
                "  /schedule— 定时任务（list/status/run/add/rm/on/off）\n"
                "  /doctor  — 模型与数据源健康检查\n"
                "  /browser — 本机 Chrome CDP（status|start|hint|stop）\n"
                "  /steer   — 忙时注入转向指令（本轮生效；也可用 !指令）\n"
                "  /resume  — 恢复历史对话\n"
                "  /fork    — 分叉当前会话\n"
                "  /new     — 新对话 (Ctrl+N)\n"
                "  /clear   — 清屏 (Ctrl+L)\n"
                "  /quit    — 退出 (Ctrl+Q)\n"
                f"\n[bold]Skills[/bold]\n{skill_lines}"
                f"\n[bold]Prompt Templates[/bold]\n{template_lines}"
                "\n[bold]快捷键[/bold]\n"
                "  Ctrl+P   — 命令面板\n"
                "  Ctrl+C   — 复制选中文本 / 忙时中断\n"
                "  Ctrl+N   — 新对话\n"
                "  Ctrl+L   — 清屏\n"
                "  忙时普通输入 — 进入 follow-ups 排队（enter 立即排队 · ↑ 编辑 · esc 取消）\n"
                "  `!指令` — 本轮转向\n"
                "  鼠标拖选  — 选择文本\n"
            )
        )

    def _handle_steer_command(self, raw: str, log) -> None:
        from cli.conversation.intents import strip_steer_prefix

        payload = strip_steer_prefix(raw)
        if not payload:
            log.write(Text.from_markup("[dim]用法: /steer <指令>  或忙时输入 !指令[/dim]"))
            return
        if self._busy:
            self._apply_session_ui(self._conversation.enqueue_steer(payload))
            log.write(Text.from_markup("  [dim]🧭 已注入转向指令（本轮下一跳生效）[/dim]"))
            return
        log.write(Text.from_markup("[dim]当前空闲，转向指令将作为普通消息发送[/dim]"))
        self._send_message(payload)

    def _show_token_usage(self, log) -> None:
        t = self._session_tokens
        if t["rounds"] == 0:
            log.write(Text.from_markup("[dim]本次会话尚无 Token 记录[/dim]"))
            return
        extras = []
        avg = _session_avg_tok_per_s(t)
        if avg is not None:
            extras.append(f"均速: {avg:g}tok/s")
        if t.get("cache_reported") and t["input"] > 0:
            from cli.usage_metrics import cache_hit_rate_pct

            hit = cache_hit_rate_pct(int(t.get("cache_read") or 0), int(t["input"]))
            if hit is not None:
                extras.append(f"缓存命中: {hit}%（读 {int(t.get('cache_read') or 0):,}）")
        extra_text = ("  " + "  ".join(extras)) if extras else ""
        log.write(
            Text.from_markup(
                f"\n[bold]Token 用量[/bold]  "
                f"输入: {t['input']:,}  输出: {t['output']:,}  "
                f"合计: {t['input'] + t['output']:,}  轮次: {t['rounds']}{extra_text}"
            )
        )

    def _handle_config_cmd(self, raw: str, log) -> None:
        parts = raw.strip().split(maxsplit=2)
        if len(parts) == 1:
            self._show_config()
        elif parts[1] == "set" and len(parts) >= 3:
            self._start_config_set(parts[2])
        else:
            log.write(
                Text.from_markup(
                    "[dim]/config 用法: /config | /config set tushare_token | tickflow_api_key | "
                    "stream_chunk_timeout_seconds | tool_timeout_seconds[/dim]"
                )
            )

    def _handle_model_cmd(self, raw: str, log) -> None:
        parts = raw.strip().split()
        if len(parts) == 1:
            self._switch_model_selector()
        elif parts[1] == "list":
            self._list_models()
        elif parts[1] == "add":
            self._start_model_add()
        elif parts[1] == "rm" and len(parts) >= 3:
            self._remove_model(parts[2])
        elif parts[1] == "default" and len(parts) >= 3:
            self._set_default_model(parts[2])
        else:
            log.write(
                Text.from_markup(
                    "[dim]/model 用法: /model (切换) | /model list | /model add | /model rm <id> | /model default <id>[/dim]"
                )
            )

    def _handle_resume_cmd(self, raw: str) -> None:
        parts = raw.strip().split(maxsplit=1)
        if len(parts) > 1:
            self._resume_session(parts[1].strip())
        else:
            self._resume_session_selector()

    def _show_changelog(self, log) -> None:
        from utils.package_resources import runtime_resource

        path = runtime_resource("CHANGELOG.md")
        if not path.exists():
            log.write(Text.from_markup("[dim]CHANGELOG.md 不存在[/dim]"))
            return
        text = path.read_text(encoding="utf-8").strip()
        lines = text.splitlines()
        # 只显示最近一个版本段落（到下一个 ## 或结尾）
        start = next((i for i, l in enumerate(lines) if l.startswith("## ")), 0)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        section = "\n".join(lines[start:end]).strip()
        log.write(Text.from_markup(f"\n[bold]{section}[/bold]\n"))

    # ----- Skills -----

    def _try_skill(self, raw: str, log) -> None:
        from cli.prompt_templates import load_prompt_templates
        from cli.skills import load_skills
        from cli.workflows.saved import load_saved_workflow

        templates = load_prompt_templates()
        skills = load_skills()
        parts = raw.strip().split(maxsplit=1)
        cmd_name = parts[0].lstrip("/").lower()
        user_input = parts[1] if len(parts) > 1 else ""
        if cmd_name in skills:
            self._execute_skill(cmd_name, user_input)
        elif cmd_name in templates:
            self._execute_prompt_template(cmd_name, user_input)
        elif load_saved_workflow(cmd_name):
            self._run_saved_workflow(cmd_name, user_input, log)
        else:
            log.write(Text.from_markup(f"[red]未知命令: {raw}[/red]，/help 查看"))

    def _execute_skill(self, name: str, user_input: str = "") -> None:
        from cli.skills import load_skills

        log = self.query_one("#chat-log", ChatLog)
        skills = load_skills()
        skill = skills.get(name)
        if not skill:
            log.write(Text.from_markup(f"[red]未知 skill: {name}[/red]"))
            return
        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请先输入 /model add[/yellow]"))
            return
        prompt = skill.prompt.replace("{user_input}", user_input).strip()
        self._send_message(prompt)

    def action_run_skill(self, name: str) -> None:
        """命令面板调用 skill 入口。"""
        self._execute_skill(name)

    def action_run_saved_workflow(self, name: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        self._run_saved_workflow(name, "", log)

    # ----- Prompt Templates -----

    def _handle_workflow_cmd(self, raw: str, log) -> None:
        parts = raw.strip().split(maxsplit=3)
        sub = parts[1].lower() if len(parts) >= 2 else "list"
        target = parts[2].strip() if len(parts) >= 3 else ""
        if sub in {"resume", "continue"}:
            if self._resume_running_workflow(target, log, quiet_missing=True):
                return
            self._resume_workflow(target, log)
            return
        if sub in {"show", "open"}:
            self._show_workflow_detail(target, log)
            return
        if sub in {"script", "json"}:
            self._show_workflow_script(target, log)
            return
        if sub == "events":
            self._show_workflow_events(target, log)
            return
        if sub == "agent":
            self._show_workflow_agent_detail(target, parts[3].strip() if len(parts) >= 4 else "", log)
            return
        if sub in {"rerun", "replay"}:
            self._rerun_workflow(target, log)
            return
        if sub in {"approve", "yes"}:
            self._approve_workflow(target, log)
            return
        if sub in {"deny", "cancel"}:
            self._deny_workflow(target, log)
            return
        if sub == "pause":
            self._pause_workflow(target, log)
            return
        if sub == "resume-run":
            self._resume_running_workflow(target, log)
            return
        if sub == "stop":
            self._stop_workflow(target, log)
            return
        if sub == "status":
            self._show_workflow_runtime_status(log)
            return
        if sub == "reload":
            self._reload_pending_workflow_script(target, log)
            return
        if sub == "restart":
            self._restart_workflow_step(target, parts[3].strip() if len(parts) >= 4 else "", log)
            return
        if sub == "save":
            self._save_workflow_command(target, parts[3].strip() if len(parts) >= 4 else "", log)
            return
        if sub == "run":
            name, args = _split_workflow_name_args(target, parts[3].strip() if len(parts) >= 4 else "")
            self._run_saved_workflow(name, args, log)
            return
        if sub.startswith("wf_"):
            self._show_workflow_detail(sub, log)
            return
        self._show_workflows()

    def _handle_workflow_control_text(self, text: str, log) -> bool:
        intent = _workflow_control_intent(text)
        if not intent and len(self._pending_workflows) == 1:
            if model_intent := self._pending_workflow_model_reply_intent(text):
                if model_intent == "revise":
                    self._revise_pending_workflow("", text, log)
                    return True
                if model_intent == "chat":
                    return False
                intent = (model_intent, "")
            elif _pending_workflow_revision_intent(text):
                self._revise_pending_workflow("", text, log)
                return True
            else:
                pending_intent = _pending_workflow_reply_intent(text)
                intent = (pending_intent, "") if pending_intent else None
        if not intent:
            return False
        action, run_id = intent
        if action == "rerun":
            self._rerun_workflow(run_id, log)
            return True
        if action == "approve":
            self._approve_workflow(run_id, log)
            return True
        if action == "deny":
            self._deny_workflow(run_id, log)
            return True
        if action == "pause":
            self._pause_workflow(run_id, log)
            return True
        if action == "resume_running":
            self._resume_running_workflow(run_id, log)
            return True
        if action == "stop":
            self._stop_workflow(run_id, log)
            return True
        if action == "reload":
            self._reload_pending_workflow_script(run_id, log)
            return True
        log.write(self._user_echo_line(text))
        if action == "events":
            self._show_workflow_events(run_id, log)
            return True
        if action == "status":
            self._show_workflow_runtime_status(log)
            return True
        if action == "script":
            self._show_workflow_script(run_id, log)
            return True
        self._show_workflow_detail(run_id, log)
        return True

    def _pending_workflow_model_reply_intent(self, text: str) -> str:
        try:
            from cli.workflows.pending_reply import route_pending_workflow_reply

            pending = next(iter(self._pending_workflows.values()))
            return route_pending_workflow_reply(
                text, getattr(self, "_provider", None), getattr(pending.runtime, "run", None)
            )
        except Exception:
            logger.debug("pending workflow model reply intent failed", exc_info=True)
            return ""

    def _latest_relevant_workflow_run(self) -> dict[str, Any] | None:
        from cli.workflows.store import list_workflow_runs

        rows = list_workflow_runs(limit=8)
        for run in rows:
            if str(run.get("session_id", "")) == self._session_id:
                return run
        if rows and _workflow_run_is_recent(rows[0]):
            return rows[0]
        return None

    def _recent_workflow_context(self, text: str) -> str:
        try:
            from cli.workflows.resume import build_recent_workflow_context, should_include_recent_workflow_context

            if not should_include_recent_workflow_context(text):
                return ""
            run = self._latest_relevant_workflow_run()
            return build_recent_workflow_context(_workflow_run_with_events(run)) if run else ""
        except Exception:
            logger.debug("recent workflow context injection failed", exc_info=True)
            return ""

    def _resume_workflow(self, run_id: str, log) -> None:
        from cli.workflows.resume import build_resume_prompt

        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        self._send_message(build_resume_prompt(run))

    def _show_workflow_detail(self, run_id: str, log) -> None:
        from cli.workflows.store import load_workflow_events

        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        script = _workflow_script(run)
        agent_details = _workflow_agent_details_from_events(load_workflow_events(str(run.get("run_id", "")), limit=500))
        lines = [
            "\n[bold]Workflow[/bold] "
            f"[cyan]{escape(str(run.get('run_id', '')))}[/cyan] "
            f"{escape(str(run.get('status', '')))} {escape(str(run.get('label', '')))}",
            f"  [dim]原始请求：{escape(str(run.get('user_text', ''))[:120])}[/dim]",
        ]
        if script.get("title"):
            lines.append(f"  动态脚本：{escape(str(script.get('title')))}")
        if script.get("rationale"):
            lines.append(f"  [dim]编排理由：{escape(str(script.get('rationale'))[:180])}[/dim]")
        runtime = script.get("runtime", {}) if isinstance(script.get("runtime"), dict) else {}
        if runtime.get("script_path"):
            lines.append(f"  [dim]脚本文件：{escape(str(runtime.get('script_path')))}[/dim]")
        steps = run.get("plan", {}).get("steps", [])
        if isinstance(steps, list) and steps:
            lines.append("  [bold]步骤[/bold]")
            lines.extend(
                _workflow_detail_step_line(
                    step,
                    agent_details.get(str(step.get("step_id") or step.get("id") or "")),
                )
                for step in steps
                if isinstance(step, dict)
            )
        log.write(Text.from_markup("\n".join(lines)))

    def _show_workflow_script(self, run_id: str, log) -> None:
        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        script = _workflow_script(run)
        if not script:
            log.write(Text.from_markup(f"[yellow]workflow {escape(str(run.get('run_id', '')))} 没有保存脚本[/yellow]"))
            return
        body = json.dumps(script, ensure_ascii=False, indent=2, default=str)
        log.write(Markdown(f"```json\n{body}\n```"))

    def _show_workflow_events(self, run_id: str, log) -> None:
        from cli.workflows.store import load_workflow_events

        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        rows = load_workflow_events(str(run.get("run_id", "")), limit=80)
        if not rows:
            log.write(Text.from_markup(f"[dim]暂无 workflow 事件: {escape(str(run.get('run_id', '')))}[/dim]"))
            return
        lines = [f"\n[bold]Workflow events[/bold] [cyan]{escape(str(run.get('run_id', '')))}[/cyan]"]
        for row in rows[-40:]:
            payload = row.get("payload", {})
            step = payload.get("step", {}) if isinstance(payload, dict) else {}
            detail = f" · {step.get('step_id', '')} {step.get('status', '')}" if isinstance(step, dict) else ""
            lines.append(
                f"  [dim]{escape(str(row.get('created_at', ''))[:19])}[/dim] "
                f"{escape(str(row.get('event_type', '')))}{escape(detail)}"
            )
        log.write(Text.from_markup("\n".join(lines)))

    def _show_workflow_agent_detail(self, run_id: str, step_id: str, log) -> None:
        if not run_id or not step_id:
            log.write(Text.from_markup("[yellow]用法: /workflow agent <run_id> <step_id>[/yellow]"))
            return
        from cli.workflows.store import load_workflow_events

        rows = load_workflow_events(run_id, limit=500)
        detail = _workflow_agent_detail_from_events(rows, step_id)
        if not detail:
            log.write(Text.from_markup(f"[yellow]未找到 agent detail: {escape(run_id)} {escape(step_id)}[/yellow]"))
            return
        body = (
            f"# {detail.get('title', step_id)}\n\n"
            f"- agent: `{detail.get('agent', '')}`\n"
            f"- status: `{detail.get('status', '')}`\n"
            f"- elapsed: `{detail.get('elapsed', 0)}`\n"
            f"- tool_calls: `{', '.join(str(item) for item in detail.get('tool_calls', [])) or '-'}`\n\n"
            "## Prompt\n\n"
            f"{detail.get('prompt', '') or '-'}\n\n"
            "## Context\n\n"
            f"{detail.get('context', '') or '-'}\n\n"
            "## Result\n\n"
            f"{detail.get('result', '') or detail.get('error', '') or '-'}"
        )
        log.write(Markdown(body))

    def _rerun_workflow(self, run_id: str, log) -> None:
        if self._busy:
            log.write(Text.from_markup("[yellow]当前 agent 正在运行，完成后再复跑 workflow[/yellow]"))
            return
        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        script = _workflow_script(run)
        if not script:
            log.write(
                Text.from_markup(f"[yellow]workflow {escape(str(run.get('run_id', '')))} 没有可复跑脚本[/yellow]")
            )
            return
        self._workflow_override = _WorkflowOverride(str(run.get("run_id", "")), script, _workflow_context_from_run(run))
        self._send_message(f"复跑 workflow {run.get('run_id', '')}\n原始请求: {run.get('user_text', '')}")

    def _save_workflow_command(self, run_id: str, name: str, log) -> None:
        if not name:
            log.write(Text.from_markup("[yellow]用法: /workflow save <run_id> <name>[/yellow]"))
            return
        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        try:
            from cli.workflows.saved import save_workflow_script

            path = save_workflow_script(name, run)
        except ValueError as exc:
            log.write(Text.from_markup(f"[red]保存 workflow 失败: {escape(str(exc))}[/red]"))
            return
        log.write(
            Text.from_markup(
                f"[green]已保存 workflow[/green] [cyan]/{escape(path.stem)}[/cyan] [dim]{escape(str(path))}[/dim]"
            )
        )

    def _run_saved_workflow(self, name: str, args: str, log) -> None:
        if self._busy:
            log.write(Text.from_markup("[yellow]当前 agent 正在运行，完成后再运行保存的 workflow[/yellow]"))
            return
        if not name:
            self._show_saved_workflows(log)
            return
        from cli.workflows.saved import load_saved_workflow

        saved = load_saved_workflow(name)
        if not saved:
            log.write(Text.from_markup(f"[red]未找到保存的 workflow: {escape(name)}[/red]"))
            return
        script = saved.get("script", {})
        if not isinstance(script, dict) or not script:
            log.write(Text.from_markup(f"[yellow]保存的 workflow {escape(name)} 没有可运行脚本[/yellow]"))
            return
        run_like = _workflow_run_from_saved(saved)
        self._workflow_override = _WorkflowOverride(
            f"saved:{saved.get('name', name)}", script, _workflow_context_from_run(run_like), args
        )
        self._send_message(f"运行 workflow /{saved.get('name', name)}\n参数: {args or '-'}")

    def _show_saved_workflows(self, log) -> None:
        from cli.workflows.saved import list_saved_workflows

        rows = list_saved_workflows()
        if not rows:
            log.write(Text.from_markup("[dim]暂无保存的 workflow[/dim]"))
            return
        lines = ["\n[bold]已保存 workflow[/bold] [dim]/workflow run <name> [args][/dim]"]
        for row in rows:
            lines.append(f"  [cyan]/{escape(str(row.get('name', '')))}[/cyan] {escape(str(row.get('label', '')))}")
        log.write(Text.from_markup("\n".join(lines)))

    def _approve_workflow(self, run_id: str, log) -> None:
        target_id = self._resolve_pending_workflow_id(run_id, log)
        if not target_id:
            return
        pending = self._pending_workflows.pop(target_id)
        task_id = f"wfbg_{target_id}_{time.time_ns()}"
        self._launch_workflow_background(
            pending.runtime,
            pending.messages,
            pending.system_prompt,
            pending.model_name,
            pending.provider_name,
            task_id,
            log.write,
            lambda: log.scroll_end(animate=False),
        )
        log.write(Text.from_markup(f"[green]已批准 workflow[/green] [dim]{escape(target_id)}[/dim]"))

    def _deny_workflow(self, run_id: str, log) -> None:
        target_id = self._resolve_pending_workflow_id(run_id, log)
        if not target_id:
            return
        self._pending_workflows.pop(target_id, None)
        from cli.workflows.models import STOPPED
        from cli.workflows.store import append_workflow_event, set_workflow_status

        set_workflow_status(target_id, STOPPED, "用户取消 workflow")
        append_workflow_event(target_id, "workflow_denied", {"type": "workflow_denied", "run_id": target_id})
        log.write(Text.from_markup(f"[yellow]已取消 workflow[/yellow] [dim]{escape(target_id)}[/dim]"))

    def _apply_workflow_control_action(
        self,
        run_id: str,
        log,
        *,
        control_method: str,
        new_status: str,
        status_reason: str,
        event_type: str,
        verb: str,
        color: str,
        quiet_missing: bool = False,
    ) -> bool:
        target_id = self._resolve_active_workflow_id(run_id, log if not quiet_missing else None)
        if not target_id:
            return False
        from cli.workflows.control import get_workflow_control
        from cli.workflows.store import append_workflow_event, set_workflow_status

        control = get_workflow_control(target_id)
        if not control:
            if not quiet_missing:
                log.write(Text.from_markup(f"[yellow]workflow 不在运行中: {escape(target_id)}[/yellow]"))
            return False
        getattr(control, control_method)()
        if status_reason:
            set_workflow_status(target_id, new_status, status_reason)
        else:
            set_workflow_status(target_id, new_status)
        append_workflow_event(target_id, event_type, {"type": event_type, "run_id": target_id})
        log.write(Text.from_markup(f"[{color}]{verb} workflow[/{color}] [dim]{escape(target_id)}[/dim]"))
        return True

    def _pause_workflow(self, run_id: str, log) -> None:
        from cli.workflows.models import PAUSED

        self._apply_workflow_control_action(
            run_id,
            log,
            control_method="pause",
            new_status=PAUSED,
            status_reason="",
            event_type="workflow_paused",
            verb="已暂停",
            color="yellow",
        )

    def _resume_running_workflow(self, run_id: str, log, *, quiet_missing: bool = False) -> bool:
        from cli.workflows.models import RUNNING

        return self._apply_workflow_control_action(
            run_id,
            log,
            control_method="resume",
            new_status=RUNNING,
            status_reason="",
            event_type="workflow_resumed",
            verb="已恢复",
            color="green",
            quiet_missing=quiet_missing,
        )

    def _stop_workflow(self, run_id: str, log) -> None:
        from cli.workflows.models import STOPPED

        self._apply_workflow_control_action(
            run_id,
            log,
            control_method="stop",
            new_status=STOPPED,
            status_reason="用户停止 workflow",
            event_type="workflow_stop_requested",
            verb="已请求停止",
            color="red",
        )

    def _reload_pending_workflow_script(self, run_id: str, log) -> None:
        target_id = self._resolve_pending_workflow_id(run_id, log)
        if not target_id:
            return
        pending = self._pending_workflows[target_id]
        script_path = _workflow_script_path(pending.runtime.run)
        if not script_path:
            log.write(Text.from_markup(f"[yellow]workflow {escape(target_id)} 没有可 reload 的脚本文件[/yellow]"))
            return
        try:
            from cli.workflows.store import load_workflow_script_payload

            event = pending.runtime.replace_prepared_script(load_workflow_script_payload(script_path))
        except Exception as exc:
            log.write(Text.from_markup(f"[red]reload workflow script 失败: {escape(str(exc))}[/red]"))
            return
        _display_workflow_plan_event(
            event,
            log.write,
            lambda: log.scroll_end(animate=False),
            launch_state="pending_approval",
        )
        log.write(Text.from_markup(f"[green]已从脚本文件 reload[/green] [dim]{escape(script_path)}[/dim]"))

    def _revise_pending_workflow(self, run_id: str, feedback: str, log) -> None:
        target_id = self._resolve_pending_workflow_id(run_id, log)
        if not target_id:
            return
        pending = self._pending_workflows[target_id]
        try:
            event = pending.runtime.revise_prepared_script(feedback)
        except Exception as exc:
            log.write(Text.from_markup(f"[red]修订 workflow 失败: {escape(str(exc))}[/red]"))
            return
        _display_workflow_plan_event(
            event,
            log.write,
            lambda: log.scroll_end(animate=False),
            launch_state="pending_approval",
        )
        log.write(
            Text.from_markup(
                f"[green]已根据反馈更新 workflow[/green] [dim]{escape(target_id)}[/dim] "
                "[dim]回复“开始”运行 · 回复“取消”停止[/dim]"
            )
        )

    def _restart_workflow_step(self, run_id: str, raw_step_and_args: str, log) -> None:
        step_id, args = _split_workflow_name_args(raw_step_and_args, "")
        if not run_id or not step_id:
            log.write(Text.from_markup("[yellow]用法: /workflow restart <run_id> <step_id> [args][/yellow]"))
            return
        run = self._load_workflow_run(run_id, log)
        if not run:
            return
        if not _workflow_has_step(run, step_id):
            log.write(Text.from_markup(f"[red]workflow {escape(run_id)} 没有 task: {escape(step_id)}[/red]"))
            return
        script = _workflow_script(run)
        if not script:
            log.write(Text.from_markup(f"[yellow]workflow {escape(run_id)} 没有可重启脚本[/yellow]"))
            return
        self._workflow_override = _WorkflowOverride(run_id, script, _workflow_context_from_run(run), args, step_id)
        self._send_message(f"重启 workflow {run_id} 的 task {step_id}\n参数: {args or '-'}")

    def _show_workflow_runtime_status(self, log) -> None:
        from cli.workflows.control import active_workflow_ids, get_workflow_control

        pending = sorted(self._pending_workflows)
        active = active_workflow_ids()
        if not pending and not active:
            log.write(Text.from_markup("[dim]暂无 pending/running workflow[/dim]"))
            return
        lines = ["\n[bold]Workflow runtime[/bold]"]
        for run_id in pending:
            lines.append(f"  [yellow]pending[/yellow] {escape(run_id)}")
        for run_id in active:
            control = get_workflow_control(run_id)
            status = "paused" if control and control.paused() else "running"
            lines.append(f"  [cyan]{escape(status)}[/cyan] {escape(run_id)}")
        log.write(Text.from_markup("\n".join(lines)))

    def _resolve_pending_workflow_id(self, run_id: str, log) -> str:
        target_id = run_id.strip()
        if target_id:
            if target_id not in self._pending_workflows:
                log.write(Text.from_markup(f"[red]未找到待批准 workflow: {escape(target_id)}[/red]"))
                return ""
            return target_id
        if len(self._pending_workflows) == 1:
            return next(iter(self._pending_workflows))
        if not self._pending_workflows:
            log.write(Text.from_markup("[dim]暂无待批准 workflow[/dim]"))
            return ""
        log.write(Text.from_markup("[yellow]存在多个待批准 workflow，请指定 run_id[/yellow]"))
        return ""

    def _resolve_active_workflow_id(self, run_id: str, log) -> str:
        from cli.workflows.control import active_workflow_ids

        target_id = run_id.strip()
        active = active_workflow_ids()
        if target_id:
            return target_id
        if len(active) == 1:
            return active[0]
        if not active:
            if log:
                log.write(Text.from_markup("[dim]暂无运行中 workflow[/dim]"))
            return ""
        if log:
            log.write(Text.from_markup("[yellow]存在多个运行中 workflow，请指定 run_id[/yellow]"))
        return ""

    def _load_workflow_run(self, run_id: str, log) -> dict[str, Any] | None:
        from cli.workflows.store import get_workflow_run, list_workflow_runs

        target_id = run_id.strip()
        if not target_id:
            rows = list_workflow_runs(limit=1)
            if not rows:
                log.write(Text.from_markup("[dim]暂无 workflow 记录[/dim]"))
                return None
            target_id = str(rows[0].get("run_id", ""))
        run = get_workflow_run(target_id)
        if not run:
            log.write(Text.from_markup(f"[red]未找到 workflow: {escape(target_id)}[/red]"))
            return None
        return run

    def _show_workflows(self) -> None:
        from cli.workflows.control import active_workflow_ids
        from cli.workflows.store import list_workflow_runs

        log = self.query_one("#chat-log", ChatLog)
        rows = list_workflow_runs(limit=8)
        active = active_workflow_ids()
        if not rows and not self._pending_workflows and not active:
            log.write(Text.from_markup("[dim]暂无 workflow 记录[/dim]"))
            return
        lines = [
            "\n[bold]最近 workflow[/bold] "
            "[dim]/workflow approve|reload|pause|resume|stop|show|script|events <id> · "
            "/workflow agent <id> <task_id> · "
            "/workflow restart <id> <task_id> · "
            "/workflow save <id> <name> · /workflow run <name>[/dim]"
        ]
        if self._pending_workflows:
            lines.append(
                "  [yellow]pending approval[/yellow] " + ", ".join(escape(run_id) for run_id in self._pending_workflows)
            )
        if active:
            lines.append("  [cyan]active[/cyan] " + ", ".join(escape(run_id) for run_id in active))
        for row in rows:
            lines.append(
                f"  [cyan]{row['run_id']}[/cyan] {row['status']} {row['label']} "
                f"[dim]{str(row.get('user_text', ''))[:48]}[/dim]"
            )
        log.write(Text.from_markup("\n".join(lines)))

    def _show_prompt_templates(self) -> None:
        from cli.prompt_templates import load_prompt_templates

        log = self.query_one("#chat-log", ChatLog)
        templates = load_prompt_templates()
        if not templates:
            log.write(Text.from_markup("[dim]暂无 Prompt 模板[/dim]"))
            return
        lines = ["\n[bold]Prompt 模板[/bold]"]
        for tpl in templates.values():
            hint = f" [dim]{tpl.argument_hint}[/dim]" if tpl.argument_hint else ""
            lines.append(f"  [cyan]/{tpl.name:<13}[/cyan] {tpl.description}{hint}")
        lines.append("\n[dim]用法: /prompt <name> [补充说明]，也可以直接输入 /daily 这类模板名[/dim]")
        log.write(Text.from_markup("\n".join(lines)))

    def _handle_prompt_cmd(self, raw: str, log) -> None:
        from cli.prompt_templates import load_prompt_templates

        templates = load_prompt_templates()
        parts = raw.strip().split(maxsplit=2)
        if len(parts) == 1 or parts[1] == "list":
            self._show_prompt_templates()
            return
        if parts[1] == "show":
            if len(parts) < 3:
                log.write(Text.from_markup("[dim]用法: /prompt show <name>[/dim]"))
                return
            tpl = templates.get(parts[2].strip().lower())
            if not tpl:
                log.write(Text.from_markup(f"[red]未知 Prompt 模板: {parts[2]}[/red]"))
                return
            body = tpl.prompt.replace("[", "\\[").replace("]", "\\]")
            log.write(Text.from_markup(f"\n[bold]{tpl.name}[/bold] — {tpl.description}\n\n[dim]{body}[/dim]"))
            return
        name = parts[1].strip().lower()
        user_input = parts[2] if len(parts) > 2 else ""
        self._execute_prompt_template(name, user_input)

    def _execute_prompt_template(self, name: str, user_input: str = "") -> None:
        from cli.prompt_templates import load_prompt_templates, render_prompt_template

        log = self.query_one("#chat-log", ChatLog)
        templates = load_prompt_templates()
        template = templates.get(name)
        if not template:
            log.write(Text.from_markup(f"[red]未知 Prompt 模板: {name}[/red]"))
            return
        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请先输入 /model add[/yellow]"))
            return
        prompt = render_prompt_template(template, user_input)
        self._send_message(prompt)

    def action_run_template(self, name: str) -> None:
        """命令面板调用 Prompt 模板入口。"""
        self._execute_prompt_template(name)

    # ----- /config 交互 -----

    _CONFIG_KEYS = {
        "tushare_token": ("Tushare Token", "TUSHARE_TOKEN", "", "secret"),
        "tickflow_api_key": (
            "TickFlow API Key",
            "TICKFLOW_API_KEY",
            "购买: https://tickflow.org/auth/register?ref=5N4NKTCPL4",
            "secret",
        ),
        "stream_chunk_timeout_seconds": (
            "模型空闲/首token超时(秒)",
            "",
            "默认 120；相邻流式包间隔超过该值会中断（含首 token）",
            "timeout",
        ),
        "tool_timeout_seconds": (
            "工具执行超时(秒)",
            "",
            "默认 60；单个工具墙钟超时（搜索/行情慢请求）",
            "timeout",
        ),
    }

    def _show_config(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import get_stream_chunk_timeout_seconds, get_tool_timeout_seconds, load_config

        cfg = load_config()
        log.write(Text.from_markup("\n[bold]数据源与超时配置[/bold]"))
        for key, (label, _, hint, kind) in self._CONFIG_KEYS.items():
            if kind == "timeout":
                current = (
                    get_stream_chunk_timeout_seconds(cfg)
                    if key == "stream_chunk_timeout_seconds"
                    else get_tool_timeout_seconds(cfg)
                )
                stored = "已自定义" if key in cfg else "默认"
                log.write(Text.from_markup(f"  {label}: [green]{int(current)}[/green] [dim]({stored})[/dim] — {hint}"))
                continue
            val = str(cfg.get(key, "") or "").strip()
            if val:
                masked = val[:4] + "****" + val[-4:] if len(val) > 8 else "****"
                log.write(Text.from_markup(f"  {label}: [green]{masked}[/green]"))
            else:
                log.write(Text.from_markup(f"  {label}: [dim]未配置[/dim] — {hint}"))
        log.write(
            Text.from_markup(
                "\n[dim]使用 /config set <key>；超时 key: stream_chunk_timeout_seconds / tool_timeout_seconds[/dim]"
            )
        )

    def _start_config_set(self, key: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        key = key.strip().lower()
        if key not in self._CONFIG_KEYS:
            log.write(Text.from_markup(f"[red]不支持的配置项: {key}[/red]，可选: {', '.join(self._CONFIG_KEYS)}"))
            return
        label, _, hint, kind = self._CONFIG_KEYS[key]
        log.write(Text.from_markup(f"\n[bold]配置 {label}[/bold]"))
        log.write(Text.from_markup(f"  {hint}"))
        log.write(Text.from_markup("  输入值（留空取消）："))
        inp.placeholder = f"{label}..."
        inp.password = kind == "secret"
        self._input_mode = _InputState.CONFIG_KEY
        self._input_buf = {"config_key": key}

    # ----- /login 交互 -----

    def _start_login(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        log.write(Text.from_markup("\n[bold]登录[/bold]"))
        log.write(Text.from_markup("  输入邮箱（留空取消）："))
        inp.placeholder = "邮箱..."
        self._input_mode = _InputState.LOGIN_EMAIL
        self._input_buf = {}

    def _do_logout(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        if self._tools:
            try:
                from cli.auth import logout

                logout()
            except Exception:
                logger.warning("logout failed", exc_info=True)
            self._tools.state.update({"user_id": "", "email": "", "access_token": "", "refresh_token": ""})
        log.write(Text.from_markup("[green]已退出登录[/green]"))
        self._update_status()

    # ----- /model 交互 -----

    def _list_models(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import load_default_model_id, load_model_configs
        from cli.model_registry import format_model_metadata, infer_model_info

        configs = load_model_configs()
        default_id = load_default_model_id()
        if not configs:
            log.write(Text.from_markup("[dim]尚无模型配置，使用 /model add 添加[/dim]"))
            return
        log.write(Text.from_markup("\n[bold]已配置模型[/bold] [dim](↑↓选择 Enter确认 Esc取消)[/dim]"))
        for c in configs:
            mark = " [green]⭐ 默认[/green]" if c["id"] == default_id else ""
            metadata = format_model_metadata(infer_model_info(c))
            log.write(
                Text.from_markup(
                    f"  [bold]{c['id']}[/bold] — {c['provider_name']}/{c.get('model', '?')} [dim]{metadata}[/dim]{mark}"
                )
            )
        self._switch_model_selector()

    def _remove_model(self, model_id: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import remove_model_entry

        if remove_model_entry(model_id):
            log.write(Text.from_markup(f"  [green]✓ 已删除 {model_id}[/green]"))
            self._rebuild_provider()
        else:
            log.write(Text.from_markup("  [red]无法删除（至少保留一个模型）[/red]"))

    def _set_default_model(self, model_id: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import load_model_configs, set_default_model

        configs = load_model_configs()
        if not any(c["id"] == model_id for c in configs):
            log.write(Text.from_markup(f"  [red]未找到: {model_id}[/red]"))
            return
        set_default_model(model_id)
        log.write(Text.from_markup(f"  [green]✓ 默认模型已设为 {model_id}[/green]"))
        self._rebuild_provider()

    def _rebuild_provider(self) -> None:
        from cli.auth import load_default_model_id, load_fallback_model_id, load_model_configs

        configs = load_model_configs()
        default_id = load_default_model_id()
        if not configs:
            self._provider = None
            return
        default_cfg = next((c for c in configs if c["id"] == default_id), configs[0])
        if len(configs) == 1:
            from cli.provider_factory import create_provider, provider_config_kwargs

            provider, err = create_provider(**provider_config_kwargs(default_cfg))
            if not err:
                self._provider = provider
        else:
            from cli.providers.fallback import FallbackProvider

            self._provider = FallbackProvider(configs, default_id, fallback_id=load_fallback_model_id())
        self._state.update(default_cfg)
        if self._tools and self._provider:
            self._tools.set_provider(self._provider)
        self._update_status()

    def _show_selector(self, options: list[tuple[str, str]], callback_id: str) -> None:
        """显示模态选择器。options: [(value, label), ...]"""
        self.push_screen(SelectorScreen(options, callback_id))

    def _dismiss_selector(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def _on_selector_choice(self, callback_id: str, value: str | None) -> None:
        """选择器回调。"""
        self._dismiss_selector()
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)

        if value is None:
            log.write(Text.from_markup("[dim]已取消[/dim]"))
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            return

        if callback_id == "model_switch":
            self._set_default_model(value)

        elif callback_id == "session_resume":
            self._resume_session(value)

        elif callback_id == "model_provider":
            self._input_buf["provider"] = value
            log.write(Text.from_markup(f"  供应商: {value}"))
            log.write(
                Text.from_markup(
                    "  输入 API Key（购买: [link=https://www.1route.dev/register?aff=359904261]1route.dev[/link]）："
                )
            )
            inp.placeholder = "API Key..."
            inp.password = True
            self._input_mode = _InputState.MODEL_KEY

    def _switch_model_selector(self) -> None:
        """弹出浮层选择器切换当前模型。"""
        from cli.auth import load_default_model_id, load_model_configs
        from cli.model_registry import format_token_window, infer_model_info

        configs = load_model_configs()
        if not configs:
            log = self.query_one("#chat-log", ChatLog)
            log.write(Text.from_markup("[dim]尚无模型配置，使用 /model add 添加[/dim]"))
            return
        default_id = load_default_model_id()
        options = []
        for c in configs:
            mark = " ⭐" if c["id"] == default_id else ""
            info = infer_model_info(c)
            label = f"{c['id']} ({c.get('model', '?')} · ctx {format_token_window(info.context_window)}){mark}"
            options.append((c["id"], label))
        self._show_selector(options, "model_switch")

    def _start_model_add(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        log.write(Text.from_markup("\n[bold]添加模型[/bold]\n  输入别名（如 gemini, longcat, deepseek）："))
        inp.placeholder = "模型别名..."
        self._input_mode = _InputState.MODEL_ID
        self._input_buf = {}

    # ----- 交互式输入状态机 -----

    def _handle_interactive_input(self, text: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        mode = self._input_mode

        if not text and mode not in (_InputState.MODEL_NAME, _InputState.MODEL_URL, _InputState.MODEL_ID):
            self._cancel_interactive_input(log, inp)
            return

        if mode == _InputState.CONFIG_KEY:
            self._handle_config_value(text, log, inp)
        elif mode == _InputState.LOGIN_EMAIL:
            self._handle_login_email_input(text, log, inp)
        elif mode == _InputState.LOGIN_PASSWORD:
            self._handle_login_password_input(text, log, inp)
        elif mode == _InputState.MODEL_ID:
            self._handle_model_id_input(text, log, inp)
        elif mode == _InputState.MODEL_PROVIDER:
            self._handle_model_provider_input(text, log, inp)
        elif mode == _InputState.MODEL_KEY:
            self._handle_model_key_input(text, log, inp)
        elif mode == _InputState.MODEL_NAME:
            self._handle_model_name_input(text, log, inp)
        elif mode == _InputState.MODEL_URL:
            self._handle_model_url_input(text, inp)
        elif mode in (_InputState.SCHED_ID, _InputState.SCHED_NAME, _InputState.SCHED_CRON, _InputState.SCHED_ACTION):
            self._handle_sched_input(mode, text, log, inp)

    def _reset_input_prompt(self, inp: Input) -> None:
        self._input_mode = _InputState.NONE
        inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"

    def _cancel_interactive_input(self, log, inp: Input) -> None:
        log.write(Text.from_markup("[dim]已取消[/dim]"))
        self._reset_input_prompt(inp)

    def _handle_config_value(self, text: str, log, inp: Input) -> None:
        inp.password = False
        key = self._input_buf["config_key"]
        label, env_key, _, kind = self._CONFIG_KEYS[key]
        from cli.auth import save_config_key

        try:
            save_config_key(key, text)
        except ValueError as exc:
            log.write(Text.from_markup(f"  [red]✗ {exc}[/red]"))
            self._reset_input_prompt(inp)
            return
        if kind == "secret" and env_key:
            import os

            os.environ[env_key] = text
        log.write(Text.from_markup(f"  [green]✓ {label} 已保存[/green]"))
        self._reset_input_prompt(inp)

    def _handle_login_email_input(self, text: str, log, inp: Input) -> None:
        self._input_buf["email"] = text
        log.write(Text.from_markup(f"  邮箱: {text}"))
        log.write(Text.from_markup("  输入密码："))
        inp.placeholder = "密码..."
        inp.password = True
        self._input_mode = _InputState.LOGIN_PASSWORD

    def _handle_login_password_input(self, text: str, log, inp: Input) -> None:
        inp.password = False
        log.write(Text.from_markup("  密码: ****"))
        self._reset_input_prompt(inp)
        try:
            from cli.auth import login

            session = login(self._input_buf["email"], text)
            self._tools.state.update(
                {
                    "user_id": session["user_id"],
                    "email": session["email"],
                    "access_token": session.get("access_token", ""),
                    "refresh_token": session.get("refresh_token", ""),
                }
            )
            log.write(Text.from_markup(f"  [green]✓ 登录成功 ({session['email']})[/green]"))
            self._update_status()
        except Exception as e:
            err = str(e)
            if "Invalid login" in err or "invalid" in err.lower():
                log.write(Text.from_markup("  [red]邮箱或密码错误，请重新输入[/red]"))
            else:
                log.write(Text.from_markup(f"  [red]登录失败: {err}，请重新输入[/red]"))
            self._start_login()

    def _handle_model_id_input(self, text: str, log, inp: Input) -> None:
        model_id = text.strip().lower() if text.strip() else ""
        if not model_id:
            self._cancel_interactive_input(log, inp)
            return
        self._input_buf["id"] = model_id
        log.write(Text.from_markup(f"  别名: {model_id}"))
        log.write(Text.from_markup("  选择供应商（↑↓ 选择，Enter 确认，Esc 取消）："))
        self._input_mode = _InputState.MODEL_PROVIDER
        self._show_selector(_MODEL_PROVIDER_OPTIONS, "model_provider")

    def _handle_model_provider_input(self, text: str, log, inp: Input) -> None:
        prov = text.strip().lower()
        if prov not in _DEFAULT_MODEL_BY_PROVIDER:
            log.write(Text.from_markup(f"  [red]不支持: {prov}[/red]"))
            self._reset_input_prompt(inp)
            return
        self._input_buf["provider"] = prov
        log.write(Text.from_markup(f"  供应商: {prov}"))
        log.write(
            Text.from_markup(
                "  输入 API Key（购买: [link=https://www.1route.dev/register?aff=359904261]1route.dev[/link]）："
            )
        )
        inp.placeholder = "API Key..."
        inp.password = True
        self._input_mode = _InputState.MODEL_KEY

    def _handle_model_key_input(self, text: str, log, inp: Input) -> None:
        inp.password = False
        self._input_buf["api_key"] = text
        log.write(Text.from_markup("  API Key: ****"))
        default = _DEFAULT_MODEL_BY_PROVIDER.get(self._input_buf["provider"], "")
        log.write(Text.from_markup(f"  输入模型名（留空使用 {default}）："))
        inp.placeholder = f"模型名，默认 {default}"
        self._input_mode = _InputState.MODEL_NAME

    def _handle_model_name_input(self, text: str, log, inp: Input) -> None:
        model = text or _DEFAULT_MODEL_BY_PROVIDER.get(self._input_buf["provider"], "")
        self._input_buf["model"] = model
        log.write(Text.from_markup(f"  模型: {model}"))
        log.write(Text.from_markup("  输入 Base URL（留空使用默认）："))
        inp.placeholder = "Base URL（可选）"
        self._input_mode = _InputState.MODEL_URL

    def _handle_model_url_input(self, text: str, inp: Input) -> None:
        self._input_buf["base_url"] = text
        self._reset_input_prompt(inp)
        self._apply_model_config()

    def _apply_model_config(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        buf = self._input_buf
        try:
            entry = {
                "id": buf.get("id", buf["provider"]),
                "provider_name": buf["provider"],
                "api_key": buf["api_key"],
                "model": buf.get("model", ""),
                "base_url": buf.get("base_url", ""),
            }
            from cli.auth import load_model_configs, save_model_entry, set_default_model

            save_model_entry(entry)
            # 首条模型或新添加的设为默认
            if len(load_model_configs()) == 1:
                set_default_model(entry["id"])
            import os

            env_key = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(
                buf["provider"]
            )
            if env_key:
                os.environ[env_key] = buf["api_key"]
            self._rebuild_provider()
            log.write(
                Text.from_markup(
                    f"  [green]✓ 已添加 {entry['id']} ({self._provider.name if self._provider else '?'})[/green]"
                )
            )
        except Exception as e:
            log.write(Text.from_markup(f"  [red]配置失败: {e}[/red]"))

    # ----- 定时调度 -----

    def _handle_schedule_cmd(self, raw: str, log) -> None:
        parts = raw.strip().split()
        sub = parts[1] if len(parts) > 1 else "list"
        if sub == "list":
            self._schedule_list(log)
        elif sub == "status":
            self._schedule_status(log)
        elif sub == "run" and len(parts) > 2:
            self._schedule_run_now(parts[2], log)
        elif sub == "add":
            self._schedule_add_start(log)
        elif sub == "rm" and len(parts) > 2:
            self._schedule_remove(parts[2], log)
        elif sub == "on" and len(parts) > 2:
            self._schedule_toggle(parts[2], True, log)
        elif sub == "off" and len(parts) > 2:
            self._schedule_toggle(parts[2], False, log)
        else:
            log.write(
                Text.from_markup(
                    "[dim]/schedule 用法: list | status | run <id> | add | rm <id> | on <id> | off <id>[/dim]"
                )
            )

    def _schedule_list(self, log) -> None:
        if not self._schedules:
            log.write(Text.from_markup("[dim]暂无定时任务。使用 /schedule add 创建[/dim]"))
            return
        log.write(Text.from_markup("\n[bold]定时任务[/bold]"))
        for s in self._schedules:
            icon = "[green]●[/green]" if s.enabled else "[dim]○[/dim]"
            log.write(Text.from_markup(f"  {icon} [bold]{s.id}[/bold] — {s.name}  [dim]{s.cron}[/dim]  → {s.action}"))

    def _schedule_status(self, log) -> None:
        from cli.scheduler import schedule_status

        rows = schedule_status(self._schedules)
        if not rows:
            log.write(Text.from_markup("[dim]暂无定时任务。使用 /schedule add 创建[/dim]"))
            return
        log.write(Text.from_markup("\n[bold]定时任务状态[/bold]"))
        for row in rows:
            enabled = "[green]运行中[/green]" if row["enabled"] else "[dim]已停用[/dim]"
            next_run = str(row["next_run"] or "-").replace("T", " ")
            last = str(row["last_fired"] or "-")
            status = escape(str(row["last_status"] or "never"))
            log.write(
                Text.from_markup(
                    f"  [bold]{escape(str(row['id']))}[/bold] {enabled} · 下次 {escape(next_run)} · 上次 {escape(last)} · {status}"
                )
            )
            if row["last_error"]:
                log.write(Text.from_markup(f"    [yellow]{escape(str(row['last_error']))}[/yellow]"))

    def _schedule_run_now(self, sched_id: str, log) -> None:
        from cli.scheduler import save_schedules

        schedule = next((item for item in self._schedules if item.id == sched_id), None)
        if schedule is None:
            log.write(Text.from_markup(f"  [red]未找到: {escape(sched_id)}[/red]"))
            return
        schedule.last_fired = datetime.now().strftime("%Y-%m-%dT%H:%M")
        schedule.last_status = "queued" if self._busy else "triggered"
        schedule.last_error = ""
        save_schedules(self._schedules)
        log.write(Text.from_markup(f"  [cyan]▶ 已立即触发 {escape(schedule.name)}[/cyan]"))
        self._fire_schedule(schedule)

    def _show_backend_doctor(self, log) -> None:
        from tools.backend_doctor import diagnose_backend

        log.write(Text.from_markup("\n[bold]模型与数据源健康检查[/bold]"))
        result = diagnose_backend()
        for group in ("llm_providers", "data_sources"):
            for name, item in (result.get(group) or {}).items():
                status = str(item.get("status") or "unknown")
                tone = "green" if status == "healthy" else "yellow" if status == "unconfigured" else "red"
                latency = item.get("latency_ms")
                detail = f" · {latency}ms" if latency is not None else ""
                if error := item.get("error"):
                    detail += f" · {str(error)[:120]}"
                log.write(
                    Text.from_markup(f"  [{tone}]{escape(str(name))}[/{tone}] · {escape(status)}{escape(detail)}")
                )

    def _handle_browser_cmd(self, raw: str, log) -> None:
        from integrations.browser_cdp import (
            browser_cdp_status,
            chrome_cdp_launch_hint,
            chrome_cdp_profile_dir,
        )

        parts = raw.strip().split()
        sub = parts[1] if len(parts) > 1 else "status"
        if sub not in {"status", "hint", "start", "stop"}:
            log.write(Text.from_markup("[dim]/browser 用法: status | start | hint | stop[/dim]"))
            return
        if sub == "hint":
            log.write(Text.from_markup(f"\n[dim]{escape(chrome_cdp_launch_hint())}[/dim]"))
            return
        if sub == "stop":
            log.write(
                Text.from_markup(
                    "\n[dim]请直接关闭专用调试 Chrome 窗口"
                    f"（配置目录: {escape(chrome_cdp_profile_dir())}）。不会结束你的日常 Chrome。[/dim]"
                )
            )
            return
        if sub == "start":
            self._browser_cdp_start_from_ui(log)
            return
        status = browser_cdp_status()
        log.write(Text.from_markup("\n[bold]本机 Chrome CDP[/bold]"))
        log.write(Text.from_markup(f"  端点: {escape(str(status.get('cdp_url') or ''))}"))
        session = "已授权自动拉起" if self._browser_cdp_autostart_allowed else "未授权自动拉起"
        log.write(Text.from_markup(f"  本会话: {escape(session)}"))
        if status.get("ok"):
            log.write(Text.from_markup(f"  [green]已连接[/green] · {escape(str(status.get('browser') or 'Chrome'))}"))
        else:
            log.write(Text.from_markup(f"  [yellow]未连接[/yellow] · {escape(str(status.get('error') or 'unknown'))}"))
            log.write(Text.from_markup("  [dim]可执行 /browser start，或在浏览器搜索时按提示授权[/dim]"))

    def _browser_cdp_start_from_ui(self, log) -> None:
        """主线程入口：不阻塞 event loop，授权后回调里 launch。"""
        from integrations.browser_cdp import browser_cdp_status, ensure_cdp_session

        log.write(Text.from_markup("\n[bold]启动本机 Chrome CDP[/bold]"))
        status = browser_cdp_status()
        if status.get("ok"):
            log.write(Text.from_markup(f"  [green]已连接[/green] · {escape(str(status.get('browser') or 'Chrome'))}"))
            return

        def _report(ensured: dict) -> None:
            if ensured.get("ok"):
                launched = "已拉起并连接" if ensured.get("launched") else "已连接"
                log.write(
                    Text.from_markup(f"  [green]{launched}[/green] · {escape(str(ensured.get('browser') or 'Chrome'))}")
                )
                return
            log.write(Text.from_markup(f"  [yellow]失败[/yellow] · {escape(str(ensured.get('error') or ''))}"))
            if ensured.get("hint"):
                log.write(Text.from_markup(f"  [dim]{escape(str(ensured.get('hint')))}[/dim]"))

        if self._browser_cdp_autostart_allowed:
            _report(ensure_cdp_session(lambda: "allow"))
            return

        def _on_dismiss(choice: dict) -> None:
            action = str((choice or {}).get("action") or "deny")
            if action != "allow":
                log.write(Text.from_markup("  [dim]已取消启动[/dim]"))
                return
            self._browser_cdp_autostart_allowed = True
            _report(ensure_cdp_session(lambda: "allow"))

        self.push_screen(BrowserCdpConsentScreen(), _on_dismiss)

    def _handle_sched_input(self, mode: str, text: str, log, inp) -> None:
        if mode == _InputState.SCHED_ID:
            self._input_buf["id"] = text
            log.write(Text.from_markup("  输入任务名称（如：盘前风控）："))
            inp.placeholder = "任务名称"
            self._input_mode = _InputState.SCHED_NAME
        elif mode == _InputState.SCHED_NAME:
            self._input_buf["name"] = text
            log.write(Text.from_markup("  输入 cron 表达式（如：25 9 * * 1-5）："))
            inp.placeholder = "分 时 日 月 周"
            self._input_mode = _InputState.SCHED_CRON
        elif mode == _InputState.SCHED_CRON:
            self._input_buf["cron"] = text
            log.write(Text.from_markup("  输入触发动作（/skill 或自由文本）："))
            inp.placeholder = "如 /checkup 或 帮我看看大盘"
            self._input_mode = _InputState.SCHED_ACTION
        elif mode == _InputState.SCHED_ACTION:
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            self._finish_schedule_add(text, log)

    def _schedule_add_start(self, log) -> None:
        log.write(Text.from_markup("\n[bold]添加定时任务[/bold]"))
        log.write(Text.from_markup("  输入任务 ID（如：mkt-open）："))
        inp = self.query_one("#chat-input", Input)
        inp.placeholder = "任务 ID"
        self._input_mode = _InputState.SCHED_ID
        self._input_buf = {}

    def _finish_schedule_add(self, action: str, log) -> None:
        from cli.scheduler import Schedule, save_schedules

        s = Schedule(
            id=self._input_buf["id"],
            name=self._input_buf["name"],
            cron=self._input_buf["cron"],
            action=action,
        )
        self._schedules.append(s)
        save_schedules(self._schedules)
        log.write(Text.from_markup(f"  [green]✓ 已添加 {s.id} ({s.cron} → {s.action})[/green]"))

    def _schedule_remove(self, sched_id: str, log) -> None:
        from cli.scheduler import save_schedules

        before = len(self._schedules)
        self._schedules = [s for s in self._schedules if s.id != sched_id]
        if len(self._schedules) < before:
            save_schedules(self._schedules)
            log.write(Text.from_markup(f"  [green]✓ 已删除 {sched_id}[/green]"))
        else:
            log.write(Text.from_markup(f"  [red]未找到: {sched_id}[/red]"))

    def _schedule_toggle(self, sched_id: str, enable: bool, log) -> None:
        from cli.scheduler import save_schedules

        for s in self._schedules:
            if s.id == sched_id:
                s.enabled = enable
                save_schedules(self._schedules)
                log.write(Text.from_markup(f"  [green]✓ {sched_id} 已{'启用' if enable else '禁用'}[/green]"))
                return
        log.write(Text.from_markup(f"  [red]未找到: {sched_id}[/red]"))

    def _find_interrupted_scratchpad(self) -> tuple[str, str] | None:
        try:
            import glob
            import os
            from pathlib import Path

            from cli.scratchpad import wyckoff_home

            scratch_dir = wyckoff_home() / "scratchpad"
            if not scratch_dir.exists():
                return None

            files = glob.glob(str(scratch_dir / "*.jsonl"))
            if not files:
                return None

            files.sort(key=os.path.getmtime, reverse=True)
            latest_file = Path(files[0])

            # 2小时内有效，避免过于陈旧的任务被意外唤醒
            if time.time() - latest_file.stat().st_mtime > 7200:
                return None

            init_entry = None
            has_final = False
            with latest_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "init":
                            init_entry = entry
                        elif entry.get("type") == "final":
                            has_final = True
                    except Exception:
                        pass

            if init_entry and not has_final:
                session_id = init_entry.get("session_id")
                query = init_entry.get("content")
                if session_id and query:
                    return session_id, query
        except Exception:
            pass
        return None

    def _check_auto_resume(self) -> None:
        try:
            from cli.conversation.events import SessionUiEvent

            res = self._find_interrupted_scratchpad()
            if not res:
                return
            session_id, query = res
            self._resume_session(session_id)
            self._conversation.begin_turn(query)
            self._conversation.on_turn_cancelled()
            self._apply_session_ui([SessionUiEvent.interrupted_banner(session_id=session_id, query=query)])
        except Exception:
            logger.debug("auto resume check failed", exc_info=True)

    def _check_schedules(self) -> None:
        from cli.daemon import is_daemon_running
        from cli.scheduler import due_schedules, save_schedules

        # daemon 持锁时它已在跑同一批任务；两边都跑会重复触发并互相覆盖 last_fired。
        if is_daemon_running():
            self._last_schedule_check_at = datetime.now()
            return

        now = datetime.now()
        fired = False
        for sched, minute_key in due_schedules(self._schedules, last_check_at=self._last_schedule_check_at, now=now):
            sched.last_fired = minute_key
            fired = True
            self._fire_schedule(sched)
        self._last_schedule_check_at = now
        if fired:
            save_schedules(self._schedules)

    def _fire_schedule(self, sched) -> None:
        from cli.scheduler import save_schedules

        log = self.query_one("#chat-log", ChatLog)
        log.write(Text.from_markup(f"[bold yellow]⏰ 定时触发：{sched.name}[/bold yellow]"))
        if sched.notify:
            self._desktop_notify(f"Wyckoff: {sched.name}")
        action = sched.action.strip()
        if action.startswith("/"):
            sched.last_status = "triggered"
            self._handle_command(action)
        elif self._busy:
            sched.last_status = "queued"
            self._apply_session_ui(self._conversation.enqueue(QueuedInput(kind="schedule", content=action)))
        else:
            sched.last_status = "triggered"
            self._send_message(action)
        schedules = getattr(self, "_schedules", None)
        if schedules is not None:
            save_schedules(schedules)

    def _desktop_notify(self, message: str) -> None:
        import subprocess
        import sys

        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["osascript", "-e", f'display notification "{message}" with title "Wyckoff 读盘室"'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    ["notify-send", "Wyckoff 读盘室", message],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except FileNotFoundError:
            print("\a", end="", flush=True)

    def _chatlog_save(self, role: str, content: str, **kwargs):
        """保存一条对话记录到 SQLite（静默失败）。"""
        try:
            from integrations.local_db import save_chat_log

            save_chat_log(self._session_id, role, content, **kwargs)
        except Exception:
            logger.debug("chat log save failed", exc_info=True)

    def _prepare_turn_memory_context(self) -> tuple[int, str]:
        if not self._messages:
            return -1, ""
        turn_index = len(self._messages) - 1
        message = self._messages[turn_index]
        user_text = message.get("content", "")
        if _is_system_notification_message(message):
            return turn_index, user_text
        memory_context = message.pop("_memory_context", "")
        if not memory_context:
            return turn_index, user_text
        try:
            from cli.memory import prepend_memory_context

            self._messages[turn_index]["_raw_content"] = user_text
            self._messages[turn_index]["content"] = prepend_memory_context(user_text, memory_context)
        except Exception:
            logger.debug("memory context prepend failed", exc_info=True)
        return turn_index, user_text

    def _restore_turn_user_message(self, turn_index: int) -> None:
        if turn_index < 0 or turn_index >= len(self._messages):
            return
        msg = self._messages[turn_index]
        if msg.get("role") == "user" and msg.get("_raw_content"):
            msg["content"] = msg.pop("_raw_content")

    def _create_scratchpad(self, user_text: str) -> AgentScratchpad | None:
        try:
            return AgentScratchpad(user_text, session_id=self._session_id)
        except Exception:
            return None

    def _create_turn_run_state(self) -> _TurnRunState:
        turn_user_index, user_text = self._prepare_turn_memory_context()
        message = self._messages[turn_user_index] if 0 <= turn_user_index < len(self._messages) else {}
        system_notification = _is_system_notification_message(message)
        return _TurnRunState(
            turn_user_index=turn_user_index,
            user_text=user_text,
            scratchpad=None if system_notification else self._create_scratchpad(user_text),
            model_name=getattr(self._provider, "name", "") if self._provider else "",
            provider_name=self._state.get("provider_name", "") if self._state else "",
            system_notification=system_notification,
        )

    def _drop_current_turn_messages(self) -> None:
        while self._messages and self._messages[-1].get("role") != "user":
            self._messages.pop()
        if self._messages:
            self._messages.pop()

    def _handle_cancelled_agent_turn(
        self,
        stream: _StreamViewState,
        ui: _AgentUiOps,
        *,
        user_text: str = "",
        system_notification: bool = False,
    ) -> None:
        ui.spinner_stop()
        _flush_stream_line(stream, ui.write_stream, ui.scroll)
        ui.write(Text.from_markup("[yellow]⏹ 已中断[/yellow]"))
        ui.scroll()
        if self._conversation.phase == TurnPhase.CANCELLED:
            return
        checkpoint = [dict(item) for item in self._messages]
        events = self._conversation.on_turn_cancelled(messages_checkpoint=checkpoint)
        if system_notification:
            self._conversation.abandon_active_turn()
        else:
            self.call_from_thread(self._apply_session_ui, events)
        self._drop_current_turn_messages()

    def _save_completed_agent_turn(self, state: _TurnRunState, final_text: str, t_start: float, chatlog_save) -> None:
        self._conversation.on_turn_completed()
        total_input = state.final_usage.get("input_tokens", 0)
        total_output = state.final_usage.get("output_tokens", 0)
        self._session_tokens["input"] += total_input
        self._session_tokens["output"] += total_output
        self._session_tokens["rounds"] += 1
        self._session_tokens["cache_read"] += int(state.final_usage.get("cache_read_tokens") or 0)
        self._session_tokens["generation_ms"] += int(state.final_usage.get("generation_ms") or 0)
        self._session_tokens["cache_reported"] = bool(
            self._session_tokens.get("cache_reported") or state.final_usage.get("cache_reported")
        )
        self.call_from_thread(self._update_status)
        self._restore_turn_user_message(state.turn_user_index)

        turn_role = _chatlog_role_for_turn(state.system_notification)
        turn_metadata = {"system_notification": True} if state.system_notification else {}
        chatlog_save(
            turn_role,
            state.user_text,
            model=state.model_name,
            provider=state.provider_name,
            metadata_json=json.dumps(turn_metadata, ensure_ascii=False) if turn_metadata else "",
        )
        tool_calls_json = (
            json.dumps(state.executed_tool_summaries, ensure_ascii=False) if state.executed_tool_summaries else ""
        )
        metadata = {
            "cache_read": state.final_usage.get("cache_read_tokens", state.last_usage.get("cache_read_tokens", 0)),
            "cache_write": state.final_usage.get("cache_write_tokens", state.last_usage.get("cache_write_tokens", 0)),
            "output_tok_per_s": state.final_usage.get("output_tok_per_s"),
            "cache_hit_rate": state.final_usage.get("cache_hit_rate"),
            "generation_ms": state.final_usage.get("generation_ms"),
            "stop_reason": state.last_usage.get("stop_reason", "stop"),
            "rounds": state.final_rounds,
            "rounds_detail": _build_rounds_detail(
                state.final_rounds,
                state.round_usages,
                state.round_tool_names,
                state.round_starts,
                t_start,
                state.model_name,
                state.round_thinking,
            ),
            "messages": list(self._messages),
            "system_prompt": self._system_prompt,
            "tools": self._tools.schemas() if self._tools else [],
            "scratchpad_path": str(state.scratchpad.path) if state.scratchpad else "",
            "workflow": state.workflow_name,
            "workflow_run_id": state.workflow_run_id,
        }
        chatlog_save(
            "assistant",
            final_text,
            model=state.model_name,
            provider=state.provider_name,
            tokens_in=total_input,
            tokens_out=total_output,
            elapsed_s=round(state.final_elapsed, 2),
            tool_calls_json=tool_calls_json,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        self._agent_log.info(
            "session=%s done in=%.1fs tokens=%d/%d",
            self._session_id,
            state.final_elapsed,
            total_input,
            total_output,
        )

    def _save_failed_agent_turn(self, e: Exception, state: _TurnRunState, t_start: float, chatlog_save, write) -> None:
        from cli.conversation import classify_failure

        self._restore_turn_user_message(state.turn_user_index)
        err = _friendly_error(e)
        if state.scratchpad:
            state.scratchpad.record_error(f"{type(e).__name__}: {err}", elapsed_s=time.monotonic() - t_start)
        write(Text.from_markup(f"[red]错误: {err}[/red]"))
        elapsed = time.monotonic() - t_start
        self._agent_log.error(
            "session=%s error after=%.1fs type=%s msg=%s",
            self._session_id,
            elapsed,
            type(e).__name__,
            str(e)[:500],
        )
        failure = classify_failure(e)
        turn_role = _chatlog_role_for_turn(state.system_notification)
        turn_metadata: dict[str, Any] = {"system_notification": True} if state.system_notification else {}
        turn = self._conversation.active_turn
        if turn:
            turn_metadata.update({"turn_id": turn.turn_id, "failure_kind": failure.kind.value})
            for summary in state.executed_tool_summaries:
                name = str(summary.get("name") or "")
                brief = ""
                if lines := summary.get("brief"):
                    brief = "; ".join(str(line) for line in lines[:3]) if isinstance(lines, list) else str(lines)[:200]
                elif err := summary.get("error"):
                    brief = f"error: {err}"[:200]
                if name or brief:
                    turn.tool_result_briefs.append(
                        {
                            "name": name,
                            "brief": brief,
                            "tool_call_id": str(summary.get("tool_call_id") or ""),
                        }
                    )
        chatlog_save(
            turn_role,
            state.user_text,
            model=state.model_name,
            provider=state.provider_name,
            metadata_json=json.dumps(turn_metadata, ensure_ascii=False) if turn_metadata else "",
        )
        chatlog_save(
            "error",
            "",
            model=state.model_name,
            provider=state.provider_name,
            elapsed_s=round(elapsed, 2),
            error=f"{type(e).__name__}: {str(e)[:500]}",
            metadata_json=json.dumps(
                {"turn_id": turn.turn_id if turn else "", "failure_kind": failure.kind.value},
                ensure_ascii=False,
            ),
        )
        checkpoint = [dict(item) for item in self._messages]
        events = self._conversation.on_turn_failed(failure, messages_checkpoint=checkpoint)
        if state.system_notification:
            self._conversation.abandon_active_turn()
        else:
            self.call_from_thread(self._apply_session_ui, events)
        self._drop_current_turn_messages()

    def _submit_workflow_background(
        self,
        runtime: WorkflowExecutor,
        state: _TurnRunState,
        system_prompt: str,
        write,
        scroll,
    ) -> bool:
        event = runtime.prepare_run()
        run_id = str(event.get("run_id", ""))
        task_id = f"wfbg_{run_id or time.time_ns()}_{time.time_ns()}"
        self._restore_turn_user_message(state.turn_user_index)
        self._chatlog_save("user", state.user_text, model=state.model_name, provider=state.provider_name)
        _display_workflow_plan_event(event, write, scroll)
        messages_snapshot = [dict(item) for item in self._messages]
        workflow_label = f"workflow {run_id}" if run_id else "workflow"
        ack = f"{workflow_label} 自动开始后台运行；步骤进度会刷到本会话，也可继续聊天或用 /workflow 查看。"
        self._messages.append({"role": "assistant", "content": ack})
        self._chatlog_save(
            "assistant",
            ack,
            model=state.model_name,
            provider=state.provider_name,
            metadata_json=json.dumps({"workflow_background": True, "workflow_run_id": run_id}, ensure_ascii=False),
        )
        self._launch_workflow_background(
            runtime,
            messages_snapshot,
            system_prompt,
            state.model_name,
            state.provider_name,
            task_id,
            write,
            scroll,
        )
        return True

    def _prepare_workflow_approval(
        self,
        runtime: WorkflowExecutor,
        state: _TurnRunState,
        system_prompt: str,
        write,
        scroll,
    ) -> bool:
        event = runtime.prepare_run()
        run_id = str(event.get("run_id", ""))
        self._restore_turn_user_message(state.turn_user_index)
        self._chatlog_save("user", state.user_text, model=state.model_name, provider=state.provider_name)
        _display_workflow_plan_event(event, write, scroll, launch_state="pending_approval")
        messages_snapshot = [dict(item) for item in self._messages]
        self._pending_workflows[run_id] = _PendingWorkflowLaunch(
            runtime=runtime,
            messages=messages_snapshot,
            system_prompt=system_prompt,
            model_name=state.model_name,
            provider_name=state.provider_name,
        )
        ack = f"workflow {run_id} 等待批准。回复“开始”运行，回复“取消”停止。"
        self._messages.append({"role": "assistant", "content": ack})
        self._chatlog_save(
            "assistant",
            ack,
            model=state.model_name,
            provider=state.provider_name,
            metadata_json=json.dumps(
                {"workflow_pending_approval": True, "workflow_run_id": run_id}, ensure_ascii=False
            ),
        )
        write(
            Text.from_markup(
                "  [yellow]等待批准[/yellow] [dim]回复“开始”运行 · 回复“取消”停止 · 也可以直接补充修改意见[/dim]"
            )
        )
        scroll()
        return True

    def _launch_workflow_background(
        self,
        runtime: WorkflowExecutor,
        messages_snapshot: list[dict[str, Any]],
        system_prompt: str,
        model_name: str,
        provider_name: str,
        task_id: str,
        write,
        scroll,
    ) -> None:
        run_id = runtime.run.run_id if runtime.run else ""
        if run_id:
            from cli.workflows.control import register_workflow_control

            runtime.set_control(register_workflow_control(run_id))

        def on_ui_event(event: dict[str, Any]) -> None:
            self.call_from_thread(self._display_workflow_background_event, event)

        self._bg_manager.submit(
            task_id,
            "dynamic_workflow",
            _run_workflow_background,
            {
                "runtime": runtime,
                "messages": messages_snapshot,
                "system_prompt": system_prompt,
                "model_name": model_name,
                "provider_name": provider_name,
                "on_ui_event": on_ui_event,
            },
            on_complete=self._on_bg_complete,
        )
        write(Text.from_markup(f"  [cyan]↗ dynamic workflow[/cyan] [dim]后台运行中，任务 {task_id}[/dim]"))
        scroll()

    def _display_workflow_background_event(self, event: dict[str, Any]) -> None:
        """从后台线程经 call_from_thread 刷阶段/步骤进度到聊天区。"""
        log = self.query_one("#chat-log", ChatLog)

        def write(renderable) -> None:
            log.write(renderable)

        def scroll() -> None:
            with contextlib.suppress(Exception):
                log.scroll_end(animate=False)

        event_type = str(event.get("type", ""))
        if event_type == "workflow_plan_update":
            _display_workflow_plan_event(event, write, scroll, launch_state="adapted")
        elif event_type in {"workflow_phase_start", "workflow_phase_done"}:
            _display_workflow_phase_event(event, write, scroll)
        elif event_type == "workflow_disagreement_summary":
            _display_workflow_disagreement_event(event, write, scroll)
        elif event_type in {"workflow_step_start", "workflow_step_done"}:
            _display_workflow_step_event(event, write, scroll)

    def _complete_workflow_background(self, task_id: str, result: dict[str, Any]) -> None:
        from cli.workflows.control import unregister_workflow_control

        log = self.query_one("#chat-log", ChatLog)
        is_error = bool(result.get("error"))
        notify_followup = self._busy or bool(self._conversation.input_queue)
        run_id = str(result.get("workflow_run_id", ""))
        if run_id:
            unregister_workflow_control(run_id)
        if is_error:
            log.write(
                Text.from_markup(
                    f"  [red]✗ workflow 后台失败[/red] [dim]{escape(str(result.get('error', ''))[:100])}[/dim]"
                )
            )
            self._queue_workflow_background_notification(task_id, result, "failed", notify_followup)
            return
        final_text = str(result.get("final_text", "") or "workflow 已完成，但没有生成最终文本。")
        log.write(Text.from_markup(f"  [green]✓ workflow 后台完成[/green] [dim]{escape(run_id or task_id)}[/dim]"))
        log.write(Markdown(final_text))
        self._messages.append({"role": "assistant", "content": final_text})
        usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}
        tokens_in = int(usage.get("input_tokens", 0) or 0)
        tokens_out = int(usage.get("output_tokens", 0) or 0)
        gen_ms = int(usage.get("generation_ms") or 0)
        self._session_tokens["input"] += tokens_in
        self._session_tokens["output"] += tokens_out
        self._session_tokens["rounds"] += 1
        self._session_tokens["cache_read"] += int(usage.get("cache_read_tokens") or 0)
        if gen_ms > 0:
            self._session_tokens["generation_ms"] += gen_ms
        else:
            # 合成轮常无 generation_ms；计入 unrated，避免会话均速虚高。
            self._session_tokens["unrated_output"] = int(self._session_tokens.get("unrated_output") or 0) + tokens_out
        self._session_tokens["cache_reported"] = bool(
            self._session_tokens.get("cache_reported") or usage.get("cache_reported")
        )
        self._update_status()
        events = result.get("events", [])
        self._chatlog_save(
            "assistant",
            final_text,
            model=str(result.get("model_name", "")),
            provider=str(result.get("provider_name", "")),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            elapsed_s=float(result.get("elapsed", 0.0) or 0.0),
            tool_calls_json=_workflow_spans_json(events),
            metadata_json=json.dumps(
                {
                    "workflow": result.get("workflow", ""),
                    "workflow_run_id": run_id,
                    "background_task_id": task_id,
                    "events": events,
                    "messages": list(self._messages),
                    "system_prompt": getattr(self, "_system_prompt", ""),
                    "tools": _tool_schemas_or_empty(getattr(self, "_tools", None)),
                },
                ensure_ascii=False,
            ),
        )
        self._queue_workflow_background_notification(task_id, result, "completed", notify_followup)

    def _queue_workflow_background_notification(
        self, task_id: str, result: dict[str, Any], status: str, notify_followup: bool
    ) -> None:
        if not notify_followup:
            return
        if status == "completed":
            # 成功时不再追加一轮：final_text 已经显示、已经落库、也已经进了 _messages，
            # 模型下一轮本来就看得到。再叫它就同一份结果说一次，只会得到一段和上一条重复的话。
            return
        summary = _background_task_summary("dynamic_workflow", task_id, result)
        item = _system_notification_queue_item(_workflow_background_notification(task_id, result, status, summary))
        events = self._conversation.enqueue(
            QueuedInput(kind="system_notification", content=str(item.get("content", "")))
        )
        if not self._busy:
            if next_item := self._conversation.dequeue_next():
                self._dispatch_queued_item(next_item)
                return
        self._apply_session_ui(events)

    def _handle_agent_event(
        self,
        event: dict[str, Any],
        state: _TurnRunState,
        stream: _StreamViewState,
        ui: _AgentUiOps,
        t_start: float,
        chatlog_save,
    ) -> bool:
        event_type = event.get("type")
        round_number = int(event.get("round") or 0)
        if round_number > 0:
            state.round_starts.setdefault(round_number, time.monotonic())
        if self._handle_workflow_agent_event(event_type, event, state, ui):
            return False
        if event_type == "compaction":
            ui.write(_compaction_panel(event))
            ui.scroll()
        elif event_type == "context_overflow":
            ui.write(_context_overflow_panel(event))
            ui.scroll()
        elif event_type == "text_delta":
            _append_stream_text(stream, event["text"], ui.write_stream, ui.scroll, ui.spinner_stop)
        elif event_type == "tool_calls":
            _flush_and_clear_stream(self, ui, stream)
            names = [call["name"] for call in event.get("tool_calls", [])]
            if round_number:
                state.round_tool_names.setdefault(round_number, []).extend(names)
        elif event_type == "usage":
            self._handle_usage_agent_event(event, state, round_number, ui)
        elif event_type == "thinking":
            self._handle_thinking_agent_event(event, state, ui)
        elif event_type == "model_start":
            ui.spinner_start("思考中")
        elif event_type == "stage_start":
            ui.spinner_start(event.get("message", "执行中"))
        elif event_type == "stage_done":
            ui.spinner_stop()
        elif event_type == "tool_start":
            _flush_and_clear_stream(self, ui, stream)
            display = self._tools.display_name(event["name"]) if self._tools else event["name"]
            ui.spinner_start(display)
        elif event_type in {"tool_result", "tool_error"}:
            ui.spinner_stop()
            state.executed_tool_summaries.append(_display_tool_result_event(event, self._tools, ui.write, ui.scroll))
        elif event_type in {"retry", "continuation", "steered"}:
            self._handle_loop_control_event(event_type, event, stream, ui)
        elif event_type == "done":
            return self._finish_agent_event(event, state, stream, ui, t_start, chatlog_save)
        return False

    def _handle_workflow_agent_event(self, event_type, event, state, ui) -> bool:
        if event_type == "workflow_plan":
            state.workflow_run_id, state.workflow_name = _display_workflow_plan_event(event, ui.write, ui.scroll)
            return True
        if event_type == "workflow_plan_update":
            state.workflow_run_id, state.workflow_name = _display_workflow_plan_event(
                event, ui.write, ui.scroll, launch_state="adapted"
            )
            return True
        if event_type in {"workflow_phase_start", "workflow_phase_done"}:
            _display_workflow_phase_event(event, ui.write, ui.scroll)
            return True
        if event_type == "workflow_disagreement_summary":
            _display_workflow_disagreement_event(event, ui.write, ui.scroll)
            return True
        if event_type in {"workflow_step_start", "workflow_step_done"}:
            _display_workflow_step_event(event, ui.write, ui.scroll)
            return True
        return event_type in {"workflow_done", "thinking_delta"}

    def _handle_usage_agent_event(self, event, state, round_number: int, ui) -> None:
        usage = event.get("usage", {})
        if round_number:
            state.round_usages[round_number] = usage
        state.last_usage = usage
        fb_msg = getattr(self._provider, "last_fallback_msg", None)
        if fb_msg:
            ui.write(Text.from_markup(f"  [yellow]⚡ {fb_msg}[/yellow]"))
            self._provider.last_fallback_msg = None

    def _handle_thinking_agent_event(self, event, state, ui) -> None:
        ui.spinner_stop()
        thinking_text = event.get("text", "")
        if round_number := event.get("round"):
            state.round_thinking[int(round_number)] = thinking_text
        if preview := _build_thinking_preview(thinking_text):
            ui.write(preview)

    def _handle_loop_control_event(self, event_type, event, stream, ui) -> None:
        if event_type == "retry":
            # retry 会改写未完成回合，清掉半截流式输出避免和下一跳叠在一起。
            _flush_and_clear_stream(self, ui, stream)
            self._agent_log.info(
                "session=%s loop_guard retry=%d required_tool=%s",
                self._session_id,
                event.get("retry", 0),
                event.get("required_tool", ""),
            )
            _display_retry_event(event, ui.write, ui.scroll)
            ui.spinner_start()
            return
        # continuation / steered：已流出的正文要留在屏幕上，只冲掉行缓冲。
        _flush_stream_line(stream, ui.write_stream, ui.scroll)
        if event_type == "continuation":
            _display_continuation_event(event, ui.write, ui.scroll)
            ui.spinner_start("续跑中")
            return
        _display_steered_event(event, ui.write, ui.scroll)
        ui.spinner_start("按转向继续")

    def _finish_agent_event(self, event, state, stream, ui, t_start, chatlog_save) -> bool:
        ui.spinner_stop()
        _flush_stream_line(stream, ui.write_stream, ui.scroll)
        final_text = event.get("text", "")
        state.final_usage = event.get("usage", state.final_usage)
        state.final_elapsed = float(event.get("elapsed", time.monotonic() - t_start))
        state.final_rounds = int(event.get("rounds", 0))
        _display_stream_final(self, ui.log, stream, final_text, ui.write, ui.scroll)
        ui.write(
            _usage_footer(
                state.final_usage.get("input_tokens", 0),
                state.final_usage.get("output_tokens", 0),
                state.final_elapsed,
                output_tok_per_s=state.final_usage.get("output_tok_per_s"),
                cache_hit_rate=state.final_usage.get("cache_hit_rate"),
                cache_reported=bool(state.final_usage.get("cache_reported")),
            )
        )
        ui.scroll()
        self._save_completed_agent_turn(state, final_text, t_start, chatlog_save)
        return True

    # ----- Agent 执行（后台 Worker）-----

    @work(thread=True, exclusive=True)
    def _run_agent(self) -> None:
        # cancel_event 在 begin_turn 时清；此处勿 clear，否则会丢掉 SUBMITTED 阶段的 Ctrl+C。
        cancelled_before_start = self._cancel_event.is_set() or self._conversation.phase == TurnPhase.CANCELLING
        if not cancelled_before_start:
            self._conversation.mark_running()
        log = self.query_one("#chat-log", ChatLog)

        def _write(renderable):
            self.call_from_thread(log.write, renderable)

        def _write_stream(renderable) -> int:
            return self.call_from_thread(_write_counted, log, renderable)

        def _scroll():
            self.call_from_thread(log.scroll_end, animate=False)

        def _spinner_start(label="思考中"):
            self.call_from_thread(self._start_spinner, label)

        def _spinner_stop():
            self.call_from_thread(self._stop_spinner)

        t_start = time.monotonic()
        state = self._create_turn_run_state()
        self._agent_log.info("session=%s user: %s", self._session_id, state.user_text[:200])
        stream = _StreamViewState()
        ui = _AgentUiOps(log, _write, _write_stream, _scroll, _spinner_start, _spinner_stop)

        try:
            if cancelled_before_start:
                self._handle_cancelled_agent_turn(
                    stream,
                    ui,
                    user_text=state.user_text,
                    system_notification=state.system_notification,
                )
                return
            self._run_agent_body(state, stream, ui, t_start)
        except AgentCancelled:
            self._handle_cancelled_agent_turn(
                stream,
                ui,
                user_text=state.user_text,
                system_notification=state.system_notification,
            )
        except Exception as e:
            _spinner_stop()
            self._save_failed_agent_turn(e, state, t_start, self._chatlog_save, _write)
        finally:
            self._finish_agent_turn()

    def _run_agent_body(self, state, stream, ui, t_start) -> None:
        if not self._provider or not self._tools:
            raise RuntimeError("模型或工具未初始化")

        self._tools._tool_context.on_progress = _make_sub_agent_progress_handler(
            self._tools,
            ui.write,
            ui.scroll,
            ui.spinner_start,
            ui.spinner_stop,
        )
        self._tools._tool_context.cancel_check = self._cancel_event.is_set

        workflow_override = self._workflow_override
        self._workflow_override = None
        workflow_context = WORKFLOWS["general_chat"] if state.system_notification else None
        runtime, workflow_context = build_turn_runtime(
            self._provider,
            self._tools,
            session_id=self._session_id,
            user_text=state.user_text,
            scratchpad=state.scratchpad,
            cancel_check=self._cancel_event.is_set,
            workflow_context=workflow_context or (workflow_override.context if workflow_override else None),
            workflow_script=workflow_override.script if workflow_override else None,
            workflow_source_run_id=workflow_override.source_run_id if workflow_override else "",
            workflow_args=workflow_override.args if workflow_override else None,
            workflow_only_step_id=workflow_override.only_step_id if workflow_override else "",
            routing_messages=self._messages,
            steer_drain=self._conversation.drain_steering,
        )
        state.workflow_name = "" if workflow_context.is_general else workflow_context.name
        system_prompt = with_current_time(self._system_prompt)
        if isinstance(runtime, WorkflowExecutor):
            ui.spinner_stop()
            self._conversation.on_turn_completed()
            if workflow_override:
                self._prepare_workflow_approval(runtime, state, system_prompt, ui.write, ui.scroll)
            else:
                self._submit_workflow_background(runtime, state, system_prompt, ui.write, ui.scroll)
            return
        self._consume_agent_event_stream(runtime, state, stream, ui, t_start, self._chatlog_save)

    def _consume_agent_event_stream(self, runtime, state, stream, ui, t_start, chatlog_save) -> None:
        resume_from = self._pending_resume_checkpoint
        self._pending_resume_checkpoint = None
        for event in runtime.run_stream(
            self._messages,
            with_current_time(self._system_prompt),
            resume_from=resume_from,
        ):
            etype = event.get("type")
            if etype not in {"turn_cancelled", "turn_failed", "done"}:
                self._conversation.on_runtime_event(event)
            if etype == "turn_cancelled":
                self._handle_cancelled_agent_turn(
                    stream,
                    ui,
                    user_text=state.user_text,
                    system_notification=state.system_notification,
                )
                return
            if etype == "turn_failed":
                continue
            if self._cancel_event.is_set():
                self._handle_cancelled_agent_turn(
                    stream,
                    ui,
                    user_text=state.user_text,
                    system_notification=state.system_notification,
                )
                return
            if self._handle_agent_event(event, state, stream, ui, t_start, chatlog_save):
                return

    def _finish_agent_turn(self) -> None:
        if self._conversation.phase in {
            TurnPhase.SUBMITTED,
            TurnPhase.RUNNING,
            TurnPhase.STREAMING,
            TurnPhase.TOOL_RUNNING,
            TurnPhase.AWAITING_USER,
            TurnPhase.CANCELLING,
        }:
            # Workflow/approval paths clear via on_turn_completed; leftover running → idle.
            if self._conversation.phase != TurnPhase.CANCELLING:
                self._conversation.abandon_active_turn()
        try:
            self.call_from_thread(self._stop_spinner)
        except Exception:
            logger.debug("stop spinner after agent turn failed", exc_info=True)
        if self._tools:
            self._tools._tool_context.on_progress = None
        if next_msg := self._conversation.dequeue_next():
            self.call_from_thread(self._dispatch_queued_item, next_msg)

    # ----- 后台任务回调 -----

    def _on_bg_progress(self, _task) -> None:
        """后台线程报进度 → 刷新状态栏。"""
        try:
            self.call_from_thread(self._update_status)
        except Exception:
            logger.debug("background status refresh failed", exc_info=True)

    def _on_bg_complete(self, task_id: str, tool_name: str, result) -> None:
        """后台任务完成，注入结果到消息队列。"""
        from cli.tools import TOOL_DISPLAY_NAMES

        if tool_name == "dynamic_workflow":
            try:
                from integrations.local_db import save_background_task_result

                save_background_task_result(
                    task_id,
                    tool_name,
                    result,
                    session_id=self._session_id,
                    status="failed" if isinstance(result, dict) and result.get("error") else "completed",
                )
            except Exception:
                logger.debug("save background workflow result failed", exc_info=True)
            self.call_from_thread(
                self._complete_workflow_background, task_id, result if isinstance(result, dict) else {}
            )
            return

        display = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
        is_error = isinstance(result, dict) and result.get("error")
        if self._tools is not None and not is_error:
            try:
                self._tools.remember_tool_handoff(tool_name, result)
            except Exception:
                logger.debug("restore background handoff failed", exc_info=True)

        try:
            from integrations.local_db import save_background_task_result

            save_background_task_result(
                task_id,
                tool_name,
                result,
                session_id=self._session_id,
                status="failed" if is_error else "completed",
            )
        except Exception:
            logger.debug("save background task result failed", exc_info=True)

        log = self.query_one("#chat-log", ChatLog)
        if is_error:
            self.call_from_thread(
                log.write,
                Text.from_markup(f"  [red]✗ 后台任务失败：{display}[/red] [dim]{str(result['error'])[:80]}[/dim]"),
            )
        else:
            self.call_from_thread(
                log.write,
                Text.from_markup(f"  [green]✅ 后台任务完成：{display}[/green]"),
            )

        summary_str = _background_task_summary(tool_name, task_id, result)

        notification = (
            "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
            "This is an automated background-task event, NOT a message from the user.\n"
            "Do NOT interpret this as user acknowledgement, confirmation, or response to any pending question.\n\n"
            "<system-reminder>\n"
            "<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            f"<tool-name>{tool_name}</tool-name>\n"
            f"<status>{'failed' if is_error else 'completed'}</status>\n"
            f"<summary>{summary_str}</summary>\n"
            "</task-notification>\n"
            "</system-reminder>"
        )
        self._conversation.enqueue(QueuedInput(kind="system_notification", content=notification))
        if not self._busy:
            if next_item := self._conversation.dequeue_next():
                self.call_from_thread(self._dispatch_queued_item, next_item)
                return
        self.call_from_thread(self._refresh_followups_panel)

    # ----- Actions -----

    def action_clear_chat(self) -> None:
        self.query_one("#chat-log", ChatLog).clear()

    def action_resume_session(self) -> None:
        self._resume_session_selector()

    def action_export_session(self) -> None:
        from cli.session_tools import SessionToolError, export_session_transcript

        log = self.query_one("#chat-log", ChatLog)
        try:
            result = export_session_transcript(session_id=self._session_id)
        except SessionToolError as exc:
            log.write(Text.from_markup(f"[red]导出失败: {exc}[/red]"))
            return
        log.write(Text.from_markup(f"[green]✓ 会话已导出[/green] [dim]{result.path}[/dim]"))

    def action_fork_session(self) -> None:
        from cli.session_tools import SessionToolError, fork_session

        log = self.query_one("#chat-log", ChatLog)
        self._save_memory_async()
        try:
            result = fork_session(session_id=self._session_id)
        except SessionToolError as exc:
            log.write(Text.from_markup(f"[red]分叉失败: {exc}[/red]"))
            return
        log.write(
            Text.from_markup(
                f"[green]✓ 会话已分叉[/green] [dim]{result.source_session_id} → {result.new_session_id}[/dim]"
            )
        )
        self._resume_session(result.new_session_id)

    def _resume_session_selector(self) -> None:
        """弹出选择器，选择要恢复的历史会话。"""
        from integrations.local_db import get_session_preview, list_chat_sessions

        log = self.query_one("#chat-log", ChatLog)
        sessions = list_chat_sessions(limit=20)
        sessions = [s for s in sessions if s["session_id"] != self._session_id]
        if not sessions:
            log.write(Text.from_markup("[dim]没有可恢复的历史会话[/dim]"))
            return
        options = []
        for s in sessions:
            preview = get_session_preview(s["session_id"])
            started = (s["started_at"] or "?")[:16]
            n = s["msg_count"]
            label = f"{started}  ({n}条)  {preview}"
            options.append((s["session_id"], label))
        self._show_selector(options, "session_resume")

    def _resume_session(self, session_id: str) -> None:
        """恢复指定会话，加载历史消息到 self._messages。"""
        from cli.session_context import build_resumed_model_context
        from integrations.local_db import list_chat_sessions, load_chat_logs

        log = self.query_one("#chat-log", ChatLog)

        if session_id.isdigit():
            idx = int(session_id)
            sessions = list_chat_sessions(limit=20)
            sessions = [s for s in sessions if s["session_id"] != self._session_id]
            if idx < 1 or idx > len(sessions):
                log.write(Text.from_markup(f"[red]无效序号: {idx} (共 {len(sessions)} 个历史会话)[/red]"))
                return
            session_id = sessions[idx - 1]["session_id"]

        rows = load_chat_logs(session_id=session_id, limit=1000)
        if not rows:
            log.write(Text.from_markup(f"[red]未找到会话: {session_id}[/red]"))
            return

        resumed = build_resumed_model_context(rows)

        # 保存当前会话记忆
        self._save_memory_async()

        self._messages[:] = resumed.messages
        self._conversation.clear_queue()
        self._conversation.abandon_active_turn()
        self._session_tokens = {
            "input": 0,
            "output": 0,
            "unrated_output": 0,
            "rounds": 0,
            "cache_read": 0,
            "generation_ms": 0,
            "cache_reported": False,
        }
        self._session_id = session_id
        self._refresh_followups_panel()
        self._update_status()
        log.clear()

        log.write(Text.from_markup(f"[green]已恢复会话[/green] [dim]{session_id} · {len(rows)} 条记录[/dim]"))
        log.write(
            Text.from_markup(
                f"[dim]模型上下文: {resumed.mode} · {resumed.model_messages} 条 · "
                f"约 {resumed.estimated_tokens:,} tokens[/dim]"
            )
        )

        for row in rows:
            role = row["role"]
            content = row["content"] or ""

            if role == "error":
                if row.get("error"):
                    log.write(Text.from_markup(f"  [dim red]✗ {str(row['error'])}[/dim red]"))
                continue

            if role == "user":
                log.write(self._user_echo_line(content))

            elif role == "assistant":
                tc = row.get("tool_calls", "")
                if tc:
                    try:
                        calls = json.loads(tc)
                        names = ", ".join(c.get("name", "?") for c in calls)
                        log.write(Text.from_markup(f"  [dim green]✓ {names}[/dim green]"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                if content:
                    log.write(Markdown(content))

        log.write(Text.from_markup("[dim]───── 历史消息结束，继续对话 ─────[/dim]"))
        log.scroll_end(animate=False)
        self._update_status()

    def action_new_chat(self) -> None:
        # 保存会话记忆
        self._save_memory_async()
        self._messages.clear()
        self._conversation.clear_queue()
        self._conversation.abandon_active_turn()
        self._session_tokens = {
            "input": 0,
            "output": 0,
            "unrated_output": 0,
            "rounds": 0,
            "cache_read": 0,
            "generation_ms": 0,
            "cache_reported": False,
        }
        self._session_id = uuid.uuid4().hex[:12]
        log = self.query_one("#chat-log", ChatLog)
        log.clear()
        log.write(Text.from_markup("[green]新对话已开始[/green]"))
        self._refresh_followups_panel()
        self._update_status()


# 注册命令面板（class 定义完成后）
try:
    from cli.commands import WyckoffCommands

    WyckoffTUI.COMMANDS = {WyckoffCommands}
except ImportError:
    pass
