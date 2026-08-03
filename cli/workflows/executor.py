"""Model-authored dynamic workflow execution runtime."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

from cli.runtime import RuntimeEvent
from cli.scratchpad import AgentScratchpad
from cli.sub_agents import (
    ANALYSIS_AGENT,
    RESEARCH_AGENT,
    TRADING_AGENT,
    WORKFLOW_TASK_AGENT,
    SubAgent,
    run_sub_agent,
)
from cli.workflows.control import WorkflowControl
from cli.workflows.disagreement import build_workflow_disagreement_summary
from cli.workflows.models import (
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    SKIPPED,
    STOPPED,
    TERMINAL_STATUSES,
    WorkflowContext,
    WorkflowRun,
    WorkflowStep,
    effective_tool_scope,
)
from cli.workflows.planner import (
    adapt_workflow_script,
    plan_workflow,
    revise_workflow_script,
    script_handoff_reason,
    workflow_steps_from_script,
)
from cli.workflows.store import append_workflow_event, persist_workflow_script, save_workflow_run
from core.candidate_actions import candidate_action_fields, candidate_action_label, candidate_action_role
from core.candidate_guards import candidate_guard_reason
from core.candidate_ranker import TRIGGER_SHORT_LABELS
from core.strategy_policy_display import policy_execution_mode_label, policy_next_action_label
from utils.safe import drop_empty as _drop_empty
from utils.tool_result_preview import tool_result_brief_lines

logger = logging.getLogger(__name__)

_AGENTS: dict[str, SubAgent] = {
    "task": WORKFLOW_TASK_AGENT,
    "research": RESEARCH_AGENT,
    "analysis": ANALYSIS_AGENT,
    "trading": TRADING_AGENT,
}
_TURN_EXPECTATION_TOOL_SCOPES = frozenset(
    {
        "analyze_stock",
        "portfolio",
        "get_market_overview",
        "screen_stocks",
        "generate_ai_report",
        "generate_strategy_decision",
        "run_backtest",
    }
)
MAX_CONCURRENT_AGENTS = 16
WORKFLOW_BACKGROUND_WAIT_SECONDS = 45.0
MAX_WORKFLOW_ADAPTATIONS = 4
SYNTHESIS_PROMPT_FIELDS = (
    "synthesis_prompt",
    "synthesis",
    "synthesis_instructions",
    "summary_prompt",
    "final_prompt",
    "final_response",
    "final_answer",
    "output",
    "deliverable",
    "deliverables",
)
_SYNTHESIS_REQUIREMENTS = (
    "输出要求：\n"
    "- 先给结论和可执行下一步，不要只复述 workflow 步骤。\n"
    "- 如果结果里有候选、选股、推荐或攻防证据，先按候选给用户可读结论：候选代码/名称、"
    "为什么入选、当前状态、主要风险、下一步动作。\n"
    "- 多候选场景不要压成一个泛泛结论；按首选、备选复核候选、受限复核候选、"
    "修复复核候选、待确认候选、观察候选、阻断候选分层。\n"
    "- 结果里出现候选护栏、市场闸门、数据质量、交易就绪或新增买入限制时，必须保留限制原因；"
    "不能把受限候选写成买入建议，只能写观察、研报复核或攻防决策下一步。\n"
    "- 如果 handoff 或 candidate_conclusion 里有入场区、触发条件、止损、失效条件、防追高限价，"
    "最终回答必须带出这些攻防边界；没有边界时明确说只能观察或复核，不能给行动建议。\n"
    "- 优先使用 handoff 里的分数、评级、主题、质量因子和风险因子作为证据，"
    "但回答要用自然语言，不要照抄内部字段名。\n"
    "- 如果没有可靠候选或数据质量不足，说明不能选出股票的原因和修复动作。\n"
)
_CANDIDATE_HANDOFF_FIELDS = (
    "code",
    "name",
    "tag",
    "tier",
    "quality",
    "profile",
    "why",
    "evidence",
    "triggers",
    "selection_source",
    "source_type",
    "track",
    "stage",
    "candidate_lane",
    "entry_type",
    "health",
    "status_label",
    "headline",
    "latest_close",
    "latest_date",
    "candidate_score",
    "style_match",
    "style_match_styles",
    "style_match_score",
    "style_match_reasons",
    "theme_match",
    "theme_match_score",
    "theme_match_reasons",
    "strategic_theme",
    "theme_score",
    "theme_source",
    "theme_event_id",
    "theme_event_date",
    "theme_event_title",
    "theme_event_reason",
    "priority_rank",
    "priority_score",
    "shadow_score",
    "score",
    "selection_strategy",
    "recommend_date",
    "is_ai_recommended",
    "selected_for_report",
    "funnel_score",
    "recommend_count",
    "candidate_shadow_score",
    "candidate_quality_score",
    "risk_adjusted_quality_score",
    "entry_risk_penalty",
    "rank_reason",
    "quality_factors",
    "risk_factors",
    "action_status",
    "action_label",
    "action_level",
    "direct_buy_allowed",
    "status",
    "trade_readiness",
    "new_buy_allowed",
    "ai_review_allowed",
    "next_step",
    "candidate_shadow_grade",
    "entry_quality_score",
    "entry_quality_grade",
    "entry_quality_risk_flags",
    "label_ready",
    "label_status",
    "entry_zone",
    "entry_zone_min",
    "entry_zone_max",
    "entry_trigger",
    "buy_zone",
    "entry_price",
    "stop_loss",
    "effective_stop_loss",
    "original_stop_loss",
    "max_entry_price",
    "tape_condition",
    "invalidate_condition",
    "trigger_condition",
    "trigger_price",
    "trigger_level",
    "trigger_reason",
    "invalid_condition",
    "invalid_price",
    "invalid_level",
    "invalid_reason",
    "support",
    "support_price",
    "resistance",
    "resistance_price",
    "target_price",
    "take_profit",
    "risk_reward",
    "rr_ratio",
    "position_plan",
    "position_size",
)


class WorkflowExecutor:
    """Run a model-authored workflow script by dispatching bounded sub-agents."""

    def __init__(
        self,
        provider,
        tools,
        *,
        session_id: str,
        user_text: str,
        scratchpad: AgentScratchpad | None = None,
        cancel_check: Callable[[], bool] | None = None,
        stream_chunk_timeout: float | None = None,
        workflow_context: WorkflowContext | None = None,
        workflow_script: dict[str, Any] | None = None,
        source_run_id: str = "",
        workflow_args: Any = None,
        workflow_control: WorkflowControl | None = None,
        only_step_id: str = "",
        planning_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.session_id = session_id
        self.user_text = user_text
        self.planning_messages = planning_messages
        self.scratchpad = scratchpad
        self.cancel_check = cancel_check
        self.stream_chunk_timeout = stream_chunk_timeout
        self.workflow_context = workflow_context
        self.workflow_script = workflow_script
        self.source_run_id = source_run_id
        self.workflow_args = workflow_args
        self.workflow_control = workflow_control
        self.only_step_id = only_step_id
        self._stopped = False
        self.run: WorkflowRun | None = None
        # plan_handoff_reason() 会先把 run 规划出来但不落库，所以「已规划」和「已落库」得分开记。
        self._persisted_run = False

    def set_control(self, control: WorkflowControl | None) -> None:
        self.workflow_control = control

    def plan_handoff_reason(self) -> str:
        """Plan this turn and report why the planner declined to orchestrate it, if it did.

        规划结果会缓存到 self.run，交还与继续执行都不会多花一次规划调用。交还时不落库：
        一次没跑过任何 step 的 run 写进 workflow_run 只会让历史里多一条噪音。
        """

        try:
            run = self._planned_run()
        except Exception:
            logger.debug("workflow handoff planning failed", exc_info=True)
            return ""
        return script_handoff_reason(run.script)

    def _planned_run(self) -> WorkflowRun:
        if self.run is None:
            self.run = plan_workflow(
                self.user_text,
                session_id=self.session_id,
                context=self.workflow_context,
                provider=self.provider,
                tools=self.tools,
                workflow_script=self.workflow_script,
                source_run_id=self.source_run_id,
                workflow_args=self.workflow_args,
                only_step_id=self.only_step_id,
                messages=self.planning_messages,
            )
        return self.run

    def prepare_run(self) -> RuntimeEvent:
        self._plan_run(PENDING)
        return self._plan_event()

    def replace_prepared_script(self, script: dict[str, Any]) -> RuntimeEvent:
        return self._replace_prepared_script(script, "workflow_script_reloaded")

    def revise_prepared_script(self, feedback: str) -> RuntimeEvent:
        run = self._require_run()
        script = revise_workflow_script(
            self.user_text,
            feedback,
            run.script,
            context=run.context,
            provider=self.provider,
            tools=self.tools,
        )
        return self._replace_prepared_script(script, "workflow_script_revised")

    def _replace_prepared_script(self, script: dict[str, Any], event_type: str) -> RuntimeEvent:
        old_run_id = self._require_run().run_id
        self.workflow_script = script
        self.run = plan_workflow(
            self.user_text,
            session_id=self.session_id,
            context=self.workflow_context,
            provider=self.provider,
            tools=self.tools,
            workflow_script=self.workflow_script,
            source_run_id=self.source_run_id,
            workflow_args=self.workflow_args,
            only_step_id=self.only_step_id,
            messages=self.planning_messages,
        )
        self.run.run_id = old_run_id
        self.run.status = PENDING
        self._persisted_run = True
        persist_workflow_script(self.run)
        save_workflow_run(self.run)
        payload = self._plan_event()
        append_workflow_event(old_run_id, event_type, payload)
        return payload

    def run_stream(self, messages: list[dict[str, Any]], system_prompt: str = "") -> Iterator[RuntimeEvent]:
        started_at = time.monotonic()
        if not self._persisted_run:
            # 判据是「计划公布过没有」而不是「规划过没有」：plan_handoff_reason() 会先规划但不落库，
            # 按 self.run is None 判会把这一轮的 workflow_plan 事件整个跳掉。
            self._plan_run(RUNNING)
            yield self._plan_event()
        else:
            yield self._mark_run_running()
        if not self._require_run().steps:
            final_text = _empty_workflow_text(self._require_run())
            messages.append({"role": "assistant", "content": final_text})
            yield self._mark_run_failed(final_text)
            yield {
                "type": "done",
                "text": final_text,
                "streamed": False,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "elapsed": time.monotonic() - started_at,
                "rounds": 1,
            }
            return

        results = yield from self._run_steps()
        if self._stopped:
            final_text = "workflow 已停止。已完成步骤可在 /workflow show 查看。"
            messages.append({"role": "assistant", "content": final_text})
            yield self._mark_run_stopped(final_text)
            yield {
                "type": "done",
                "text": final_text,
                "streamed": False,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "elapsed": time.monotonic() - started_at,
                "rounds": len(self._require_run().steps) + 1,
            }
            return
        final_text, usage = self._synthesize_results(results, system_prompt)
        messages.append({"role": "assistant", "content": final_text})
        if self.scratchpad:
            self.scratchpad.record_final(
                final_text,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                elapsed_s=time.monotonic() - started_at,
                provider=str(getattr(self.provider, "name", "")),
                model=str(getattr(self.provider, "model", "")),
            )
        if disagreement_event := self._mark_disagreement_summary(results):
            yield disagreement_event
        yield self._mark_run_done(final_text)
        yield {
            "type": "done",
            "text": final_text,
            "streamed": False,
            "usage": usage,
            "elapsed": time.monotonic() - started_at,
            "rounds": len(self.run.steps) + 1,
        }

    def _plan_run(self, status: str) -> None:
        if self._persisted_run:
            run = self._require_run()
            run.status = status
            save_workflow_run(run)
            return
        run = self._planned_run()
        run.status = status
        self._persisted_run = True
        persist_workflow_script(run)
        save_workflow_run(run)

    def _plan_event(self) -> RuntimeEvent:
        run = self._require_run()
        payload = {
            "type": "workflow_plan",
            "run_id": run.run_id,
            "workflow": run.workflow,
            "label": run.label,
            "route": run.context.route_payload(),
            "plan": run.plan_payload(),
        }
        append_workflow_event(run.run_id, "workflow_plan", payload)
        return payload

    def _run_steps(self) -> Iterator[RuntimeEvent | list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        while phase_steps := _next_phase_steps(self._require_run().steps):
            if not self._wait_if_paused():
                self._stopped = True
                break
            yield self._phase_event("workflow_phase_start", phase_steps)
            phase_results = yield from self._run_phase(phase_steps, results)
            results.extend(phase_results)
            yield self._phase_event("workflow_phase_done", phase_steps)
            if self._cancel_requested():
                self._stopped = True
                break
            if event := self._adapt_after_phase(results):
                yield event
        return results

    def _adapt_after_phase(self, results: list[dict[str, Any]]) -> RuntimeEvent | None:
        run = self._require_run()
        if not _should_adapt_workflow(run):
            return None
        remaining = _pending_steps(run.steps)
        if not remaining:
            return None
        script = adapt_workflow_script(
            self.user_text,
            run.script,
            results,
            [run.step_payload(step) for step in remaining],
            context=run.context,
            provider=self.provider,
            tools=self.tools,
        )
        return self._apply_adapted_script(script, remaining)

    def _apply_adapted_script(
        self,
        script: dict[str, Any],
        previous_remaining: list[WorkflowStep],
    ) -> RuntimeEvent | None:
        runtime = script.get("runtime") if isinstance(script.get("runtime"), dict) else {}
        if runtime.get("adaptation") != "model_phase":
            return None
        self._merge_adapted_script(script)
        if runtime.get("adaptation_complete"):
            self._skip_remaining_steps(previous_remaining)
            self._record_adaptation_step_delta([], previous_remaining, completed=True)
        else:
            continuation = self._replace_remaining_steps(script)
            self._record_adaptation_step_delta(continuation, previous_remaining, completed=False)
        persist_workflow_script(self._require_run())
        save_workflow_run(self._require_run())
        return self._plan_update_event()

    def _merge_adapted_script(self, script: dict[str, Any]) -> None:
        run = self._require_run()
        payload = deepcopy(run.script)
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        runtime["adaptive"] = True
        runtime["adaptation"] = "model_phase"
        runtime["adaptation_count"] = _workflow_adaptation_count(run) + 1
        runtime["last_adaptation_title"] = _clip(str(script.get("title") or ""), 120)
        runtime["last_adaptation_rationale"] = _clip(str(script.get("rationale") or ""), 240)
        script_runtime = script.get("runtime") if isinstance(script.get("runtime"), dict) else {}
        if script_runtime.get("adaptation_complete"):
            runtime["adaptation_complete"] = True
        payload["runtime"] = runtime
        payload["adapted_continuation"] = script
        if synthesis := script.get("synthesis_prompt"):
            payload["synthesis_prompt"] = synthesis
        run.script = payload

    def _replace_remaining_steps(self, script: dict[str, Any]) -> list[WorkflowStep]:
        run = self._require_run()
        completed = _terminal_steps(run.steps)
        completed_ids = {step.step_id for step in completed}
        continuation = [
            step
            for step in workflow_steps_from_script(script, run.user_text, run.context)
            if step.step_id not in completed_ids
        ]
        if continuation:
            run.steps = completed + continuation
            run.refresh_current_step()
        return continuation

    def _skip_remaining_steps(self, previous_remaining: list[WorkflowStep]) -> None:
        run = self._require_run()
        for step in previous_remaining:
            step.status = SKIPPED
            step.summary = "model_adapted_complete"
        run.refresh_current_step()

    def _record_adaptation_step_delta(
        self,
        continuation: list[WorkflowStep],
        previous_remaining: list[WorkflowStep],
        *,
        completed: bool,
    ) -> None:
        run = self._require_run()
        runtime = run.script.get("runtime") if isinstance(run.script.get("runtime"), dict) else {}
        previous_ids = _workflow_step_ids(previous_remaining)
        continuation_ids = _workflow_step_ids(continuation)
        previous_set = set(previous_ids)
        continuation_set = set(continuation_ids)
        removed = [step_id for step_id in previous_ids if step_id not in continuation_set]
        added = [step_id for step_id in continuation_ids if step_id not in previous_set]
        kept = [step_id for step_id in continuation_ids if step_id in previous_set]
        previous_by_id = _workflow_steps_by_id(previous_remaining)
        continuation_by_id = _workflow_steps_by_id(continuation)
        runtime.update(
            {
                "adapted_previous_step_count": len(previous_ids),
                "adapted_continuation_step_count": len(continuation_ids),
                "adapted_kept_step_count": len(kept),
                "adapted_removed_step_count": len(removed),
                "adapted_added_step_count": len(added),
                "adapted_removed_step_ids": removed[:12],
                "adapted_added_step_ids": added[:12],
                "adapted_kept_steps": _workflow_step_summaries(kept, continuation_by_id),
                "adapted_removed_steps": _workflow_step_summaries(removed, previous_by_id),
                "adapted_added_steps": _workflow_step_summaries(added, continuation_by_id),
            }
        )
        if completed:
            runtime["adapted_skipped_step_count"] = len(previous_ids)
        else:
            runtime.pop("adapted_skipped_step_count", None)
        run.script["runtime"] = runtime

    def _plan_update_event(self) -> RuntimeEvent:
        run = self._require_run()
        payload = {
            "type": "workflow_plan_update",
            "run_id": run.run_id,
            "workflow": run.workflow,
            "label": run.label,
            "route": run.context.route_payload(),
            "plan": run.plan_payload(),
        }
        append_workflow_event(run.run_id, "workflow_plan_update", payload)
        return payload

    def _run_phase(
        self,
        phase_steps: list[WorkflowStep],
        prior_results: list[dict[str, Any]],
    ) -> Iterator[RuntimeEvent | list[dict[str, Any]]]:
        if len(phase_steps) == 1:
            return (yield from self._run_phase_sequential(phase_steps, prior_results))
        return (yield from self._run_phase_parallel(phase_steps, prior_results))

    def _run_phase_sequential(
        self,
        phase_steps: list[WorkflowStep],
        prior_results: list[dict[str, Any]],
    ) -> Iterator[RuntimeEvent | list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        for step in phase_steps:
            if not self._wait_if_paused():
                self._stopped = True
                break
            yield self._mark_step_start(step)
            result = self._run_step(step, prior_results)
            results.append({"step": self._step_payload(step), "result": result})
            yield self._mark_step_done(step, result)
        return results

    def _run_phase_parallel(
        self,
        phase_steps: list[WorkflowStep],
        prior_results: list[dict[str, Any]],
    ) -> Iterator[RuntimeEvent | list[dict[str, Any]]]:
        for step in phase_steps:
            yield self._mark_step_start(step)
        results: list[tuple[int, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=_max_workers(phase_steps), thread_name_prefix="workflow-agent") as pool:
            futures = {
                pool.submit(self._run_step, step, prior_results): (idx, step) for idx, step in enumerate(phase_steps)
            }
            for future in as_completed(futures):
                idx, step = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"status": "error", "error": str(exc)}
                results.append((idx, {"step": self._step_payload(step), "result": result}))
                yield self._mark_step_done(step, result)
        return _script_ordered_results(results)

    def _run_step(self, step: WorkflowStep, prior_results: list[dict[str, Any]]) -> dict[str, Any]:
        agent = _AGENTS.get(step.agent)
        if not agent:
            return {"status": "error", "error": f"未知 workflow agent: {step.agent}"}
        context = _step_context(step, prior_results)
        result = run_sub_agent(
            agent,
            step.prompt or step.title,
            context,
            self.provider,
            self.tools,
            cancel_check=self._cancel_requested,
            tool_names=_step_tool_names(step, self._require_run().allowed_tools),
            enforce_turn_expectations=_step_turn_expectations_enabled(step),
            required_tool_names=_step_required_tool_names(step),
            required_tool_args=_step_required_tool_args(step),
            required_tool_arg_sets=_step_required_tool_arg_sets(step, prior_results),
        )
        if wait_result := _wait_step_background_tasks(self.tools, result.get("background_task_ids") or []):
            result["background_tasks"] = wait_result
        if handoff_state := _workflow_handoff_state(self.tools):
            result["handoff_state"] = handoff_state
        return result

    def _phase_event(self, event_type: str, phase_steps: list[WorkflowStep]) -> RuntimeEvent:
        run = self._require_run()
        phase_id = _batch_phase_id(phase_steps)
        payload = {
            "type": event_type,
            "run_id": run.run_id,
            "phase": phase_id,
            "steps": [run.step_payload(step) for step in phase_steps],
            "parallel": len(phase_steps) > 1,
        }
        append_workflow_event(run.run_id, event_type, payload)
        return payload

    def _mark_run_running(self) -> RuntimeEvent:
        run = self._require_run()
        run.status = RUNNING
        save_workflow_run(run)
        payload = {"type": "workflow_start", "run_id": run.run_id, "status": run.status}
        append_workflow_event(run.run_id, "workflow_start", payload)
        return payload

    def _mark_step_start(self, step: WorkflowStep) -> RuntimeEvent:
        step.status = RUNNING
        step.summary = "start"
        return self._save_step_event("workflow_step_start", step, {"type": "workflow_task_start"})

    def _mark_step_done(self, step: WorkflowStep, result: dict[str, Any]) -> RuntimeEvent:
        status = str(result.get("status", ""))
        step.status = COMPLETED if status == "completed" else FAILED
        step.summary = _brief_agent_result(result)
        return self._save_step_event(
            "workflow_step_done",
            step,
            {"type": "workflow_task_done", "status": status, "agent_detail": _agent_detail(step, result)},
        )

    def _save_step_event(self, event_type: str, step: WorkflowStep, source: RuntimeEvent) -> RuntimeEvent:
        run = self._require_run()
        run.refresh_current_step()
        save_workflow_run(run)
        payload = {
            "type": event_type,
            "run_id": run.run_id,
            "step": run.step_payload(step),
            "source": _source_payload(source),
            "step_total": len(run.steps),
        }
        # 用身份比较：WorkflowStep 是值相等的 dataclass，index() 可能命中同构的另一步
        step_index = next((idx for idx, item in enumerate(run.steps, 1) if item is step), 0)
        if step_index:
            payload["step_index"] = step_index
        append_workflow_event(run.run_id, event_type, payload)
        return payload

    def _step_payload(self, step: WorkflowStep) -> dict[str, Any]:
        return self._require_run().step_payload(step)

    def _synthesize_results(self, results: list[dict[str, Any]], system_prompt: str) -> tuple[str, dict[str, int]]:
        run = self._require_run()
        prompt = _synthesis_prompt(run, results)
        fallback_text = _fallback_summary(results)
        try:
            text, usage = _collect_synthesis(self.provider, prompt, system_prompt, fallback_text=fallback_text)
        except Exception:
            text, usage = fallback_text, {"input_tokens": 0, "output_tokens": 0}
        return _with_failure_lead(_ensure_candidate_delivery(text, results), run), usage

    def _mark_run_done(self, final_text: str) -> RuntimeEvent:
        run = self._require_run()
        run.status = FAILED if any(step.status == FAILED for step in run.steps) else COMPLETED
        run.result_summary = final_text[:500]
        run.refresh_current_step()
        save_workflow_run(run)
        payload = {"type": "workflow_done", "run_id": run.run_id, "status": run.status}
        append_workflow_event(run.run_id, "workflow_done", payload)
        return payload

    def _mark_disagreement_summary(self, results: list[dict[str, Any]]) -> RuntimeEvent | None:
        summary = build_workflow_disagreement_summary(results)
        if not _has_material_disagreement(summary):
            return None
        run = self._require_run()
        payload = {"type": "workflow_disagreement_summary", "run_id": run.run_id, "summary": summary}
        append_workflow_event(run.run_id, "workflow_disagreement_summary", payload)
        return payload

    def _mark_run_stopped(self, final_text: str) -> RuntimeEvent:
        run = self._require_run()
        run.status = STOPPED
        run.result_summary = final_text[:500]
        run.refresh_current_step()
        save_workflow_run(run)
        payload = {"type": "workflow_stopped", "run_id": run.run_id, "status": run.status}
        append_workflow_event(run.run_id, "workflow_stopped", payload)
        return payload

    def _mark_run_failed(self, final_text: str) -> RuntimeEvent:
        run = self._require_run()
        run.status = FAILED
        run.result_summary = final_text[:500]
        run.refresh_current_step()
        save_workflow_run(run)
        payload = {"type": "workflow_done", "run_id": run.run_id, "status": run.status}
        append_workflow_event(run.run_id, "workflow_done", payload)
        return payload

    def _require_run(self) -> WorkflowRun:
        if self.run is None:
            raise RuntimeError("workflow run has not been planned")
        return self.run

    def _cancel_requested(self) -> bool:
        if self.workflow_control and self.workflow_control.stopped():
            return True
        return bool(self.cancel_check and self.cancel_check())

    def _wait_if_paused(self) -> bool:
        if self.workflow_control is None:
            return not self._cancel_requested()
        return self.workflow_control.wait_if_paused() and not self._cancel_requested()


def _next_phase_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    pending = _pending_steps(steps)
    return (_phase_batches(pending) or [[]])[0]


def _pending_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    return [step for step in steps if step.status not in TERMINAL_STATUSES]


def _terminal_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    return [step for step in steps if step.status in TERMINAL_STATUSES]


def _should_adapt_workflow(run: WorkflowRun) -> bool:
    runtime = _workflow_runtime(run)
    if runtime.get("planner") != "model_script":
        return False
    if not _workflow_adaptive_enabled(runtime.get("adaptive")):
        return False
    return _workflow_adaptation_count(run) < MAX_WORKFLOW_ADAPTATIONS


def _workflow_adaptive_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "enabled", "adaptive", "是", "需要", "启用"}


def _workflow_adaptation_count(run: WorkflowRun) -> int:
    try:
        return int(_workflow_runtime(run).get("adaptation_count") or 0)
    except (TypeError, ValueError):
        return 0


def _workflow_runtime(run: WorkflowRun) -> dict[str, Any]:
    return run.script.get("runtime") if isinstance(run.script.get("runtime"), dict) else {}


def _with_failure_lead(text: str, run: WorkflowRun) -> str:
    """Put failed steps at the top when the run did not fully succeed.

    昊华科技那次 run 是 failed，用户读到的开头却是三条候选结论——候选前插逻辑不看运行状态，
    真正重要的事实（那一步超时了、清仓没被记录）被埋在后面。失败必须先说。
    """

    failed = [step for step in run.steps if step.status == FAILED]
    if not failed:
        return text
    titles = ", ".join(_clip(step.title or step.step_id, 40) for step in failed[:4])
    more = f" 等 {len(failed)} 步" if len(failed) > 4 else ""
    lead = f"⚠️ 本次 workflow 未完全成功：{len(failed)} 个步骤失败（{titles}{more}）。以下结论缺少这些步骤的事实支撑。"
    return f"{lead}\n\n{text}" if text.strip() else lead


def _empty_workflow_text(run: WorkflowRun) -> str:
    runtime = _workflow_runtime(run)
    if missing := str(runtime.get("only_step_missing") or "").strip():
        return f"未找到 workflow step: {missing}。请先用 /workflow show 查看可重启的 step id。"
    return "workflow 没有可执行步骤。请检查脚本或重新生成 workflow。"


def _workflow_step_ids(steps: list[WorkflowStep]) -> list[str]:
    return [step.step_id for step in steps if step.step_id]


def _workflow_steps_by_id(steps: list[WorkflowStep]) -> dict[str, WorkflowStep]:
    return {step.step_id: step for step in steps if step.step_id}


def _workflow_step_summaries(step_ids: list[str], steps_by_id: dict[str, WorkflowStep]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step_id in step_ids[:12]:
        step = steps_by_id.get(step_id)
        title = step.title if step else step_id
        rows.append({"id": step_id, "title": _clip(title, 80)})
    return rows


def _step_context(step: WorkflowStep, prior_results: list[dict[str, Any]]) -> str:
    lines = [f"phase={step.phase}"]
    if step.depends_on:
        lines.append(f"depends_on={', '.join(step.depends_on)}")
    if step.rationale:
        lines.extend(["", "task rationale:", step.rationale])
    if step.success_criteria:
        lines.extend(["", "success criteria:", step.success_criteria])
    if step.risk_guard:
        lines.extend(["", "risk guard:", step.risk_guard])
    if step.context:
        lines.extend(["", "task context:", step.context])
    args_hint = step.args_hint or _handoff_tool_args_hint(step, prior_results)
    if args_hint:
        lines.extend(["", "tool args hint:", args_hint])
    if not prior_results:
        return "\n".join(lines)
    if handoff_lines := _prior_handoff_context_lines(prior_results):
        lines.extend(["", "前序候选 handoff 摘要:", *handoff_lines])
    preview = json.dumps(prior_results[-3:], ensure_ascii=False, default=str)[:6000]
    lines.extend(["", "前序 agent 结果:", preview])
    return "\n".join(lines)


def _handoff_tool_args_hint(step: WorkflowStep, prior_results: list[dict[str, Any]]) -> str:
    scope = set(_concrete_tools(step.tool_scope))
    if not scope:
        return ""
    targets: list[dict[str, Any]] = []
    selected_tool = ""
    for item in reversed(prior_results):
        result = item.get("result") if isinstance(item, dict) else {}
        handoff = result.get("handoff_state") if isinstance(result, dict) else {}
        for payload in _handoff_tool_payloads(handoff):
            tool_name = str(payload.get("tool") or "").strip()
            args = payload.get("args")
            if tool_name in scope and isinstance(args, dict) and args:
                selected_tool = selected_tool or tool_name
                if tool_name == selected_tool:
                    targets.append({"args": args})
    return _handoff_tool_args_hint_text(selected_tool, targets)


def _handoff_tool_args_hint_text(tool_name: str, targets: list[dict[str, Any]]) -> str:
    if not tool_name or not targets:
        return ""
    if len(targets) == 1:
        return json.dumps({"tool": tool_name, "args": targets[0]["args"]}, ensure_ascii=False)
    return json.dumps(
        {
            "tool": tool_name,
            "call_each": True,
            "instruction": "按 targets 顺序逐个调用 tool，每个 targets[].args 调用一次；不要把 call_each/targets 包装成工具参数。",
            "targets": targets[:6],
        },
        ensure_ascii=False,
    )


def _handoff_tool_payloads(handoff: Any) -> list[dict[str, Any]]:
    if not isinstance(handoff, dict):
        return []
    payloads: list[dict[str, Any]] = []
    for stage in handoff.values():
        if isinstance(stage, dict):
            payloads.extend(_handoff_stage_tool_payloads(stage))
    return payloads


def _handoff_stage_tool_payloads(stage: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[Any] = [stage.get("next_tool")]
    selection = stage.get("selection_brief") if isinstance(stage.get("selection_brief"), dict) else {}
    action_plan = stage.get("action_plan") if isinstance(stage.get("action_plan"), dict) else {}
    payloads.extend([selection.get("tool_handoff"), action_plan.get("next_tool"), action_plan.get("review_targets")])
    payloads.extend(_as_list(stage.get("diagnosis_targets")))
    payloads.extend(_as_list(action_plan.get("diagnosis_targets")))
    return [payload for payload in payloads if isinstance(payload, dict) and payload.get("tool")]


def _prior_handoff_context_lines(prior_results: list[dict[str, Any]], limit: int = 8) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in reversed(prior_results):
        result = item.get("result") if isinstance(item, dict) else {}
        handoff = result.get("handoff_state") if isinstance(result, dict) else {}
        for line in _fallback_handoff_lines(handoff):
            _append_handoff_line(lines, seen, line, limit)
    return [f"- {line}" for line in lines]


def _phase_batches(steps: list[WorkflowStep]) -> list[list[WorkflowStep]]:
    if _has_cross_phase_dependencies(steps):
        return _dependency_batches(steps)
    batches: list[list[WorkflowStep]] = []
    phase_steps: list[WorkflowStep] = []
    for step in steps:
        phase = _step_phase_id(step)
        if not phase_steps or _step_phase_id(phase_steps[-1]) == phase:
            phase_steps.append(step)
        else:
            batches.extend(_dependency_batches(phase_steps))
            phase_steps = [step]
    if phase_steps:
        batches.extend(_dependency_batches(phase_steps))
    return batches


def _dependency_batches(steps: list[WorkflowStep]) -> list[list[WorkflowStep]]:
    batches: list[list[WorkflowStep]] = []
    remaining = list(steps)
    completed_ids: set[str] = set()
    step_ids = {step.step_id for step in steps}
    while remaining:
        ready = [step for step in remaining if _known_dependencies(step, step_ids).issubset(completed_ids)]
        if not ready:
            batches.extend([step] for step in remaining)
            break
        batches.append(ready)
        completed_ids.update(step.step_id for step in ready)
        ready_ids = {id(step) for step in ready}
        remaining = [step for step in remaining if id(step) not in ready_ids]
    return batches


def _has_cross_phase_dependencies(steps: list[WorkflowStep]) -> bool:
    phase_by_step_id = {step.step_id: _step_phase_id(step) for step in steps}
    return any(
        dep in phase_by_step_id and phase_by_step_id[dep] != _step_phase_id(step)
        for step in steps
        for dep in step.depends_on
    )


def _known_dependencies(step: WorkflowStep, step_ids: set[str]) -> set[str]:
    return {dep for dep in step.depends_on if dep in step_ids}


def _batch_phase_id(phase_steps: list[WorkflowStep]) -> str:
    if not phase_steps:
        return ""
    phase_ids = [_step_phase_id(step) for step in phase_steps]
    return phase_ids[0] if len(set(phase_ids)) == 1 else "mixed"


def _step_phase_id(step: WorkflowStep) -> str:
    return step.phase or step.step_id


def _script_ordered_results(results: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [item for _idx, item in sorted(results, key=lambda pair: pair[0])]


def _step_tool_names(step: WorkflowStep, allowed_tools: tuple[str, ...]) -> tuple[str, ...] | None:
    allowed = _concrete_tools(allowed_tools)
    if not allowed:
        return step.tool_scope or None
    if not step.tool_scope and step.tool_scope_source == "model_declared":
        return ()
    return effective_tool_scope(step.tool_scope, allowed)


def _step_turn_expectations_enabled(step: WorkflowStep) -> bool:
    return bool(set(step.tool_scope).intersection(_TURN_EXPECTATION_TOOL_SCOPES))


def _step_required_tool_names(step: WorkflowStep) -> tuple[str, ...]:
    return tuple(name for name in _concrete_tools(step.tool_scope) if name in _TURN_EXPECTATION_TOOL_SCOPES)


def _step_required_tool_args(step: WorkflowStep) -> dict[str, dict[str, str]]:
    tools = _step_required_tool_names(step)
    args = _simple_args_hint(step.args_hint)
    if not tools or not args:
        return {}
    if len(tools) == 1:
        return {tools[0]: args}
    return {tool: tool_args for tool in tools if (tool_args := _tool_scoped_args(tool, args))}


def _step_required_tool_arg_sets(
    step: WorkflowStep,
    prior_results: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    tools = _step_required_tool_names(step)
    args_hint = step.args_hint or _handoff_tool_args_hint(step, prior_results)
    if not tools or not args_hint:
        return {}
    if arg_sets := _json_tool_arg_sets(args_hint):
        return {tool: sets for tool, sets in arg_sets.items() if tool in tools}
    args = _simple_args_hint(args_hint)
    if not args:
        return {}
    if len(tools) == 1:
        return {tools[0]: (_tool_scoped_args(tools[0], args) or args,)}
    return {tool: (tool_args,) for tool in tools if (tool_args := _tool_scoped_args(tool, args))}


def _json_tool_arg_sets(text: str) -> dict[str, tuple[dict[str, Any], ...]]:
    try:
        payload = json.loads(str(text or ""))
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    tool_name = str(payload.get("tool") or "").strip()
    if not tool_name:
        return {}
    targets = payload.get("targets")
    if isinstance(targets, list):
        arg_sets = tuple(
            args
            for target in targets
            if isinstance(target, dict)
            and isinstance(target.get("args"), dict)
            and (args := _clean_json_tool_args(tool_name, target["args"]))
        )
        return {tool_name: arg_sets} if arg_sets else {}
    args = payload.get("args")
    if isinstance(args, dict) and (cleaned := _clean_json_tool_args(tool_name, args)):
        return {tool_name: (cleaned,)}
    return {}


def _clean_json_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    keys = _TOOL_ARG_KEYS.get(tool_name)
    return {
        str(key): value
        for key, value in args.items()
        if str(key).strip() and value not in (None, "") and (not keys or str(key) in keys)
    }


_SIMPLE_ARGS_HINT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.+?)\s*$")
_NON_TOOL_ARG_HINT_KEYS = {"tool", "targets", "call_each", "instruction"}
_TOOL_ARG_KEYS = {
    "screen_stocks": frozenset({"board", "style", "theme", "limit", "financial_metrics"}),
    "generate_ai_report": frozenset({"stock_codes"}),
    "generate_strategy_decision": frozenset({"reviewed_codes", "reviewed_symbols", "report_text"}),
    "portfolio": frozenset({"mode", "action"}),
    "analyze_stock": frozenset({"code", "symbol", "mode"}),
}


def _simple_args_hint(text: str) -> dict[str, str]:
    args: dict[str, str] = {}
    for part in re.split(r"[；;\n]+", str(text or "")):
        match = _SIMPLE_ARGS_HINT_RE.match(part)
        if not match:
            continue
        key, value = match.group(1), _clean_args_hint_value(match.group(2))
        if key in _NON_TOOL_ARG_HINT_KEYS or not value:
            continue
        args[key] = value
    return args


def _tool_scoped_args(tool_name: str, args: dict[str, str]) -> dict[str, str]:
    keys = _TOOL_ARG_KEYS.get(tool_name)
    if not keys:
        return {}
    return {key: value for key, value in args.items() if key in keys}


def _clean_args_hint_value(value: str) -> str:
    return str(value or "").strip().strip("\"'")


def _concrete_tools(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names if name and not name.startswith("delegate_to_"))


def _max_workers(steps: list[WorkflowStep]) -> int:
    return max(1, min(len(steps), MAX_CONCURRENT_AGENTS))


def _brief_agent_result(result: dict[str, Any]) -> str:
    status = str(result.get("status", ""))
    elapsed = float(result.get("elapsed", 0.0))
    if result.get("error"):
        return f"{status} {str(result['error'])[:100]}"
    if background := _brief_background_result(result.get("background_tasks")):
        return f"{status} {elapsed:.1f}s {background}"
    return f"{status} {elapsed:.1f}s"


def _brief_background_result(tasks: Any) -> str:
    for task in _as_list(tasks):
        if not isinstance(task, dict):
            continue
        if summary := _clip_text(task.get("result_summary"), 160):
            name = str(task.get("tool_name") or task.get("task_id") or "background").strip()
            return f"{name}: {summary}"
        if task.get("status") == "failed" and task.get("error"):
            return f"{task.get('tool_name') or 'background'} failed: {_clip_text(task.get('error'), 120)}"
    return ""


def _source_payload(event: RuntimeEvent) -> dict[str, Any]:
    payload = {
        "type": event.get("type", ""),
        "name": event.get("name", ""),
        "status": event.get("status", ""),
        "elapsed_ms": event.get("elapsed_ms", 0),
        "error": event.get("error", ""),
    }
    if event.get("agent_detail"):
        payload["agent_detail"] = event["agent_detail"]
    return payload


def _agent_detail(step: WorkflowStep, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "title": step.title,
        "agent": step.agent,
        "phase": step.phase,
        "prompt": _clip(step.prompt, 4000),
        "context": _clip(step.context, 4000),
        "args_hint": _clip(step.args_hint, 1000),
        "rationale": _clip(step.rationale, 1000),
        "success_criteria": _clip(step.success_criteria, 1000),
        "risk_guard": _clip(step.risk_guard, 1000),
        "tool_scope": list(step.tool_scope),
        "status": str(result.get("status", "")),
        "elapsed": result.get("elapsed", 0),
        "tool_calls": list(result.get("tool_calls", []) or [])[:40],
        "background_task_ids": list(result.get("background_task_ids", []) or [])[:20],
        "background_tasks": list(result.get("background_tasks", []) or [])[:20],
        "handoff_state": result.get("handoff_state", {}),
        "result": _clip(str(result.get("result", "") or ""), 8000),
        "error": _clip(str(result.get("error", "") or ""), 2000),
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _wait_step_background_tasks(tools: Any, task_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
    if not ids or not hasattr(tools, "wait_background_tasks"):
        return []
    return tools.wait_background_tasks(ids, timeout_seconds=_workflow_background_wait_seconds())


def _workflow_background_wait_seconds() -> float:
    raw = os.getenv("WYCKOFF_WORKFLOW_BG_WAIT_SECONDS", "").strip()
    if not raw:
        return WORKFLOW_BACKGROUND_WAIT_SECONDS
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return WORKFLOW_BACKGROUND_WAIT_SECONDS


def _workflow_handoff_state(tools: Any) -> dict[str, Any]:
    context = getattr(tools, "_tool_context", None)
    state = getattr(context, "state", {}) if context is not None else {}
    if not isinstance(state, dict):
        return {}
    return _drop_empty(
        {
            "last_screen_result": _compact_screen_handoff(state.get("last_screen_result")),
            "last_recommendation_event_eval": _compact_recommendation_handoff(
                state.get("last_recommendation_event_eval")
            ),
            "last_stock_diagnosis": _compact_stock_diagnosis_handoff(state.get("last_stock_diagnosis")),
            "last_ai_report": _compact_ai_report_handoff(state.get("last_ai_report")),
            "last_strategy_decision": _compact_strategy_handoff(state.get("last_strategy_decision")),
        }
    )


def _compact_screen_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _pick_fields(
        value,
        (
            "style_preference",
            "theme_preference",
            "preference_match",
            "scan_scope",
            "summary",
            "data_quality",
            "decision_brief",
            "selection_brief",
            "diagnosis_targets",
            "next_action",
            "next_tool",
        ),
    )
    payload["theme_context"] = _compact_theme_context(value.get("theme_context"))
    payload["strategy_policy"] = _compact_strategy_policy(value.get("strategy_policy"))
    payload["action_plan"] = _pick_fields(
        value.get("action_plan"),
        (
            "candidate_action",
            "new_buy_allowed",
            "ai_review_allowed",
            "trade_readiness",
            "reason",
            "next_step",
            "data_quality_gate",
            "quality_gate",
            "review_targets",
            "diagnosis_targets",
        ),
    )
    payload["quality_gate"] = value.get("quality_gate") if isinstance(value.get("quality_gate"), dict) else {}
    payload["symbols_for_report"] = _candidate_rows(value.get("symbols_for_report"), 6)
    payload["report_candidates"] = _candidate_rows(value.get("report_candidates"), 6)
    payload["watch_candidates"] = _candidate_rows(value.get("watch_candidates"), 6)
    payload["top_candidates"] = _candidate_rows(value.get("top_candidates"), 6)
    payload["candidate_guard_summary"] = _compact_candidate_guard(value.get("candidate_guard_summary"))
    return _drop_empty(payload)


def _compact_theme_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty(
        {
            "event_mainlines": _clip_text(value.get("event_mainlines"), 240),
            "today_activity": _clip_text(value.get("today_activity"), 240),
            "theme_radar": _clip_text(value.get("theme_radar"), 240),
            "theme_radar_source": value.get("theme_radar_source"),
            "hot_concepts": _as_list(value.get("hot_concepts"))[:6],
        }
    )


def _compact_stock_diagnosis_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _pick_fields(value, ("latest", "next_action"))
    payload["diagnosed_symbols"] = _candidate_rows(value.get("diagnosed_symbols"), 6)
    return _drop_empty(payload)


def _compact_recommendation_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    selection = value.get("policy_selection") if isinstance(value.get("policy_selection"), dict) else {}
    return _drop_empty(
        {
            "result_summary": str(value.get("result_summary") or "")[:1000],
            "metadata": value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
            "policy_selection": {
                **_pick_fields(
                    selection,
                    ("status", "selection_strategy", "top_k", "recommend_date", "uses_promoted_ranking", "action_plan"),
                ),
                "picks": _candidate_rows(selection.get("picks"), 6),
            },
            "candidate_guard_summary": _compact_candidate_guard(value.get("candidate_guard_summary")),
        }
    )


def _compact_ai_report_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _pick_fields(
        value, ("ok", "reason", "model", "stock_count", "reviewed_codes", "next_action", "next_tool")
    )
    payload["strategy_policy"] = _compact_strategy_policy(value.get("strategy_policy"))
    payload["reviewed_symbols"] = _candidate_rows(value.get("reviewed_symbols"), 8)
    payload["candidate_guard_summary"] = _compact_candidate_guard(value.get("candidate_guard_summary"))
    return _drop_empty(payload)


def _compact_strategy_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _pick_fields(
        value,
        (
            "ok",
            "status",
            "reason",
            "report_source",
            "candidate_count",
            "reviewed_codes",
            "screen_summary",
            "decision_brief",
            "next_action",
            "message",
        ),
    )
    payload["strategy_policy"] = _compact_strategy_policy(value.get("strategy_policy"))
    payload["reviewed_symbols"] = _candidate_rows(value.get("reviewed_symbols"), 8)
    payload["candidate_guard_summary"] = _compact_candidate_guard(value.get("candidate_guard_summary"))
    return _drop_empty(payload)


def _compact_strategy_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _pick_fields(
        value,
        (
            "dynamic_mode",
            "execution_policy",
            "policy_weight_active_scope",
            "selection_action_count",
            "formal_dynamic_allowed",
            "next_action",
            "signal_weights",
            "attribution_signal_weights",
        ),
    )
    if mode := str(value.get("dynamic_mode") or "").strip():
        payload["dynamic_mode_label"] = policy_execution_mode_label(mode)
    if mode := str(value.get("execution_policy") or "").strip():
        payload["execution_policy_label"] = policy_execution_mode_label(mode)
    if action := str(value.get("next_action") or "").strip():
        payload["next_action_label"] = policy_next_action_label(action)
    if summary := _clip_text(value.get("selection_action_summary"), 240):
        payload["selection_action_summary"] = summary
    return _drop_empty(payload)


def _compact_candidate_guard(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _pick_fields(value, ("direct_buy_blocked_count", "message"))
    payload["candidates"] = _candidate_guard_rows(value.get("candidates"), 5)
    return _drop_empty(payload)


def _candidate_guard_rows(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_compact_candidate_guard_row(row) for row in value[:limit] if isinstance(row, dict)]


def _compact_candidate_guard_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = _pick_fields(
        row,
        (
            "code",
            "name",
            "reason",
            "action_status",
            "action_label",
            "action_level",
            "direct_buy_allowed",
            "label_ready",
            "trade_readiness",
            "new_buy_allowed",
            "risk_factors",
            "next_step",
        ),
    )
    if payload.get("action_status") or payload.get("status") or "direct_buy_allowed" in row:
        payload.update(candidate_action_fields({**row, **payload}))
    return _drop_empty(payload)


def _candidate_rows(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(value):
        row = _compact_candidate(item) if isinstance(item, dict) else _scalar_candidate(item)
        if row:
            rank_row = item if isinstance(item, dict) else row
            rows.append((index, row, rank_row))
    ranked_rows = sorted(rows, key=lambda item: _candidate_rank_key(item[2], item[0]), reverse=True)
    return [row for _index, row, _rank_row in ranked_rows[:limit]]


def _compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    payload = _pick_fields(row, _CANDIDATE_HANDOFF_FIELDS)
    if "code" not in payload and (code := _candidate_code(row)):
        payload["code"] = code
    if payload.get("action_status") or payload.get("status") or "direct_buy_allowed" in row:
        payload.update(candidate_action_fields({**row, **payload}))
    for field in (
        "profile",
        "theme_event_title",
        "theme_event_reason",
        "tape_condition",
        "invalidate_condition",
        "trigger_condition",
        "trigger_reason",
        "invalid_condition",
        "invalid_reason",
    ):
        if field in payload:
            payload[field] = _clip_text(payload[field], 240)
    if "triggers" in payload:
        payload["triggers"] = _compact_candidate_triggers(payload["triggers"])
    return _drop_empty(payload)


def _compact_candidate_triggers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clip_text(item, 80) for item in (str(item).strip() for item in value[:5]) if item]


def _scalar_candidate(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    return {"code": text} if any(char.isdigit() for char in text) else {"name": text}


def _candidate_code(row: dict[str, Any]) -> str:
    return str(
        row.get("symbol")
        or row.get("stock_code")
        or row.get("stockCode")
        or row.get("ticker")
        or row.get("sec_code")
        or ""
    ).strip()


def _pick_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty({field: value.get(field) for field in fields})


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _synthesis_prompt(run: WorkflowRun, results: list[dict[str, Any]]) -> str:
    script_prompt = _script_synthesis_prompt(run.script)
    handoff_summary = _synthesis_handoff_summary(results)
    agent_results = _synthesis_agent_results(results)
    disagreement_summary = build_workflow_disagreement_summary(results)
    return (
        "请基于以下动态 workflow 执行结果，给用户输出最终中文答复。\n"
        "要求：只使用 agent 结果里的事实；如果某步失败，明确说明影响和降级结论。"
        "若多 Agent 方向冲突，先说明冲突再给保守行动条件。\n"
        f"{_SYNTHESIS_REQUIREMENTS}\n"
        f"模型脚本的汇总要求:\n{script_prompt or '-'}\n\n"
        f"用户请求:\n{run.user_text}\n\n"
        f"workflow script:\n{json.dumps(run.script, ensure_ascii=False, default=str)[:4000]}\n\n"
        f"priority candidate handoff:\n{json.dumps(handoff_summary, ensure_ascii=False, default=str)[:6000]}\n\n"
        f"low-sensitivity agent disagreement summary:\n{json.dumps(disagreement_summary, ensure_ascii=False, default=str)[:3000]}\n\n"
        f"agent results:\n{json.dumps(agent_results, ensure_ascii=False, default=str)[:12000]}"
    )


def _has_material_disagreement(summary: dict[str, Any]) -> bool:
    return bool(summary.get("degraded_steps")) or summary.get("conflict_type") in {
        "mixed_directional_signals",
        "partial_bullish_with_degraded_inputs",
        "partial_bearish_with_degraded_inputs",
        "degraded_inputs",
    }


def _script_synthesis_prompt(script: Any) -> str:
    if not isinstance(script, dict):
        return ""
    for field in SYNTHESIS_PROMPT_FIELDS:
        if value := _script_text_value(script.get(field)):
            return value
    return ""


def _script_text_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple, set)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "；".join(f"{key}: {item}" for key, item in value.items() if str(item).strip())
    return str(value).strip()


def _synthesis_agent_results(results: list[dict[str, Any]]) -> list[Any]:
    return [_synthesis_agent_result(item) for item in results]


def _synthesis_agent_result(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    result = item.get("result")
    if not isinstance(result, dict) or "handoff_state" not in result:
        return item
    clean = dict(item)
    clean_result = dict(result)
    clean_result.pop("handoff_state", None)
    clean_result["handoff_state_ref"] = "see priority candidate handoff"
    clean["result"] = clean_result
    return clean


def _synthesis_handoff_summary(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    aggregate_handoff: dict[str, Any] = {}
    seen_keys: set[str] = set()
    for item in reversed(results):
        result = item.get("result") if isinstance(item, dict) else {}
        handoff = result.get("handoff_state") if isinstance(result, dict) else {}
        if not isinstance(handoff, dict) or not handoff:
            continue
        handoff = _latest_handoff_keys(handoff, seen_keys)
        if not handoff:
            continue
        aggregate_handoff.update(handoff)
        step = item.get("step") if isinstance(item.get("step"), dict) else {}
        summary.append(
            _drop_empty(
                {
                    "step_id": step.get("step_id"),
                    "title": step.get("title"),
                    "handoff_state": handoff,
                }
            )
        )
    ordered = list(reversed(summary))
    if ordered and (conclusions := _candidate_conclusions_from_handoff(aggregate_handoff)):
        ordered[0] = {
            "candidate_conclusion": conclusions[0],
            "candidate_conclusions": conclusions,
            **ordered[0],
        }
    return ordered


def _latest_handoff_keys(handoff: dict[str, Any], seen_keys: set[str]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for key, value in handoff.items():
        if key in seen_keys or value in (None, "", [], {}):
            continue
        seen_keys.add(key)
        latest[key] = value
    return latest


def _collect_synthesis(
    provider: Any,
    prompt: str,
    system_prompt: str,
    *,
    fallback_text: str = "",
) -> tuple[str, dict[str, int]]:
    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for chunk in provider.chat_stream([{"role": "user", "content": prompt}], [], system_prompt):
        if chunk.get("type") == "text_delta":
            text_parts.append(str(chunk.get("text", "")))
        elif chunk.get("type") == "usage":
            usage["input_tokens"] += int(chunk.get("input_tokens", 0) or 0)
            usage["output_tokens"] += int(chunk.get("output_tokens", 0) or 0)
    text = "".join(text_parts).strip()
    return (text or fallback_text or _fallback_summary([]), usage)


def _fallback_summary(results: list[dict[str, Any]]) -> str:
    lines = ["动态 workflow 已完成，以下是各 sub-agent 的结果摘要："]
    if conclusion := _fallback_candidate_conclusion(results):
        lines.append(conclusion)
    for item in results:
        step = item.get("step", {})
        result = item.get("result", {})
        title = step.get("title", "任务")
        status = result.get("status", "unknown")
        content = result.get("result") or result.get("error") or "无结果"
        lines.append(f"- {title} [{status}]: {str(content)[:500]}")
        for line in _fallback_background_lines(result.get("background_tasks")):
            lines.append(f"  后台: {line}")
        for line in _fallback_handoff_lines(result.get("handoff_state")):
            lines.append(f"  证据: {line}")
    return "\n".join(lines)


def _fallback_background_lines(tasks: Any) -> list[str]:
    lines: list[str] = []
    for task in _as_list(tasks):
        if not isinstance(task, dict):
            continue
        name = str(task.get("tool_name") or task.get("task_id") or "background").strip()
        status = str(task.get("status") or "").strip()
        if summary := _clip_text(task.get("result_summary"), 500):
            lines.append(f"{name} [{status}]: {summary}")
        elif task.get("error"):
            lines.append(f"{name} [{status or 'failed'}]: {_clip_text(task.get('error'), 300)}")
    return lines


def _fallback_candidate_conclusion(results: list[dict[str, Any]]) -> str:
    conclusions = _candidate_conclusions_from_results(results)
    lines = [str(item.get("line") or "") for item in conclusions if item.get("line")]
    if lines:
        return "\n".join(lines)
    return ""


def _candidate_conclusion_from_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    return next(iter(_candidate_conclusions_from_handoff(handoff, limit=1)), {})


def _candidate_conclusions_from_results(results: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    handoff = _latest_results_handoff(results)
    return _candidate_conclusions_from_handoff(handoff, limit=limit) if handoff else []


def _latest_results_handoff(results: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    seen_keys: set[str] = set()
    for item in reversed(results):
        result = item.get("result") if isinstance(item, dict) else {}
        handoff = result.get("handoff_state") if isinstance(result, dict) else {}
        if isinstance(handoff, dict):
            aggregate.update(_latest_handoff_keys(handoff, seen_keys))
    return aggregate


def _ensure_candidate_delivery(text: str, results: list[dict[str, Any]]) -> str:
    conclusions = _candidate_conclusions_from_results(results)
    if not conclusions or _text_covers_candidate_conclusions(text, conclusions):
        return text
    lowered = text.lower()
    lines = [
        str(item.get("line") or "")
        for item in conclusions
        if item.get("line") and not _text_covers_candidate_conclusion(lowered, item)
    ]
    if not lines:
        return text
    candidate_text = "\n".join(lines)
    return f"{candidate_text}\n\n{text}" if text.strip() else candidate_text


def _text_covers_candidate_conclusions(text: str, conclusions: list[dict[str, Any]]) -> bool:
    if not text.strip():
        return False
    lowered = text.lower()
    return all(_text_covers_candidate_conclusion(lowered, item) for item in conclusions)


def _text_covers_candidate_conclusion(lowered_text: str, item: dict[str, Any]) -> bool:
    return (
        _text_mentions_candidate_identity(lowered_text, item)
        and _text_covers_candidate_role(lowered_text, item)
        and _text_covers_candidate_action(lowered_text, item)
        and _text_covers_candidate_support(lowered_text, item)
    )


def _text_covers_candidate_role(lowered_text: str, item: dict[str, Any]) -> bool:
    role = str(item.get("role") or "").strip().lower()
    return not role or role in lowered_text


def _text_mentions_candidate_identity(lowered_text: str, item: dict[str, Any]) -> bool:
    for field in ("code", "name"):
        value = str(item.get(field) or "").strip()
        if value and value.lower() in lowered_text:
            return True
    return False


def _text_covers_candidate_action(lowered_text: str, item: dict[str, Any]) -> bool:
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    return all(str(value).lower() in lowered_text for value in action.values() if str(value).strip())


def _text_covers_candidate_support(lowered_text: str, item: dict[str, Any]) -> bool:
    guard = str(item.get("guard_reason") or "").strip().lower()
    if guard and guard not in lowered_text:
        return False
    preference_misses = [str(value).strip().lower() for value in _as_list(item.get("preference_misses"))]
    if preference_misses and not any(value in lowered_text for value in preference_misses):
        return False
    support_items = [
        *_as_list(item.get("evidence")),
        *_as_list(item.get("quality_factors")),
        *_as_list(item.get("risk_factors")),
        *_as_list(item.get("preference_misses")),
        item.get("next_step"),
    ]
    support_items = [str(value).strip().lower() for value in support_items if str(value or "").strip()]
    return not support_items or any(value in lowered_text for value in support_items)


def _candidate_conclusions_from_handoff(handoff: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    conclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    primary_ready_count = 0
    for stage in (
        "last_strategy_decision",
        "last_ai_report",
        "last_stock_diagnosis",
        "last_recommendation_event_eval",
        "last_screen_result",
    ):
        value = handoff.get(stage)
        if not isinstance(value, dict):
            continue
        for row in _ranked_stage_candidates(stage, value):
            merged = _fallback_merged_candidate(row, handoff)
            key = _candidate_identity(merged)
            if key in seen:
                continue
            seen.add(key)
            guard_reason = _fallback_guard_reason_from_handoff(merged, value, handoff)
            ready_rank = _ready_candidate_rank(merged, guard_reason, primary_ready_count)
            primary_ready_count += 1 if ready_rank else 0
            conclusions.append(
                _fallback_candidate_conclusion_payload(merged, value, handoff, stage, guard_reason, ready_rank)
            )
            if len(conclusions) >= limit:
                return conclusions
    return conclusions


def _ranked_stage_candidates(stage: str, value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _fallback_stage_candidates(stage, value)
    return [
        row
        for _index, row in sorted(
            enumerate(rows),
            key=lambda item: _candidate_rank_key(item[1], item[0]),
            reverse=True,
        )
    ]


def _candidate_identity(row: dict[str, Any]) -> str:
    return str(row.get("code") or row.get("name") or id(row)).strip()


def _fallback_candidate_conclusion_payload(
    row: dict[str, Any],
    stage: dict[str, Any],
    handoff: dict[str, Any],
    source_stage: str,
    guard_reason: str = "",
    ready_rank: int = 0,
) -> dict[str, Any]:
    role = _fallback_candidate_prefix(row, guard_reason, ready_rank)
    line = _fallback_candidate_line(row, stage, handoff, guard_reason, ready_rank, role)
    action_fields = _fallback_candidate_action_fields(row)
    return _drop_empty(
        {
            "line": line,
            "role": role,
            "code": str(row.get("code") or "").strip(),
            "name": str(row.get("name") or "").strip(),
            **action_fields,
            "evidence": _fallback_evidence_items(row),
            "action": _fallback_action_payload(row),
            "quality_factors": _fallback_quality_items(row, 4, 120),
            "risk_factors": _fallback_risk_items(row, 4, 120),
            "preference_misses": _fallback_preference_miss_items(row, stage, handoff, 4, 120),
            "guard_reason": guard_reason,
            "next_step": _fallback_next_value(row, stage),
            "source_stage": source_stage,
        }
    )


def _fallback_candidate_action_fields(row: dict[str, Any]) -> dict[str, Any]:
    if not any(key in row for key in ("action_status", "status", "direct_buy_allowed")):
        return {}
    return candidate_action_fields(row)


def _ready_candidate_rank(row: dict[str, Any], guard_reason: str, ready_count: int) -> int:
    if guard_reason or str(row.get("action_status") or "").strip() != "ready_for_ai_review":
        return 0
    return ready_count + 1


def _fallback_candidate_line(
    row: dict[str, Any],
    stage: dict[str, Any],
    handoff: dict[str, Any],
    guard_reason: str = "",
    ready_rank: int = 0,
    role: str = "",
) -> str:
    if not guard_reason:
        guard_reason = _fallback_guard_reason_from_handoff(row, stage, handoff)
    if not role:
        role = _fallback_candidate_prefix(row, guard_reason, ready_rank)
    parts = [
        f"{role} {_fallback_candidate_name(row)}",
        _fallback_status_part(row),
        _fallback_action_part(row),
        _fallback_evidence_part(row),
        _fallback_quality_part(row),
        _fallback_preference_part(row, stage, handoff),
        _fallback_risk_part(row),
        _fallback_guard_part(guard_reason),
        _fallback_next_part(row, stage),
    ]
    return "候选结论: " + "；".join(part for part in parts if part)


def _fallback_candidate_prefix(row: dict[str, Any], guard_reason: str = "", ready_rank: int = 0) -> str:
    return candidate_action_role(
        row.get("action_status"),
        guard_reason=guard_reason,
        ready_rank=ready_rank,
        has_code=bool(str(row.get("code") or "").strip()),
    )


def _fallback_merged_candidate(row: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    allow_name_match = not _ambiguous_name_match(row, handoff)
    for stage in (
        "last_screen_result",
        "last_recommendation_event_eval",
        "last_stock_diagnosis",
        "last_ai_report",
        "last_strategy_decision",
    ):
        value = handoff.get(stage)
        if not isinstance(value, dict):
            continue
        for candidate in _fallback_stage_candidates(stage, value):
            if _candidate_matches(candidate, row, allow_name_match=allow_name_match):
                merged.update(_drop_empty(candidate))
    merged.update(_drop_empty(row))
    return merged


def _ambiguous_name_match(row: dict[str, Any], handoff: dict[str, Any]) -> bool:
    if str(row.get("code") or "").strip():
        return False
    name = str(row.get("name") or "").strip()
    if not name:
        return False
    codes: set[str] = set()
    for stage in (
        "last_screen_result",
        "last_recommendation_event_eval",
        "last_stock_diagnosis",
        "last_ai_report",
        "last_strategy_decision",
    ):
        value = handoff.get(stage)
        if not isinstance(value, dict):
            continue
        for candidate in _fallback_stage_candidates(stage, value):
            if str(candidate.get("name") or "").strip() == name and (code := str(candidate.get("code") or "").strip()):
                codes.add(code)
            if len(codes) > 1:
                return True
    return False


def _fallback_candidate_name(row: dict[str, Any]) -> str:
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip()
    return " ".join(part for part in (code, name) if part) or "候选"


def _fallback_status_part(row: dict[str, Any]) -> str:
    parts = []
    if status := _fallback_status_text(row):
        parts.append(f"状态={status}")
    if readiness := str(row.get("trade_readiness") or "").strip():
        parts.append(f"交易就绪={readiness}")
    if row.get("new_buy_allowed") is False:
        parts.append("不允许新增买入")
    return "，".join(parts)


def _fallback_status_text(row: dict[str, Any]) -> str:
    label = str(row.get("status_label") or "").strip()
    if label:
        return label
    status = str(row.get("action_status") or "").strip()
    return candidate_action_label(status) if status else ""


def _fallback_action_part(row: dict[str, Any]) -> str:
    action = _fallback_action_payload(row)
    parts = [
        _action_payload_part(action, "entry_zone", "入场区"),
        _action_payload_part(action, "tape_condition", "触发"),
        _action_payload_part(action, "trigger_price", "触发价"),
        _action_payload_part(action, "stop_loss", "止损"),
        _action_payload_part(action, "invalidate_condition", "失效"),
        _action_payload_part(action, "invalid_price", "失效价"),
        _action_payload_part(action, "max_entry_price", "防追高限价"),
    ]
    return "，".join(part for part in parts if part)


def _fallback_action_payload(row: dict[str, Any]) -> dict[str, str]:
    return _drop_empty(
        {
            "entry_zone": _fallback_entry_zone(row),
            "tape_condition": _first_action_value(row, ("tape_condition", "trigger_condition", "entry_trigger")),
            "trigger_price": _first_action_value(row, ("trigger_price", "trigger_level", "entry_price")),
            "stop_loss": _first_action_value(row, ("stop_loss", "effective_stop_loss", "original_stop_loss")),
            "invalidate_condition": _first_action_value(row, ("invalidate_condition", "invalid_condition")),
            "invalid_price": _first_action_value(row, ("invalid_price", "invalid_level")),
            "max_entry_price": _first_action_value(row, ("max_entry_price",)),
        }
    )


def _action_payload_part(action: dict[str, str], field: str, label: str) -> str:
    return f"{label}={value}" if (value := action.get(field)) else ""


def _fallback_entry_zone(row: dict[str, Any]) -> str:
    zone = row.get("entry_zone")
    if isinstance(zone, (list, tuple)) and len(zone) >= 2:
        return f"{_format_action_value(zone[0])}-{_format_action_value(zone[1])}"
    if isinstance(zone, str) and zone.strip():
        return _clip_text(zone, 90)
    low, high = row.get("entry_zone_min"), row.get("entry_zone_max")
    if _has_action_value(low) and _has_action_value(high):
        return f"{_format_action_value(low)}-{_format_action_value(high)}"
    return _first_action_value(row, ("buy_zone",))


def _first_action_value(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = row.get(field)
        if _has_action_value(value):
            return _format_action_value(value)
    return ""


def _has_action_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _format_action_value(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return _clip_text(value, 90)


def _fallback_evidence_part(row: dict[str, Any]) -> str:
    evidence = _fallback_evidence_items(row)
    return f"证据={','.join(evidence)}" if evidence else ""


def _fallback_evidence_items(row: dict[str, Any]) -> list[str]:
    evidence = [
        _grade_score_part("候选影子", row.get("candidate_shadow_grade"), row.get("candidate_shadow_score")),
        _grade_score_part("入场", row.get("entry_quality_grade"), row.get("entry_quality_score")),
        _score_part("漏斗分", row.get("funnel_score")),
        _score_part("风险调整分", row.get("risk_adjusted_quality_score")),
        _score_part("候选质量分", row.get("candidate_quality_score")),
        _score_part("诊断分", row.get("candidate_score")),
        _score_part("优先分", row.get("priority_score")),
        _theme_evidence_part(row),
        _trigger_evidence_part(row),
    ]
    return [part for part in evidence if part]


def _trigger_evidence_part(row: dict[str, Any]) -> str:
    labels = [TRIGGER_SHORT_LABELS.get(str(item), str(item)) for item in _as_list(row.get("triggers"))[:4] if str(item)]
    if not labels:
        labels = _profile_trigger_labels(row)
    return f"触发={'+'.join(labels)}" if labels else ""


def _fallback_quality_part(row: dict[str, Any]) -> str:
    factors = _fallback_quality_items(row, 3, 80)
    return f"亮点={','.join(factors)}" if factors else ""


def _fallback_quality_items(row: dict[str, Any], limit: int, clip: int) -> list[str]:
    factors: list[str] = []
    for value in (
        row.get("style_match_reasons"),
        row.get("theme_match_reasons"),
        _profile_quality_items(row, clip),
        _rank_reason_items(row, clip),
        row.get("quality_factors"),
    ):
        for item in _fallback_text_items(value, limit, clip):
            if item not in factors:
                factors.append(item)
            if len(factors) >= limit:
                return factors
    return factors


def _rank_reason_items(row: dict[str, Any], clip: int) -> list[str]:
    text = str(row.get("rank_reason") or "").strip()
    if not text:
        return []
    return [_clip_text(part, clip) for part in re.split(r"[；;]", text) if part.strip()]


def _profile_quality_items(row: dict[str, Any], clip: int) -> list[str]:
    return [_clip_text(part, clip) for part in _profile_parts(row) if not part.startswith("触发:")]


def _profile_trigger_labels(row: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for part in _profile_parts(row):
        if not part.startswith("触发:"):
            continue
        labels.extend(item.strip() for item in part.split(":", 1)[1].split("+") if item.strip())
    return labels[:4]


def _profile_parts(row: dict[str, Any]) -> list[str]:
    return [part.strip() for part in str(row.get("profile") or "").split("/") if part.strip()]


def _fallback_preference_part(row: dict[str, Any], stage: dict[str, Any], handoff: dict[str, Any]) -> str:
    misses = _fallback_preference_miss_items(row, stage, handoff, 3, 80)
    return f"偏好={','.join(misses)}" if misses else ""


def _fallback_preference_miss_items(
    row: dict[str, Any], stage: dict[str, Any], handoff: dict[str, Any], limit: int, clip: int
) -> list[str]:
    source = _fallback_screen_preference_source(stage, handoff)
    items: list[str] = []
    for key, label in (("style", "风格偏好未命中"), ("theme", "主题偏好未命中")):
        if key == "style":
            items.extend(_fallback_missing_style_preference_items(row, source, clip))
            if len(items) >= limit:
                return items[:limit]
            continue
        if not _fallback_has_preference(source, key) or _candidate_preference_hit(row, key):
            continue
        if item := _fallback_preference_miss_item(label, source.get(f"{key}_preference"), clip):
            items.append(item)
        if len(items) >= limit:
            return items
    return items


def _fallback_screen_preference_source(stage: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
    if _fallback_has_any_preference(stage) or isinstance(stage.get("preference_match"), dict):
        return stage
    screen = handoff.get("last_screen_result") if isinstance(handoff.get("last_screen_result"), dict) else {}
    return screen if _fallback_has_any_preference(screen) or isinstance(screen.get("preference_match"), dict) else stage


def _fallback_has_any_preference(source: dict[str, Any]) -> bool:
    return _fallback_has_preference(source, "style") or _fallback_has_preference(source, "theme")


def _fallback_has_preference(source: dict[str, Any], key: str) -> bool:
    value = source.get(f"{key}_preference")
    if not isinstance(value, dict):
        return False
    if key == "style":
        return bool(_as_list(value.get("styles")) or str(value.get("raw") or "").strip())
    return bool(value.get("theme") or str(value.get("raw") or "").strip())


def _fallback_missing_style_preference_items(row: dict[str, Any], source: dict[str, Any], clip: int) -> list[str]:
    value = source.get("style_preference")
    if not isinstance(value, dict):
        return []
    requested = [str(item) for item in _as_list(value.get("styles")) if str(item)]
    if not requested:
        if not _fallback_has_preference(source, "style") or _candidate_preference_hit(row, "style"):
            return []
        return [_fallback_preference_miss_item("风格偏好未命中", value, clip)]
    matched = set(_fallback_candidate_style_match_styles(row, requested))
    labels = {"trend": "趋势", "pullback": "低吸", "quality": "质量"}
    missing = [labels.get(style, style) for style in requested if style not in matched]
    return [_clip_text(f"风格偏好未命中: {'/'.join(missing)}", clip)] if missing else []


def _fallback_candidate_style_match_styles(row: dict[str, Any], requested: list[str]) -> list[str]:
    styles = [str(item) for item in _as_list(row.get("style_match_styles")) if str(item)]
    if not styles:
        reasons = [str(item) for item in _as_list(row.get("style_match_reasons"))]
        styles = [
            *("trend" for reason in reasons if reason.startswith("趋势偏好")),
            *("pullback" for reason in reasons if reason.startswith("低吸偏好")),
            *("quality" for reason in reasons if reason.startswith("稳健偏好")),
        ]
    if not styles and row.get("style_match") is True:
        styles = requested
    return list(dict.fromkeys(style for style in styles if style in requested))


def _fallback_preference_miss_item(label: str, value: Any, clip: int) -> str:
    text = _fallback_preference_text(value)
    return _clip_text(f"{label}: {text}" if text else label, clip)


def _fallback_preference_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if styles := _as_list(value.get("styles")):
        labels = {"trend": "趋势", "pullback": "低吸", "quality": "质量"}
        return ",".join(dict.fromkeys(labels.get(str(item), str(item)) for item in styles if str(item)))
    return str(value.get("theme") or value.get("raw") or "").strip()


def _candidate_preference_hit(row: dict[str, Any], key: str) -> bool:
    if row.get(f"{key}_match") is True:
        return True
    try:
        if int(row.get(f"{key}_match_score") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return bool(_as_list(row.get(f"{key}_match_reasons")))


def _fallback_risk_part(row: dict[str, Any]) -> str:
    risks = _fallback_risk_items(row, 3, 80)
    return f"风险={','.join(risks)}" if risks else ""


def _fallback_risk_items(row: dict[str, Any], limit: int, clip: int) -> list[str]:
    risks: list[str] = []
    for value in (row.get("risk_factors"), row.get("entry_quality_risk_flags")):
        for item in _fallback_text_items(value, limit, clip):
            if item not in risks:
                risks.append(item)
            if len(risks) >= limit:
                return risks
    return risks


def _fallback_text_items(value: Any, limit: int, clip: int) -> list[str]:
    return [_clip_text(item, clip) for item in _as_list(value)[:limit] if str(item or "").strip()]


def _theme_evidence_part(row: dict[str, Any]) -> str:
    theme = str(row.get("strategic_theme") or row.get("theme") or "").strip()
    if not theme:
        return ""
    source = str(row.get("theme_source") or "").strip()
    label = "事件主线" if source == "ths_hot_event" else "主题"
    reason = str(row.get("theme_event_reason") or "").strip()
    return f"{label}{theme}({reason})" if reason else f"{label}{theme}"


def _fallback_guard_part(reason: str) -> str:
    return f"护栏={reason}" if reason else ""


def _fallback_guard_reason_from_handoff(row: dict[str, Any], stage: dict[str, Any], handoff: dict[str, Any]) -> str:
    if reason := _fallback_guard_reason(row, stage):
        return reason
    for value in handoff.values():
        if isinstance(value, dict) and (reason := _fallback_guard_reason(row, value)):
            return reason
    return ""


def _fallback_guard_reason(row: dict[str, Any], stage: dict[str, Any]) -> str:
    guard = stage.get("candidate_guard_summary") if isinstance(stage.get("candidate_guard_summary"), dict) else {}
    candidates = _as_list(guard.get("candidates")) if isinstance(guard, dict) else []
    for item in candidates:
        if isinstance(item, dict) and _guard_candidate_matches(row, item) and item.get("reason"):
            return str(item["reason"])
    first = next((item for item in candidates if isinstance(item, dict) and item.get("reason")), {})
    if first and not _has_candidate_identity(row):
        return str(first["reason"])
    if reason := _fallback_gate_reason(row, stage):
        return reason
    if reason := _missing_code_guard_reason(row):
        return reason
    if reason := candidate_guard_reason(row):
        return reason
    return ""


def _guard_candidate_matches(row: dict[str, Any], item: dict[str, Any]) -> bool:
    code = str(row.get("code") or "").strip()
    item_code = str(item.get("code") or "").strip()
    if code and item_code and code == item_code:
        return True
    name = str(row.get("name") or "").strip()
    item_name = str(item.get("name") or "").strip()
    return bool(name and item_name and name == item_name)


def _has_candidate_identity(row: dict[str, Any]) -> bool:
    return bool(str(row.get("code") or row.get("name") or "").strip())


def _missing_code_guard_reason(row: dict[str, Any]) -> str:
    if str(row.get("code") or "").strip():
        return ""
    return "候选缺少股票代码，需先确认具体标的" if str(row.get("name") or "").strip() else ""


def _fallback_gate_reason(row: dict[str, Any], stage: dict[str, Any]) -> str:
    action_plan = stage.get("action_plan") if isinstance(stage.get("action_plan"), dict) else {}
    for gate in _fallback_gate_sources(stage, action_plan):
        if reason := _gate_reason_for_candidate(row, gate):
            return reason
    return ""


def _fallback_gate_sources(stage: dict[str, Any], action_plan: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ("review_targets", "quality_gate", "data_quality_gate")
    gates = [action_plan.get(key) for key in keys]
    gates.extend(stage.get(key) for key in keys)
    return [gate for gate in gates if isinstance(gate, dict)]


def _gate_reason_for_candidate(row: dict[str, Any], gate: dict[str, Any]) -> str:
    candidates = _as_list(gate.get("candidates"))
    for item in candidates:
        if isinstance(item, dict) and _guard_candidate_matches(row, item) and item.get("reason"):
            return str(item["reason"])
    if candidates and _has_candidate_identity(row):
        return ""
    return str(gate.get("reason") or "")


def _fallback_next_part(row: dict[str, Any], stage: dict[str, Any]) -> str:
    return f"下一步={value}" if (value := _fallback_next_value(row, stage)) else ""


def _fallback_next_value(row: dict[str, Any], stage: dict[str, Any]) -> str:
    action_plan = stage.get("action_plan") if isinstance(stage.get("action_plan"), dict) else {}
    next_step = row.get("next_step") or stage.get("next_action") or action_plan.get("next_step")
    return str(next_step or "")


def _candidate_rank_key(row: dict[str, Any], index: int) -> tuple[int, int, float, int]:
    return (
        _candidate_status_rank(row),
        1 if row.get("selected_for_report") is True or row.get("is_ai_recommended") is True else 0,
        _candidate_best_score(row),
        -index,
    )


def _candidate_status_rank(row: dict[str, Any]) -> int:
    status = str(row.get("action_status") or row.get("status") or "").strip()
    if status == "ready_for_ai_review":
        return 4
    if status in {"priority_watch", "trigger_watch"}:
        return 3
    if status in {"candidate", "review_ready", "repair_review_only", "confirmation_required"}:
        return 3
    if status in {"caution_watch", "watch_only", "watch"}:
        return 2
    if status.startswith("blocked_"):
        return 1
    if status == "avoid":
        return 0
    return 2


def _candidate_best_score(row: dict[str, Any]) -> float:
    scores = (
        row.get("candidate_shadow_score"),
        row.get("risk_adjusted_quality_score"),
        row.get("candidate_quality_score"),
        row.get("candidate_score"),
        row.get("entry_quality_score"),
        row.get("funnel_score"),
        row.get("priority_score"),
        row.get("shadow_score"),
        row.get("score"),
    )
    values = [_score_float(value) for value in scores]
    return max((value for value in values if value is not None), default=0.0)


def _score_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fallback_stage_candidates(stage: str, value: dict[str, Any]) -> list[dict[str, Any]]:
    if stage == "last_screen_result":
        selection = value.get("selection_brief") if isinstance(value.get("selection_brief"), dict) else {}
        rows: list[Any] = []
        rows.extend(_as_list(value.get("report_candidates")))
        rows.extend(_as_list(value.get("symbols_for_report")))
        rows.append(selection.get("primary_pick"))
        rows.extend(_as_list(selection.get("best_candidates")))
        rows.extend(_as_list(value.get("watch_candidates")))
        rows.extend(_as_list(value.get("top_candidates")))
    elif stage == "last_recommendation_event_eval":
        selection = value.get("policy_selection") if isinstance(value.get("policy_selection"), dict) else {}
        rows = _as_list(selection.get("picks"))
    elif stage == "last_stock_diagnosis":
        rows = _as_list(value.get("diagnosed_symbols"))
    else:
        rows = _as_list(value.get("reviewed_symbols"))
    return [row for row in rows if isinstance(row, dict)]


def _candidate_matches(candidate: dict[str, Any], target: dict[str, Any], *, allow_name_match: bool = True) -> bool:
    code = str(target.get("code") or "").strip()
    candidate_code = str(candidate.get("code") or "").strip()
    if code and candidate_code:
        return code == candidate_code
    if not allow_name_match:
        return False
    name = str(target.get("name") or "").strip()
    candidate_name = str(candidate.get("name") or "").strip()
    return bool(name and candidate_name and name == candidate_name)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _grade_score_part(label: str, grade: Any, score: Any) -> str:
    grade_text = str(grade or "").strip()
    score_text = _score_text(score)
    if grade_text and score_text:
        return f"{label}{grade_text}/{score_text}"
    return f"{label}{grade_text or score_text}" if grade_text or score_text else ""


def _score_part(label: str, score: Any) -> str:
    return f"{label}{value}" if (value := _score_text(score)) else ""


def _score_text(value: Any) -> str:
    try:
        return f"{float(value):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def _fallback_handoff_lines(handoff: Any) -> list[str]:
    if not isinstance(handoff, dict):
        return []
    groups: list[list[str]] = []
    screen = handoff.get("last_screen_result")
    if isinstance(screen, dict):
        groups.append(tool_result_brief_lines("screen_stocks", screen, max_lines=3))
    recommendation = handoff.get("last_recommendation_event_eval")
    if isinstance(recommendation, dict):
        groups.append(tool_result_brief_lines("evaluate_recommendation_events", recommendation, max_lines=3))
    diagnosis = handoff.get("last_stock_diagnosis")
    if isinstance(diagnosis, dict):
        groups.append(tool_result_brief_lines("analyze_stock", diagnosis, max_lines=3))
    report = handoff.get("last_ai_report")
    if isinstance(report, dict):
        groups.append(tool_result_brief_lines("generate_ai_report", report, max_lines=3))
    decision = handoff.get("last_strategy_decision")
    if isinstance(decision, dict):
        groups.append(tool_result_brief_lines("generate_strategy_decision", decision, max_lines=3))
    return _balanced_handoff_lines(groups, limit=8)


def _balanced_handoff_lines(groups: list[list[str]], limit: int) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for group in groups:
        _append_handoff_line(selected, seen, _first_handoff_line(group), limit)
    for group in groups:
        for line in group[1:]:
            _append_handoff_line(selected, seen, line, limit)
    return selected


def _first_handoff_line(lines: list[str]) -> str:
    return next((line for line in lines if line), "")


def _append_handoff_line(selected: list[str], seen: set[str], line: str, limit: int) -> None:
    if len(selected) >= limit or not line or line in seen:
        return
    seen.add(line)
    selected.append(line)
