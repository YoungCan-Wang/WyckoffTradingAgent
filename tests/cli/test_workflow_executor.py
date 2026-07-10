from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

from cli.tools import TOOL_SCHEMAS
from cli.workflows.control import WorkflowControl
from cli.workflows.dispatch import build_turn_runtime
from cli.workflows.executor import (
    WorkflowExecutor,
    _candidate_conclusion_from_handoff,
    _candidate_conclusions_from_handoff,
    _ensure_candidate_delivery,
    _fallback_handoff_lines,
    _fallback_summary,
    _phase_batches,
    _step_context,
    _synthesis_handoff_summary,
    _synthesis_prompt,
    _workflow_handoff_state,
)
from cli.workflows.models import WorkflowContext, WorkflowRun, WorkflowStep
from cli.workflows.planner import (
    _ADAPTATION_SYSTEM_PROMPT,
    _PLAN_SYSTEM_PROMPT,
    _REPAIR_SYSTEM_PROMPT,
    MAX_WORKFLOW_STEPS,
    _adaptation_handoff_summary,
    plan_workflow,
)
from cli.workflows.resume import (
    build_chat_resume_prompt,
    build_recent_workflow_context,
    build_resume_prompt,
    is_recent_workflow_followup,
    should_include_recent_workflow_context,
)
from cli.workflows.router import WORKFLOWS, route_workflow
from cli.workflows.store import get_workflow_run, load_workflow_events
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry

_PLAN_JSON = """{
  "title": "持仓复盘",
  "rationale": "先让 sub-agent 读取持仓，再汇总风险动作。",
  "phases": [
    {
      "id": "review",
      "title": "持仓检查",
      "tasks": [
        {
          "id": "read_positions",
          "title": "读取并诊断持仓",
          "agent": "analysis",
          "prompt": "读取用户持仓并输出风险摘要",
          "context": "只看核心仓位，不处理现金。"
        }
      ]
    }
  ],
  "synthesis_prompt": "汇总持仓风险和下一步动作。"
}"""

_PARALLEL_PLAN_JSON = """{
  "title": "并发复核",
  "rationale": "同一阶段并发收集两个视角。",
  "phases": [
    {
      "id": "fanout",
      "title": "并发检查",
      "tasks": [
        {
          "id": "research_view",
          "title": "研究视角",
          "agent": "research",
          "prompt": "从市场数据视角复核"
        },
        {
          "id": "analysis_view",
          "title": "结构视角",
          "agent": "analysis",
          "prompt": "从量价结构视角复核"
        }
      ]
    }
  ],
  "synthesis_prompt": "合并两个视角。"
}"""

_DEPENDENT_PLAN_JSON = """{
  "title": "依赖复核",
  "rationale": "同一阶段内按模型声明的 task 依赖分批执行。",
  "phases": [
    {
      "id": "review",
      "title": "复核阶段",
      "tasks": [
        {
          "id": "scan",
          "title": "扫描候选",
          "agent": "research",
          "prompt": "先扫描候选"
        },
        {
          "id": "risk_plan",
          "title": "攻防计划",
          "agent": "trading",
          "depends_on": ["scan"],
          "prompt": "基于扫描结果输出攻防计划"
        }
      ]
    }
  ],
  "synthesis_prompt": "按依赖顺序汇总。"
}"""

_OUT_OF_ORDER_DEPENDENT_PLAN_JSON = """{
  "title": "乱序依赖复核",
  "phases": [
    {
      "id": "review",
      "tasks": [
        {
          "id": "risk_plan",
          "title": "攻防计划",
          "agent": "trading",
          "depends_on": ["scan"],
          "prompt": "基于扫描结果输出攻防计划"
        },
        {
          "id": "scan",
          "title": "扫描候选",
          "agent": "research",
          "prompt": "先扫描候选"
        }
      ]
    }
  ],
  "synthesis_prompt": "按依赖顺序汇总。"
}"""

_EDITED_PLAN_JSON = """{
  "title": "编辑后脚本",
  "rationale": "用户修改了脚本。",
  "phases": [
    {
      "id": "edited",
      "title": "编辑阶段",
      "tasks": [
        {
          "id": "edited_task",
          "title": "编辑任务",
          "agent": "analysis",
          "prompt": "执行编辑后的任务"
        }
      ]
    }
  ],
  "synthesis_prompt": "汇总编辑后的任务。"
}"""

_REVISED_PLAN_JSON = """{
  "title": "按反馈修订脚本",
  "rationale": "用户要求先收窄到真实持仓事实。",
  "phases": [
    {
      "id": "revised",
      "title": "修订阶段",
      "tasks": [
        {
          "id": "read_positions_only",
          "title": "只读取持仓",
          "tools": ["portfolio"],
          "prompt": "读取用户真实持仓并输出摘要。",
          "risk_guard": "不写入交易。"
        }
      ]
    }
  ],
  "synthesis_prompt": "基于持仓事实给出简洁复盘。"
}"""

_SCOPED_TOOL_PLAN_JSON = """{
  "title": "收窄工具复核",
  "phases": [
    {
      "id": "review",
      "tasks": [
        {
          "id": "read_positions",
          "title": "只读取持仓",
          "tools": ["portfolio"],
          "prompt": "读取持仓并输出摘要"
        }
      ]
    }
  ],
  "synthesis_prompt": "汇总持仓摘要。"
}"""


class ParallelResultProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.synthesis_prompt = ""
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "ParallelResultProvider"

    def chat_stream(self, messages, _tools, _system_prompt=""):
        content = messages[0]["content"]
        with self._lock:
            self.calls.append(content)
        if content.startswith("请基于以下动态 workflow 执行结果"):
            self.synthesis_prompt = content
            yield {"type": "text_delta", "text": "两个视角已按脚本顺序合并。"}
            return
        if "从市场数据视角复核" in content:
            time.sleep(0.05)
            yield {"type": "text_delta", "text": "研究视角完成。"}
            return
        if "从量价结构视角复核" in content:
            time.sleep(0.01)
            yield {"type": "text_delta", "text": "结构视角完成。"}
            return
        yield {"type": "text_delta", "text": "未知任务。"}


class RoutingScriptedProvider(ScriptedProvider):
    def __init__(self, decision: str, rounds: list):
        super().__init__(rounds)
        self.decision = decision
        self.chat_calls: list[dict] = []

    def chat(self, messages, tools, system_prompt=""):
        self.chat_calls.append(
            {"messages": deepcopy(messages), "tools": deepcopy(tools), "system_prompt": system_prompt}
        )
        return {"type": "text", "text": self.decision}


def _reset_local_db(local_db) -> None:
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


def test_workflow_planner_prompt_keeps_task_semantics_model_authored():
    assert "自然语言语义、上下文恢复和任务拆分由你完成" in _PLAN_SYSTEM_PROMPT
    assert "合理推断" in _PLAN_SYSTEM_PROMPT
    assert "按最高置信假设生成可执行 task" in _PLAN_SYSTEM_PROMPT
    assert '"adaptive": true' in _PLAN_SYSTEM_PROMPT
    assert "runtime.adaptive" in _PLAN_SYSTEM_PROMPT
    assert "措辞恢复" not in _PLAN_SYSTEM_PROMPT
    assert "不要单独生成元任务" not in _PLAN_SYSTEM_PROMPT
    assert "错字" not in _PLAN_SYSTEM_PROMPT
    assert "错别字" in _PLAN_SYSTEM_PROMPT
    assert "不需要选择内部执行角色" in _PLAN_SYSTEM_PROMPT
    assert "不要填写 agent/role" in _PLAN_SYSTEM_PROMPT
    assert "可用 agent" not in _PLAN_SYSTEM_PROMPT
    assert '"agent": "可选，research|analysis|trading"' not in _PLAN_SYSTEM_PROMPT
    assert "rationale" in _PLAN_SYSTEM_PROMPT
    assert "success_criteria" in _PLAN_SYSTEM_PROMPT
    assert "risk_guard" in _PLAN_SYSTEM_PROMPT
    assert "工具是脚本契约" in _PLAN_SYSTEM_PROMPT
    assert "精确工具名填写 tools" in _PLAN_SYSTEM_PROMPT
    assert "无工具占位 task" in _PLAN_SYSTEM_PROMPT
    assert "runtime 会跨 phase 按依赖顺序切批执行" in _PLAN_SYSTEM_PROMPT
    assert "depends_on 指向提供这些事实的 task id" in _PLAN_SYSTEM_PROMPT
    assert "depends_on 指向前序 task" not in _PLAN_SYSTEM_PROMPT
    assert "必须出现 screen_stocks" in _PLAN_SYSTEM_PROMPT
    assert "generate_strategy_decision" in _PLAN_SYSTEM_PROMPT


def test_workflow_adaptation_prompt_rewrites_remaining_tasks_only():
    assert "运行中续写器" in _ADAPTATION_SYSTEM_PROMPT
    assert "尚未执行的后续任务" in _ADAPTATION_SYSTEM_PROMPT
    assert "不要重复已经完成的 task" in _ADAPTATION_SYSTEM_PROMPT
    assert '{"complete": true' in _ADAPTATION_SYSTEM_PROMPT
    assert "真实结果已经足够完成用户目标" in _ADAPTATION_SYSTEM_PROMPT


def test_model_generated_workflow_ignores_legacy_agent_and_tool_aliases():
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "text_delta",
                    "text": json.dumps(
                        {
                            "title": "今日选股",
                            "phases": [
                                {
                                    "id": "screen",
                                    "tasks": [
                                        {
                                            "id": "scan",
                                            "title": "扫描候选",
                                            "agent": "analysis",
                                            "tools": ["选股", "screen_stocks"],
                                            "prompt": "扫描今日候选并保留风险原因",
                                        }
                                    ],
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        ]
    )

    run = plan_workflow(
        "帮我完整做一遍今天的 A 股选股",
        context=WORKFLOWS["dynamic_task"],
        provider=provider,
        tools=StubToolRegistry(),
    )

    assert run.script["runtime"]["planner"] == "model_script"
    assert run.steps[0].agent == "task"
    assert run.steps[0].tools == ()
    assert run.steps[0].tool_scope == ("screen_stocks",)


def test_model_generated_workflow_script_is_trimmed_before_persistence():
    tasks = [
        {"id": f"task_{index}", "title": f"复核候选 {index}", "prompt": f"复核候选 {index}"}
        for index in range(MAX_WORKFLOW_STEPS + 3)
    ]
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "text_delta",
                    "text": json.dumps(
                        {"title": "过长选股计划", "phases": [{"id": "review", "tasks": tasks}]},
                        ensure_ascii=False,
                    ),
                }
            ]
        ]
    )

    run = plan_workflow(
        "帮我选出好股票",
        context=WORKFLOWS["dynamic_task"],
        provider=provider,
        tools=StubToolRegistry(),
    )

    runtime = run.script["runtime"]
    assert len(run.steps) == MAX_WORKFLOW_STEPS
    assert len(run.script["phases"][0]["tasks"]) == MAX_WORKFLOW_STEPS
    assert run.steps[-1].step_id == f"task_{MAX_WORKFLOW_STEPS - 1}"
    assert runtime["planner"] == "model_script"
    assert runtime["step_limit"] == MAX_WORKFLOW_STEPS
    assert runtime["original_step_count"] == MAX_WORKFLOW_STEPS + 3
    assert runtime["truncated_step_count"] == 3


def test_workflow_planner_ignores_agent_aliases_and_keeps_steps_field():
    run = plan_workflow(
        "用 workflow 做完整选股和攻防计划",
        context=route_workflow("用 workflow 做完整选股和攻防计划"),
        workflow_script={
            "title": "选股攻防",
            "phases": [
                {
                    "id": "fanout",
                    "steps": [
                        {"id": "scan", "title": "扫描候选", "agent": "研究", "prompt": "筛选候选股票"},
                        {
                            "id": "risk_plan",
                            "title": "攻防计划",
                            "agent": "delegate_to_trading",
                            "dependsOn": [{"id": "scan"}],
                            "instruction": "输出观察、买入和失效条件",
                        },
                    ],
                }
            ],
        },
    )

    assert [step.agent for step in run.steps] == ["task", "task"]
    assert [step.tools for step in run.steps] == [(), ()]
    assert run.steps[1].prompt == "输出观察、买入和失效条件"
    assert run.steps[1].depends_on == ("scan",)
    assert run.script["runtime"]["planner"] == "stored_script"


def test_workflow_planner_preserves_task_outcome_metadata():
    run = plan_workflow(
        "用 workflow 做今日选股",
        context=route_workflow("用 workflow 做今日选股"),
        workflow_script={
            "title": "带边界的脚本",
            "tasks": [
                {
                    "id": "scan",
                    "title": "扫描候选",
                    "tools": ["screen_stocks"],
                    "prompt": "扫描今日候选并输出风险状态",
                    "why": "先缩小候选池，再决定是否需要研报",
                    "done_when": ["返回候选代码", "说明不能直接买入的边界"],
                    "guardrails": {"write": "不写入推荐或持仓"},
                }
            ],
        },
    )

    step = run.steps[0]
    payload = step.to_dict()

    assert step.rationale == "先缩小候选池，再决定是否需要研报"
    assert step.success_criteria == "返回候选代码；说明不能直接买入的边界"
    assert step.risk_guard == "write: 不写入推荐或持仓"
    assert payload["rationale"] == step.rationale
    assert payload["success_criteria"] == step.success_criteria
    assert payload["risk_guard"] == step.risk_guard


def test_workflow_planner_accepts_top_level_task_script():
    run = plan_workflow(
        "用 workflow 诊断 300750",
        context=route_workflow("用 workflow 诊断 300750"),
        workflow_script={
            "title": "单股诊断",
            "steps": [{"id": "structure", "role": "分析", "task": "诊断 300750 的量价结构"}],
        },
    )

    assert len(run.steps) == 1
    assert run.steps[0].agent == "task"
    assert run.steps[0].phase == "top_level"
    assert run.steps[0].prompt == "诊断 300750 的量价结构"


def test_workflow_planner_accepts_keyed_phase_and_task_objects():
    run = plan_workflow(
        "用 workflow 做完整选股和攻防计划",
        context=route_workflow("用 workflow 做完整选股和攻防计划"),
        workflow_script={
            "title": "对象式脚本",
            "phases": {
                "collect": {
                    "tasks": {
                        "scan": {"title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选"},
                        "market": {"title": "读取水温", "tools": ["get_market_overview"], "prompt": "读取市场水温"},
                    }
                },
                "decision": {
                    "tasks": {
                        "plan": {
                            "title": "攻防计划",
                            "tools": ["generate_strategy_decision"],
                            "after": ["scan", "market"],
                            "prompt": "整合候选和市场水温输出攻防计划",
                        }
                    }
                },
            },
        },
    )

    assert [step.step_id for step in run.steps] == ["scan", "market", "plan"]
    assert [step.phase for step in run.steps] == ["collect", "collect", "decision"]
    assert [step.tool_scope for step in run.steps] == [
        ("screen_stocks",),
        ("get_market_overview",),
        ("generate_strategy_decision",),
    ]
    assert run.steps[2].depends_on == ("scan", "market")


def test_workflow_planner_splits_inline_tool_and_dependency_fields():
    run = plan_workflow(
        "用 workflow 做候选扫描和市场水温后再决策",
        context=route_workflow("用 workflow 做候选扫描和市场水温后再决策"),
        workflow_script={
            "title": "逗号字段脚本",
            "tasks": [
                {
                    "id": "scan",
                    "title": "扫描候选",
                    "tools": "screen_stocks，get_market_overview",
                    "prompt": "扫描候选并读取市场水温",
                },
                {
                    "id": "plan",
                    "title": "生成攻防计划",
                    "tool": "generate_strategy_decision",
                    "depends_on": "scan, market",
                    "prompt": "基于候选和市场环境生成攻防计划",
                },
            ],
        },
    )

    assert run.steps[0].tool_scope == ("screen_stocks", "get_market_overview")
    assert run.steps[1].tool_scope == ("generate_strategy_decision",)
    assert run.steps[1].depends_on == ("scan", "market")


def test_workflow_planner_keeps_subtasks_without_agent_fields():
    run = plan_workflow(
        "帮我做今日选股",
        context=WORKFLOWS["stock_screen"],
        workflow_script={
            "title": "自然拆分选股",
            "phases": [
                {
                    "id": "screen",
                    "subtasks": [
                        {"id": "collect", "title": "收集候选", "prompt": "跑全市场候选扫描"},
                        {"id": "review", "title": "复核结构", "after": "collect", "instructions": "复核候选结构"},
                    ],
                }
            ],
        },
    )

    assert [step.step_id for step in run.steps] == ["collect", "review"]
    assert [step.agent for step in run.steps] == ["task", "task"]
    assert [step.tools for step in run.steps] == [(), ()]
    assert run.steps[1].depends_on == ("collect",)
    assert run.steps[1].prompt == "复核候选结构"


def test_workflow_planner_fallback_builds_stock_selection_tool_chain():
    run = plan_workflow(
        "用 workflow 完整做一遍今天的 A 股选股，给出候选、理由和买卖计划",
        context=route_workflow("用 workflow 完整做一遍今天的 A 股选股，给出候选、理由和买卖计划"),
    )

    assert [step.step_id for step in run.steps] == ["scan_candidates", "diagnose_candidates", "strategy_decision"]
    assert [step.tool_scope for step in run.steps] == [
        ("screen_stocks",),
        ("analyze_stock",),
        ("generate_strategy_decision",),
    ]
    assert run.steps[1].depends_on == ("scan_candidates",)
    assert run.steps[2].depends_on == ("diagnose_candidates", "scan_candidates")
    assert "用户原文" in run.steps[0].prompt
    assert "A 股选股" in run.steps[0].prompt
    assert run.script["runtime"] == {
        "planner": "fallback_script",
        "fallback_reason": "provider unavailable",
        "fallback_kind": "stock_selection",
    }


def test_workflow_planner_stock_fallback_adds_report_when_requested():
    run = plan_workflow(
        "选出好股票，给出研报和攻防计划",
        context=WORKFLOWS["dynamic_task"],
    )

    assert [step.step_id for step in run.steps] == [
        "scan_candidates",
        "diagnose_candidates",
        "ai_report",
        "strategy_decision",
    ]
    assert [step.tool_scope for step in run.steps] == [
        ("screen_stocks",),
        ("analyze_stock",),
        ("generate_ai_report",),
        ("generate_strategy_decision",),
    ]
    assert run.steps[1].depends_on == ("scan_candidates",)
    assert run.steps[2].depends_on == ("diagnose_candidates", "scan_candidates")
    assert run.steps[3].depends_on == ("ai_report",)
    assert run.script["runtime"]["fallback_kind"] == "stock_selection"


def test_workflow_planner_short_stock_fallback_forms_action_boundaries():
    run = plan_workflow(
        "帮我选出好股票",
        context=WORKFLOWS["dynamic_task"],
    )

    assert [step.step_id for step in run.steps] == ["scan_candidates", "diagnose_candidates", "strategy_decision"]
    assert [step.tool_scope for step in run.steps] == [
        ("screen_stocks",),
        ("analyze_stock",),
        ("generate_strategy_decision",),
    ]
    assert run.steps[1].depends_on == ("scan_candidates",)
    assert run.steps[2].depends_on == ("diagnose_candidates", "scan_candidates")
    assert run.script["runtime"]["fallback_kind"] == "stock_selection"


def test_workflow_planner_stock_fallback_handles_tracking_direction_wording():
    for text in (
        "研究一下今天哪些方向值得重点跟踪",
        "今天哪些方向值得关注",
        "今天有什么值得看的方向",
    ):
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert [step.step_id for step in run.steps] == ["scan_candidates", "diagnose_candidates", "strategy_decision"]
        assert [step.tool_scope for step in run.steps] == [
            ("screen_stocks",),
            ("analyze_stock",),
            ("generate_strategy_decision",),
        ]
        assert run.steps[0].args_hint == "board: all"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all"}
        assert run.script["runtime"]["fallback_kind"] == "stock_selection"


def test_workflow_planner_stock_fallback_handles_temporal_buy_wording():
    for text in ("今天买啥", "明天买什么", "现在能买啥", "尾盘能买什么"):
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert [step.step_id for step in run.steps] == ["scan_candidates", "diagnose_candidates", "strategy_decision"]
        assert [step.tool_scope for step in run.steps] == [
            ("screen_stocks",),
            ("analyze_stock",),
            ("generate_strategy_decision",),
        ]
        assert run.steps[0].args_hint == "board: all"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all"}
        assert run.script["runtime"]["fallback_kind"] == "stock_selection"


def test_workflow_planner_stock_fallback_handles_watch_direction_wording():
    for text in ("明天看什么方向", "今天关注什么板块", "盘中看啥机会", "尾盘关注啥"):
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert [step.step_id for step in run.steps] == ["scan_candidates", "diagnose_candidates", "strategy_decision"]
        assert [step.tool_scope for step in run.steps] == [
            ("screen_stocks",),
            ("analyze_stock",),
            ("generate_strategy_decision",),
        ]
        assert run.steps[0].args_hint == "board: all"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all"}
        assert run.script["runtime"]["fallback_kind"] == "stock_selection"


def test_workflow_planner_stock_fallback_handles_candidate_ticket_wording():
    for text in ("今天有什么票", "给我几只票", "今天给我几个标的", "给我几个股票", "今天有啥候选"):
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert [step.step_id for step in run.steps] == ["scan_candidates", "diagnose_candidates", "strategy_decision"]
        assert [step.tool_scope for step in run.steps] == [
            ("screen_stocks",),
            ("analyze_stock",),
            ("generate_strategy_decision",),
        ]
        assert run.steps[0].args_hint == "board: all"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all"}
        assert run.script["runtime"]["fallback_kind"] == "stock_selection"


def test_workflow_planner_stock_fallback_passes_defensive_quality_args():
    for text in ("今天给我几只稳一点的票", "今天低风险股票有哪些", "今天找几只波动小的票", "别太激进，给我几个标的"):
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert run.steps[0].tool_scope == ("screen_stocks",)
        assert run.steps[0].args_hint == "board: all；style: quality"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "style": "quality"}


def test_workflow_planner_stock_fallback_passes_fundamental_quality_args():
    for text in (
        "今天找几只基本面好的票",
        "今天给我几只财务好的股票",
        "今天业绩好的股票有哪些",
        "今天盈利能力强的标的",
        "今天ROE高的票",
        "今天找几只现金流好的票",
    ):
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert run.steps[0].tool_scope == ("screen_stocks",)
        assert run.steps[0].args_hint == "board: all；style: quality；financial_metrics: true"
        assert run.script["phases"][0]["tasks"][0]["args"] == {
            "board": "all",
            "style": "quality",
            "financial_metrics": "true",
        }


def test_workflow_planner_stock_fallback_passes_dividend_value_quality_args():
    cases = (
        ("今天高股息红利票有哪些", "红利低波"),
        ("今天价值蓝筹标的有哪些", "价值蓝筹"),
        ("今天找几只大盘蓝筹", "价值蓝筹"),
    )
    for text, theme in cases:
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert run.steps[0].tool_scope == ("screen_stocks",)
        assert run.steps[0].args_hint == f"board: all；style: quality；financial_metrics: true；theme: {theme}"
        assert run.script["phases"][0]["tasks"][0]["args"] == {
            "board": "all",
            "style": "quality",
            "financial_metrics": "true",
            "theme": theme,
        }


def test_workflow_planner_stock_fallback_passes_style_args():
    run = plan_workflow(
        "今天帮我找几只强势低吸标的，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.steps[0].args_hint == "board: all；style: trend,pullback"
    assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "style": "trend,pullback"}


def test_workflow_planner_stock_fallback_tolerates_style_typos_and_punctuation():
    cases = (
        "今天，帮我找几只强事底吸标的，给下一步",
        "今天帮我找几只趋式回调标的，给下一步",
    )
    for text in cases:
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert run.steps[0].tool_scope == ("screen_stocks",)
        assert run.steps[0].args_hint == "board: all；style: trend,pullback"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "style": "trend,pullback"}


def test_workflow_planner_stock_fallback_passes_anti_chase_style_args():
    cases = (
        ("今天别追高，找几只票", "pullback"),
        ("今天不要高位票，找低位机会", "pullback"),
        ("今天找几只低位刚启动的标的", "trend,pullback"),
        ("今天找几只性价比高的票", "quality"),
    )
    for text, style in cases:
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert run.steps[0].tool_scope == ("screen_stocks",)
        assert run.steps[0].args_hint == f"board: all；style: {style}"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "style": style}


def test_workflow_planner_stock_fallback_passes_liquidity_bluechip_elasticity_args():
    quality_cases = ("今天找几只成交活跃的票", "今天找几只流动性好的标的", "今天找几只白马股")
    for text in quality_cases:
        run = plan_workflow(text, context=WORKFLOWS["dynamic_task"])

        assert run.steps[0].tool_scope == ("screen_stocks",)
        assert run.steps[0].args_hint == "board: all；style: quality"
        assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "style": "quality"}

    elasticity = plan_workflow("今天找几只小盘弹性票", context=WORKFLOWS["dynamic_task"])
    assert elasticity.steps[0].args_hint == "board: all；style: trend"
    assert elasticity.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "style": "trend"}


def test_workflow_planner_stock_fallback_passes_board_and_style_args():
    run = plan_workflow(
        "今天帮我筛创业板强势低吸标的，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.steps[0].args_hint == "board: chinext；style: trend,pullback"
    assert run.script["phases"][0]["tasks"][0]["args"] == {
        "board": "chinext",
        "style": "trend,pullback",
    }


def test_workflow_planner_stock_fallback_passes_combined_board_phrases():
    run = plan_workflow(
        "今天帮我筛主板和科创板强势标的，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.steps[0].args_hint == "board: main_chinext_star；style: trend"
    assert run.script["phases"][0]["tasks"][0]["args"] == {
        "board": "main_chinext_star",
        "style": "trend",
    }


def test_workflow_planner_stock_fallback_passes_non_bse_a_share_phrases():
    run = plan_workflow(
        "今天沪深A全量扫描强势标的，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.steps[0].args_hint == "board: main_chinext_star；style: trend；limit: 0"
    assert run.script["phases"][0]["tasks"][0]["args"] == {
        "board": "main_chinext_star",
        "style": "trend",
        "limit": "0",
    }


def test_workflow_planner_stock_fallback_passes_theme_args():
    run = plan_workflow(
        "今天机器人机会有哪些，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )
    concept = plan_workflow(
        "机器人是什么",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.steps[0].args_hint == "board: all；theme: 机器人"
    assert run.script["phases"][0]["tasks"][0]["args"] == {"board": "all", "theme": "机器人"}
    assert run.script["runtime"]["fallback_kind"] == "stock_selection"
    assert "fallback_kind" not in concept.script["runtime"]


def test_workflow_planner_stock_fallback_passes_theme_strength_args():
    run = plan_workflow(
        "今天机器人龙头有哪些，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.steps[0].args_hint == "board: all；style: trend；theme: 机器人"
    assert run.script["phases"][0]["tasks"][0]["args"] == {
        "board": "all",
        "style": "trend",
        "theme": "机器人",
    }


def test_workflow_planner_stock_fallback_handles_etf_screening_wording():
    run = plan_workflow(
        "今天帮我筛ETF机会，给下一步",
        context=WORKFLOWS["dynamic_task"],
    )
    concept = plan_workflow(
        "ETF是什么",
        context=WORKFLOWS["dynamic_task"],
    )

    assert run.steps[0].tool_scope == ("screen_stocks",)
    assert run.script["runtime"]["fallback_kind"] == "stock_selection"
    assert "fallback_kind" not in concept.script["runtime"]
    assert concept.steps[0].tool_scope == ()


def test_workflow_planner_stock_method_question_keeps_generic_fallback():
    run = plan_workflow(
        "用 workflow 解释怎么选出好股票",
        context=route_workflow("用 workflow 解释怎么选出好股票"),
    )

    assert len(run.steps) == 1
    assert run.steps[0].step_id == "agent_task"
    assert run.steps[0].tool_scope == ()
    assert run.script["runtime"] == {
        "planner": "fallback_script",
        "fallback_reason": "provider unavailable",
    }


def test_workflow_planner_fallback_keeps_typo_like_task_model_authored():
    run = plan_workflow(
        "用 workflow 给我做磁场诊断",
        context=route_workflow("用 workflow 给我做磁场诊断"),
    )

    assert len(run.steps) == 1
    assert run.script["title"] == "用 workflow 给我做磁场诊断"
    assert run.steps[0].agent == "task"
    assert run.steps[0].tools == ()
    assert run.steps[0].tool_scope == ()
    assert run.steps[0].title == "用 workflow 给我做磁场诊断"
    assert "sub-agent" not in run.steps[0].title
    assert "自然语言语义" in run.steps[0].prompt
    assert "常见别字" not in run.steps[0].prompt
    assert "磁场诊断" in run.steps[0].prompt
    assert run.script["runtime"]["planner"] == "fallback_script"


def test_workflow_planner_treats_tool_named_tasks_as_task_scopes():
    run = plan_workflow(
        "用 workflow 做选股和攻防计划",
        context=route_workflow("用 workflow 做选股和攻防计划"),
        workflow_script={
            "title": "工具式脚本",
            "steps": [
                {"id": "scan", "title": "扫描候选", "tool": "screen_stocks", "prompt": "扫描今日候选"},
                {
                    "id": "plan",
                    "title": "攻防计划",
                    "tool": {"name": "generate_strategy_decision"},
                    "after": "scan",
                    "prompt": "输出候选攻防计划",
                },
            ],
        },
    )

    assert [step.agent for step in run.steps] == ["task", "task"]
    assert [step.tools for step in run.steps] == [(), ()]
    assert [step.tool_scope for step in run.steps] == [("screen_stocks",), ("generate_strategy_decision",)]
    assert run.steps[1].depends_on == ("scan",)


def test_workflow_step_context_includes_outcome_metadata():
    step = WorkflowStep(
        step_id="scan",
        title="扫描候选",
        phase="collect",
        rationale="先缩小候选池",
        success_criteria="输出候选和风险边界",
        risk_guard="不写入推荐或持仓",
        context="只读运行",
        args_hint="limit: 20",
    )

    context = _step_context(step, [])

    assert "task rationale:\n先缩小候选池" in context
    assert "success criteria:\n输出候选和风险边界" in context
    assert "risk guard:\n不写入推荐或持仓" in context
    assert "task context:\n只读运行" in context
    assert "tool args hint:\nlimit: 20" in context


def test_workflow_step_context_surfaces_prior_candidate_handoff_summary():
    step = WorkflowStep(
        step_id="decision",
        title="形成攻防",
        phase="decision",
        depends_on=("scan",),
        tool_scope=("generate_strategy_decision",),
    )
    handoff = {
        "last_screen_result": {
            "symbols_for_report": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "action_status": "ready_for_ai_review",
                    "candidate_shadow_score": 92.0,
                    "candidate_shadow_grade": "S",
                    "risk_adjusted_quality_score": 87.0,
                    "quality_factors": ["事件主线"],
                    "risk_factors": ["未来窗口标签尚未成熟"],
                    "next_step": "生成 AI 研报",
                }
            ],
            "candidate_guard_summary": {"candidates": [{"code": "300750", "reason": "只能研报复核，不可直接买入"}]},
        }
    }
    prior_results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {"status": "completed", "result": "候选扫描完成。", "handoff_state": handoff},
        }
    ]

    context = _step_context(step, prior_results)

    assert "前序候选 handoff 摘要:" in context
    assert "- 候选结论:" in context
    assert "300750 宁德时代" in context
    assert "护栏: 只能研报复核，不可直接买入" in context
    assert context.index("前序候选 handoff 摘要:") < context.index("前序 agent 结果:")
    assert '"handoff_state"' in context


def test_workflow_step_context_derives_tool_args_from_handoff_next_tool():
    step = WorkflowStep(
        step_id="diagnose",
        title="诊断首选候选",
        phase="diagnose",
        depends_on=("scan",),
        tool_scope=("analyze_stock",),
    )
    prior_results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "status": "completed",
                "result": "扫描完成。",
                "handoff_state": {
                    "last_screen_result": {
                        "next_tool": {
                            "tool": "analyze_stock",
                            "args": {"code": "002326", "mode": "diagnose"},
                            "reason": "观察候选先做个股结构诊断",
                        },
                        "diagnosis_targets": [
                            {
                                "tool": "analyze_stock",
                                "args": {"code": "000566", "mode": "diagnose"},
                            },
                            {
                                "tool": "analyze_stock",
                                "args": {"code": "002628", "mode": "diagnose"},
                            },
                        ],
                    }
                },
            },
        }
    ]

    context = _step_context(step, prior_results)

    assert "tool args hint:" in context
    args_block = context.split("tool args hint:\n", 1)[1].split("\n\n", 1)[0]
    assert json.loads(args_block) == {
        "tool": "analyze_stock",
        "call_each": True,
        "instruction": "按 targets 顺序逐个调用 tool，每个 targets[].args 调用一次；不要把 call_each/targets 包装成工具参数。",
        "targets": [
            {"args": {"code": "002326", "mode": "diagnose"}},
            {"args": {"code": "000566", "mode": "diagnose"}},
            {"args": {"code": "002628", "mode": "diagnose"}},
        ],
    }


def test_workflow_step_context_keeps_explicit_args_hint_over_handoff():
    step = WorkflowStep(
        step_id="diagnose",
        title="诊断指定候选",
        phase="diagnose",
        args_hint="code: 300750；mode: diagnose",
        tool_scope=("analyze_stock",),
    )
    prior_results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "next_tool": {"tool": "analyze_stock", "args": {"code": "002326", "mode": "diagnose"}}
                    }
                }
            },
        }
    ]

    context = _step_context(step, prior_results)

    assert "code: 300750；mode: diagnose" in context
    args_block = context.split("tool args hint:\n", 1)[1].split("\n\n", 1)[0]
    assert args_block == "code: 300750；mode: diagnose"


def test_workflow_handoff_state_compacts_candidate_context():
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "scan_scope": {"source": "screen_stocks"},
                "style_preference": {"raw": "trend", "styles": ["trend"]},
                "theme_preference": {"raw": "机器人", "theme": "机器人"},
                "preference_match": {"style": "miss", "theme": "miss"},
                "theme_context": {
                    "event_mainlines": "机器人 0.82/爆发",
                    "today_activity": "机器人 0.76/活跃",
                    "theme_radar": "人形机器人 0.78/confirmed",
                    "theme_radar_source": "current",
                    "hot_concepts": ["机器人", "灵巧手", "滚柱丝杠", "电子皮肤", "控制系统", "减速器", "extra"],
                },
                "selection_brief": {"status": "ready_for_ai_review", "best_codes": ["300750"]},
                "strategy_policy": {
                    "dynamic_mode": "shadow",
                    "execution_policy": "shadow",
                    "policy_weight_active_scope": "尾盘+漏斗shadow",
                    "selection_action_count": 1,
                    "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
                    "attribution_signal_weights": {"lps": 0.5, "trend_pullback": 0.75},
                    "formal_dynamic_allowed": False,
                    "next_action": "manual_review_dynamic_on",
                },
                "next_action": "首选候选已通过市场闸门，可进入 AI 研报复核",
                "next_tool": {"tool": "generate_ai_report", "args": {"stock_codes": ["300750"]}},
                "diagnosis_targets": [
                    {
                        "tool": "analyze_stock",
                        "args": {"code": "000013", "mode": "diagnose"},
                        "reason": "观察候选先做个股结构诊断",
                    }
                ],
                "action_plan": {
                    "candidate_action": "generate_ai_report",
                    "new_buy_allowed": False,
                    "ai_review_allowed": True,
                    "trade_readiness": "research_only",
                    "quality_gate": {
                        "status": "blocked_by_quality_gate",
                        "reason": "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00",
                    },
                    "review_targets": {"codes": ["300750"], "tool": "generate_ai_report"},
                    "diagnosis_targets": [
                        {
                            "tool": "analyze_stock",
                            "args": {"code": "000013", "mode": "diagnose"},
                            "reason": "观察候选先做个股结构诊断",
                        }
                    ],
                },
                "symbols_for_report": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "tag": "推荐评估候选",
                        "profile": "趋势线 / 主升阶段 / 触发:SOS+EVR",
                        "track": "Trend",
                        "stage": "Markup",
                        "triggers": ["sos", "evr", "spring", "lps", "compression", "extra"],
                        "candidate_lane": "mainline",
                        "entry_type": "launchpad",
                        "strategic_theme": "机器人",
                        "theme_score": 0.72,
                        "theme_source": "ths_hot_event",
                        "theme_event_id": "evt-robot",
                        "theme_event_title": "特斯拉量产临近，机器人板块还能回归市场主线吗？",
                        "theme_event_reason": "灵巧手",
                        "style_match": True,
                        "style_match_score": 2,
                        "style_match_reasons": ["趋势偏好: 趋势线", "趋势偏好: 主升阶段"],
                        "theme_match": True,
                        "theme_match_score": 1,
                        "theme_match_reasons": ["主题偏好: 机器人"],
                        "selection_source": "recommendation_event_eval",
                        "source_type": "policy_selection",
                        "priority_rank": 1,
                        "priority_score": 91.2,
                        "shadow_score": 88.4,
                        "score": 12.5,
                        "selection_strategy": "candidate_shadow_then_score",
                        "recommend_date": "2026-06-30",
                        "is_ai_recommended": True,
                        "funnel_score": 89.5,
                        "recommend_count": 2,
                        "candidate_shadow_score": 92.0,
                        "candidate_shadow_grade": "S",
                        "candidate_quality_score": 92.0,
                        "risk_adjusted_quality_score": 87.0,
                        "entry_risk_penalty": 5.0,
                        "entry_quality_score": 84.0,
                        "entry_quality_grade": "A",
                        "entry_quality_risk_flags": ["短线涨幅偏快"],
                        "label_ready": False,
                        "label_status": "pending",
                        "rank_reason": "推荐评估候选#1；候选影子评级 S",
                        "quality_factors": ["高优先级研报候选"],
                        "risk_factors": ["不直接买入"],
                        "action_status": "ready_for_ai_review",
                        "next_step": "生成 AI 研报",
                        "entry_zone": [196.0, 202.0],
                        "stop_loss": 188.5,
                        "max_entry_price": 204.0,
                        "tape_condition": "放量站回5日线",
                        "invalidate_condition": "跌破188.5取消交易",
                    }
                ],
                "watch_candidates": [
                    {
                        "code": "000013",
                        "name": "低质量候选",
                        "action_status": "watch_only",
                        "risk_adjusted_quality_score": 65.0,
                    }
                ],
                "quality_gate": {
                    "status": "blocked_by_quality_gate",
                    "reason": "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00",
                },
                "trigger_groups": [{"large": "omitted"}],
                "candidate_guard_summary": {
                    "direct_buy_blocked_count": 1,
                    "message": "以下候选仅可复核或观察，禁止直接买入",
                    "candidates": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "reason": "候选标签未成熟，禁止直接买入",
                            "action_status": "ready_for_ai_review",
                            "label_ready": False,
                        }
                    ],
                },
            },
            "last_ai_report": {
                "ok": True,
                "model": "gpt-test",
                "stock_count": 1,
                "reviewed_codes": ["300750"],
                "reviewed_symbols": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "action_status": "ready_for_ai_review",
                        "label_ready": False,
                    }
                ],
                "candidate_guard_summary": {
                    "direct_buy_blocked_count": 1,
                    "message": "以下候选仅可复核或观察，禁止直接买入",
                    "candidates": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "reason": "候选标签未成熟，禁止直接买入",
                            "action_status": "ready_for_ai_review",
                            "label_ready": False,
                            "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                        }
                    ],
                },
                "strategy_policy": {
                    "dynamic_mode": "shadow",
                    "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
                    "attribution_signal_weights": {"lps": 0.5, "trend_pullback": 0.75},
                },
            },
            "last_strategy_decision": {
                "status": "skipped_notify_unconfigured",
                "report_source": "last_ai_report",
                "candidate_count": 1,
                "reviewed_codes": ["300750"],
                "reviewed_symbols": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "action_status": "ready_for_ai_review",
                        "label_ready": False,
                        "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                    }
                ],
                "candidate_guard_summary": {
                    "direct_buy_blocked_count": 1,
                    "message": "以下候选仅可复核或观察，禁止直接买入",
                    "candidates": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "reason": "候选标签未成熟，禁止直接买入",
                            "action_status": "ready_for_ai_review",
                            "label_ready": False,
                            "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                            "debug_payload": "omitted",
                        }
                    ],
                },
                "strategy_policy": {
                    "dynamic_mode": "shadow",
                    "execution_policy": "shadow",
                    "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
                    "signal_weights": {"trend_pullback": 0.75},
                },
            },
        }
    )

    handoff = _workflow_handoff_state(tools)

    screen = handoff["last_screen_result"]
    assert screen["style_preference"] == {"raw": "trend", "styles": ["trend"]}
    assert screen["theme_preference"] == {"raw": "机器人", "theme": "机器人"}
    assert screen["preference_match"] == {"style": "miss", "theme": "miss"}
    assert screen["selection_brief"]["best_codes"] == ["300750"]
    assert screen["next_action"] == "首选候选已通过市场闸门，可进入 AI 研报复核"
    assert screen["next_tool"]["tool"] == "generate_ai_report"
    assert screen["next_tool"]["args"]["stock_codes"] == ["300750"]
    assert screen["theme_context"]["event_mainlines"] == "机器人 0.82/爆发"
    assert screen["theme_context"]["hot_concepts"] == ["机器人", "灵巧手", "滚柱丝杠", "电子皮肤", "控制系统", "减速器"]
    assert screen["strategy_policy"]["policy_weight_active_scope"] == "尾盘+漏斗shadow"
    assert screen["strategy_policy"]["execution_policy"] == "shadow"
    assert screen["strategy_policy"]["execution_policy_label"] == "shadow 对照(shadow)"
    assert "candidate_lane=trend_pullback" in screen["strategy_policy"]["selection_action_summary"]
    assert screen["strategy_policy"]["attribution_signal_weights"] == {"lps": 0.5, "trend_pullback": 0.75}
    assert screen["action_plan"]["new_buy_allowed"] is False
    assert screen["action_plan"]["quality_gate"]["status"] == "blocked_by_quality_gate"
    assert screen["diagnosis_targets"][0]["tool"] == "analyze_stock"
    assert screen["diagnosis_targets"][0]["args"] == {"code": "000013", "mode": "diagnose"}
    assert screen["action_plan"]["diagnosis_targets"][0]["args"]["code"] == "000013"
    assert screen["quality_gate"]["status"] == "blocked_by_quality_gate"
    candidate = screen["symbols_for_report"][0]
    assert candidate["code"] == "300750"
    assert candidate["profile"] == "趋势线 / 主升阶段 / 触发:SOS+EVR"
    assert candidate["triggers"] == ["sos", "evr", "spring", "lps", "compression"]
    assert candidate["style_match"] is True
    assert candidate["style_match_reasons"] == ["趋势偏好: 趋势线", "趋势偏好: 主升阶段"]
    assert candidate["theme_match"] is True
    assert candidate["theme_match_reasons"] == ["主题偏好: 机器人"]
    assert candidate["candidate_shadow_score"] == 92.0
    assert candidate["entry_quality_score"] == 84.0
    assert candidate["funnel_score"] == 89.5
    assert candidate["candidate_quality_score"] == 92.0
    assert candidate["risk_adjusted_quality_score"] == 87.0
    assert candidate["entry_risk_penalty"] == 5.0
    assert candidate["selection_strategy"] == "candidate_shadow_then_score"
    assert candidate["is_ai_recommended"] is True
    assert candidate["label_ready"] is False
    assert candidate["strategic_theme"] == "机器人"
    assert candidate["theme_source"] == "ths_hot_event"
    assert candidate["theme_event_id"] == "evt-robot"
    assert candidate["theme_event_reason"] == "灵巧手"
    assert candidate["entry_zone"] == [196.0, 202.0]
    assert candidate["stop_loss"] == 188.5
    assert candidate["max_entry_price"] == 204.0
    assert candidate["tape_condition"] == "放量站回5日线"
    assert candidate["invalidate_condition"] == "跌破188.5取消交易"
    assert screen["watch_candidates"][0]["code"] == "000013"
    screen_guard = screen["candidate_guard_summary"]
    assert screen_guard["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    report_guard = handoff["last_ai_report"]["candidate_guard_summary"]
    assert report_guard["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    report_policy = handoff["last_ai_report"]["strategy_policy"]
    assert report_policy["attribution_signal_weights"] == {"lps": 0.5, "trend_pullback": 0.75}
    assert "candidate_lane=trend_pullback" in report_policy["selection_action_summary"]
    guard = handoff["last_strategy_decision"]["candidate_guard_summary"]
    assert guard["direct_buy_blocked_count"] == 1
    assert guard["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    assert guard["candidates"][0]["label_ready"] is False
    assert "debug_payload" not in guard["candidates"][0]
    decision_policy = handoff["last_strategy_decision"]["strategy_policy"]
    assert decision_policy["execution_policy"] == "shadow"
    assert decision_policy["execution_policy_label"] == "shadow 对照(shadow)"
    assert decision_policy["signal_weights"] == {"trend_pullback": 0.75}
    assert "trigger_groups" not in screen


def test_workflow_handoff_candidate_rows_rank_before_limit():
    tools = StubToolRegistry()
    low_rows = [
        {
            "code": f"00000{index}",
            "name": f"观察候选{index}",
            "action_status": "watch_only",
            "candidate_shadow_score": 20 + index,
        }
        for index in range(6)
    ]
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "symbols_for_report": [
                    *low_rows,
                    {
                        "code": "300999",
                        "name": "高质量候选",
                        "status": "candidate",
                        "selected_for_report": True,
                        "risk_adjusted_quality_score": 91.0,
                    },
                ]
            }
        }
    )

    handoff = _workflow_handoff_state(tools)

    rows = handoff["last_screen_result"]["symbols_for_report"]
    codes = [row["code"] for row in rows]
    assert len(rows) == 6
    assert codes[0] == "300999"
    assert "000000" not in codes
    assert rows[0]["selected_for_report"] is True
    assert rows[0]["status"] == "candidate"


def test_workflow_handoff_state_compacts_stock_diagnosis_and_candidate_conclusion():
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "watch_candidates": [
                    {
                        "code": "002326",
                        "name": "永太科技",
                        "action_status": "watch_only",
                        "candidate_shadow_score": 82.0,
                        "risk_factors": ["市场闸门关闭"],
                    }
                ]
            },
            "last_stock_diagnosis": {
                "latest": {"code": "002326", "name": "永太科技"},
                "diagnosed_symbols": [
                    {
                        "code": "002326",
                        "name": "永太科技",
                        "action_status": "priority_watch",
                        "status_label": "重点观察",
                        "candidate_score": 83.04,
                        "risk_factors": ["短线涨幅偏快"],
                        "next_step": "加入重点观察，等待市场闸门打开",
                    }
                ],
            },
        }
    )

    handoff = _workflow_handoff_state(tools)
    conclusion = _candidate_conclusion_from_handoff(handoff)

    diagnosis = handoff["last_stock_diagnosis"]["diagnosed_symbols"][0]
    assert diagnosis["code"] == "002326"
    assert diagnosis["candidate_score"] == 83.04
    assert conclusion["code"] == "002326"
    assert "诊断分83" in conclusion["line"]
    assert "短线涨幅偏快" in conclusion["line"]
    assert conclusion["next_step"] == "加入重点观察，等待市场闸门打开"


def test_workflow_candidate_conclusion_labels_diagnosis_statuses():
    conclusions = _candidate_conclusions_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "002436",
                        "name": "兴森科技",
                        "action_status": "ready_for_ai_review",
                        "priority_score": 111.08,
                    }
                ]
            },
            "last_stock_diagnosis": {
                "diagnosed_symbols": [
                    {
                        "code": "002436",
                        "name": "兴森科技",
                        "action_status": "avoid",
                        "status_label": "回避",
                        "candidate_score": 96.05,
                        "risk_factors": ["结构止损（从高点回撤>10%）"],
                        "next_step": "回避新增，等待结构止损解除",
                    },
                    {
                        "code": "002669",
                        "name": "康达新材",
                        "action_status": "priority_watch",
                        "status_label": "重点观察",
                        "candidate_score": 99.05,
                        "quality_factors": ["L2通道: 主升通道"],
                        "next_step": "加入重点观察，等待回踩/触发确认",
                    },
                ]
            },
        },
        limit=3,
    )

    assert [row["code"] for row in conclusions[:2]] == ["002669", "002436"]
    assert conclusions[0]["role"] == "重点观察"
    assert "候选结论: 重点观察 002669 康达新材" in conclusions[0]["line"]
    assert "状态=重点观察" in conclusions[0]["line"]
    assert conclusions[1]["role"] == "回避"
    assert "候选结论: 回避 002436 兴森科技" in conclusions[1]["line"]
    assert "状态=回避" in conclusions[1]["line"]
    assert "结构止损" in conclusions[1]["line"]


def test_workflow_adaptation_handoff_summary_includes_stock_diagnosis():
    summary = _adaptation_handoff_summary(
        [
            {
                "step": {"step_id": "diagnose_candidates", "title": "诊断重点候选结构"},
                "result": {
                    "handoff_state": {
                        "last_stock_diagnosis": {
                            "latest": {"code": "002326", "name": "永太科技"},
                            "next_action": "诊断已完成，可继续形成攻防边界",
                            "diagnosed_symbols": [
                                {
                                    "code": "002326",
                                    "name": "永太科技",
                                    "health": "健康",
                                    "status_label": "重点观察",
                                    "candidate_score": 83.04,
                                    "risk_factors": ["短线涨幅偏快"],
                                    "next_step": "等待市场闸门打开",
                                    "data_status": "ok",
                                }
                            ],
                        }
                    }
                },
            }
        ]
    )

    assert summary[0]["source"] == "last_stock_diagnosis"
    assert summary[0]["latest"]["code"] == "002326"
    assert summary[0]["next_action"] == "诊断已完成，可继续形成攻防边界"
    candidate = summary[0]["candidates"][0]
    assert candidate["code"] == "002326"
    assert candidate["candidate_score"] == 83.04
    assert candidate["status_label"] == "重点观察"
    assert candidate["risk_factors"] == ["短线涨幅偏快"]
    assert candidate["next_step"] == "等待市场闸门打开"


def test_workflow_handoff_state_preserves_recommendation_candidate_guard():
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_recommendation_event_eval": {
                "result_summary": "推荐事件评估完成",
                "policy_selection": {
                    "status": "candidate",
                    "selection_strategy": "candidate_shadow_then_score",
                    "top_k": 1,
                    "recommend_date": "2026-06-30",
                    "picks": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "action_status": "ready_for_ai_review",
                            "label_ready": False,
                        }
                    ],
                },
                "candidate_guard_summary": {
                    "direct_buy_blocked_count": 1,
                    "message": "以下候选仅可复核或观察，禁止直接买入",
                    "candidates": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "reason": "候选标签未成熟，禁止直接买入",
                            "action_status": "ready_for_ai_review",
                            "label_ready": False,
                            "debug_payload": "omitted",
                        }
                    ],
                },
            }
        }
    )

    handoff = _workflow_handoff_state(tools)

    guard = handoff["last_recommendation_event_eval"]["candidate_guard_summary"]
    assert guard["direct_buy_blocked_count"] == 1
    assert guard["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    assert "debug_payload" not in guard["candidates"][0]


def test_workflow_handoff_state_normalizes_scalar_candidate_rows():
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "symbols_for_report": ["300750", "宁德时代", {"symbol": "AAPL.US", "name": "Apple"}],
            },
            "last_ai_report": {
                "reviewed_symbols": ["000001"],
            },
        }
    )

    handoff = _workflow_handoff_state(tools)
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {"status": "completed", "result": "扫描完成。", "handoff_state": handoff},
            }
        ]
    )

    screen_rows = handoff["last_screen_result"]["symbols_for_report"]
    assert screen_rows == [{"code": "300750"}, {"name": "宁德时代"}, {"name": "Apple", "code": "AAPL.US"}]
    assert handoff["last_ai_report"]["reviewed_symbols"] == [{"code": "000001"}]
    assert "候选结论: 候选 000001" in summary


def test_workflow_synthesis_receives_step_handoff_state(tmp_path, monkeypatch):
    from integrations import local_db

    def _synthesis_round(messages, _tools, _system_prompt):
        content = messages[0]["content"]
        assert '"handoff_state"' in content
        assert '"last_screen_result"' in content
        assert '"300750"' in content
        return [{"type": "text_delta", "text": "已基于候选 handoff 汇总。"}]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-handoff.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "selection_brief": {"status": "ready_for_ai_review", "best_codes": ["300750"]},
                "symbols_for_report": [{"code": "300750", "name": "宁德时代"}],
            }
        }
    )
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "已完成候选扫描。"}],
            _synthesis_round,
        ]
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_handoff",
        user_text="用 workflow 选出好股票",
        workflow_context=route_workflow("用 workflow 选出好股票"),
        workflow_script={"tasks": [{"id": "scan", "title": "扫描候选", "prompt": "扫描候选"}]},
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 选出好股票"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        assert (
            done_event["source"]["agent_detail"]["handoff_state"]["last_screen_result"]["symbols_for_report"][0]["code"]
            == "300750"
        )
        assert events[-1]["text"].startswith("候选结论: 候选 300750 宁德时代")
        assert "已基于候选 handoff 汇总。" in events[-1]["text"]
    finally:
        _reset_local_db(local_db)


def test_workflow_explicit_tool_scope_continues_attack_plan_after_screen(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-tool-contract.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选已出，风险边界我先口头整理。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "攻防计划已基于工具结果生成。"}],
            [{"type": "text_delta", "text": "已汇总候选与攻防计划。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"]},
            "generate_strategy_decision": {"status": "skipped_notify_unconfigured", "reviewed_codes": ["300750"]},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_tool_contract",
        user_text="用 workflow 选出好股票，带风险边界",
        workflow_context=route_workflow("用 workflow 选出好股票，带风险边界"),
        workflow_script={
            "tasks": [
                {
                    "id": "scan_decide",
                    "title": "候选与攻防",
                    "tools": ["screen_stocks", "generate_strategy_decision"],
                    "prompt": "给我找几只值得复核的票，带理由和风险边界",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 选出好股票，带风险边界"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert detail["tool_scope"] == ["screen_stocks", "generate_strategy_decision"]
        assert detail["tool_calls"] == ["screen_stocks", "generate_strategy_decision"]
        assert detail["result"] == "攻防计划已基于工具结果生成。"
        assert [call["name"] for call in tools.calls] == ["screen_stocks", "generate_strategy_decision"]
        assert events[-1]["text"] == "已汇总候选与攻防计划。"
    finally:
        _reset_local_db(local_db)


def test_workflow_multitool_step_enforces_screen_args_before_strategy(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-multitool-screen-args.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_partial", "name": "screen_stocks", "args": {"board": "chinext"}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_screen",
                            "name": "screen_stocks",
                            "args": {
                                "board": "chinext",
                                "style": ["trend", "pullback"],
                                "limit": 0,
                                "financial_metrics": True,
                            },
                        }
                    ],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "全量候选和攻防计划已生成。"}],
            [{"type": "text_delta", "text": "已汇总全量筛选和攻防计划。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"], "selection_brief": {"best_codes": ["300750"]}},
            "generate_strategy_decision": {"status": "ok", "reviewed_codes": ["300750"]},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_multitool_screen_args",
        user_text="用 workflow 全量扫描创业板强势低吸标的，带财务过滤和风险边界",
        workflow_context=route_workflow("用 workflow 全量扫描创业板强势低吸标的，带财务过滤和风险边界"),
        workflow_script={
            "tasks": [
                {
                    "id": "scan_decide",
                    "title": "候选与攻防",
                    "tools": ["screen_stocks", "generate_strategy_decision"],
                    "args": {
                        "board": "chinext",
                        "style": ["trend", "pullback"],
                        "limit": 0,
                        "financial_metrics": True,
                    },
                    "prompt": "按完整筛选条件找候选，并给出风险边界。",
                }
            ]
        },
    )

    events = list(
        executor.run_stream(
            [{"role": "user", "content": "用 workflow 全量扫描创业板强势低吸标的，带财务过滤和风险边界"}]
        )
    )

    try:
        plan_event = next(event for event in events if event["type"] == "workflow_plan")
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert plan_event["plan"]["steps"][0]["args_hint"] == (
            "board: chinext；style: trend,pullback；limit: 0；financial_metrics: true"
        )
        assert detail["tool_calls"] == ["screen_stocks", "screen_stocks", "generate_strategy_decision"]
        assert [call["args"] for call in tools.calls] == [
            {"board": "chinext"},
            {"board": "chinext", "style": ["trend", "pullback"], "limit": 0, "financial_metrics": True},
            {},
        ]
        assert events[-1]["text"] == "已汇总全量筛选和攻防计划。"
    finally:
        _reset_local_db(local_db)


def test_workflow_multitool_step_retries_wrong_tool_order(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-multitool-order.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_strategy_first", "name": "generate_strategy_decision", "args": {}},
                        {
                            "id": "tc_screen_late",
                            "name": "screen_stocks",
                            "args": {
                                "board": "chinext",
                                "style": ["trend", "pullback"],
                                "limit": 0,
                                "financial_metrics": True,
                            },
                        },
                    ],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_screen",
                            "name": "screen_stocks",
                            "args": {
                                "board": "chinext",
                                "style": ["trend", "pullback"],
                                "limit": 0,
                                "financial_metrics": True,
                            },
                        }
                    ],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "全量候选和攻防计划已生成。"}],
            [{"type": "text_delta", "text": "已按正确顺序汇总全量筛选和攻防计划。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"], "selection_brief": {"best_codes": ["300750"]}},
            "generate_strategy_decision": {"status": "ok", "reviewed_codes": ["300750"]},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_multitool_order",
        user_text="用 workflow 全量扫描创业板强势低吸标的，带财务过滤和风险边界",
        workflow_context=route_workflow("用 workflow 全量扫描创业板强势低吸标的，带财务过滤和风险边界"),
        workflow_script={
            "tasks": [
                {
                    "id": "scan_decide",
                    "title": "候选与攻防",
                    "tools": ["screen_stocks", "generate_strategy_decision"],
                    "args": {
                        "board": "chinext",
                        "style": ["trend", "pullback"],
                        "limit": 0,
                        "financial_metrics": True,
                    },
                    "prompt": "按完整筛选条件找候选，并给出风险边界。",
                }
            ]
        },
    )

    events = list(
        executor.run_stream(
            [{"role": "user", "content": "用 workflow 全量扫描创业板强势低吸标的，带财务过滤和风险边界"}]
        )
    )

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert detail["tool_calls"] == ["screen_stocks", "generate_strategy_decision"]
        assert [call["name"] for call in tools.calls] == ["screen_stocks", "generate_strategy_decision"]
        retry_context = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        assert "工具调用顺序错误" in retry_context
        assert "必须先调用 `screen_stocks" in retry_context
        assert events[-1]["text"] == "已按正确顺序汇总全量筛选和攻防计划。"
    finally:
        _reset_local_db(local_db)


def test_workflow_multitool_step_orders_reversed_model_declared_tools(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-multitool-declared-order.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选和攻防计划已生成。"}],
            [{"type": "text_delta", "text": "已汇总候选与攻防计划。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"], "selection_brief": {"best_codes": ["300750"]}},
            "generate_strategy_decision": {"status": "ok", "reviewed_codes": ["300750"]},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_multitool_declared_order",
        user_text="用 workflow 选出好股票，带风险边界",
        workflow_context=route_workflow("用 workflow 选出好股票，带风险边界"),
        workflow_script={
            "tasks": [
                {
                    "id": "scan_decide",
                    "title": "候选与攻防",
                    "tools": ["generate_strategy_decision", "screen_stocks"],
                    "prompt": "先筛候选，再形成攻防计划。",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 选出好股票，带风险边界"}]))

    try:
        plan_event = next(event for event in events if event["type"] == "workflow_plan")
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert plan_event["plan"]["steps"][0]["tool_scope"] == ["screen_stocks", "generate_strategy_decision"]
        assert plan_event["plan"]["script"]["tasks"][0]["tools"] == ["screen_stocks", "generate_strategy_decision"]
        assert detail["tool_scope"] == ["screen_stocks", "generate_strategy_decision"]
        assert detail["tool_calls"] == ["screen_stocks", "generate_strategy_decision"]
        assert [call["name"] for call in tools.calls] == ["screen_stocks", "generate_strategy_decision"]
        assert events[-1]["text"] == "已汇总候选与攻防计划。"
    finally:
        _reset_local_db(local_db)


def test_natural_stock_selection_turn_runs_dynamic_workflow_end_to_end(tmp_path, monkeypatch):
    from integrations import local_db

    user_text = "帮我完整做一遍今天的 A 股选股，给出候选、理由和买卖计划"
    script = {
        "title": "自然聊天选股",
        "rationale": "用户需要候选、理由和买卖计划，适合用可见 workflow 链路交付。",
        "tasks": [
            {
                "id": "select_and_plan",
                "title": "筛选候选并形成攻防",
                "tools": ["screen_stocks", "generate_strategy_decision"],
                "prompt": "筛选今日 A 股候选，保留理由并给出买卖计划和风险边界。",
                "success_criteria": "输出候选、理由、风险边界和下一步动作。",
            }
        ],
        "synthesis_prompt": "输出候选、理由、风险边界和下一步动作。",
    }
    provider = RoutingScriptedProvider(
        '{"mode":"dynamic_workflow","confidence":0.9,"reason":"需要候选池、理由和行动计划"}',
        rounds=[
            [{"type": "text_delta", "text": json.dumps(script, ensure_ascii=False)}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}},
                        {"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}},
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选和攻防计划已基于工具结果生成。"}],
            [{"type": "text_delta", "text": "首选候选：300750 宁德时代；风险边界已给出。"}],
        ],
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"], "selection_brief": {"best_codes": ["300750"]}},
            "generate_strategy_decision": {"status": "ok", "reviewed_codes": ["300750"]},
        },
    )

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "natural-workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    runtime, workflow = build_turn_runtime(provider, tools, session_id="s_natural", user_text=user_text)
    events = list(runtime.run_stream([{"role": "user", "content": user_text}]))

    try:
        assert isinstance(runtime, WorkflowExecutor)
        assert workflow.name == "dynamic_task"
        assert workflow.route_reason == "模型判断需要动态 workflow：需要候选池、理由和行动计划"
        assert provider.chat_calls
        assert "用 workflow" not in provider.chat_calls[0]["messages"][0]["content"]
        plan_event = events[0]
        assert plan_event["type"] == "workflow_plan"
        assert plan_event["route"]["matches"] == ["model_router"]
        assert plan_event["plan"]["script"]["runtime"]["planner"] == "model_script"
        assert plan_event["plan"]["steps"][0]["tool_scope"] == ["screen_stocks", "generate_strategy_decision"]
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        assert done_event["source"]["agent_detail"]["tool_calls"] == ["screen_stocks", "generate_strategy_decision"]
        assert [call["name"] for call in tools.calls] == ["screen_stocks", "generate_strategy_decision"]
        assert events[-1]["text"] == "首选候选：300750 宁德时代；风险边界已给出。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_until_step_args_hint_is_used(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-required-style-args.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_plain", "name": "screen_stocks", "args": {}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_style", "name": "screen_stocks", "args": {"style": ["trend", "pullback"]}}
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "风格候选已筛出。"}],
            [{"type": "text_delta", "text": "已按风格偏好汇总候选。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={"screen_stocks": {"symbols_for_report": ["000002"]}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_required_style_args",
        user_text="用 workflow 找强势低吸标的",
        workflow_context=WORKFLOWS["dynamic_task"],
        workflow_script={
            "tasks": [
                {
                    "id": "scan",
                    "title": "扫描风格候选",
                    "tools": ["screen_stocks"],
                    "args": {"style": "trend,pullback"},
                    "prompt": "按用户风格偏好扫描候选。",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 找强势低吸标的"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert detail["tool_calls"] == ["screen_stocks", "screen_stocks"]
        assert [call["args"] for call in tools.calls] == [{}, {"style": ["trend", "pullback"]}]
        assert events[-1]["text"] == "已按风格偏好汇总候选。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_until_theme_args_hint_is_used(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-required-theme-args.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_style", "name": "screen_stocks", "args": {"style": "quality"}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_theme",
                            "name": "screen_stocks",
                            "args": {"style": "quality", "theme": "红利低波"},
                        }
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "红利质量候选已筛出。"}],
            [{"type": "text_delta", "text": "已按红利质量偏好汇总候选。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={"screen_stocks": {"symbols_for_report": ["000002"]}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_required_theme_args",
        user_text="用 workflow 找高股息红利票",
        workflow_context=WORKFLOWS["dynamic_task"],
        workflow_script={
            "tasks": [
                {
                    "id": "scan",
                    "title": "扫描红利质量候选",
                    "tools": ["screen_stocks"],
                    "args": {"style": "quality", "theme": "红利低波"},
                    "prompt": "按用户红利质量偏好扫描候选。",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 找高股息红利票"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert detail["tool_calls"] == ["screen_stocks", "screen_stocks"]
        assert [call["args"] for call in tools.calls] == [
            {"style": "quality"},
            {"style": "quality", "theme": "红利低波"},
        ]
        assert events[-1]["text"] == "已按红利质量偏好汇总候选。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_enforces_model_declared_bool_zero_args(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-required-full-args.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_partial", "name": "screen_stocks", "args": {"board": "chinext"}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_full",
                            "name": "screen_stocks",
                            "args": {
                                "board": "chinext",
                                "style": ["trend", "pullback"],
                                "limit": 0,
                                "financial_metrics": True,
                            },
                        }
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "全量财务候选已筛出。"}],
            [{"type": "text_delta", "text": "已按全量财务过滤汇总候选。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={"screen_stocks": {"symbols_for_report": ["000002"]}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_required_full_args",
        user_text="用 workflow 全量扫描创业板强势低吸标的，要带财务过滤",
        workflow_context=WORKFLOWS["dynamic_task"],
        workflow_script={
            "tasks": [
                {
                    "id": "scan",
                    "title": "扫描全量财务候选",
                    "tools": ["screen_stocks"],
                    "args": {
                        "board": "chinext",
                        "style": ["trend", "pullback"],
                        "limit": 0,
                        "financial_metrics": True,
                    },
                    "prompt": "按用户完整筛选条件扫描候选。",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 全量扫描创业板强势低吸标的"}]))

    try:
        plan_event = next(event for event in events if event["type"] == "workflow_plan")
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert plan_event["plan"]["steps"][0]["args_hint"] == (
            "board: chinext；style: trend,pullback；limit: 0；financial_metrics: true"
        )
        assert detail["tool_calls"] == ["screen_stocks", "screen_stocks"]
        assert [call["args"] for call in tools.calls] == [
            {"board": "chinext"},
            {"board": "chinext", "style": ["trend", "pullback"], "limit": 0, "financial_metrics": True},
        ]
        assert events[-1]["text"] == "已按全量财务过滤汇总候选。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_enforces_inferred_stock_screen_args(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-inferred-screen-args.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "text_delta",
                    "text": '{"title":"自然选股","tasks":[{"id":"scan","title":"扫描候选","prompt":"扫描候选"}]}',
                }
            ],
            [{"type": "text_delta", "text": "这不是 JSON"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_plain", "name": "screen_stocks", "args": {"style": ["trend", "pullback"]}}
                    ],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_scoped",
                            "name": "screen_stocks",
                            "args": {"board": "chinext", "style": ["trend", "pullback"]},
                        }
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "风格候选已筛出。"}],
            [{"type": "text_delta", "text": "已按强势低吸偏好汇总候选。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={"screen_stocks": {"symbols_for_report": ["000002"]}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_inferred_screen_args",
        user_text="今天帮我筛创业板强势低吸标的",
        workflow_context=WORKFLOWS["dynamic_task"],
    )

    events = list(executor.run_stream([{"role": "user", "content": "今天帮我筛创业板强势低吸标的"}]))

    try:
        plan_event = next(event for event in events if event["type"] == "workflow_plan")
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert plan_event["plan"]["steps"][0]["tool_scope"] == ["screen_stocks"]
        assert plan_event["plan"]["steps"][0]["tool_scope_source"] == "semantic_inference"
        assert plan_event["plan"]["steps"][0]["args_hint"] == "board: chinext；style: trend,pullback"
        assert detail["tool_calls"] == ["screen_stocks", "screen_stocks"]
        assert [call["args"] for call in tools.calls] == [
            {"style": ["trend", "pullback"]},
            {"board": "chinext", "style": ["trend", "pullback"]},
        ]
        assert events[-1]["text"] == "已按强势低吸偏好汇总候选。"
    finally:
        _reset_local_db(local_db)


def test_workflow_synthesis_prompt_requires_candidate_answer_contract():
    run = WorkflowRun(
        run_id="wf_contract",
        session_id="s_contract",
        user_text="帮我完整做一遍今天的 A 股选股，给出候选、理由和买卖计划",
        context=WORKFLOWS["dynamic_task"],
        script={"synthesis_prompt": "输出候选、理由和买卖计划。"},
    )
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "action_plan": {"new_buy_allowed": False, "trade_readiness": "research_only"},
                        "symbols_for_report": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "candidate_shadow_score": 92.0,
                                "candidate_shadow_grade": "S",
                                "entry_quality_score": 84.0,
                                "entry_quality_grade": "A",
                                "risk_factors": ["评估标签尚未成熟"],
                                "trade_readiness": "research_only",
                                "new_buy_allowed": False,
                                "next_step": "生成 AI 研报",
                            }
                        ],
                    }
                }
            },
        }
    ]

    prompt = _synthesis_prompt(run, results)

    assert "候选代码/名称" in prompt
    assert "为什么入选" in prompt
    assert "主要风险" in prompt
    assert "下一步动作" in prompt
    assert "多候选场景不要压成一个泛泛结论" in prompt
    assert "首选、备选复核候选、受限复核候选、修复复核候选、待确认候选、观察候选、阻断候选" in prompt
    assert "候选护栏、市场闸门、数据质量、交易就绪或新增买入限制" in prompt
    assert "不能把受限候选写成买入建议" in prompt
    assert "入场区、触发条件、止损、失效条件、防追高限价" in prompt
    assert "没有边界时明确说只能观察或复核" in prompt
    assert "自然语言，不要照抄内部字段名" in prompt
    assert "候选行要优先使用代码/名称、action_status" not in prompt
    assert '"candidate_shadow_score": 92.0' in prompt
    assert '"new_buy_allowed": false' in prompt


def test_workflow_synthesis_prompt_accepts_model_summary_aliases():
    run = WorkflowRun(
        run_id="wf_synth_alias",
        session_id="s_synth_alias",
        user_text="帮我选出好股票",
        context=WORKFLOWS["dynamic_task"],
        script={"final_response": "最终按候选、理由、风险边界输出。"},
    )

    prompt = _synthesis_prompt(run, [])

    assert "模型脚本的汇总要求:\n最终按候选、理由、风险边界输出。" in prompt


def test_workflow_synthesis_prompt_prefers_canonical_field_over_aliases():
    run = WorkflowRun(
        run_id="wf_synth_prefer",
        session_id="s_synth_prefer",
        user_text="帮我选出好股票",
        context=WORKFLOWS["dynamic_task"],
        script={
            "synthesis_prompt": "优先输出可执行下一步。",
            "final_response": "这个别名不应覆盖 canonical 字段。",
        },
    )

    prompt = _synthesis_prompt(run, [])
    script_section = prompt.split("模型脚本的汇总要求:\n", 1)[1].split("\n\n用户请求:", 1)[0]

    assert script_section == "优先输出可执行下一步。"


def test_workflow_synthesis_prioritizes_handoff_before_long_agent_results():
    run = WorkflowRun(
        run_id="wf_long_handoff",
        session_id="s_long_handoff",
        user_text="帮我选出今天最值得复核的股票",
        context=WORKFLOWS["dynamic_task"],
        script={"synthesis_prompt": "优先汇总候选。"},
    )
    handoff = {
        "last_screen_result": {
            "theme_context": {"event_mainlines": "机器人 0.82/爆发", "today_activity": "机器人 0.76/活跃"},
            "symbols_for_report": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "action_status": "ready_for_ai_review",
                    "candidate_shadow_score": 92.0,
                    "candidate_shadow_grade": "S",
                    "candidate_quality_score": 92.0,
                    "risk_adjusted_quality_score": 87.0,
                    "strategic_theme": "机器人",
                    "theme_source": "ths_hot_event",
                    "theme_event_reason": "灵巧手",
                    "quality_factors": ["事件主线:机器人", "候选影子评级 S"],
                    "risk_factors": ["未来窗口标签尚未成熟"],
                    "next_step": "生成 AI 研报",
                    "entry_zone": [196.0, 202.0],
                    "stop_loss": 188.5,
                    "max_entry_price": 204.0,
                    "tape_condition": "放量站回5日线",
                    "invalidate_condition": "跌破188.5取消交易",
                }
            ],
        }
    }
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {"status": "completed", "result": "候选扫描完成。", "handoff_state": handoff},
        }
    ]

    prompt = _synthesis_prompt(run, results)
    handoff_section = prompt.split("priority candidate handoff:\n", 1)[1].split("\n\nagent results:", 1)[0]
    agent_results_section = prompt.split("agent results:\n", 1)[1]

    assert '"candidate_conclusion"' in handoff_section
    assert "候选结论: 首选 300750 宁德时代" in handoff_section
    assert '"candidate_shadow_score": 92.0' in handoff_section
    assert '"risk_adjusted_quality_score": 87.0' in handoff_section
    assert '"theme_context": {"event_mainlines": "机器人 0.82/爆发"' in handoff_section
    assert "事件主线机器人(灵巧手)" in handoff_section
    assert "风险调整分87" in handoff_section
    assert "亮点=事件主线:机器人,候选影子评级 S" in handoff_section
    assert "风险=未来窗口标签尚未成熟" in handoff_section
    assert "入场区=196-202" in handoff_section
    assert "触发=放量站回5日线" in handoff_section
    assert "止损=188.5" in handoff_section
    assert "失效=跌破188.5取消交易" in handoff_section
    assert "防追高限价=204" in handoff_section
    assert '"300750"' in handoff_section
    assert '"candidate_shadow_score": 92.0' not in agent_results_section
    assert "候选扫描完成" in agent_results_section
    assert '"handoff_state_ref": "see priority candidate handoff"' in agent_results_section


def test_workflow_synthesis_handoff_summary_dedupes_latest_keys():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [{"code": "300750", "name": "宁德时代"}],
                    }
                }
            },
        },
        {
            "step": {"step_id": "report", "title": "AI研报"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [{"code": "300750", "name": "宁德时代", "funnel_score": 89.5}],
                    },
                    "last_ai_report": {"reviewed_codes": ["300750"], "model": "gpt-test"},
                }
            },
        },
        {
            "step": {"step_id": "decision", "title": "攻防决策"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "candidate_shadow_score": 92.0,
                                "quality_factors": ["候选影子评级 S"],
                                "risk_factors": ["未来窗口标签尚未成熟"],
                            }
                        ],
                    },
                    "last_ai_report": {"reviewed_codes": ["300750"], "model": "gpt-test"},
                    "last_strategy_decision": {"reviewed_codes": ["300750"], "status": "completed"},
                }
            },
        },
    ]

    summary = _synthesis_handoff_summary(results)

    assert len(summary) == 1
    handoff = summary[0]["handoff_state"]
    assert summary[0]["step_id"] == "decision"
    assert set(handoff) == {"last_screen_result", "last_ai_report", "last_strategy_decision"}
    assert handoff["last_screen_result"]["symbols_for_report"][0]["candidate_shadow_score"] == 92.0
    assert summary[0]["candidate_conclusion"]["evidence"] == ["候选影子92"]
    assert summary[0]["candidate_conclusion"]["quality_factors"] == ["候选影子评级 S"]
    assert summary[0]["candidate_conclusion"]["risk_factors"] == ["未来窗口标签尚未成熟"]
    assert json.dumps(handoff, ensure_ascii=False).count("last_screen_result") == 1


def test_workflow_synthesis_handoff_summary_merges_split_candidate_conclusion():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "candidate_shadow_score": 92.0,
                                "candidate_shadow_grade": "S",
                            }
                        ]
                    }
                }
            },
        },
        {
            "step": {"step_id": "decision", "title": "攻防决策"},
            "result": {
                "handoff_state": {
                    "last_strategy_decision": {
                        "reviewed_symbols": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "action_status": "ready_for_ai_review",
                                "next_step": "等待通知配置后生成 OMS 工单",
                            }
                        ]
                    }
                }
            },
        },
    ]

    conclusion = _synthesis_handoff_summary(results)[0]["candidate_conclusion"]

    assert conclusion["source_stage"] == "last_strategy_decision"
    assert conclusion["evidence"] == ["候选影子S/92"]
    assert conclusion["next_step"] == "等待通知配置后生成 OMS 工单"


def test_workflow_synthesis_handoff_summary_merges_name_only_candidate_conclusion():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "candidate_shadow_score": 92.0,
                                "candidate_shadow_grade": "S",
                            }
                        ]
                    }
                }
            },
        },
        {
            "step": {"step_id": "decision", "title": "攻防决策"},
            "result": {
                "handoff_state": {
                    "last_strategy_decision": {
                        "reviewed_symbols": [
                            {
                                "name": "宁德时代",
                                "action_status": "ready_for_ai_review",
                                "next_step": "等待通知配置后生成 OMS 工单",
                            }
                        ]
                    }
                }
            },
        },
    ]

    summary = _synthesis_handoff_summary(results)[0]
    conclusion = summary["candidate_conclusion"]

    assert [item["code"] for item in summary["candidate_conclusions"]] == ["300750"]
    assert conclusion["code"] == "300750"
    assert conclusion["name"] == "宁德时代"
    assert conclusion["evidence"] == ["候选影子S/92"]
    assert "候选结论: 首选 300750 宁德时代" in conclusion["line"]


def test_workflow_synthesis_handoff_summary_does_not_merge_ambiguous_name_only_candidate():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [
                            {
                                "code": "000001",
                                "name": "同名科技",
                                "candidate_shadow_score": 92.0,
                            },
                            {
                                "code": "000002",
                                "name": "同名科技",
                                "candidate_shadow_score": 91.0,
                            },
                        ]
                    }
                }
            },
        },
        {
            "step": {"step_id": "decision", "title": "攻防决策"},
            "result": {
                "handoff_state": {
                    "last_strategy_decision": {
                        "reviewed_symbols": [
                            {
                                "name": "同名科技",
                                "action_status": "ready_for_ai_review",
                                "next_step": "等待确认具体代码",
                            }
                        ]
                    }
                }
            },
        },
    ]

    conclusions = _synthesis_handoff_summary(results)[0]["candidate_conclusions"]

    assert conclusions[0]["name"] == "同名科技"
    assert "code" not in conclusions[0]
    assert "evidence" not in conclusions[0]
    assert "候选结论: 待确认候选 同名科技" in conclusions[0]["line"]
    assert conclusions[0]["guard_reason"] == "候选缺少股票代码，需先确认具体标的"
    assert [item.get("code") for item in conclusions[1:]] == ["000001", "000002"]


def test_workflow_synthesis_handoff_summary_keeps_multiple_candidate_conclusions():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "report_candidates": [
                            {
                                "code": "000014",
                                "name": "高质量候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 92.0,
                                "candidate_shadow_grade": "S",
                            },
                            {
                                "code": "000015",
                                "name": "次优候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 88.0,
                            },
                        ],
                        "watch_candidates": [
                            {
                                "code": "000013",
                                "name": "观察候选",
                                "action_status": "watch_only",
                                "candidate_shadow_score": 96.0,
                            }
                        ],
                    }
                }
            },
        }
    ]

    summary = _synthesis_handoff_summary(results)

    conclusions = summary[0]["candidate_conclusions"]
    assert [item["code"] for item in conclusions] == ["000014", "000015", "000013"]
    assert summary[0]["candidate_conclusion"]["code"] == "000014"
    assert "首选 000014 高质量候选" in conclusions[0]["line"]
    assert "备选复核候选 000015 次优候选" in conclusions[1]["line"]
    assert "观察候选 000013 观察候选" in conclusions[2]["line"]


def test_workflow_synthesis_handoff_summary_derives_guard_from_candidate_fields():
    results = [
        {
            "step": {"step_id": "decision", "title": "攻防决策"},
            "result": {
                "handoff_state": {
                    "last_strategy_decision": {
                        "reviewed_symbols": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "action_status": "ready_for_ai_review",
                                "trade_readiness": "research_only",
                                "new_buy_allowed": False,
                                "next_step": "生成 AI 研报",
                            }
                        ]
                    }
                }
            },
        },
    ]

    conclusion = _synthesis_handoff_summary(results)[0]["candidate_conclusion"]

    assert conclusion["guard_reason"] == "候选未开放新增买入，禁止直接买入"
    assert "候选结论: 受限复核候选 300750 宁德时代" in conclusion["line"]
    assert "护栏=候选未开放新增买入，禁止直接买入" in conclusion["line"]
    assert "交易就绪=research_only" in conclusion["line"]
    assert "不允许新增买入" in conclusion["line"]


def test_workflow_synthesis_handoff_guard_reason_matches_candidate_identity():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "report_candidates": [
                            {
                                "code": "000014",
                                "name": "高质量候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 92.0,
                            },
                            {
                                "code": "000015",
                                "name": "受限候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 91.0,
                            },
                        ],
                        "candidate_guard_summary": {
                            "candidates": [
                                {
                                    "code": "000015",
                                    "name": "受限候选",
                                    "reason": "受限候选标签未成熟，禁止直接买入",
                                }
                            ]
                        },
                    }
                }
            },
        }
    ]

    conclusions = _synthesis_handoff_summary(results)[0]["candidate_conclusions"]

    assert "候选结论: 首选 000014 高质量候选" in conclusions[0]["line"]
    assert "护栏=" not in conclusions[0]["line"]
    assert conclusions[0].get("guard_reason") is None
    assert "候选结论: 受限复核候选 000015 受限候选" in conclusions[1]["line"]
    assert conclusions[1]["guard_reason"] == "受限候选标签未成熟，禁止直接买入"


def test_workflow_synthesis_handoff_gate_reason_matches_candidate_identity():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "report_candidates": [
                            {
                                "code": "000014",
                                "name": "高质量候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 92.0,
                            },
                            {
                                "code": "000015",
                                "name": "低质量候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 91.0,
                            },
                        ],
                        "quality_gate": {
                            "status": "blocked_by_quality_gate",
                            "reason": "000015 低质量候选 风险调整质量分低于AI复核门槛",
                            "candidates": [
                                {
                                    "code": "000015",
                                    "name": "低质量候选",
                                    "reason": "000015 低质量候选 风险调整质量分低于AI复核门槛",
                                }
                            ],
                        },
                    }
                }
            },
        }
    ]

    conclusions = _synthesis_handoff_summary(results)[0]["candidate_conclusions"]

    assert "候选结论: 首选 000014 高质量候选" in conclusions[0]["line"]
    assert "护栏=" not in conclusions[0]["line"]
    assert conclusions[0].get("guard_reason") is None
    assert "候选结论: 受限复核候选 000015 低质量候选" in conclusions[1]["line"]
    assert conclusions[1]["guard_reason"] == "000015 低质量候选 风险调整质量分低于AI复核门槛"


def test_workflow_candidate_delivery_backfills_missing_action_boundaries():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "action_status": "ready_for_ai_review",
                                "entry_zone": [196.0, 202.0],
                                "stop_loss": 188.5,
                                "max_entry_price": 204.0,
                                "tape_condition": "放量站回5日线",
                                "invalidate_condition": "跌破188.5取消交易",
                            }
                        ]
                    }
                }
            },
        }
    ]
    thin_text = "300750 宁德时代是今天的首选，可进入研报复核。"

    final_text = _ensure_candidate_delivery(thin_text, results)

    assert final_text.startswith("候选结论: 首选 300750 宁德时代")
    assert "入场区=196-202" in final_text
    assert "触发=放量站回5日线" in final_text
    assert "止损=188.5" in final_text
    assert "失效=跌破188.5取消交易" in final_text
    assert "防追高限价=204" in final_text
    assert final_text.endswith(thin_text)
    complete_text = (
        "首选 300750 宁德时代，入场区196-202，触发放量站回5日线，止损188.5，失效跌破188.5取消交易，防追高限价204。"
    )
    assert _ensure_candidate_delivery(complete_text, results) == complete_text


def test_workflow_candidate_delivery_backfills_missing_evidence():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "symbols_for_report": [
                            {
                                "code": "300750",
                                "name": "宁德时代",
                                "action_status": "ready_for_ai_review",
                                "strategic_theme": "机器人",
                                "theme_source": "ths_hot_event",
                                "theme_event_reason": "灵巧手",
                                "risk_factors": ["未来窗口标签尚未成熟"],
                                "next_step": "生成 AI 研报",
                            }
                        ]
                    }
                }
            },
        }
    ]
    thin_text = "300750 宁德时代是今天的首选。"

    final_text = _ensure_candidate_delivery(thin_text, results)

    assert final_text.startswith("候选结论: 首选 300750 宁德时代")
    assert "证据=事件主线机器人(灵巧手)" in final_text
    assert "风险=未来窗口标签尚未成熟" in final_text
    assert "下一步=生成 AI 研报" in final_text
    assert final_text.endswith(thin_text)
    complete_text = "首选 300750 宁德时代，证据是事件主线机器人(灵巧手)。"
    assert _ensure_candidate_delivery(complete_text, results) == complete_text


def test_workflow_candidate_delivery_backfills_only_missing_candidates():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "report_candidates": [
                            {
                                "code": "000014",
                                "name": "高质量候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 92.0,
                            },
                            {
                                "code": "000015",
                                "name": "次优候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 88.0,
                            },
                        ]
                    }
                }
            },
        }
    ]
    text = "首选 000014 高质量候选，证据是候选影子92。"

    final_text = _ensure_candidate_delivery(text, results)
    prepended = final_text.split("\n\n", 1)[0]

    assert prepended.startswith("候选结论: 备选复核候选 000015 次优候选")
    assert "候选结论: 首选 000014 高质量候选" not in prepended
    assert final_text.endswith(text)


def test_workflow_candidate_delivery_backfills_missing_candidate_roles():
    results = [
        {
            "step": {"step_id": "scan", "title": "扫描候选"},
            "result": {
                "handoff_state": {
                    "last_screen_result": {
                        "report_candidates": [
                            {
                                "code": "000014",
                                "name": "高质量候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 92.0,
                            },
                            {
                                "code": "000015",
                                "name": "次优候选",
                                "action_status": "ready_for_ai_review",
                                "candidate_shadow_score": 88.0,
                            },
                        ]
                    }
                }
            },
        }
    ]
    text = "000014 高质量候选，证据是候选影子92。000015 次优候选，证据是候选影子88。"

    final_text = _ensure_candidate_delivery(text, results)
    prepended = final_text.split("\n\n", 1)[0]

    assert "候选结论: 首选 000014 高质量候选" in prepended
    assert "候选结论: 备选复核候选 000015 次优候选" in prepended
    assert final_text.endswith(text)


def test_workflow_executor_empty_synthesis_uses_candidate_fallback(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-empty-synthesis.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "symbols_for_report": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "candidate_shadow_score": 92.0,
                        "candidate_shadow_grade": "S",
                        "action_status": "ready_for_ai_review",
                        "trade_readiness": "research_only",
                        "new_buy_allowed": False,
                        "next_step": "生成 AI 研报",
                    }
                ]
            },
            "last_ai_report": {
                "ok": True,
                "model": "gpt-test",
                "stock_count": 1,
                "reviewed_codes": ["300750"],
                "reviewed_symbols": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "action_status": "ready_for_ai_review",
                        "risk_factors": ["不直接买入"],
                    }
                ],
                "next_action": "研报已完成，可进入组合攻防决策",
            },
            "last_strategy_decision": {
                "ok": True,
                "status": "skipped_notify_unconfigured",
                "report_source": "last_ai_report",
                "candidate_count": 1,
                "reviewed_codes": ["300750"],
                "reviewed_symbols": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "action_status": "ready_for_ai_review",
                        "next_step": "等待通知配置后生成 OMS 工单",
                    }
                ],
                "candidate_guard_summary": {
                    "direct_buy_blocked_count": 1,
                    "message": "以下候选仅可复核或观察，禁止直接买入",
                    "candidates": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "reason": "候选标签未成熟，禁止直接买入",
                            "action_status": "ready_for_ai_review",
                            "trade_readiness": "research_only",
                            "new_buy_allowed": False,
                            "label_ready": False,
                            "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                        }
                    ],
                },
                "next_action": "补充 Telegram 配置后可发送攻防工单",
            },
        }
    )
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "候选扫描完成。"}],
            [],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_empty_synthesis",
        user_text="用 workflow 选出好股票",
        workflow_context=route_workflow("用 workflow 选出好股票"),
        workflow_script={"tasks": [{"id": "scan", "title": "扫描候选", "prompt": "扫描候选"}]},
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 选出好股票"}]))

    try:
        final_text = events[-1]["text"]
        assert "动态 workflow 已完成" in final_text
        assert "候选结论: 受限复核候选 300750 宁德时代" in final_text
        assert "状态=可进入AI复核" in final_text
        assert "交易就绪=research_only" in final_text
        assert "不允许新增买入" in final_text
        assert "证据=候选影子S/92" in final_text
        assert "护栏=候选标签未成熟，禁止直接买入" in final_text
        assert "下一步=等待通知配置后生成 OMS 工单" in final_text
        assert "候选扫描完成" in final_text
        assert "300750 宁德时代" in final_text
        assert "候选影子S/92" in final_text
        assert "下一步: 生成 AI 研报" in final_text
        assert "AI研报: reviewed=1, model=gpt-test, next=研报已完成，可进入组合攻防决策" in final_text
        assert "攻防决策: 未发送工单 · 来源: 上一轮AI研报" in final_text
        assert "候选护栏: 1只禁止直接买入" in final_text
        assert "候选标签未成熟，禁止直接买入" in final_text
        assert "等待通知配置后生成 OMS 工单" in final_text
    finally:
        _reset_local_db(local_db)


def test_workflow_fallback_labels_watch_only_quality_gate_candidate():
    reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {
                    "status": "completed",
                    "result": "候选扫描完成。",
                    "handoff_state": {
                        "last_screen_result": {
                            "selection_brief": {
                                "status": "watch_only",
                                "primary_pick": {
                                    "code": "000013",
                                    "name": "低质量候选",
                                    "action_status": "watch_only",
                                    "risk_adjusted_quality_score": 65.0,
                                    "next_step": "观察池跟踪，暂不进入本轮AI复核",
                                },
                            },
                            "quality_gate": {"status": "blocked_by_quality_gate", "reason": reason},
                            "watch_candidates": [{"code": "000013", "name": "低质量候选"}],
                        }
                    },
                },
            }
        ]
    )

    assert "候选结论: 观察候选 000013 低质量候选" in summary
    assert "首选 000013 低质量候选" not in summary
    assert f"护栏={reason}" in summary
    assert "下一步=观察池跟踪，暂不进入本轮AI复核" in summary


def test_workflow_handoff_and_fallback_prioritize_report_candidates_over_watch():
    tools = StubToolRegistry()
    tools._tool_context = SimpleNamespace(
        state={
            "last_screen_result": {
                "summary": {"report_candidates": 1, "watch_candidates": 1},
                "selection_brief": {
                    "status": "ready_for_ai_review",
                    "primary_pick": {"code": "000013", "name": "观察候选", "action_status": "watch_only"},
                    "best_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                },
                "symbols_for_report": [],
                "report_candidates": [
                    {
                        "code": "000014",
                        "name": "高质量候选",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_grade": "S",
                        "candidate_shadow_score": 92.0,
                        "next_step": "生成 AI 研报",
                    }
                ],
                "watch_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
            }
        }
    )

    handoff = _workflow_handoff_state(tools)
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {"status": "completed", "result": "扫描完成。", "handoff_state": handoff},
            }
        ]
    )

    screen = handoff["last_screen_result"]
    assert screen["report_candidates"][0]["code"] == "000014"
    assert screen["watch_candidates"][0]["code"] == "000013"
    assert "候选结论: 首选 000014 高质量候选" in summary
    assert "候选影子S/92" in summary
    assert "观察候选 000013 观察候选" not in summary.splitlines()[1]


def test_workflow_fallback_summary_keeps_multiple_candidate_conclusions():
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {
                    "status": "completed",
                    "result": "扫描完成。",
                    "handoff_state": {
                        "last_screen_result": {
                            "report_candidates": [
                                {
                                    "code": "000014",
                                    "name": "高质量候选",
                                    "action_status": "ready_for_ai_review",
                                    "candidate_shadow_score": 92.0,
                                    "candidate_shadow_grade": "S",
                                },
                                {
                                    "code": "000015",
                                    "name": "次优候选",
                                    "action_status": "ready_for_ai_review",
                                    "candidate_shadow_score": 88.0,
                                },
                            ],
                            "watch_candidates": [
                                {
                                    "code": "000013",
                                    "name": "观察候选",
                                    "action_status": "watch_only",
                                    "candidate_shadow_score": 96.0,
                                }
                            ],
                        }
                    },
                },
            }
        ]
    )

    assert "候选结论: 首选 000014 高质量候选" in summary
    assert "候选结论: 备选复核候选 000015 次优候选" in summary
    assert "候选结论: 观察候选 000013 观察候选" in summary
    assert summary.index("000014") < summary.index("000015") < summary.index("000013")


def test_workflow_candidate_conclusion_prefers_ready_high_score_over_first_watch():
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {
                    "status": "completed",
                    "result": "扫描完成。",
                    "handoff_state": {
                        "last_screen_result": {
                            "report_candidates": [
                                {
                                    "code": "000013",
                                    "name": "观察候选",
                                    "action_status": "watch_only",
                                    "candidate_shadow_score": 96.0,
                                },
                                {
                                    "code": "000014",
                                    "name": "高质量候选",
                                    "action_status": "ready_for_ai_review",
                                    "candidate_shadow_score": 92.0,
                                    "candidate_shadow_grade": "S",
                                },
                            ]
                        }
                    },
                },
            }
        ]
    )

    assert "候选结论: 首选 000014 高质量候选" in summary
    assert "候选影子S/92" in summary
    assert "观察候选 000013 观察候选" not in summary.splitlines()[1]


def test_workflow_candidate_conclusion_prefers_limited_review_over_watch():
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {
                    "status": "completed",
                    "result": "扫描完成。",
                    "handoff_state": {
                        "last_screen_result": {
                            "report_candidates": [
                                {
                                    "code": "000013",
                                    "name": "观察候选",
                                    "action_status": "watch_only",
                                    "candidate_shadow_score": 99.0,
                                },
                                {
                                    "code": "000014",
                                    "name": "修复候选",
                                    "action_status": "repair_review_only",
                                    "candidate_shadow_score": 88.0,
                                },
                            ]
                        }
                    },
                },
            }
        ]
    )

    assert "候选结论: 修复复核候选 000014 修复候选" in summary.splitlines()[1]
    assert "观察候选 000013 观察候选" not in summary.splitlines()[1]


def test_workflow_candidate_conclusion_prefers_higher_quality_within_same_status():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "000011",
                        "name": "低分候选",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_score": 72.0,
                    },
                    {
                        "code": "000012",
                        "name": "高分候选",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_score": 91.0,
                    },
                ]
            }
        }
    )

    assert conclusion["code"] == "000012"
    assert "候选结论: 首选 000012 高分候选" in conclusion["line"]
    assert conclusion["evidence"] == ["候选影子91"]


def test_workflow_candidate_conclusion_preserves_trigger_evidence():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "高分候选",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_score": 91.0,
                        "triggers": ["sos", "evr", "trend_pullback"],
                    }
                ]
            }
        }
    )

    assert conclusion["evidence"] == ["候选影子91", "触发=SOS+EVR+TrendPB"]
    assert "证据=候选影子91,触发=SOS+EVR+TrendPB" in conclusion["line"]


def test_workflow_candidate_conclusion_uses_profile_as_quality_context():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "画像候选",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_score": 91.0,
                        "profile": "趋势线 / 主升阶段 / 触发:SOS+EVR",
                    }
                ]
            }
        }
    )

    assert conclusion["evidence"] == ["候选影子91", "触发=SOS+EVR"]
    assert conclusion["quality_factors"] == ["趋势线", "主升阶段"]
    assert "证据=候选影子91,触发=SOS+EVR" in conclusion["line"]
    assert "亮点=趋势线,主升阶段" in conclusion["line"]


def test_workflow_candidate_conclusion_uses_rank_reason_as_quality_context():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "排序候选",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_score": 91.0,
                        "rank_reason": "推荐评估候选#1；候选影子评级 S",
                    }
                ]
            }
        }
    )

    assert conclusion["quality_factors"] == ["推荐评估候选#1", "候选影子评级 S"]
    assert "亮点=推荐评估候选#1,候选影子评级 S" in conclusion["line"]


def test_workflow_candidate_conclusion_labels_limited_review_statuses():
    repair = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "修复候选",
                        "action_status": "repair_review_only",
                        "candidate_shadow_score": 91.0,
                    }
                ]
            }
        }
    )
    confirmation = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "report_candidates": [
                    {
                        "code": "000013",
                        "name": "确认候选",
                        "action_status": "confirmation_required",
                        "candidate_shadow_score": 90.0,
                    }
                ]
            }
        }
    )

    assert "候选结论: 修复复核候选 000012 修复候选" in repair["line"]
    assert "护栏=候选状态 repair_review_only 不允许直接买入" in repair["line"]
    assert "候选结论: 待确认候选 000013 确认候选" in confirmation["line"]
    assert "护栏=候选状态 confirmation_required 不允许直接买入" in confirmation["line"]


def test_workflow_candidate_conclusion_preserves_entry_risk_flags():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_ai_report": {
                "reviewed_symbols": [
                    {
                        "code": "000012",
                        "name": "高分带风险",
                        "action_status": "ready_for_ai_review",
                        "candidate_shadow_score": 91.0,
                        "risk_factors": ["估值偏高"],
                        "entry_quality_risk_flags": ["估值偏高", "短线涨幅偏快"],
                    }
                ]
            }
        }
    )

    assert conclusion["risk_factors"] == ["估值偏高", "短线涨幅偏快"]
    assert "风险=估值偏高,短线涨幅偏快" in conclusion["line"]


def test_workflow_candidate_conclusion_preserves_theme_match_reasons():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "theme_preference": {"raw": "机器人", "theme": "机器人"},
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "机器人候选",
                        "action_status": "ready_for_ai_review",
                        "theme_match": True,
                        "theme_match_reasons": ["主题偏好: 机器人"],
                    }
                ],
            }
        }
    )

    assert conclusion["quality_factors"] == ["主题偏好: 机器人"]
    assert "亮点=主题偏好: 机器人" in conclusion["line"]


def test_workflow_candidate_conclusion_prioritizes_preference_reasons():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "theme_preference": {"raw": "机器人", "theme": "机器人"},
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "机器人候选",
                        "action_status": "ready_for_ai_review",
                        "style_match_reasons": ["趋势偏好: 趋势线"],
                        "theme_match_reasons": ["主题偏好: 机器人"],
                        "quality_factors": ["高质量研报候选", "候选影子评级 S", "入场质量评级 A", "主升阶段"],
                    }
                ],
            }
        }
    )

    assert conclusion["quality_factors"] == [
        "趋势偏好: 趋势线",
        "主题偏好: 机器人",
        "高质量研报候选",
        "候选影子评级 S",
    ]
    assert "亮点=趋势偏好: 趋势线,主题偏好: 机器人,高质量研报候选" in conclusion["line"]


def test_workflow_candidate_conclusion_preserves_preference_misses():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "style_preference": {"raw": "trend", "styles": ["trend"]},
                "theme_preference": {"raw": "机器人", "theme": "机器人"},
                "preference_match": {"style": "miss", "theme": "miss"},
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "非主题候选",
                        "action_status": "ready_for_ai_review",
                        "quality_factors": ["高质量研报候选"],
                    }
                ],
            }
        }
    )

    assert conclusion["preference_misses"] == ["风格偏好未命中: 趋势", "主题偏好未命中: 机器人"]
    assert "偏好=风格偏好未命中: 趋势,主题偏好未命中: 机器人" in conclusion["line"]


def test_workflow_candidate_conclusion_marks_row_miss_when_pool_has_preference_hit():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "theme_preference": {"raw": "机器人", "theme": "机器人"},
                "preference_match": {"theme": "hit"},
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "非主题候选",
                        "action_status": "ready_for_ai_review",
                        "quality_factors": ["高质量研报候选"],
                    }
                ],
                "top_candidates": [
                    {
                        "code": "000099",
                        "name": "机器人观察",
                        "action_status": "watch_only",
                        "theme_match": True,
                        "theme_match_reasons": ["主题偏好: 机器人"],
                    }
                ],
            }
        }
    )

    assert conclusion["code"] == "000012"
    assert conclusion["preference_misses"] == ["主题偏好未命中: 机器人"]
    assert "偏好=主题偏好未命中: 机器人" in conclusion["line"]


def test_workflow_candidate_conclusion_marks_missing_style_in_combined_preference():
    conclusion = _candidate_conclusion_from_handoff(
        {
            "last_screen_result": {
                "style_preference": {"raw": "trend,pullback", "styles": ["trend", "pullback"]},
                "report_candidates": [
                    {
                        "code": "000012",
                        "name": "纯趋势候选",
                        "action_status": "ready_for_ai_review",
                        "style_match": True,
                        "style_match_styles": ["trend"],
                        "style_match_reasons": ["趋势偏好: 趋势线"],
                    }
                ],
            }
        }
    )

    assert conclusion["preference_misses"] == ["风格偏好未命中: 低吸"]
    assert "偏好=风格偏好未命中: 低吸" in conclusion["line"]


def test_workflow_candidate_delivery_prepends_missing_preference_miss():
    results = [
        {
            "step": {"title": "扫描候选"},
            "result": {
                "status": "completed",
                "result": "扫描完成。",
                "handoff_state": {
                    "last_screen_result": {
                        "theme_preference": {"raw": "机器人", "theme": "机器人"},
                        "preference_match": {"theme": "miss"},
                        "report_candidates": [
                            {
                                "code": "000012",
                                "name": "非主题候选",
                                "action_status": "ready_for_ai_review",
                                "quality_factors": ["高质量研报候选"],
                            }
                        ],
                    }
                },
            },
        }
    ]

    text = _ensure_candidate_delivery("000012 非主题候选，高质量研报候选。", results)

    assert text.startswith("候选结论: 首选 000012 非主题候选")
    assert "主题偏好未命中: 机器人" in text.split("\n\n", 1)[0]


def test_workflow_handoff_lines_surface_preference_alternatives():
    lines = _fallback_handoff_lines(
        {
            "last_screen_result": {
                "theme_preference": {"raw": "机器人", "theme": "机器人"},
                "preference_match": {"theme": "hit"},
                "selection_brief": {
                    "primary_pick": {
                        "code": "000012",
                        "name": "非主题候选",
                        "action_status": "ready_for_ai_review",
                        "quality_factors": ["高质量研报候选"],
                    },
                    "preference_alternatives": [
                        {
                            "code": "000099",
                            "name": "机器人观察",
                            "action_status": "watch_only",
                            "theme_match": True,
                            "theme_match_reasons": ["主题偏好: 机器人"],
                        }
                    ],
                },
            }
        }
    )

    assert any("偏好命中观察: 000099 机器人观察" in line for line in lines)


def test_workflow_fallback_handoff_lines_keep_each_stage_when_truncated():
    handoff = {
        "last_screen_result": {
            "selection_brief": {
                "headline": "候选池已形成",
                "best_candidates": [
                    {"code": "300750", "name": "宁德时代", "action_status": "ready_for_ai_review"},
                    {"code": "000001", "name": "平安银行", "action_status": "watch_only"},
                    {"code": "000002", "name": "万科A", "action_status": "watch_only"},
                ],
            }
        },
        "last_recommendation_event_eval": {
            "result_summary": "推荐评估完成\n样本仍需继续观察",
            "summary": {
                "all": {"rows_ready": 12, "rows_total": 20, "hit_rate_pct": 60},
                "ranking_decision": {
                    "status": "candidate",
                    "recommended_strategy": "candidate_shadow_then_score",
                    "recommended_top_k": 1,
                },
            },
            "policy_selection": {
                "selection_strategy": "candidate_shadow_then_score",
                "recommend_date": "2026-06-30",
                "picks": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "label_ready": False,
                        "next_step": "生成 AI 研报",
                    }
                ],
            },
            "candidate_guard_summary": {
                "direct_buy_blocked_count": 1,
                "message": "以下候选仅可复核或观察，禁止直接买入",
                "candidates": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "reason": "候选标签未成熟，禁止直接买入",
                        "label_ready": False,
                    }
                ],
            },
        },
        "last_ai_report": {
            "model": "gpt-test",
            "stock_count": 1,
            "reviewed_symbols": [
                {"code": "300750", "name": "宁德时代", "risk_factors": ["不直接买入"]},
                {"code": "000001", "name": "平安银行", "risk_factors": ["观察"]},
            ],
            "next_action": "研报完成，进入攻防决策",
        },
        "last_strategy_decision": {
            "status": "skipped_notify_unconfigured",
            "report_source": "last_ai_report",
            "candidate_count": 1,
            "reviewed_symbols": [{"code": "300750", "name": "宁德时代", "next_step": "等待通知配置后生成 OMS 工单"}],
            "next_action": "补充 Telegram 配置后可发送攻防工单",
        },
    }

    lines = _fallback_handoff_lines(handoff)

    assert len(lines) == 8
    assert lines[0] == "候选池已形成"
    assert "推荐评估完成" in lines
    assert any("候选护栏: 1只禁止直接买入" in line for line in lines)
    assert any(line.startswith("AI研报:") for line in lines)
    assert any(line.startswith("攻防决策:") for line in lines)
    assert any("补充 Telegram 配置后可发送攻防工单" in line for line in lines)


def test_workflow_executor_waits_step_background_tasks_for_handoff(tmp_path, monkeypatch):
    from integrations import local_db

    class BackgroundHandoffTools(StubToolRegistry):
        def __init__(self):
            super().__init__(tool_results={"screen_stocks": {"status": "background", "task_id": "bg_screen"}})
            self._tool_context = SimpleNamespace(state={})
            self.wait_calls: list[dict[str, object]] = []

        def wait_background_tasks(self, task_ids, timeout_seconds=30.0):
            self.wait_calls.append({"task_ids": list(task_ids), "timeout_seconds": timeout_seconds})
            self._tool_context.state["last_screen_result"] = {
                "selection_brief": {"status": "ready_for_ai_review", "best_codes": ["300750"]},
                "symbols_for_report": [{"code": "300750", "name": "宁德时代"}],
            }
            return [
                {
                    "task_id": "bg_screen",
                    "tool_name": "screen_stocks",
                    "status": "completed",
                    "result_summary": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
                }
            ]

    def _synthesis_round(messages, _tools, _system_prompt):
        content = messages[0]["content"]
        assert '"background_task_ids": ["bg_screen"]' in content
        assert '"background_tasks": [{"task_id": "bg_screen"' in content
        assert '"symbols_for_report": [{"code": "300750"' in content
        return [{"type": "text_delta", "text": "已等待后台筛选并汇总候选。"}]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-bg-wait.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    monkeypatch.setenv("WYCKOFF_WORKFLOW_BG_WAIT_SECONDS", "2")
    tools = BackgroundHandoffTools()
    provider = ScriptedProvider(
        rounds=[
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {}}]}],
            [{"type": "text_delta", "text": "筛选已提交后台。"}],
            _synthesis_round,
        ]
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_bg_wait",
        user_text="用 workflow 选出好股票",
        workflow_context=route_workflow("用 workflow 选出好股票"),
        workflow_script={
            "tasks": [{"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描候选"}]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 选出好股票"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert tools.wait_calls == [{"task_ids": ["bg_screen"], "timeout_seconds": 2.0}]
        assert detail["background_task_ids"] == ["bg_screen"]
        assert detail["background_tasks"][0]["status"] == "completed"
        assert detail["handoff_state"]["last_screen_result"]["selection_brief"]["best_codes"] == ["300750"]
        assert "screen_stocks: 本轮首选可进入 AI 研报复核: 300750 宁德时代" in done_event["step"]["summary"]
        assert events[-1]["text"].startswith("候选结论: 候选 300750 宁德时代")
        assert "已等待后台筛选并汇总候选。" in events[-1]["text"]
    finally:
        _reset_local_db(local_db)


def test_workflow_fallback_summary_includes_completed_background_result_summary():
    summary = _fallback_summary(
        [
            {
                "step": {"title": "扫描候选"},
                "result": {
                    "status": "completed",
                    "result": "筛选已提交后台。",
                    "background_tasks": [
                        {
                            "task_id": "bg_screen",
                            "tool_name": "screen_stocks",
                            "status": "completed",
                            "result_summary": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
                        }
                    ],
                },
            }
        ]
    )

    assert "扫描候选 [completed]: 筛选已提交后台。" in summary
    assert "后台: screen_stocks [completed]: 本轮首选可进入 AI 研报复核: 300750 宁德时代" in summary


def test_workflow_executor_persists_plan_and_steps(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": _PLAN_JSON}],
            [{"type": "text_delta", "text": _PLAN_JSON}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "diagnose"}}],
                }
            ],
            [{"type": "text_delta", "text": "持仓风险低，继续观察。"}],
            [{"type": "text_delta", "text": "持仓复盘完成。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})

    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s1",
        user_text="用 workflow 复盘我的持仓",
    )
    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 复盘我的持仓"}]))

    assert executor.run is not None
    run = get_workflow_run(executor.run.run_id)
    stored_events = load_workflow_events(executor.run.run_id)
    try:
        assert events[0]["type"] == "workflow_plan"
        assert events[0]["label"] == "持仓复盘"
        assert events[0]["route"]["reason"] == "用户显式要求动态 workflow"
        assert events[0]["plan"]["route"]["matches"] == ["用 workflow"]
        assert events[0]["plan"]["label"] == "持仓复盘"
        assert events[0]["plan"]["script"]["title"] == "持仓复盘"
        assert events[0]["plan"]["steps"][0]["agent"] == "task"
        assert events[0]["plan"]["steps"][0]["tool_scope"] == ["portfolio"]
        assert "portfolio" in events[0]["plan"]["steps"][0]["effective_tool_scope"]
        assert any(event["type"] == "workflow_step_start" for event in events)
        assert any(event["type"] == "workflow_done" for event in events)
        assert events[-1]["type"] == "done"
        assert events[-1]["text"] == "持仓复盘完成。"
        assert provider.calls[1]["system_prompt"] == _REPAIR_SYSTEM_PROMPT
        assert "只看核心仓位" in provider.calls[2]["messages"][0]["content"]
        assert "汇总持仓风险和下一步动作" in provider.calls[4]["messages"][0]["content"]
        assert run and run["status"] == "completed"
        assert run["workflow"] == "dynamic_task"
        assert run["label"] == "持仓复盘"
        assert run["plan"]["script"]["runtime"]["script_path"].startswith(str(tmp_path / "workflow-runs"))
        assert (tmp_path / "workflow-runs" / "s1" / f"{executor.run.run_id}.json").is_file()
        assert "措辞恢复" not in provider.calls[0]["system_prompt"]
        assert "错别字" in provider.calls[0]["system_prompt"]
        assert "运行上下文" in provider.calls[0]["messages"][0]["content"]
        assert stored_events[0]["event_type"] == "workflow_plan"
        done_event = next(row for row in stored_events if row["event_type"] == "workflow_step_done")
        detail = done_event["payload"]["source"]["agent_detail"]
        assert detail["step_id"] == "read_positions"
        assert detail["tool_calls"] == ["portfolio"]
        assert "持仓风险低" in detail["result"]
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_adapts_remaining_steps_after_phase_results(tmp_path, monkeypatch):
    from integrations import local_db

    initial_plan = {
        "title": "自适应选股",
        "rationale": "先收集候选，再按真实结果决定后续。",
        "runtime": {"adaptive": True},
        "phases": [
            {
                "id": "scan",
                "tasks": [{"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描候选"}],
            },
            {
                "id": "followup",
                "tasks": [
                    {
                        "id": "report",
                        "title": "生成候选研报",
                        "tools": ["generate_ai_report"],
                        "depends_on": ["scan"],
                        "prompt": "基于候选生成研报",
                    }
                ],
            },
        ],
        "synthesis_prompt": "汇总候选后续。",
    }
    adapted_plan = {
        "title": "改为攻防计划",
        "rationale": "扫描结果显示候选已经明确，下一步直接给攻防边界。",
        "phases": [
            {
                "id": "decision",
                "tasks": [
                    {
                        "id": "decision",
                        "title": "形成攻防计划",
                        "tools": ["generate_strategy_decision"],
                        "depends_on": ["scan"],
                        "prompt": "基于扫描结果给 300750 输出触发位、失效位和下一步动作。",
                    }
                ],
            }
        ],
        "synthesis_prompt": "输出候选、攻防边界和下一步。",
    }
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": json.dumps(initial_plan, ensure_ascii=False)}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {}}],
                }
            ],
            [{"type": "text_delta", "text": "扫描完成：300750 候选明确，不需要研报。"}],
            [{"type": "text_delta", "text": json.dumps(adapted_plan, ensure_ascii=False)}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_decision", "name": "generate_strategy_decision", "args": {}}],
                }
            ],
            [{"type": "text_delta", "text": "攻防完成：等待回踩确认，跌破结构失效。"}],
            [{"type": "text_delta", "text": "300750 可复核，下一步等回踩确认。"}],
        ]
    )
    context = WorkflowContext(name="dynamic_task", label="动态任务")

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-adaptive.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_adaptive",
        user_text="帮我找几个好票，后续按结果调整",
        workflow_context=context,
    )
    events = list(executor.run_stream([{"role": "user", "content": "帮我找几个好票，后续按结果调整"}]))

    try:
        update = next(event for event in events if event["type"] == "workflow_plan_update")
        step_ids = [step["step_id"] for step in update["plan"]["steps"]]
        run = get_workflow_run(update["run_id"])
        stored_events = load_workflow_events(update["run_id"], limit=30)

        assert step_ids == ["scan", "decision"]
        assert update["plan"]["steps"][0]["status"] == "completed"
        assert update["plan"]["steps"][1]["status"] == "pending"
        assert update["plan"]["script"]["runtime"]["adaptation"] == "model_phase"
        assert update["plan"]["script"]["runtime"]["adaptation_count"] == 1
        assert update["plan"]["script"]["runtime"]["last_adaptation_title"] == "改为攻防计划"
        assert update["plan"]["script"]["runtime"]["adapted_previous_step_count"] == 1
        assert update["plan"]["script"]["runtime"]["adapted_continuation_step_count"] == 1
        assert update["plan"]["script"]["runtime"]["adapted_removed_step_count"] == 1
        assert update["plan"]["script"]["runtime"]["adapted_added_step_count"] == 1
        assert update["plan"]["script"]["runtime"]["adapted_removed_step_ids"] == ["report"]
        assert update["plan"]["script"]["runtime"]["adapted_added_step_ids"] == ["decision"]
        assert update["plan"]["script"]["runtime"]["adapted_removed_steps"] == [
            {"id": "report", "title": "生成候选研报"}
        ]
        assert update["plan"]["script"]["runtime"]["adapted_added_steps"] == [
            {"id": "decision", "title": "形成攻防计划"}
        ]
        assert "report" not in step_ids
        assert any(row["event_type"] == "workflow_plan_update" for row in stored_events)
        assert run and [step["step_id"] for step in run["plan"]["steps"]] == ["scan", "decision"]
        assert "已完成结果" in provider.calls[3]["messages"][0]["content"]
        assert "生成候选研报" in provider.calls[3]["messages"][0]["content"]
        assert provider.calls[4]["messages"][0]["content"].startswith("基于扫描结果给 300750")
        assert events[-1]["text"] == "300750 可复核，下一步等回踩确认。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_can_stop_remaining_steps_after_adaptation(tmp_path, monkeypatch):
    from integrations import local_db

    initial_plan = {
        "title": "自适应复核",
        "runtime": {"adaptive": True},
        "phases": [
            {
                "id": "scan",
                "tasks": [{"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描候选"}],
            },
            {
                "id": "report",
                "tasks": [{"id": "report", "title": "生成研报", "tools": ["generate_ai_report"], "prompt": "生成研报"}],
            },
        ],
    }
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": json.dumps(initial_plan, ensure_ascii=False)}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {}}],
                }
            ],
            [{"type": "text_delta", "text": "没有可靠候选，后续研报不应继续。"}],
            [
                {
                    "type": "text_delta",
                    "text": json.dumps(
                        {"complete": True, "synthesis_prompt": "说明没有可靠候选和下一步修复动作。"},
                        ensure_ascii=False,
                    ),
                }
            ],
            [{"type": "text_delta", "text": "没有可靠候选，先修复数据质量。"}],
        ]
    )
    context = WorkflowContext(name="dynamic_task", label="动态任务")

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-adaptive-complete.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_adaptive_complete",
        user_text="帮我筛候选，没有可靠候选就别继续",
        workflow_context=context,
    )
    events = list(executor.run_stream([{"role": "user", "content": "帮我筛候选，没有可靠候选就别继续"}]))

    try:
        update = next(event for event in events if event["type"] == "workflow_plan_update")
        statuses = {step["step_id"]: step["status"] for step in update["plan"]["steps"]}

        assert statuses == {"scan": "completed", "report": "skipped"}
        assert update["plan"]["script"]["runtime"]["adaptation_complete"] is True
        assert update["plan"]["script"]["runtime"]["adapted_skipped_step_count"] == 1
        assert update["plan"]["script"]["runtime"]["adapted_removed_step_ids"] == ["report"]
        assert update["plan"]["script"]["synthesis_prompt"] == "说明没有可靠候选和下一步修复动作。"
        assert len(provider.calls) == 5
        assert events[-1]["text"] == "没有可靠候选，先修复数据质量。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_scopes_sub_agent_tools_per_step(tmp_path, monkeypatch):
    from integrations import local_db

    def _portfolio_only_round(_messages, tools, _system_prompt):
        assert {schema["name"] for schema in tools} == {"portfolio"}
        return [{"type": "tool_calls", "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {}}]}]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    schemas = [
        {"name": "portfolio", "description": "Mock portfolio tool", "parameters": {"type": "object"}},
        {"name": "analyze_stock", "description": "Mock analyze tool", "parameters": {"type": "object"}},
        {"name": "generate_ai_report", "description": "Mock report tool", "parameters": {"type": "object"}},
    ]
    provider = ScriptedProvider(
        rounds=[
            _portfolio_only_round,
            [{"type": "text_delta", "text": "已读取持仓。"}],
            [{"type": "text_delta", "text": "持仓摘要完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(schemas=schemas, tool_results={"portfolio": {"positions": []}}),
        session_id="s_scope",
        user_text="只读取持仓",
        workflow_context=route_workflow("用 workflow 复核持仓"),
        workflow_script=json.loads(_SCOPED_TOOL_PLAN_JSON),
    )

    events = list(executor.run_stream([{"role": "user", "content": "只读取持仓"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert events[0]["plan"]["steps"][0]["tool_scope"] == ["portfolio"]
        assert detail["tool_scope"] == ["portfolio"]
        assert detail["tool_calls"] == ["portfolio"]
        assert events[-1]["text"] == "持仓摘要完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_read_positions_task_until_portfolio_runs(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-read-positions-expectation.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先口头整理持仓。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}]}],
            [{"type": "text_delta", "text": "持仓工具已读取。"}],
            [{"type": "text_delta", "text": "已汇总持仓结果。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "portfolio", "description": "Mock portfolio", "parameters": {"type": "object"}},
        ],
        tool_results={"portfolio": {"positions": []}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_read_positions_expectation",
        user_text="用 workflow 读取真实持仓",
        workflow_context=route_workflow("用 workflow 读取真实持仓"),
        workflow_script={
            "tasks": [{"id": "positions", "title": "读取真实持仓", "tools": ["portfolio"], "prompt": "读取真实持仓"}]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 读取真实持仓"}]))

    try:
        detail = next(event for event in events if event["type"] == "workflow_step_done")["source"]["agent_detail"]
        assert detail["tool_scope"] == ["portfolio"]
        assert detail["tool_calls"] == ["portfolio"]
        assert [call["name"] for call in tools.calls] == ["portfolio"]
        assert events[-1]["text"] == "已汇总持仓结果。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_scoped_report_task_until_tool_runs(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-report-expectation.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先口头整理研报。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_report", "name": "generate_ai_report", "args": {}}]}],
            [{"type": "text_delta", "text": "研报工具已完成。"}],
            [{"type": "text_delta", "text": "已汇总研报结果。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "generate_ai_report", "description": "Mock report", "parameters": {"type": "object"}},
        ],
        tool_results={"generate_ai_report": {"reviewed_codes": ["300750"]}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_report_expectation",
        user_text="用 workflow 生成研报",
        workflow_context=route_workflow("用 workflow 生成研报"),
        workflow_script={
            "tasks": [{"id": "report", "title": "生成研报", "tools": ["generate_ai_report"], "prompt": "生成研报"}]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 生成研报"}]))

    try:
        detail = next(event for event in events if event["type"] == "workflow_step_done")["source"]["agent_detail"]
        assert detail["tool_scope"] == ["generate_ai_report"]
        assert detail["tool_calls"] == ["generate_ai_report"]
        assert [call["name"] for call in tools.calls] == ["generate_ai_report"]
        assert events[-1]["text"] == "已汇总研报结果。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_stock_diagnosis_until_analyze_stock_runs(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-diagnosis-expectation.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先口头判断结构。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_analyze", "name": "analyze_stock", "args": {"code": "300750", "mode": "diagnose"}}
                    ],
                }
            ],
            [{"type": "text_delta", "text": "个股诊断工具已完成。"}],
            [{"type": "text_delta", "text": "已汇总诊断结果。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "analyze_stock", "description": "Mock analyze", "parameters": {"type": "object"}},
        ],
        tool_results={"analyze_stock": {"code": "300750", "stage": "Markup"}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_analyze_expectation",
        user_text="用 workflow 诊断 300750",
        workflow_context=route_workflow("用 workflow 诊断 300750"),
        workflow_script={
            "tasks": [
                {
                    "id": "diagnose",
                    "title": "诊断 300750",
                    "tools": ["analyze_stock"],
                    "args": {"code": "300750", "mode": "diagnose"},
                    "prompt": "诊断 300750",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 诊断 300750"}]))

    try:
        detail = next(event for event in events if event["type"] == "workflow_step_done")["source"]["agent_detail"]
        assert detail["tool_scope"] == ["analyze_stock"]
        assert detail["tool_calls"] == ["analyze_stock"]
        assert [call["name"] for call in tools.calls] == ["analyze_stock"]
        assert events[-1]["text"] == "已汇总诊断结果。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_requires_each_handoff_diagnosis_target(tmp_path, monkeypatch):
    from integrations import local_db

    class HandoffTools(StubToolRegistry):
        def __init__(self):
            super().__init__(
                schemas=[
                    {"name": "screen_stocks", "description": "Mock screen", "parameters": {"type": "object"}},
                    {"name": "analyze_stock", "description": "Mock analyze", "parameters": {"type": "object"}},
                ],
                tool_results={
                    "screen_stocks": {"candidate_count": 3},
                    "analyze_stock": lambda _name, args: {"code": args["code"], "stage": "Markup"},
                },
                concurrency_safe_tools=set(),
            )
            self._tool_context = SimpleNamespace(state={})

        def execute(self, name, args, messages=None):
            result = super().execute(name, args, messages=messages)
            if name == "screen_stocks":
                self._tool_context.state["last_screen_result"] = {
                    "selection_brief": {"status": "ready_for_ai_review", "best_codes": ["002001"]},
                    "diagnosis_targets": [
                        {"tool": "analyze_stock", "args": {"code": "002001", "mode": "diagnose"}},
                        {"tool": "analyze_stock", "args": {"code": "002002", "mode": "diagnose"}},
                        {"tool": "analyze_stock", "args": {"code": "002003", "mode": "diagnose"}},
                    ],
                }
            return result

    def _remaining_diagnosis_round(messages, _tools, _system_prompt):
        prompt = messages[-1]["content"]
        assert "002002" in prompt
        assert "002003" in prompt
        return [
            {
                "type": "tool_calls",
                "tool_calls": [
                    {"id": "tc_analyze_2", "name": "analyze_stock", "args": {"code": "002002", "mode": "diagnose"}},
                    {"id": "tc_analyze_3", "name": "analyze_stock", "args": {"code": "002003", "mode": "diagnose"}},
                ],
            }
        ]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-diagnosis-targets.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {}}]}],
            [{"type": "text_delta", "text": "筛选完成。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_analyze_1", "name": "analyze_stock", "args": {"code": "002001", "mode": "diagnose"}}
                    ],
                }
            ],
            _remaining_diagnosis_round,
            [{"type": "text_delta", "text": "三个候选都已诊断。"}],
            [{"type": "text_delta", "text": "已汇总三只候选诊断。"}],
        ]
    )
    tools = HandoffTools()
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_handoff_diagnosis_targets",
        user_text="用 workflow 筛选并诊断候选",
        workflow_context=route_workflow("用 workflow 筛选并诊断候选"),
        workflow_script={
            "tasks": [
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描候选"},
                {
                    "id": "diagnose",
                    "title": "逐个诊断候选",
                    "tools": ["analyze_stock"],
                    "depends_on": ["scan"],
                    "prompt": "逐个诊断候选",
                },
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 筛选并诊断候选"}]))

    try:
        diagnose_detail = [
            event["source"]["agent_detail"] for event in events if event["type"] == "workflow_step_done"
        ][1]
        assert diagnose_detail["tool_calls"] == ["analyze_stock", "analyze_stock", "analyze_stock"]
        assert [call["args"].get("code") for call in tools.calls if call["name"] == "analyze_stock"] == [
            "002001",
            "002002",
            "002003",
        ]
        assert events[-1]["text"] == "已汇总三只候选诊断。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_market_scope_until_tool_runs(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-market-expectation.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先口头判断市场环境。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_market", "name": "get_market_overview", "args": {}}]}],
            [{"type": "text_delta", "text": "市场工具已读取。"}],
            [{"type": "text_delta", "text": "已汇总市场环境。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "get_market_overview", "description": "Mock market", "parameters": {"type": "object"}},
        ],
        tool_results={"get_market_overview": {"status": "ok"}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_market_expectation",
        user_text="用 workflow 读取市场环境",
        workflow_context=route_workflow("用 workflow 读取市场环境"),
        workflow_script={
            "tasks": [
                {
                    "id": "market",
                    "title": "读取市场环境",
                    "tools": ["get_market_overview"],
                    "prompt": "读取市场环境",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 读取市场环境"}]))

    try:
        detail = next(event for event in events if event["type"] == "workflow_step_done")["source"]["agent_detail"]
        assert detail["tool_scope"] == ["get_market_overview"]
        assert detail["tool_calls"] == ["get_market_overview"]
        assert [call["name"] for call in tools.calls] == ["get_market_overview"]
        assert events[-1]["text"] == "已汇总市场环境。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_retries_backtest_scope_until_tool_runs(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-backtest-expectation.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先口头整理回测口径。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_backtest", "name": "run_backtest", "args": {}}]}],
            [{"type": "text_delta", "text": "回测工具已执行。"}],
            [{"type": "text_delta", "text": "已汇总回测结果。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "run_backtest", "description": "Mock backtest", "parameters": {"type": "object"}},
        ],
        tool_results={"run_backtest": {"status": "ok"}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_backtest_expectation",
        user_text="用 workflow 跑策略回测",
        workflow_context=route_workflow("用 workflow 跑策略回测"),
        workflow_script={
            "tasks": [{"id": "backtest", "title": "跑策略回测", "tools": ["run_backtest"], "prompt": "跑策略回测"}]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 跑策略回测"}]))

    try:
        detail = next(event for event in events if event["type"] == "workflow_step_done")["source"]["agent_detail"]
        assert detail["tool_scope"] == ["run_backtest"]
        assert detail["tool_calls"] == ["run_backtest"]
        assert [call["name"] for call in tools.calls] == ["run_backtest"]
        assert events[-1]["text"] == "已汇总回测结果。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_filters_disallowed_explicit_tool_scope_without_widening(tmp_path, monkeypatch):
    from integrations import local_db

    def _blocked_round(messages, tools, _system_prompt):
        assert tools == []
        return [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc_exec", "name": "exec_command", "args": {"command": "pwd"}}],
            }
        ]

    def _after_block_round(messages, tools, _system_prompt):
        assert tools == []
        assert any(
            message.get("role") == "tool" and "不在当前 workflow 允许范围内" in message.get("content", "")
            for message in messages
        )
        return [{"type": "text_delta", "text": "已阻止越权工具。"}]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-filtered-scope.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            _blocked_round,
            _after_block_round,
            [{"type": "text_delta", "text": "越权工具已被 workflow 边界拦截。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "exec_command", "description": "Mock command", "parameters": {"type": "object"}},
            {"name": "screen_stocks", "description": "Mock screen", "parameters": {"type": "object"}},
        ],
        tool_results={"exec_command": {"status": "should_not_run"}},
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_filtered_scope",
        user_text="用 workflow 跑本地命令",
        workflow_context=route_workflow("用 workflow 跑本地命令"),
        workflow_script={
            "tasks": [{"id": "local", "title": "本地命令", "tools": ["exec_command"], "prompt": "尝试运行命令"}]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 跑本地命令"}]))

    try:
        detail = next(event for event in events if event["type"] == "workflow_step_done")["source"]["agent_detail"]
        plan_step = events[0]["plan"]["steps"][0]
        assert plan_step["tool_scope"] == []
        assert "effective_tool_scope" not in plan_step
        assert detail["tool_scope"] == []
        assert detail["result"] == "已阻止越权工具。"
        assert tools.calls == []
        assert events[-1]["text"] == "越权工具已被 workflow 边界拦截。"
    finally:
        _reset_local_db(local_db)


def test_workflow_plan_surfaces_effective_tool_scope_for_generic_task(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-effective-scope.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "按持仓上下文完成。"}],
            [{"type": "text_delta", "text": "复盘完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_effective_scope",
        user_text="复盘持仓",
        workflow_context=WORKFLOWS["portfolio_review"],
        workflow_script={"tasks": [{"id": "review", "title": "复盘持仓", "prompt": "读取事实后复盘持仓"}]},
    )

    events = list(executor.run_stream([{"role": "user", "content": "复盘持仓"}]))

    try:
        plan_step = events[0]["plan"]["steps"][0]
        done_step = next(event for event in events if event["type"] == "workflow_step_done")["step"]
        assert plan_step["tool_scope"] == []
        assert "portfolio" in plan_step["effective_tool_scope"]
        assert "analyze_stock" in plan_step["effective_tool_scope"]
        assert done_step["effective_tool_scope"] == plan_step["effective_tool_scope"]
    finally:
        _reset_local_db(local_db)


def test_workflow_portfolio_scope_blocks_question_before_reading_positions(tmp_path, monkeypatch):
    from integrations import local_db

    def _portfolio_round(_messages, tools, _system_prompt):
        assert {schema["name"] for schema in tools} == {"portfolio"}
        return [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
            }
        ]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-portfolio-question.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            _portfolio_round,
            [{"type": "text_delta", "text": "已读取持仓。"}],
            [{"type": "text_delta", "text": "持仓摘要完成。"}],
        ]
    )
    schemas = [
        {"name": "portfolio", "description": "Mock portfolio tool", "parameters": {"type": "object"}},
        {"name": "ask_user_question", "description": "Mock ask tool", "parameters": {"type": "object"}},
    ]
    tools = StubToolRegistry(schemas=schemas, tool_results={"portfolio": {"positions": []}})
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_portfolio_question",
        user_text="你看我持仓呀",
        workflow_context=route_workflow("用 workflow 复核持仓"),
        workflow_script={
            "tasks": [
                {
                    "id": "read_positions",
                    "title": "读取持仓",
                    "tools": ["portfolio", "ask_user_question"],
                    "prompt": "查看持仓并输出摘要",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "你看我持仓呀"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert events[0]["plan"]["steps"][0]["tool_scope"] == ["portfolio"]
        assert detail["tool_calls"] == ["portfolio"]
        assert [call["name"] for call in tools.calls] == ["portfolio"]
        assert events[-1]["text"] == "持仓摘要完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_bounds_generic_task_tools_by_workflow_context(tmp_path, monkeypatch):
    from integrations import local_db

    def _bounded_round(_messages, tools, _system_prompt):
        exposed = {schema["name"] for schema in tools}
        assert "portfolio" in exposed
        assert "ask_user_question" not in exposed
        assert "run_backtest" not in exposed
        return [{"type": "text_delta", "text": "已按持仓上下文复盘。"}]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            _bounded_round,
            [{"type": "text_delta", "text": "复盘完成。"}],
        ]
    )
    schemas = [
        {"name": "portfolio", "description": "Mock portfolio tool", "parameters": {"type": "object"}},
        {"name": "run_backtest", "description": "Mock backtest tool", "parameters": {"type": "object"}},
        {"name": "ask_user_question", "description": "Mock ask tool", "parameters": {"type": "object"}},
    ]
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(schemas=schemas, tool_results={"portfolio": {"positions": []}}),
        session_id="s_bound",
        user_text="复盘持仓",
        workflow_context=WORKFLOWS["portfolio_review"],
        workflow_script={
            "title": "持仓动态任务",
            "tasks": [{"id": "review", "title": "复盘持仓", "prompt": "读取持仓并判断风险"}],
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "复盘持仓"}]))

    try:
        assert events[0]["plan"]["steps"][0]["agent"] == "task"
        assert "ask_user_question" not in events[0]["plan"]["steps"][0]["effective_tool_scope"]
        assert events[-1]["text"] == "复盘完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_keeps_explicit_clarification_scope(tmp_path, monkeypatch):
    from integrations import local_db

    def _ask_round(_messages, tools, _system_prompt):
        assert {schema["name"] for schema in tools} == {"ask_user_question"}
        return [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc_ask", "name": "ask_user_question", "args": {"question": "回测区间？"}}],
            }
        ]

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-explicit-ask.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            _ask_round,
            [{"type": "text_delta", "text": "已确认需要回测区间。"}],
            [{"type": "text_delta", "text": "澄清完成。"}],
        ]
    )
    schemas = [
        {"name": "portfolio", "description": "Mock portfolio tool", "parameters": {"type": "object"}},
        {"name": "ask_user_question", "description": "Mock ask tool", "parameters": {"type": "object"}},
    ]
    tools = StubToolRegistry(schemas=schemas, tool_results={"ask_user_question": {"status": "queued"}})
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_explicit_ask",
        user_text="用 workflow 先问清楚回测区间",
        workflow_context=route_workflow("用 workflow 先问清楚回测区间"),
        workflow_script={
            "tasks": [
                {
                    "id": "clarify",
                    "title": "确认回测区间",
                    "tools": ["ask_user_question"],
                    "prompt": "询问用户回测区间。",
                }
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 先问清楚回测区间"}]))

    try:
        done_event = next(event for event in events if event["type"] == "workflow_step_done")
        detail = done_event["source"]["agent_detail"]
        assert events[0]["plan"]["steps"][0]["effective_tool_scope"] == ["ask_user_question"]
        assert detail["tool_calls"] == ["ask_user_question"]
        assert [call["name"] for call in tools.calls] == ["ask_user_question"]
        assert events[-1]["text"] == "澄清完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_reruns_stored_script_without_replanning(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "diagnose"}}],
                }
            ],
            [{"type": "text_delta", "text": "复跑后的持仓风险低。"}],
            [{"type": "text_delta", "text": "已按原脚本复跑完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(tool_results={"portfolio": {"positions": []}}),
        session_id="s2",
        user_text="复跑 workflow wf_old",
        workflow_context=route_workflow("用 workflow 复盘我的持仓"),
        workflow_script=json.loads(_PLAN_JSON),
        source_run_id="wf_old",
    )

    events = list(executor.run_stream([{"role": "user", "content": "复跑 workflow wf_old"}]))

    try:
        assert events[0]["type"] == "workflow_plan"
        assert events[0]["plan"]["script"]["runtime"]["rerun_of"] == "wf_old"
        assert events[-1]["text"] == "已按原脚本复跑完成。"
        assert len(provider.calls) == 3
        assert "动态 workflow 编排器" not in provider.calls[0]["system_prompt"]
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_runs_same_phase_tasks_in_parallel(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "研究视角完成。"}],
            [{"type": "text_delta", "text": "结构视角完成。"}],
            [{"type": "text_delta", "text": "两个视角已合并。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s3",
        user_text="并发复核 300750",
        workflow_context=route_workflow("用 workflow 并发复核 300750"),
        workflow_script=json.loads(_PARALLEL_PLAN_JSON),
        workflow_args="300750",
    )

    events = list(executor.run_stream([{"role": "user", "content": "并发复核 300750"}]))

    try:
        phase_start = next(event for event in events if event["type"] == "workflow_phase_start")
        step_starts = [event for event in events if event["type"] == "workflow_step_start"]
        step_dones = [event for event in events if event["type"] == "workflow_step_done"]
        assert phase_start["parallel"] is True
        assert len(step_starts) == 2
        assert len(step_dones) == 2
        assert events.index(step_starts[0]) < events.index(step_dones[0])
        assert events.index(step_starts[1]) < events.index(step_dones[0])
        assert "本次运行输入" in provider.calls[0]["messages"][0]["content"]
        assert "300750" in provider.calls[0]["messages"][0]["content"]
        assert events[-1]["text"] == "两个视角已合并。"
    finally:
        _reset_local_db(local_db)


def test_parallel_workflow_synthesis_keeps_script_order(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ParallelResultProvider()
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_parallel_order",
        user_text="并发复核 300750",
        workflow_context=route_workflow("用 workflow 并发复核 300750"),
        workflow_script=json.loads(_PARALLEL_PLAN_JSON),
    )

    events = list(executor.run_stream([{"role": "user", "content": "并发复核 300750"}]))

    try:
        done_steps = [event["step"]["step_id"] for event in events if event["type"] == "workflow_step_done"]
        assert done_steps == ["analysis_view", "research_view"]
        assert provider.synthesis_prompt.index('"step_id": "research_view"') < provider.synthesis_prompt.index(
            '"step_id": "analysis_view"'
        )
        assert events[-1]["text"] == "两个视角已按脚本顺序合并。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_respects_task_dependencies_within_phase(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "扫描完成，候选 A。"}],
            [{"type": "text_delta", "text": "基于候选 A 制定攻防计划。"}],
            [{"type": "text_delta", "text": "依赖复核完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_dep",
        user_text="按依赖复核候选",
        workflow_context=route_workflow("用 workflow 复核候选"),
        workflow_script=json.loads(_DEPENDENT_PLAN_JSON),
    )

    events = list(executor.run_stream([{"role": "user", "content": "按依赖复核候选"}]))

    try:
        step_starts = [event for event in events if event["type"] == "workflow_step_start"]
        step_dones = [event for event in events if event["type"] == "workflow_step_done"]
        phase_starts = [event for event in events if event["type"] == "workflow_phase_start"]
        assert [event["step"]["step_id"] for event in step_starts] == ["scan", "risk_plan"]
        assert step_starts[1]["step"]["depends_on"] == ["scan"]
        assert events.index(step_dones[0]) < events.index(step_starts[1])
        assert [event["parallel"] for event in phase_starts] == [False, False]
        assert "depends_on=scan" in provider.calls[1]["messages"][0]["content"]
        assert "扫描完成，候选 A。" in provider.calls[1]["messages"][0]["content"]
        assert events[-1]["text"] == "依赖复核完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_respects_task_dependencies_across_phases(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-cross-phase.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "扫描完成，候选 A。"}],
            [{"type": "text_delta", "text": "研报完成，确认候选 A。"}],
            [{"type": "text_delta", "text": "攻防计划完成。"}],
            [{"type": "text_delta", "text": "跨阶段依赖复核完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_cross_phase_dep",
        user_text="按依赖完成选股研报和攻防计划",
        workflow_context=route_workflow("用 workflow 完成选股研报和攻防计划"),
        workflow_script={
            "title": "跨阶段依赖复核",
            "phases": [
                {
                    "id": "decision",
                    "tasks": [
                        {
                            "id": "decision",
                            "title": "形成攻防",
                            "depends_on": ["report"],
                            "prompt": "基于研报输出攻防边界",
                        }
                    ],
                },
                {
                    "id": "report",
                    "tasks": [
                        {
                            "id": "report",
                            "title": "生成研报",
                            "depends_on": ["scan"],
                            "prompt": "基于候选生成研报",
                        }
                    ],
                },
                {
                    "id": "scan",
                    "tasks": [{"id": "scan", "title": "扫描候选", "prompt": "先扫描候选"}],
                },
            ],
            "synthesis_prompt": "按依赖顺序汇总。",
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "按依赖完成选股研报和攻防计划"}]))

    try:
        step_starts = [event for event in events if event["type"] == "workflow_step_start"]
        step_dones = [event for event in events if event["type"] == "workflow_step_done"]
        phase_starts = [event for event in events if event["type"] == "workflow_phase_start"]
        assert [event["step"]["step_id"] for event in step_starts] == ["scan", "report", "decision"]
        assert [event["phase"] for event in phase_starts] == ["scan", "report", "decision"]
        assert events.index(step_dones[0]) < events.index(step_starts[1])
        assert events.index(step_dones[1]) < events.index(step_starts[2])
        assert "扫描完成，候选 A。" in provider.calls[1]["messages"][0]["content"]
        assert "研报完成，确认候选 A。" in provider.calls[2]["messages"][0]["content"]
        assert events[-1]["text"] == "跨阶段依赖复核完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_serializes_inferred_stock_selection_dependencies(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-inferred-deps.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_scan", "name": "screen_stocks", "args": {}}]}],
            [{"type": "text_delta", "text": "候选扫描完成。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_report", "name": "generate_ai_report", "args": {}}]}],
            [{"type": "text_delta", "text": "研报生成完成。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_decision", "name": "generate_strategy_decision", "args": {}}],
                }
            ],
            [{"type": "text_delta", "text": "攻防计划完成。"}],
            [{"type": "text_delta", "text": "选股链路完成。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"selection_brief": {"best_codes": ["300750"]}},
            "generate_ai_report": {"reviewed_codes": ["300750"]},
            "generate_strategy_decision": {"reviewed_codes": ["300750"], "status": "completed"},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_inferred_deps",
        user_text="用 workflow 做选股、研报和攻防计划",
        workflow_context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "tasks": [
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"},
                {"id": "report", "title": "生成研报", "tools": ["generate_ai_report"], "prompt": "基于候选生成研报。"},
                {
                    "id": "decision",
                    "title": "形成攻防",
                    "tools": ["generate_strategy_decision"],
                    "prompt": "基于候选和研报输出攻防边界。",
                },
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 做选股、研报和攻防计划"}]))

    try:
        starts = [event for event in events if event["type"] == "workflow_step_start"]
        dones = [event for event in events if event["type"] == "workflow_step_done"]
        assert [event["step"]["step_id"] for event in starts] == ["scan", "report", "decision"]
        assert starts[1]["step"]["depends_on"] == ["scan"]
        assert starts[2]["step"]["depends_on"] == ["report"]
        assert events.index(dones[0]) < events.index(starts[1])
        assert events.index(dones[1]) < events.index(starts[2])
        assert [call["name"] for call in tools.calls] == [
            "screen_stocks",
            "generate_ai_report",
            "generate_strategy_decision",
        ]
        assert events[-1]["text"] == "选股链路完成。"
    finally:
        _reset_local_db(local_db)


def test_phase_batches_serializes_no_tool_synthesis_after_fact_tasks():
    run = plan_workflow(
        "复盘我的持仓，结合市场给出去留和风险动作",
        context=WORKFLOWS["portfolio_review"],
        workflow_script={
            "tasks": [
                {"id": "positions", "title": "读取持仓", "tools": ["portfolio"], "prompt": "读取当前持仓。"},
                {
                    "id": "market",
                    "title": "读取市场环境",
                    "tools": ["get_market_overview"],
                    "prompt": "读取当前市场水温。",
                },
                {
                    "id": "decision",
                    "title": "形成去留和风险动作",
                    "prompt": "基于持仓和市场环境，输出每个持仓的去留、风险边界和下一步动作。",
                },
            ]
        },
    )

    assert [[step.step_id for step in batch] for batch in _phase_batches(run.steps)] == [
        ["positions", "market"],
        ["decision"],
    ]


def test_phase_batches_keeps_unrelated_following_fact_task_out_of_synthesis_dependency():
    run = plan_workflow(
        "复盘我的持仓，结合市场给出去留和风险动作，然后再扫候选",
        context=WORKFLOWS["portfolio_review"],
        workflow_script={
            "tasks": [
                {"id": "positions", "title": "读取持仓", "tools": ["portfolio"], "prompt": "读取当前持仓。"},
                {
                    "id": "market",
                    "title": "读取市场环境",
                    "tools": ["get_market_overview"],
                    "prompt": "读取当前市场水温。",
                },
                {
                    "id": "decision",
                    "title": "形成去留和风险动作",
                    "prompt": "基于持仓和市场环境，输出每个持仓的去留、风险边界和下一步动作。",
                },
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描候选股票。"},
            ]
        },
    )

    assert [[step.step_id for step in batch] for batch in _phase_batches(run.steps)] == [
        ["positions", "market", "scan"],
        ["decision"],
    ]


def test_phase_batches_respects_previous_step_dependency_aliases():
    run = plan_workflow(
        "做选股、研报和攻防计划",
        context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "tasks": [
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"},
                {
                    "id": "report",
                    "title": "生成研报",
                    "tools": ["generate_ai_report"],
                    "after": "上一步",
                    "prompt": "基于候选生成研报。",
                },
                {
                    "id": "decision",
                    "title": "形成攻防",
                    "tools": ["generate_strategy_decision"],
                    "after": "previous step",
                    "prompt": "基于候选和研报输出攻防边界。",
                },
            ]
        },
    )

    assert [[step.step_id for step in batch] for batch in _phase_batches(run.steps)] == [
        ["scan"],
        ["report"],
        ["decision"],
    ]


def test_phase_batches_respects_tool_name_dependency_aliases():
    run = plan_workflow(
        "做选股、研报和攻防计划",
        context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "tasks": [
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"},
                {
                    "id": "report",
                    "title": "生成研报",
                    "tools": ["generate_ai_report"],
                    "after": "screen_stocks",
                    "prompt": "基于候选生成研报。",
                },
                {
                    "id": "decision",
                    "title": "形成攻防",
                    "tools": ["generate_strategy_decision"],
                    "after": "深度审讯",
                    "prompt": "基于候选和研报输出攻防边界。",
                },
            ]
        },
    )

    assert [[step.step_id for step in batch] for batch in _phase_batches(run.steps)] == [
        ["scan"],
        ["report"],
        ["decision"],
    ]


def test_phase_batches_respects_dependency_object_tool_fields():
    run = plan_workflow(
        "做选股、研报和攻防计划",
        context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "tasks": [
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"},
                {
                    "id": "report",
                    "title": "生成研报",
                    "tools": ["generate_ai_report"],
                    "after": {"tool": "screen_stocks"},
                    "prompt": "基于候选生成研报。",
                },
                {
                    "id": "decision",
                    "title": "形成攻防",
                    "tools": ["generate_strategy_decision"],
                    "after": {"function": {"name": "generate_ai_report"}},
                    "prompt": "基于候选和研报输出攻防边界。",
                },
            ]
        },
    )

    assert [[step.step_id for step in batch] for batch in _phase_batches(run.steps)] == [
        ["scan"],
        ["report"],
        ["decision"],
    ]


def test_phase_batches_respects_ordinal_dependency_aliases():
    run = plan_workflow(
        "做选股、研报和攻防计划",
        context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "tasks": [
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"},
                {
                    "id": "report",
                    "title": "生成研报",
                    "tools": ["generate_ai_report"],
                    "after": 1,
                    "prompt": "基于候选生成研报。",
                },
                {
                    "id": "decision",
                    "title": "形成攻防",
                    "tools": ["generate_strategy_decision"],
                    "after": "step 2",
                    "prompt": "基于候选和研报输出攻防边界。",
                },
            ]
        },
    )

    assert [[step.step_id for step in batch] for batch in _phase_batches(run.steps)] == [
        ["scan"],
        ["report"],
        ["decision"],
    ]


def test_workflow_executor_topologically_runs_out_of_order_stock_selection_tools(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-out-of-order-stock-deps.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_scan", "name": "screen_stocks", "args": {}}]}],
            [{"type": "text_delta", "text": "候选扫描完成。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_report", "name": "generate_ai_report", "args": {}}]}],
            [{"type": "text_delta", "text": "研报生成完成。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_decision", "name": "generate_strategy_decision", "args": {}}],
                }
            ],
            [{"type": "text_delta", "text": "攻防计划完成。"}],
            [{"type": "text_delta", "text": "乱序选股链路完成。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"selection_brief": {"best_codes": ["300750"]}},
            "generate_ai_report": {"reviewed_codes": ["300750"]},
            "generate_strategy_decision": {"reviewed_codes": ["300750"], "status": "completed"},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_out_of_order_stock_deps",
        user_text="用 workflow 做选股、研报和攻防计划",
        workflow_context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "tasks": [
                {
                    "id": "decision",
                    "title": "形成攻防",
                    "tools": ["generate_strategy_decision"],
                    "prompt": "基于候选和研报输出攻防边界。",
                },
                {"id": "report", "title": "生成研报", "tools": ["generate_ai_report"], "prompt": "基于候选生成研报。"},
                {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"},
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 做选股、研报和攻防计划"}]))

    try:
        starts = [event for event in events if event["type"] == "workflow_step_start"]
        assert [event["step"]["step_id"] for event in starts] == ["scan", "report", "decision"]
        assert starts[0]["step"]["depends_on"] == []
        assert starts[1]["step"]["depends_on"] == ["scan"]
        assert starts[2]["step"]["depends_on"] == ["report"]
        assert [call["name"] for call in tools.calls] == [
            "screen_stocks",
            "generate_ai_report",
            "generate_strategy_decision",
        ]
        assert events[-1]["text"] == "乱序选股链路完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_topologically_runs_cross_phase_stock_selection_tools(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-cross-phase-stock-deps.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_scan", "name": "screen_stocks", "args": {}}]}],
            [{"type": "text_delta", "text": "候选扫描完成。"}],
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_report", "name": "generate_ai_report", "args": {}}]}],
            [{"type": "text_delta", "text": "研报生成完成。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_decision", "name": "generate_strategy_decision", "args": {}}],
                }
            ],
            [{"type": "text_delta", "text": "攻防计划完成。"}],
            [{"type": "text_delta", "text": "跨阶段乱序选股链路完成。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"selection_brief": {"best_codes": ["300750"]}},
            "generate_ai_report": {"reviewed_codes": ["300750"]},
            "generate_strategy_decision": {"reviewed_codes": ["300750"], "status": "completed"},
        },
    )
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_cross_phase_stock_deps",
        user_text="用 workflow 做选股、研报和攻防计划",
        workflow_context=route_workflow("用 workflow 做选股、研报和攻防计划"),
        workflow_script={
            "phases": [
                {
                    "id": "decision",
                    "tasks": [
                        {
                            "id": "decision",
                            "title": "形成攻防",
                            "tools": ["generate_strategy_decision"],
                            "prompt": "基于候选和研报输出攻防边界。",
                        }
                    ],
                },
                {
                    "id": "report",
                    "tasks": [
                        {
                            "id": "report",
                            "title": "生成研报",
                            "tools": ["generate_ai_report"],
                            "prompt": "基于候选生成研报。",
                        }
                    ],
                },
                {
                    "id": "scan",
                    "tasks": [
                        {"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描今日候选。"}
                    ],
                },
            ]
        },
    )

    events = list(executor.run_stream([{"role": "user", "content": "用 workflow 做选股、研报和攻防计划"}]))

    try:
        starts = [event for event in events if event["type"] == "workflow_step_start"]
        assert [event["step"]["step_id"] for event in starts] == ["scan", "report", "decision"]
        assert starts[0]["step"]["depends_on"] == []
        assert starts[1]["step"]["depends_on"] == ["scan"]
        assert starts[2]["step"]["depends_on"] == ["report"]
        assert [call["name"] for call in tools.calls] == [
            "screen_stocks",
            "generate_ai_report",
            "generate_strategy_decision",
        ]
        assert events[-1]["text"] == "跨阶段乱序选股链路完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_topologically_orders_out_of_order_dependencies(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "扫描完成，候选 A。"}],
            [{"type": "text_delta", "text": "基于候选 A 制定攻防计划。"}],
            [{"type": "text_delta", "text": "乱序依赖复核完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s_dep_order",
        user_text="按依赖复核候选",
        workflow_context=route_workflow("用 workflow 复核候选"),
        workflow_script=json.loads(_OUT_OF_ORDER_DEPENDENT_PLAN_JSON),
    )

    events = list(executor.run_stream([{"role": "user", "content": "按依赖复核候选"}]))

    try:
        step_starts = [event for event in events if event["type"] == "workflow_step_start"]
        assert [event["step"]["step_id"] for event in step_starts] == ["scan", "risk_plan"]
        assert "先扫描候选" in provider.calls[0]["messages"][0]["content"]
        assert "扫描完成，候选 A。" in provider.calls[1]["messages"][0]["content"]
        assert events[-1]["text"] == "乱序依赖复核完成。"
    finally:
        _reset_local_db(local_db)


def test_phase_batches_tolerates_external_and_cyclic_dependencies():
    external = WorkflowStep(step_id="external", title="外部依赖", phase="review", depends_on=("prior_phase",))
    free = WorkflowStep(step_id="free", title="无依赖", phase="review")
    assert [[step.step_id for step in batch] for batch in _phase_batches([external, free])] == [["external", "free"]]

    a = WorkflowStep(step_id="a", title="A", phase="cycle", depends_on=("b",))
    b = WorkflowStep(step_id="b", title="B", phase="cycle", depends_on=("a",))
    assert [[step.step_id for step in batch] for batch in _phase_batches([a, b])] == [["a"], ["b"]]


def test_phase_batches_orders_cross_phase_dependencies_globally():
    decision = WorkflowStep(step_id="decision", title="形成攻防", phase="decision", depends_on=("report",))
    report = WorkflowStep(step_id="report", title="生成研报", phase="report", depends_on=("scan",))
    scan = WorkflowStep(step_id="scan", title="扫描候选", phase="scan")

    assert [[step.step_id for step in batch] for batch in _phase_batches([decision, report, scan])] == [
        ["scan"],
        ["report"],
        ["decision"],
    ]


def test_prepared_workflow_can_be_stopped_before_agents_run(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    executor = WorkflowExecutor(
        ScriptedProvider([]),
        StubToolRegistry(),
        session_id="s4",
        user_text="复跑 workflow wf_old",
        workflow_context=route_workflow("用 workflow 复盘我的持仓"),
        workflow_script=json.loads(_PLAN_JSON),
        source_run_id="wf_old",
    )
    plan = executor.prepare_run()
    control = WorkflowControl(plan["run_id"])
    control.stop()
    executor.set_control(control)

    events = list(executor.run_stream([{"role": "user", "content": "复跑 workflow wf_old"}]))

    try:
        run = get_workflow_run(plan["run_id"])
        assert events[0]["type"] == "workflow_start"
        assert any(event["type"] == "workflow_stopped" for event in events)
        assert events[-1]["text"] == "workflow 已停止。已完成步骤可在 /workflow show 查看。"
        assert run and run["status"] == "stopped"
    finally:
        _reset_local_db(local_db)


def test_prepared_workflow_can_reload_edited_script(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    executor = WorkflowExecutor(
        ScriptedProvider([]),
        StubToolRegistry(),
        session_id="s5",
        user_text="复跑 workflow wf_old",
        workflow_context=route_workflow("用 workflow 复盘我的持仓"),
        workflow_script=json.loads(_PLAN_JSON),
        source_run_id="wf_old",
    )
    plan = executor.prepare_run()

    event = executor.replace_prepared_script(json.loads(_EDITED_PLAN_JSON))

    try:
        run = get_workflow_run(plan["run_id"])
        assert event["run_id"] == plan["run_id"]
        assert event["plan"]["script"]["title"] == "编辑后脚本"
        assert event["plan"]["steps"][0]["step_id"] == "edited_task"
        assert run and run["plan"]["script"]["title"] == "编辑后脚本"
    finally:
        _reset_local_db(local_db)


def test_prepared_workflow_can_revise_script_from_feedback(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider([[{"type": "text_delta", "text": _REVISED_PLAN_JSON}]])
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s6",
        user_text="用 workflow 复盘我的持仓",
        workflow_context=route_workflow("用 workflow 复盘我的持仓"),
        workflow_script=json.loads(_PLAN_JSON),
    )
    plan = executor.prepare_run()

    event = executor.revise_prepared_script("别这么拆，直接只读持仓")

    try:
        run = get_workflow_run(plan["run_id"])
        events = load_workflow_events(plan["run_id"], limit=20)
        assert event["run_id"] == plan["run_id"]
        assert event["plan"]["script"]["title"] == "按反馈修订脚本"
        assert event["plan"]["script"]["runtime"]["revision"] == "model_feedback"
        assert event["plan"]["steps"][0]["step_id"] == "read_positions_only"
        assert run and run["plan"]["script"]["title"] == "按反馈修订脚本"
        assert any(row["event_type"] == "workflow_script_revised" for row in events)
        assert "用户最新反馈" in provider.calls[0]["messages"][0]["content"]
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_restarts_only_selected_step(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "结构视角重启完成。"}],
            [{"type": "text_delta", "text": "单 task 重启完成。"}],
        ]
    )
    executor = WorkflowExecutor(
        provider,
        StubToolRegistry(),
        session_id="s6",
        user_text="重启 workflow wf_old 的 task analysis_view",
        workflow_context=route_workflow("用 workflow 并发复核 300750"),
        workflow_script=json.loads(_PARALLEL_PLAN_JSON),
        source_run_id="wf_old",
        only_step_id="analysis_view",
    )

    events = list(executor.run_stream([{"role": "user", "content": "restart"}]))

    try:
        starts = [event for event in events if event["type"] == "workflow_step_start"]
        assert len(starts) == 1
        assert starts[0]["step"]["step_id"] == "analysis_view"
        assert events[0]["plan"]["script"]["runtime"]["only_step_id"] == "analysis_view"
        assert events[-1]["text"] == "单 task 重启完成。"
    finally:
        _reset_local_db(local_db)


def test_workflow_executor_fails_missing_only_step_without_fallback(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "workflow-missing-step.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    provider = ScriptedProvider(rounds=[])
    tools = StubToolRegistry()
    executor = WorkflowExecutor(
        provider,
        tools,
        session_id="s_missing_step",
        user_text="重启 workflow wf_old 的 task missing",
        workflow_context=route_workflow("用 workflow 做选股研报"),
        workflow_script={"tasks": [{"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描"}]},
        source_run_id="wf_old",
        only_step_id="missing",
    )

    events = list(executor.run_stream([{"role": "user", "content": "restart missing"}]))

    try:
        assert events[0]["plan"]["steps"] == []
        assert events[0]["plan"]["script"]["runtime"]["only_step_missing"] == "missing"
        assert events[-2]["status"] == "failed"
        assert events[-1]["text"] == "未找到 workflow step: missing。请先用 /workflow show 查看可重启的 step id。"
        assert tools.calls == []
    finally:
        _reset_local_db(local_db)


def test_build_resume_prompt_includes_step_state():
    prompt = build_resume_prompt(
        {
            "run_id": "wf_1",
            "label": "持仓复盘",
            "status": "completed",
            "user_text": "我的持仓怎么样",
            "result_summary": "初步建议继续观察",
            "plan": {
                "allowed_tools": ["portfolio", "generate_strategy_decision"],
                "steps": [
                    {
                        "step_id": "positions",
                        "status": "completed",
                        "phase": "collect",
                        "title": "读取持仓与资金",
                        "tool_scope": ["portfolio"],
                        "summary": "portfolio: ok",
                    },
                    {
                        "step_id": "decision",
                        "status": "skipped",
                        "phase": "decide",
                        "title": "形成去留和风险动作",
                        "depends_on": ["positions"],
                        "tool_scope": ["generate_strategy_decision"],
                        "effective_tool_scope": ["generate_strategy_decision"],
                        "prompt": "根据持仓事实输出去留和风险动作",
                        "summary": "",
                    },
                ],
            },
        }
    )

    assert "继续 workflow wf_1" in prompt
    assert "可用工具: portfolio, generate_strategy_decision" in prompt
    assert "已有结果摘要: 初步建议继续观察" in prompt
    assert "[completed] 读取持仓与资金 (id=positions; phase=collect; tool_scope=portfolio) - portfolio: ok" in prompt
    assert "id=decision; phase=decide; depends_on=positions; tool_scope=generate_strategy_decision" in prompt
    assert "prompt: 根据持仓事实输出去留和风险动作" in prompt
    assert "不要重复已完成工具调用" in prompt
    assert "保持原有 tool_scope 和 depends_on" in prompt


def test_short_recent_workflow_followup_detection_is_narrow():
    assert is_recent_workflow_followup("继续")
    assert is_recent_workflow_followup("接着刚才那个")
    assert is_recent_workflow_followup("继续上一个")
    assert not is_recent_workflow_followup("继续观察吗")
    assert not is_recent_workflow_followup("继续 workflow wf_1")


def test_recent_workflow_context_detection_targets_references():
    assert should_include_recent_workflow_context("第一个怎么样")
    assert should_include_recent_workflow_context("刚才那个候选风险呢")
    assert should_include_recent_workflow_context("哪个更稳")
    assert should_include_recent_workflow_context("2号还能买吗")
    assert should_include_recent_workflow_context("这个能买吗")
    assert not should_include_recent_workflow_context("今天市场怎么样")
    assert not should_include_recent_workflow_context("600519 怎么样")
    assert not should_include_recent_workflow_context("这个 CLI 怎么迭代")
    assert not should_include_recent_workflow_context("继续 workflow wf_1")


def test_build_chat_resume_prompt_keeps_user_reply():
    prompt = build_chat_resume_prompt(
        {
            "run_id": "wf_1",
            "label": "选股",
            "status": "running",
            "user_text": "给我选股",
            "plan": {"steps": [{"step_id": "scan", "title": "扫描候选", "tool_scope": ["screen_stocks"]}]},
        },
        "接着刚才那个",
    )

    assert "继续 workflow wf_1" in prompt
    assert "tool_scope=screen_stocks" in prompt
    assert "用户当前回复: 接着刚才那个" in prompt


def test_build_recent_workflow_context_is_bounded_reference():
    context = build_recent_workflow_context(
        {
            "run_id": "wf_1",
            "label": "选股",
            "status": "completed",
            "user_text": "给我选出三只股票",
            "result_summary": "候选：A、B、C",
            "plan": {
                "steps": [
                    {"status": "completed", "title": "扫描候选", "tool_scope": ["screen_stocks"], "summary": "A 入选"},
                    {
                        "status": "completed",
                        "title": "形成攻防",
                        "tool_scope": ["generate_strategy_decision"],
                        "summary": "A 低吸，跌破支撑失效",
                    },
                ]
            },
            "events": [
                {
                    "payload": {
                        "type": "workflow_step_done",
                        "step": {
                            "title": "扫描候选",
                            "status": "completed",
                            "evidence": [
                                "候选结论: 首选 A 高质量候选",
                                "候选护栏: 禁止直接买入",
                            ],
                        },
                    }
                }
            ],
        }
    )

    assert context.startswith("<recent-workflow-context>")
    assert "仅当用户问题引用刚才、上面、候选、推荐、序号或代词时参考" in context
    assert "run_id: wf_1" in context
    assert "结果摘要: 候选：A、B、C" in context
    assert "tools=screen_stocks" in context
    assert "证据: 候选结论: 首选 A 高质量候选" in context
    assert "证据: 候选护栏: 禁止直接买入" in context
    assert context.endswith("</recent-workflow-context>")
