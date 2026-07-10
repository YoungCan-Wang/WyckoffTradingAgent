from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from rich.markdown import Markdown

from cli.tui import (
    WyckoffTUI,
    _background_task_summary,
    _chatlog_role_for_turn,
    _display_final_response,
    _display_retry_event,
    _display_workflow_plan_event,
    _display_workflow_step_event,
    _is_system_notification_message,
    _make_sub_agent_progress_handler,
    _pending_user_question_answer,
    _pending_user_question_lines,
    _pending_workflow_reply_intent,
    _pending_workflow_revision_intent,
    _PendingUserQuestion,
    _pop_lines,
    _replace_streamed_response,
    _settle_markdown_render,
    _system_notification_queue_item,
    _tool_result_view,
    _workflow_bg_event_summary,
    _workflow_control_intent,
    _workflow_detail_step_line,
    _write_counted,
)


def _recent_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _FakeLog:
    def __init__(self) -> None:
        self.lines = ["kept"]
        self._widest_line_width = 0
        self.virtual_size = None
        self.refreshed = False
        self.layout_refreshed = False
        self.scrolled = False

    def write(self, renderable) -> None:
        if isinstance(renderable, list):
            self.lines.extend(renderable)
        else:
            self.lines.append(renderable)

    def clear(self) -> None:
        self.lines.clear()

    def refresh(self, *, layout: bool = False) -> None:
        self.refreshed = True
        self.layout_refreshed = self.layout_refreshed or layout

    def scroll_end(self, *, animate: bool = False) -> None:
        self.scrolled = True


class _FakeInput:
    def __init__(self) -> None:
        self._pasted_text = None
        self.cleared = False

    def consume_pasted(self):
        return None

    def clear(self) -> None:
        self.cleared = True


class _ReplyProvider:
    def __init__(self, decision: str):
        self.decision = decision
        self.chat_calls: list[dict] = []

    def chat(self, messages, tools, system_prompt=""):
        self.chat_calls.append({"messages": messages, "tools": tools, "system_prompt": system_prompt})
        return {"type": "text", "text": self.decision}


def test_write_counted_returns_actual_added_strips_for_wrapped_renderable():
    log = _FakeLog()

    added = _write_counted(log, ["wrap line 1", "wrap line 2"])

    assert added == 2
    assert log.lines == ["kept", "wrap line 1", "wrap line 2"]


def test_pop_lines_removes_actual_added_strips():
    log = _FakeLog()
    added = _write_counted(log, ["wrap line 1", "wrap line 2"])

    _pop_lines(log, added)

    assert log.lines == ["kept"]
    assert log.refreshed is True


def test_expand_recent_workflow_followup_uses_current_session(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "s1"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_other",
                "session_id": "s2",
                "label": "其他",
                "status": "running",
                "user_text": "其他任务",
                "plan": {"steps": [{"step_id": "other", "title": "其他"}]},
            },
            {
                "run_id": "wf_current",
                "session_id": "s1",
                "label": "当前",
                "status": "running",
                "user_text": "给我选股",
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            },
        ],
    )

    expanded = app._expand_recent_workflow_followup("接着刚才那个")

    assert "继续 workflow wf_current" in expanded
    assert "wf_other" not in expanded


def test_expand_recent_workflow_followup_falls_back_to_latest_run(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "new_session"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_latest",
                "session_id": "old_session",
                "label": "最近",
                "status": "completed",
                "user_text": "给我选股",
                "updated_at": _recent_timestamp(),
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            }
        ],
    )

    expanded = app._expand_recent_workflow_followup("接着刚才那个")

    assert "继续 workflow wf_latest" in expanded


def test_expand_recent_workflow_followup_ignores_stale_latest_run(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "new_session"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_stale",
                "session_id": "old_session",
                "label": "过期",
                "status": "completed",
                "user_text": "给我选股",
                "updated_at": "2000-01-01 00:00:00",
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            }
        ],
    )

    assert app._expand_recent_workflow_followup("接着刚才那个") == "接着刚才那个"


def test_recent_workflow_context_skips_explicit_resume(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "s1"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_current",
                "session_id": "s1",
                "label": "当前",
                "status": "completed",
                "user_text": "给我选股",
                "result_summary": "候选 A",
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            }
        ],
    )

    context = app._recent_workflow_context("第一个怎么样")

    assert "<recent-workflow-context>" in context
    assert "run_id: wf_current" in context
    assert app._recent_workflow_context("今天市场怎么样") == ""
    assert app._recent_workflow_context("继续 workflow wf_current") == ""


def test_recent_workflow_context_falls_back_to_latest_run(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "new_session"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_latest",
                "session_id": "old_session",
                "label": "最近",
                "status": "completed",
                "user_text": "给我选股",
                "result_summary": "候选 A",
                "updated_at": _recent_timestamp(),
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            }
        ],
    )

    context = app._recent_workflow_context("第一个怎么样")

    assert "run_id: wf_latest" in context


def test_recent_workflow_context_loads_event_evidence(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "s1"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_current",
                "session_id": "s1",
                "label": "当前",
                "status": "completed",
                "user_text": "给我选股",
                "result_summary": "候选摘要",
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            }
        ],
    )
    monkeypatch.setattr(
        "cli.workflows.store.load_workflow_events",
        lambda run_id, limit=120: [
            {
                "payload": {
                    "type": "workflow_step_done",
                    "step": {
                        "title": "扫描候选",
                        "status": "completed",
                        "evidence": [
                            "候选结论: 首选 300750 宁德时代",
                            "候选护栏: 禁止直接买入",
                        ],
                    },
                }
            }
        ],
    )

    context = app._recent_workflow_context("第一个怎么样")

    assert "run_id: wf_current" in context
    assert "证据: 候选结论: 首选 300750 宁德时代" in context
    assert "证据: 候选护栏: 禁止直接买入" in context


def test_recent_workflow_context_ignores_stale_latest_run(monkeypatch):
    app = object.__new__(WyckoffTUI)
    app._session_id = "new_session"
    monkeypatch.setattr(
        "cli.workflows.store.list_workflow_runs",
        lambda limit=8: [
            {
                "run_id": "wf_stale",
                "session_id": "old_session",
                "label": "过期",
                "status": "completed",
                "user_text": "给我选股",
                "result_summary": "候选 A",
                "updated_at": "2000-01-01 00:00:00",
                "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
            }
        ],
    )

    assert app._recent_workflow_context("第一个怎么样") == ""


def test_replace_streamed_response_redraws_markdown():
    log = _FakeLog()
    log.lines.extend(["  ---", "## raw", "| a |"])

    added = _replace_streamed_response(log, 3, "## Rendered\n\n| a |\n| - |")

    assert added == 1
    assert log.lines[0] == "kept"
    assert isinstance(log.lines[1], Markdown)
    assert log.refreshed is True
    assert log.layout_refreshed is True


def test_settle_markdown_render_refreshes_layout_and_scrolls():
    log = _FakeLog()

    _settle_markdown_render(log)

    assert log.layout_refreshed is True
    assert log.scrolled is True


def test_display_final_response_replaces_streamed_raw_text():
    log = _FakeLog()
    log.lines.extend(["  ---", "**raw**"])
    writes = []

    displayed = _display_final_response(
        log,
        "**rendered**",
        streaming_started=True,
        stream_separator_strips=1,
        stream_text_strips=1,
        write=writes.append,
        call_from_thread=lambda func, *args: func(*args),
    )

    assert displayed is True
    assert writes == []
    assert log.lines[0] == "kept"
    assert isinstance(log.lines[1], Markdown)


def test_display_workflow_plan_event_keeps_pending_plan_compact():
    writes = []
    scrolled = []

    run_id, workflow_name = _display_workflow_plan_event(
        {
            "run_id": "wf_1",
            "workflow": "backtest",
            "label": "策略回测",
            "route": {"reason": "检测到策略回测意图", "matches": ["回测"], "confidence": 0.9},
            "plan": {"steps": [{"title": "执行回测任务", "agent": "research", "tool_scope": ["run_backtest"]}]},
        },
        writes.append,
        lambda: scrolled.append(True),
    )

    assert run_id == "wf_1"
    assert workflow_name == "backtest"
    rendered = "\n".join(str(item) for item in writes)
    assert "策略回测" in str(writes[0])
    assert "1 个动态任务" in str(writes[0])
    assert "识别原因：检测到策略回测意图" in rendered
    assert "命中：回测" in rendered
    assert "置信度：90%" in rendered
    assert "/workflow show wf_1" in rendered
    assert "待执行" not in rendered
    assert "执行接力：执行回测任务" in rendered
    assert "工具边界：回测" in rendered
    assert "1. 执行回测任务" not in rendered
    assert "research" not in rendered
    assert len(writes) == 1
    assert all(line.strip() for line in rendered.splitlines())
    assert scrolled == [True]


def test_display_workflow_plan_event_shows_pending_approval_state():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_pending",
            "workflow": "dynamic_task",
            "label": "持仓复盘",
            "plan": {"steps": [{"title": "读取持仓", "tool_scope": ["portfolio"]}]},
        },
        writes.append,
        lambda: None,
        launch_state="pending_approval",
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "计划已准备好，等待批准" in rendered
    assert "/workflow show wf_pending" in rendered
    assert "已交给 agent 动态执行" not in rendered


def test_display_workflow_plan_event_uses_plan_route_for_saved_events():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_saved",
            "workflow": "dynamic_task",
            "label": "保存脚本",
            "plan": {
                "route": {"reason": "复用已保存 workflow script", "matches": ["saved"], "confidence": 0.75},
                "script": {"runtime": {"planner": "stored_script"}},
                "steps": [{"title": "读取持仓", "rationale": "复用保存脚本"}],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "识别原因：复用已保存 workflow script" in rendered
    assert "命中：saved" in rendered
    assert "脚本来源：已保存脚本" in rendered


def test_display_workflow_plan_event_hides_internal_model_router_matches():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_model",
            "workflow": "dynamic_task",
            "label": "动态任务",
            "route": {
                "reason": "模型判断需要动态 workflow",
                "matches": ["model_router_guard", "stock_selection_guard", "选股", "model_router_fallback"],
                "confidence": 0.82,
            },
            "plan": {"steps": [{"title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "命中：选股" in rendered
    assert "model_router" not in rendered
    assert "stock_selection_guard" not in rendered


def test_display_workflow_plan_event_omits_empty_internal_matches():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_model",
            "workflow": "dynamic_task",
            "label": "动态任务",
            "route": {
                "reason": "模型判断需要动态 workflow",
                "matches": ["model_router", "stock_selection_guard"],
                "confidence": 0.82,
            },
            "plan": {"steps": [{"title": "读取事实", "tool_scope": ["get_market_overview"]}]},
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "识别原因：模型判断需要动态 workflow" in rendered
    assert "命中：" not in rendered
    assert "model_router" not in rendered
    assert "stock_selection_guard" not in rendered


def test_display_workflow_plan_event_surfaces_trimmed_model_plan():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_trimmed",
            "workflow": "dynamic_task",
            "label": "今日选股",
            "plan": {
                "script": {
                    "runtime": {
                        "step_limit": 24,
                        "original_step_count": 27,
                        "truncated_step_count": 3,
                    }
                },
                "steps": [{"title": f"任务 {index}"} for index in range(24)],
            },
        },
        writes.append,
        lambda: None,
    )

    assert "24/27 个动态任务" in str(writes[0])
    assert "已收敛 3 个过长任务" in str(writes[0])


def test_display_workflow_plan_event_surfaces_planner_provenance():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_model",
            "workflow": "dynamic_task",
            "label": "今日选股",
            "plan": {
                "script": {"rationale": "模型决定先缩小候选池再做攻防复核", "runtime": {"planner": "model_script"}},
                "steps": [{"title": "扫描候选", "rationale": "先缩小候选池"}],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "脚本来源：模型生成" in rendered
    assert "模型拆分：模型决定先缩小候选池再做攻防复核" in rendered
    assert "执行接力：扫描候选" in rendered
    assert "1. 扫描候选" not in rendered


def test_display_workflow_plan_update_surfaces_model_adaptation():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_adapted",
            "workflow": "dynamic_task",
            "label": "今日选股",
            "plan": {
                "script": {
                    "runtime": {
                        "planner": "model_script",
                        "adaptation": "model_phase",
                        "adaptation_count": 1,
                        "last_adaptation_title": "改为攻防计划",
                        "adapted_removed_step_count": 1,
                        "adapted_added_step_count": 1,
                        "adapted_removed_steps": [{"id": "report", "title": "生成候选研报"}],
                        "adapted_added_steps": [{"id": "decision", "title": "形成攻防计划"}],
                    }
                },
                "steps": [
                    {"title": "扫描候选", "status": "completed"},
                    {"title": "形成攻防计划", "status": "pending"},
                ],
            },
        },
        writes.append,
        lambda: None,
        launch_state="adapted",
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "阶段结果已触发第 1 次模型改稿：改为攻防计划" in rendered
    assert "后续变更：移除 生成候选研报、新增 形成攻防计划" in rendered
    assert "已根据阶段结果更新后续计划" in rendered
    assert "/workflow show wf_adapted" in rendered


def test_display_workflow_plan_event_surfaces_model_contract_repair():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_repaired",
            "workflow": "dynamic_task",
            "label": "今日选股",
            "plan": {
                "script": {
                    "runtime": {
                        "planner": "model_script",
                        "tool_contract_repair": "model",
                        "unscoped_step_count_before_repair": 2,
                    }
                },
                "steps": [{"title": "扫描候选", "tool_scope": ["screen_stocks"]}],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "脚本来源：模型生成" in rendered
    assert "模型已修订工具契约（修订前 2 个任务未声明必用工具）" in rendered
    assert "脚本边界" not in rendered


def test_display_workflow_plan_event_surfaces_fallback_reason():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_fallback",
            "workflow": "dynamic_task",
            "label": "动态任务",
            "plan": {
                "script": {
                    "runtime": {
                        "planner": "fallback_script",
                        "fallback_reason": "provider unavailable",
                    }
                },
                "steps": [{"title": "单步处理", "success_criteria": "给出结论"}],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "脚本来源：回退单步 · provider unavailable" in rendered
    assert "执行接力：单步处理" in rendered
    assert "1. 单步处理" not in rendered


def test_display_workflow_plan_event_labels_stock_selection_fallback():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_stock_fallback",
            "workflow": "dynamic_task",
            "label": "动态任务",
            "plan": {
                "script": {
                    "runtime": {
                        "planner": "fallback_script",
                        "fallback_reason": "provider unavailable",
                        "fallback_kind": "stock_selection",
                    }
                },
                "steps": [
                    {"title": "扫描候选", "tool_scope": ["screen_stocks"]},
                    {"title": "形成攻防边界", "tool_scope": ["generate_strategy_decision"]},
                ],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "脚本来源：选股兜底 · provider unavailable" in rendered
    assert "回退单步" not in rendered
    assert "2 个动态任务" in rendered


def test_display_workflow_plan_event_surfaces_model_step_boundaries():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_dynamic",
            "workflow": "dynamic_task",
            "label": "今日选股",
            "plan": {
                "steps": [
                    {
                        "title": "扫描候选",
                        "tool_scope": ["screen_stocks", "get_market_overview"],
                        "rationale": "先缩小候选池",
                        "success_criteria": "输出候选代码和风险状态",
                        "risk_guard": "不写入推荐或持仓",
                    },
                    {
                        "title": "攻防计划",
                        "tool_scope": ["generate_strategy_decision"],
                        "depends_on": ["scan"],
                        "success_criteria": "说明观察、复核和禁止直接买入的边界",
                    },
                ],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "执行接力：扫描候选 → 攻防计划" in rendered
    assert "工具边界：全市场扫描、大盘水温、攻防决策" in rendered
    assert "1. 扫描候选" not in rendered
    assert "目标: 先缩小候选池" not in rendered
    assert "依赖: scan" not in rendered
    assert "/workflow show wf_dynamic" in rendered


def test_display_workflow_plan_event_marks_semantic_tool_boundaries():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_dynamic",
            "workflow": "dynamic_task",
            "label": "今日选股",
            "plan": {
                "steps": [
                    {
                        "title": "扫描候选",
                        "tool_scope": ["screen_stocks"],
                        "tool_scope_source": "semantic_inference",
                    }
                ],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "工具边界（含语义恢复）：全市场扫描" in rendered


def test_display_workflow_plan_event_surfaces_effective_tool_scope():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_generic",
            "workflow": "portfolio_review",
            "label": "持仓复盘",
            "plan": {
                "script": {"runtime": {"planner": "model_script"}},
                "steps": [
                    {
                        "title": "复盘持仓",
                        "tool_scope": [],
                        "effective_tool_scope": ["portfolio", "analyze_stock"],
                        "rationale": "让模型按上下文决定最小工具调用",
                    }
                ],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "执行接力：复盘持仓" in rendered
    assert "脚本边界：1 个任务未声明必用工具" in rendered
    assert "工具边界：持仓、个股分析" in rendered
    assert "1. 复盘持仓" not in rendered


def test_display_workflow_plan_event_does_not_warn_for_explicit_tool_scope():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_scoped",
            "workflow": "dynamic_task",
            "label": "选股扫描",
            "plan": {
                "script": {"runtime": {"planner": "model_script"}},
                "steps": [
                    {
                        "title": "扫描候选",
                        "tool_scope": ["screen_stocks"],
                        "effective_tool_scope": ["screen_stocks"],
                    }
                ],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "脚本边界" not in rendered
    assert "工具边界：全市场扫描" in rendered


def test_display_workflow_plan_event_previews_tool_only_model_steps():
    writes = []

    _display_workflow_plan_event(
        {
            "run_id": "wf_outline",
            "workflow": "dynamic_task",
            "label": "持仓复盘",
            "plan": {
                "steps": [
                    {"title": "读取持仓与资金", "tool_scope": ["portfolio"]},
                    {"title": "诊断持仓与市场环境", "tool_scope": ["portfolio", "get_market_overview"]},
                    {"title": "形成去留和风险动作", "tool_scope": ["generate_strategy_decision"]},
                ],
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "执行接力：读取持仓与资金 → 诊断持仓与市场环境 → 形成去留和风险动作" in rendered
    assert "工具边界：持仓、大盘水温、攻防决策" in rendered
    assert "1. 读取持仓与资金" not in rendered


def test_sub_agent_progress_handler_uses_reader_facing_tool_labels():
    writes = []
    scrolls = []
    spinners = []
    stops = []
    tools = SimpleNamespace(display_name=lambda name: {"ask_user_question": "提问用户"}.get(name, name))
    handler = _make_sub_agent_progress_handler(
        tools,
        writes.append,
        lambda: scrolls.append(True),
        spinners.append,
        lambda: stops.append(True),
    )

    handler({"type": "tool_start", "sub_agent": "task", "name": "ask_user_question"})
    handler(
        {
            "type": "tool_result",
            "sub_agent": "task",
            "name": "ask_user_question",
            "elapsed_ms": 1234,
            "status": "ok",
        }
    )

    rendered = "\n".join(str(item) for item in writes)
    assert spinners == ["agent 调用 提问用户"]
    assert "agent 完成 提问用户 1.2s" in rendered
    assert "ask_user_question" not in rendered
    assert "task →" not in rendered
    assert stops == [True]
    assert scrolls == [True]


def test_busy_input_answers_pending_user_question_instead_of_queueing():
    app = object.__new__(WyckoffTUI)
    event = threading.Event()
    result = [""]
    app._pending_user_question = _PendingUserQuestion(
        "把你要看的持仓代码发来",
        [],
        True,
        "",
        event,
        result,
    )
    log = _FakeLog()

    handled = WyckoffTUI._answer_pending_user_question(app, "你看我持仓呀", log)

    assert handled is True
    assert event.is_set()
    assert result[0] == "你看我持仓呀"
    assert app._pending_user_question is None
    assert "已作为当前提问的回答" in str(log.lines[-1])


def test_busy_input_rejects_invalid_pending_question_option():
    app = object.__new__(WyckoffTUI)
    event = threading.Event()
    result = [""]
    pending = _PendingUserQuestion("是否确认？", ["确认", "取消"], False, "", event, result)
    app._pending_user_question = pending
    log = _FakeLog()

    handled = WyckoffTUI._answer_pending_user_question(app, "随便说", log)

    assert handled is True
    assert not event.is_set()
    assert result == [""]
    assert app._pending_user_question is pending
    assert "请从当前提问的选项中选择" in str(log.lines[-1])


def test_slash_command_still_runs_while_pending_user_question():
    app = object.__new__(WyckoffTUI)
    fake_input = _FakeInput()
    log = _FakeLog()
    event = threading.Event()
    result = [""]
    app._input_mode = "none"
    app._busy = True
    app._pending_user_question = _PendingUserQuestion("是否确认？", ["确认", "取消"], False, "", event, result)
    handled = []
    app.query_one = lambda selector, *_args, **_kwargs: fake_input if selector == "#chat-input" else log
    app._handle_command = lambda text: handled.append(text)

    WyckoffTUI.on_input_submitted(app, SimpleNamespace(value="/help"))

    assert handled == ["/help"]
    assert not event.is_set()
    assert result == [""]
    assert app._pending_user_question is not None
    assert fake_input.cleared is True


def test_empty_input_uses_pending_question_default_answer():
    app = object.__new__(WyckoffTUI)
    fake_input = _FakeInput()
    log = _FakeLog()
    event = threading.Event()
    result = [""]
    app._input_mode = "none"
    app._busy = True
    app._pending_user_question = _PendingUserQuestion("确认默认范围？", [], True, "最近一年", event, result)
    app.query_one = lambda selector, *_args, **_kwargs: fake_input if selector == "#chat-input" else log

    WyckoffTUI.on_input_submitted(app, SimpleNamespace(value=""))

    assert event.is_set()
    assert result[0] == "最近一年"
    assert app._pending_user_question is None
    assert "最近一年" in str(log.lines[-1])


def test_empty_input_without_pending_default_is_ignored():
    app = object.__new__(WyckoffTUI)
    fake_input = _FakeInput()
    log = _FakeLog()
    event = threading.Event()
    result = [""]
    app._input_mode = "none"
    app._busy = True
    app._pending_user_question = _PendingUserQuestion("请输入范围", [], True, "", event, result)
    app.query_one = lambda selector, *_args, **_kwargs: fake_input if selector == "#chat-input" else log

    WyckoffTUI.on_input_submitted(app, SimpleNamespace(value=""))

    assert not event.is_set()
    assert result == [""]
    assert app._pending_user_question is not None
    assert log.lines == ["kept"]


def test_send_message_does_not_insert_blank_log_line(monkeypatch):
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    app._messages = []
    app.query_one = lambda *_args, **_kwargs: log
    app._recent_workflow_context = lambda _text: ""
    app._start_spinner = lambda _label: None
    app._run_agent = lambda: None

    import cli.memory as memory

    monkeypatch.setattr(memory, "build_memory_context", lambda _text: "")

    WyckoffTUI._send_message(app, "你看我持仓呀")

    assert len(log.lines) == 2
    assert str(log.lines[-1]) == "❯ 你看我持仓呀"
    assert app._messages == [{"role": "user", "content": "你看我持仓呀"}]


def test_send_system_notification_does_not_insert_blank_log_line():
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    app._messages = []
    app.query_one = lambda *_args, **_kwargs: log
    app._start_spinner = lambda _label: None
    app._run_agent = lambda: None

    WyckoffTUI._send_system_notification(app, "workflow done")

    assert len(log.lines) == 2
    assert "后台结果已回传给 agent" in str(log.lines[-1])
    assert app._messages == [{"role": "user", "content": "workflow done", "_system_notification": True}]


def test_auto_resume_notice_does_not_insert_blank_log_line():
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    resumed = []
    sent = []
    app._find_interrupted_scratchpad = lambda: ("sess123", "帮我找机器人机会")
    app._resume_session = lambda session_id: resumed.append(session_id)
    app.query_one = lambda *_args, **_kwargs: log
    app._send_message = lambda text: sent.append(text)

    WyckoffTUI._check_auto_resume(app)

    rendered = [str(item) for item in log.lines[1:]]
    assert resumed == ["sess123"]
    assert sent == ["帮我找机器人机会"]
    assert len(rendered) == 2
    assert all(line.strip() for line in rendered)
    assert all(not line.startswith("\n") and not line.endswith("\n") for line in rendered)


def test_new_chat_notice_does_not_insert_blank_log_line():
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    app._save_memory_async = lambda: None
    app._messages = [{"role": "user", "content": "旧会话"}]
    app._queue = deque(["queued"])
    app._session_tokens = {"input": 1, "output": 2, "rounds": 1}
    app._update_status = lambda: None
    app.query_one = lambda *_args, **_kwargs: log

    WyckoffTUI.action_new_chat(app)

    assert [str(item) for item in log.lines] == ["新对话已开始"]
    assert app._messages == []
    assert list(app._queue) == []


def test_schedule_trigger_notice_does_not_insert_blank_log_line():
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    sent = []
    app._busy = False
    app._queue = deque()
    app.query_one = lambda *_args, **_kwargs: log
    app._send_message = lambda text: sent.append(text)
    app._handle_command = lambda _action: None
    app._desktop_notify = lambda _message: None
    schedule = SimpleNamespace(name="盘前风控", notify=False, action="帮我看看大盘")

    WyckoffTUI._fire_schedule(app, schedule)

    rendered = str(log.lines[-1])
    assert sent == ["帮我看看大盘"]
    assert rendered == "⏰ 定时触发：盘前风控"
    assert not rendered.startswith("\n") and not rendered.endswith("\n")


def test_pending_user_question_lines_render_inline_chat_prompt():
    pending = _PendingUserQuestion(
        "是否确认执行？",
        ["确认", "取消"],
        False,
        "",
        threading.Event(),
        [""],
    )

    rendered = "\n".join(_pending_user_question_lines(pending))

    assert "agent 需要你补充" in rendered
    assert "直接在输入框回复" in rendered
    assert "1. 确认" in rendered
    assert "2. 取消" in rendered


def test_pending_user_question_answer_accepts_displayed_option_number():
    pending = _PendingUserQuestion(
        "是否确认执行？",
        ["确认", "取消"],
        False,
        "",
        threading.Event(),
        [""],
    )

    assert _pending_user_question_answer("1", pending) == "确认"
    assert _pending_user_question_answer("2", pending) == "取消"
    assert _pending_user_question_answer("0", pending) == "确认"


def test_submit_workflow_background_auto_starts_model_plan():
    app = object.__new__(WyckoffTUI)
    app._messages = [{"role": "user", "content": "memory\n帮我选出好股票", "_raw_content": "帮我选出好股票"}]
    chatlog_rows = []
    launched = []
    writes = []
    scrolls = []

    class Runtime:
        def __init__(self) -> None:
            self.prepared = False

        def prepare_run(self):
            self.prepared = True
            return {
                "run_id": "wf_auto",
                "workflow": "dynamic_task",
                "label": "今日选股",
                "plan": {
                    "script": {"runtime": {"planner": "model_script"}},
                    "steps": [{"title": "扫描候选", "rationale": "先缩小候选池"}],
                },
            }

    runtime = Runtime()
    app._chatlog_save = lambda role, content, **kwargs: chatlog_rows.append((role, content, kwargs))
    app._launch_workflow_background = lambda *args: launched.append(args)

    started = WyckoffTUI._submit_workflow_background(
        app,
        runtime,
        SimpleNamespace(
            turn_user_index=0,
            user_text="帮我选出好股票",
            model_name="model",
            provider_name="provider",
        ),
        "system prompt",
        writes.append,
        lambda: scrolls.append(True),
    )

    assert started is True
    assert runtime.prepared is True
    assert app._messages[0]["content"] == "帮我选出好股票"
    assert app._messages[-1]["role"] == "assistant"
    assert "自动开始后台运行" in app._messages[-1]["content"]
    assert chatlog_rows[0][0] == "user"
    assert chatlog_rows[1][0] == "assistant"
    assert "workflow_run_id" in chatlog_rows[1][2]["metadata_json"]
    assert len(launched) == 1
    assert launched[0][1] == [{"role": "user", "content": "帮我选出好股票"}]
    assert launched[0][2] == "system prompt"
    assert launched[0][5].startswith("wfbg_wf_auto_")
    rendered = "\n".join(str(item) for item in writes)
    assert "脚本来源：模型生成" in rendered
    assert "已交给 agent 动态执行" in rendered
    assert "等待批准" not in rendered
    assert "/workflow show wf_auto" in rendered
    assert scrolls


def test_prepare_workflow_approval_keeps_plan_pending():
    app = object.__new__(WyckoffTUI)
    app._messages = [{"role": "user", "content": "继续 workflow"}]
    app._pending_workflows = {}
    chatlog_rows = []
    writes = []
    scrolls = []

    class Runtime:
        def prepare_run(self):
            return {
                "run_id": "wf_pending",
                "workflow": "dynamic_task",
                "label": "持仓复盘",
                "plan": {
                    "script": {"runtime": {"planner": "model_script"}},
                    "steps": [{"title": "读取持仓", "rationale": "先取当前事实"}],
                },
            }

    app._chatlog_save = lambda role, content, **kwargs: chatlog_rows.append((role, content, kwargs))

    prepared = WyckoffTUI._prepare_workflow_approval(
        app,
        Runtime(),
        SimpleNamespace(
            turn_user_index=0,
            user_text="继续 workflow",
            model_name="model",
            provider_name="provider",
        ),
        "system prompt",
        writes.append,
        lambda: scrolls.append(True),
    )

    rendered = "\n".join(str(item) for item in writes)
    assert prepared is True
    assert "wf_pending" in app._pending_workflows
    assert "计划已准备好，等待批准" in rendered
    assert "也可以直接补充修改意见" in rendered
    assert "已交给 agent 动态执行" not in rendered
    assert chatlog_rows[1][0] == "assistant"
    assert "workflow_pending_approval" in chatlog_rows[1][2]["metadata_json"]
    assert scrolls == [True, True]


def test_workflow_background_event_summary_keeps_handoff_evidence():
    summary = _workflow_bg_event_summary(
        {
            "type": "workflow_step_done",
            "step": {
                "title": "形成攻防",
                "agent": "trading",
                "status": "completed",
                "summary": "completed 1.0s",
            },
            "source": {
                "agent_detail": {
                    "tool_calls": ["generate_strategy_decision"],
                    "handoff_state": {
                        "last_strategy_decision": {
                            "status": "skipped_notify_unconfigured",
                            "report_source": "last_ai_report",
                            "reviewed_codes": ["300750"],
                            "reviewed_symbols": [
                                {
                                    "code": "300750",
                                    "name": "宁德时代",
                                    "action_status": "blocked_by_market_gate",
                                    "risk_factors": ["大盘风险闸门关闭"],
                                    "next_step": "补充 Telegram 配置后可生成并发送 OMS 工单",
                                }
                            ],
                            "next_action": "补充 Telegram 配置后可生成并发送 OMS 工单",
                        }
                    },
                }
            },
        }
    )

    assert summary["step"]["title"] == "形成攻防"
    assert summary["step"]["evidence"][0].startswith("攻防决策: 未发送工单")
    assert "候选结论: 阻断候选 300750 宁德时代" in summary["step"]["evidence"][1]


def test_complete_workflow_background_does_not_wake_idle_agent():
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    chatlog_rows = []
    updates = []
    app.query_one = lambda *_args, **_kwargs: log
    app._busy = False
    app._queue = deque()
    app._messages = []
    app._session_tokens = {"input": 0, "output": 0, "rounds": 0}
    app._update_status = lambda: updates.append(True)
    app._chatlog_save = lambda role, content, **kwargs: chatlog_rows.append((role, content, kwargs))

    WyckoffTUI._complete_workflow_background(
        app,
        "wfbg_idle",
        {
            "workflow_run_id": "wf_idle",
            "workflow": "dynamic_task",
            "final_text": "候选结论: 首选 300750 宁德时代",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )

    assert app._queue == deque()
    assert app._messages[-1]["content"] == "候选结论: 首选 300750 宁德时代"
    assert app._session_tokens == {"input": 10, "output": 5, "rounds": 1}
    assert chatlog_rows[0][0] == "assistant"
    assert updates == [True]
    assert "workflow 后台完成" in str(log.lines[-2])


def test_complete_workflow_background_queues_system_notification_when_busy():
    app = object.__new__(WyckoffTUI)
    log = _FakeLog()
    app.query_one = lambda *_args, **_kwargs: log
    app._busy = True
    app._queue = deque()
    app._messages = []
    app._session_tokens = {"input": 0, "output": 0, "rounds": 0}
    app._update_status = lambda: None
    app._chatlog_save = lambda *_args, **_kwargs: None

    WyckoffTUI._complete_workflow_background(
        app,
        "wfbg_busy",
        {
            "workflow_run_id": "wf_busy",
            "workflow": "dynamic_task",
            "final_text": "候选结论: 首选 300750 宁德时代\n风险边界: 跌破 20 日线转观察",
            "events": [{"type": "workflow_step_done", "step": {"title": "扫描候选", "status": "completed"}}],
        },
    )

    assert len(app._queue) == 1
    item = app._queue[0]
    assert item["type"] == "system_notification"
    assert "<run-id>wf_busy</run-id>" in item["content"]
    assert "dynamic-workflow event" in item["content"]
    assert "候选结论: 首选 300750 宁德时代" in item["content"]
    assert app._messages[-1]["role"] == "assistant"


def test_display_retry_event_surfaces_required_tool_and_reason():
    writes = []
    scrolled = []

    _display_retry_event(
        {
            "retry": 1,
            "required_tool": "portfolio",
            "message": "持仓体检需要先读取真实持仓数据。请先调用 `portfolio` 获取真实数据。",
        },
        writes.append,
        lambda: scrolled.append(True),
    )

    rendered = str(writes[0])
    assert "运行时校验：第 1 次要求先调用 持仓" in rendered
    assert "持仓体检需要先读取真实持仓数据" in rendered
    assert scrolled == [True]


def test_display_workflow_step_event_hides_internal_scope():
    writes = []
    scrolled = []

    _display_workflow_step_event(
        {
            "step": {
                "title": "读取持仓",
                "agent": "analysis",
                "status": "running",
                "tool_scope": ["portfolio", "analyze_stock"],
                "summary": "analysis: start",
            }
        },
        writes.append,
        lambda: scrolled.append(True),
    )

    rendered = str(writes[0])
    assert "读取持仓" in rendered
    assert "运行中" in rendered
    assert "工具: 持仓、个股分析" in rendered
    assert "工具：portfolio, analyze_stock" not in rendered
    assert "analysis:" not in rendered
    assert scrolled == [True]


def test_display_workflow_step_event_shows_effective_tool_scope():
    writes = []

    _display_workflow_step_event(
        {
            "step": {
                "title": "复盘持仓",
                "agent": "task",
                "status": "running",
                "tool_scope": [],
                "effective_tool_scope": ["portfolio", "analyze_stock"],
            }
        },
        writes.append,
        lambda: None,
    )

    rendered = str(writes[0])
    assert "复盘持仓" in rendered
    assert "可选工具: 持仓、个股分析" in rendered
    assert "task" not in rendered


def test_display_workflow_step_event_surfaces_handoff_candidate_brief():
    writes = []

    _display_workflow_step_event(
        {
            "step": {
                "title": "扫描候选",
                "status": "completed",
                "tool_scope": ["screen_stocks"],
                "summary": "completed 1.0s",
            },
            "source": {
                "agent_detail": {
                    "handoff_state": {
                        "last_screen_result": {
                            "selection_brief": {
                                "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
                                "primary_pick": {
                                    "code": "300750",
                                    "name": "宁德时代",
                                    "action_status": "ready_for_ai_review",
                                    "candidate_shadow_score": 92.0,
                                    "candidate_shadow_grade": "S",
                                    "next_step": "生成 AI 研报",
                                },
                            }
                        }
                    }
                }
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "扫描候选" in rendered
    assert "工具: 全市场扫描" in rendered
    assert "证据: 本轮首选可进入 AI 研报复核: 300750 宁德时代" in rendered
    assert "候选结论: 首选 300750 宁德时代" in rendered
    assert "候选影子S/92" in rendered


def test_display_workflow_step_event_surfaces_daily_trap_reason():
    writes = []

    _display_workflow_step_event(
        {
            "step": {
                "title": "扫描候选",
                "status": "completed",
                "tool_scope": ["screen_stocks"],
                "summary": "completed 1.0s",
            },
            "source": {
                "agent_detail": {
                    "handoff_state": {
                        "last_screen_result": {
                            "selection_brief": {
                                "primary_pick": {
                                    "code": "002217",
                                    "name": "合力泰",
                                    "action_status": "watch_only",
                                    "daily_trap_reason": "日线放量上影(2.6x)",
                                    "next_step": "等待回踩确认",
                                },
                            }
                        }
                    }
                }
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "候选结论: 观察候选 002217 合力泰" in rendered
    assert "风险: 日线放量上影(2.6x)" in rendered
    assert "下一步: 等待回踩确认" in rendered


def test_display_workflow_step_event_prioritizes_current_tool_handoff():
    writes = []

    _display_workflow_step_event(
        {
            "step": {
                "title": "形成攻防",
                "status": "completed",
                "tool_scope": ["generate_strategy_decision"],
                "summary": "completed 1.0s",
            },
            "source": {
                "agent_detail": {
                    "tool_calls": ["generate_strategy_decision"],
                    "handoff_state": {
                        "last_screen_result": {
                            "selection_brief": {
                                "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
                                "primary_pick": {
                                    "code": "300750",
                                    "name": "宁德时代",
                                    "action_status": "ready_for_ai_review",
                                },
                            }
                        },
                        "last_strategy_decision": {
                            "status": "skipped_notify_unconfigured",
                            "report_source": "last_ai_report",
                            "reviewed_codes": ["300750"],
                            "reviewed_symbols": [
                                {
                                    "code": "300750",
                                    "name": "宁德时代",
                                    "action_status": "blocked_by_market_gate",
                                    "risk_factors": ["大盘风险闸门关闭"],
                                    "next_step": "补充 Telegram 配置后可生成并发送 OMS 工单",
                                }
                            ],
                            "next_action": "补充 Telegram 配置后可生成并发送 OMS 工单",
                        },
                    },
                }
            },
        },
        writes.append,
        lambda: None,
    )

    rendered = "\n".join(str(item) for item in writes)
    assert "形成攻防" in rendered
    assert "工具: 攻防决策" in rendered
    assert "证据: 攻防决策: 未发送工单" in str(writes[1])
    assert "本轮首选可进入 AI 研报复核" not in str(writes[1])
    assert rendered.index("攻防决策: 未发送工单") < rendered.index("本轮首选可进入 AI 研报复核")


def test_tool_result_view_surfaces_screen_candidate_risk():
    summary, renderable = _tool_result_view(
        {
            "type": "tool_result",
            "name": "screen_stocks",
            "elapsed_ms": 1200,
            "result": {
                "selection_brief": {
                    "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
                    "primary_pick": {
                        "code": "300750",
                        "name": "宁德时代",
                        "priority_score": 12.5,
                        "shadow_score": 4.2,
                        "quality_factors": ["高优先级研报候选", "趋势线"],
                        "risk_factors": ["大盘风险闸门关闭"],
                        "action_status": "blocked_by_market_gate",
                        "next_step": "只观察",
                    },
                }
            },
        },
        None,
    )

    rendered = str(renderable)
    assert summary["brief"] == [
        "本轮首选可进入 AI 研报复核: 300750 宁德时代",
        "候选结论: 阻断候选 300750 宁德时代 · 风险闸门关闭 · 证据: 优先分12.5；动态分4.2 · 亮点: 高优先级研报候选；趋势线 · 风险: 大盘风险闸门关闭 · 下一步: 只观察",
    ]
    assert "screen_stocks" in rendered
    assert "候选结论: 阻断候选 300750 宁德时代" in rendered
    assert "优先分12.5" in rendered
    assert "动态分4.2" in rendered
    assert "高优先级研报候选" in rendered
    assert "风险闸门关闭" in rendered
    assert "大盘风险闸门关闭" in rendered


def test_tool_result_view_surfaces_screen_quick_scan_financial_scope():
    summary, renderable = _tool_result_view(
        {
            "type": "tool_result",
            "name": "screen_stocks",
            "elapsed_ms": 1200,
            "result": {
                "scan_scope": {
                    "scope": "bounded",
                    "board": "all",
                    "limit": 1200,
                    "total_scanned": 1200,
                    "financial_metrics": "skipped_quick_scan",
                    "financial_metrics_count": 0,
                },
                "selection_brief": {"headline": "本轮只有观察候选: 002326 永太科技"},
            },
        },
        None,
    )

    rendered = str(renderable)
    assert summary["brief"][0] == "快扫: all 前1200只，实际扫描1200只，财务过滤: 快扫跳过"
    assert "财务过滤: 快扫跳过" in rendered


def test_tool_result_view_surfaces_recommendation_eval_pick_action():
    summary, renderable = _tool_result_view(
        {
            "type": "tool_result",
            "name": "evaluate_recommendation_events",
            "elapsed_ms": 1200,
            "result": {
                "result_summary": (
                    "推荐事件评估: ready=12/20, hit=60%, ranking_decision=candidate\n"
                    "排序接入候选: candidate_shadow_then_score top1 已通过样本/lift/风险门槛\n"
                    "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代"
                ),
                "policy_selection": {
                    "picks": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "funnel_score": 89.5,
                            "candidate_shadow_score": 92.0,
                            "candidate_shadow_grade": "S",
                            "entry_quality_score": 84.0,
                            "entry_quality_grade": "A",
                            "candidate_quality_score": 92.0,
                            "risk_adjusted_quality_score": 87.0,
                            "entry_risk_penalty": 5.0,
                            "action_status": "ready_for_ai_review",
                            "quality_factors": ["候选影子评级 S"],
                            "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                            "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                        }
                    ]
                },
            },
        },
        None,
    )

    rendered = str(renderable)
    assert summary["brief"][-1].startswith("候选结论: 首选 300750 宁德时代 · 可进入AI复核")
    assert "候选结论: 首选 300750 宁德时代" in rendered
    assert "漏斗分89.5" in rendered
    assert "候选影子S/92" in rendered
    assert "入场A/84" in rendered
    assert "风险调整分87" in rendered
    assert "候选影子评级 S" in rendered
    assert "最新候选的未来窗口标签尚未成熟" in rendered


def test_workflow_detail_step_line_includes_tool_scope():
    line = _workflow_detail_step_line(
        {
            "step_id": "read_positions",
            "title": "读取持仓",
            "agent": "analysis",
            "status": "completed",
            "tool_scope": ["portfolio"],
            "depends_on": ["market"],
            "rationale": "先确认真实仓位",
            "success_criteria": "输出持仓风险",
            "risk_guard": "不写入交易",
            "context": "只读运行",
            "args_hint": "stock_codes: ['300750']",
            "summary": "analysis: completed 1.2s",
        }
    )

    assert "read_positions" in line
    assert "工具：portfolio" in line
    assert "completed" in line
    assert "analysis: completed" in line
    assert "依赖: market" in line
    assert "目标: 先确认真实仓位" in line
    assert "验收: 输出持仓风险" in line
    assert "边界: 不写入交易" in line
    assert "参数: stock_codes: ['300750']" in line


def test_workflow_detail_step_line_surfaces_handoff_evidence():
    line = _workflow_detail_step_line(
        {
            "step_id": "decision",
            "title": "形成攻防",
            "agent": "trading",
            "status": "completed",
            "tool_scope": ["generate_strategy_decision"],
            "summary": "completed 1.0s",
        },
        {
            "step_id": "decision",
            "tool_calls": ["generate_strategy_decision"],
            "handoff_state": {
                "last_screen_result": {
                    "selection_brief": {
                        "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
                        "primary_pick": {
                            "code": "300750",
                            "name": "宁德时代",
                            "action_status": "ready_for_ai_review",
                        },
                    }
                },
                "last_strategy_decision": {
                    "status": "skipped_notify_unconfigured",
                    "report_source": "last_ai_report",
                    "reviewed_codes": ["300750"],
                    "reviewed_symbols": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "action_status": "blocked_by_market_gate",
                            "risk_factors": ["大盘风险闸门关闭"],
                            "next_step": "补充 Telegram 配置后可生成并发送 OMS 工单",
                        }
                    ],
                    "next_action": "补充 Telegram 配置后可生成并发送 OMS 工单",
                },
            },
        },
    )

    assert "decision" in line
    assert "形成攻防" in line
    assert "证据: 攻防决策: 未发送工单" in line
    assert line.index("攻防决策: 未发送工单") < line.index("本轮首选可进入 AI 研报复核")


def test_workflow_detail_step_line_includes_effective_tool_scope():
    line = _workflow_detail_step_line(
        {
            "step_id": "review",
            "title": "复盘持仓",
            "agent": "task",
            "status": "running",
            "tool_scope": [],
            "effective_tool_scope": ["portfolio", "analyze_stock"],
        }
    )

    assert "可选工具：portfolio, analyze_stock" in line


def test_workflow_control_intent_requires_explicit_control_action():
    assert _workflow_control_intent("解释一下 workflow 是什么") is None
    assert _workflow_control_intent("查看 workflow wf_abc123") == ("show", "wf_abc123")
    assert _workflow_control_intent("workflow wf_abc123 详情") == ("show", "wf_abc123")
    assert _workflow_control_intent("把 workflow wf_abc123 的脚本打开") == ("script", "wf_abc123")
    assert _workflow_control_intent("看 workflow wf_abc123 的事件") == ("events", "wf_abc123")
    assert _workflow_control_intent("打开 workflow wf_abc123 日志") == ("events", "wf_abc123")
    assert _workflow_control_intent("workflow 运行状态") == ("status", "")
    assert _workflow_control_intent("复跑刚才的 workflow") == ("rerun", "")
    assert _workflow_control_intent("批准 workflow wf_abc123") == ("approve", "wf_abc123")
    assert _workflow_control_intent("暂停 workflow wf_abc123") == ("pause", "wf_abc123")
    assert _workflow_control_intent("停止 workflow wf_abc123") == ("stop", "wf_abc123")


def test_pending_workflow_reply_intent_accepts_chat_style_approval():
    assert _pending_workflow_reply_intent("好") == "approve"
    assert _pending_workflow_reply_intent("开始吧") == "approve"
    assert _pending_workflow_reply_intent("好，开始吧") == "approve"
    assert _pending_workflow_reply_intent("执行吧") == "approve"
    assert _pending_workflow_reply_intent("嗯可以跑一下") == "approve"
    assert _pending_workflow_reply_intent("没问题，按这个执行") == "approve"
    assert _pending_workflow_reply_intent("按这个来") == "approve"
    assert _pending_workflow_reply_intent("取消") == "deny"
    assert _pending_workflow_reply_intent("不用 workflow") == "deny"
    assert _pending_workflow_reply_intent("先别跑") == "deny"
    assert _pending_workflow_reply_intent("解释一下 workflow 是什么") == ""
    assert _pending_workflow_reply_intent("好，但是不用研报，先给攻防") == ""
    assert _pending_workflow_reply_intent("可以开始吗") == ""
    assert _pending_workflow_reply_intent("要不要开始") == ""


def test_pending_workflow_revision_intent_accepts_chat_style_edits():
    assert _pending_workflow_revision_intent("别这么拆，直接先扫候选")
    assert _pending_workflow_revision_intent("不用研报，先给攻防")
    assert _pending_workflow_revision_intent("好，但是不用研报，先给攻防")
    assert _pending_workflow_revision_intent("把第二步改成生成研报")
    assert not _pending_workflow_revision_intent("开始吧")
    assert not _pending_workflow_revision_intent("取消")
    assert not _pending_workflow_revision_intent("解释一下 workflow 是什么")
    assert not _pending_workflow_revision_intent("先等等")
    assert not _pending_workflow_revision_intent("直接回答我")


def test_pending_workflow_feedback_revises_single_pending_workflow():
    app = object.__new__(WyckoffTUI)
    feedbacks = []

    class Runtime:
        def revise_prepared_script(self, feedback):
            feedbacks.append(feedback)
            return {
                "run_id": "wf_pending",
                "workflow": "dynamic_task",
                "label": "动态任务",
                "plan": {
                    "script": {"title": "改后脚本", "runtime": {"planner": "model_script"}},
                    "steps": [{"title": "扫描候选", "tool_scope": ["screen_stocks"]}],
                },
            }

    app._pending_workflows = {"wf_pending": SimpleNamespace(runtime=Runtime())}
    log = _FakeLog()

    handled = WyckoffTUI._handle_workflow_control_text(app, "别这么拆，直接先扫候选", log)

    assert handled is True
    assert feedbacks == ["别这么拆，直接先扫候选"]
    assert any("改后脚本" in str(line) for line in log.lines)
    assert "已根据反馈更新 workflow" in str(log.lines[-1])


def test_pending_workflow_feedback_prefers_revision_over_approval_phrase():
    app = object.__new__(WyckoffTUI)
    feedbacks = []
    approved = []

    class Runtime:
        def revise_prepared_script(self, feedback):
            feedbacks.append(feedback)
            return {
                "run_id": "wf_pending",
                "workflow": "dynamic_task",
                "label": "动态任务",
                "plan": {"steps": [{"title": "形成攻防", "tool_scope": ["generate_strategy_decision"]}]},
            }

    app._pending_workflows = {"wf_pending": SimpleNamespace(runtime=Runtime())}
    app._approve_workflow = lambda run_id, log: approved.append(run_id)
    log = _FakeLog()

    handled = WyckoffTUI._handle_workflow_control_text(app, "好，但是不用研报，先给攻防", log)

    assert handled is True
    assert feedbacks == ["好，但是不用研报，先给攻防"]
    assert approved == []
    assert "已根据反馈更新 workflow" in str(log.lines[-1])


def test_pending_workflow_model_reply_approves_single_pending_workflow():
    app = object.__new__(WyckoffTUI)
    app._provider = _ReplyProvider('{"intent":"approve","confidence":0.92,"reason":"同意运行"}')
    approved = []

    class Runtime:
        run = SimpleNamespace(
            plan_payload=lambda: {
                "run_id": "wf_pending",
                "label": "动态任务",
                "steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}],
            }
        )

    app._pending_workflows = {"wf_pending": SimpleNamespace(runtime=Runtime())}
    app._approve_workflow = lambda run_id, log: approved.append(run_id)
    log = _FakeLog()

    handled = WyckoffTUI._handle_workflow_control_text(app, "这版没毛病，直接走", log)

    assert handled is True
    assert approved == [""]
    assert app._provider.chat_calls


def test_pending_workflow_model_reply_revises_single_pending_workflow():
    app = object.__new__(WyckoffTUI)
    app._provider = _ReplyProvider('{"intent":"revise","confidence":0.88,"reason":"用户要缩小范围"}')
    feedbacks = []

    class Runtime:
        run = SimpleNamespace(plan_payload=lambda: {"run_id": "wf_pending", "label": "动态任务", "steps": []})

        def revise_prepared_script(self, feedback):
            feedbacks.append(feedback)
            return {
                "run_id": "wf_pending",
                "workflow": "dynamic_task",
                "label": "动态任务",
                "plan": {"steps": [{"title": "只扫候选", "tool_scope": ["screen_stocks"]}]},
            }

    app._pending_workflows = {"wf_pending": SimpleNamespace(runtime=Runtime())}
    log = _FakeLog()

    handled = WyckoffTUI._handle_workflow_control_text(app, "这版太重了，聚焦候选池就好", log)

    assert handled is True
    assert feedbacks == ["这版太重了，聚焦候选池就好"]
    assert "已根据反馈更新 workflow" in str(log.lines[-1])


def test_pending_workflow_model_reply_leaves_chat_to_agent():
    app = object.__new__(WyckoffTUI)
    app._provider = _ReplyProvider('{"intent":"chat","confidence":0.9,"reason":"只是提问"}')

    class Runtime:
        run = SimpleNamespace(plan_payload=lambda: {"run_id": "wf_pending", "label": "动态任务", "steps": []})

    app._pending_workflows = {"wf_pending": SimpleNamespace(runtime=Runtime())}
    log = _FakeLog()

    handled = WyckoffTUI._handle_workflow_control_text(app, "解释一下 workflow 是什么", log)

    assert handled is False


def test_background_task_summary_uses_tool_result_preview_for_large_screen_result(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {
        "ok": True,
        "selection_brief": {
            "status": "ready_for_ai_review",
            "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
            "best_codes": ["300750"],
        },
        "trigger_groups": {"huge": [{"code": f"{idx:06d}", "blob": "x" * 200} for idx in range(80)]},
    }

    summary = _background_task_summary("screen_stocks", "bg_screen", result, max_chars=1000)

    assert "result_ref:" in summary
    assert '"selection_brief": {"status": "ready_for_ai_review"' in summary
    assert "本轮首选可进入 AI 研报复核: 300750 宁德时代" in summary
    assert '"trigger_groups"' not in summary
    stored = list((tmp_path / "tool-results").glob("*.json"))
    assert len(stored) == 1


def test_system_notification_queue_item_is_not_user_chat_role():
    item = _system_notification_queue_item("后台任务完成")
    message = {"role": "user", "content": item["content"], "_system_notification": True}

    assert item == {"type": "system_notification", "content": "后台任务完成"}
    assert _is_system_notification_message(message)
    assert not _is_system_notification_message({"role": "user", "content": "后台任务完成"})
    assert _chatlog_role_for_turn(system_notification=True) == "system"
    assert _chatlog_role_for_turn(system_notification=False) == "user"
