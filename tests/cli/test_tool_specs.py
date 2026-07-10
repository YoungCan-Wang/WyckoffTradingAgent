from __future__ import annotations

import time

from cli.background import BackgroundTask, BackgroundTaskManager
from cli.sub_agent_prompts import RESEARCH_AGENT_PROMPT, WORKFLOW_TASK_AGENT_PROMPT
from cli.tools import (
    BACKGROUND_TOOLS,
    CONCURRENCY_SAFE_TOOLS,
    CONFIRM_TOOLS,
    TOOL_DISPLAY_NAMES,
    TOOL_SCHEMAS,
    TOOL_SPECS,
    ToolRegistry,
)


def test_tool_specs_cover_all_public_schemas():
    schema_names = {schema["name"] for schema in TOOL_SCHEMAS}

    assert set(TOOL_SPECS) == schema_names
    assert "ask_user" not in schema_names


def test_legacy_tool_sets_are_derived_from_specs():
    assert {name for name, spec in TOOL_SPECS.items() if spec.requires_approval} == CONFIRM_TOOLS
    assert {name for name, spec in TOOL_SPECS.items() if spec.background} == BACKGROUND_TOOLS
    assert {name for name, spec in TOOL_SPECS.items() if spec.concurrency_safe} == CONCURRENCY_SAFE_TOOLS
    assert {name: spec.display_name for name, spec in TOOL_SPECS.items()} == TOOL_DISPLAY_NAMES


def test_tool_registry_reads_runtime_behavior_from_specs():
    registry = ToolRegistry()

    assert registry.display_name("portfolio") == "持仓"
    assert registry.concurrency_safe("portfolio")
    assert registry.requires_approval("write_file")
    assert registry.is_background("run_backtest")
    assert registry.is_background("evaluate_recommendation_events")
    assert registry.display_name("unknown_tool") == "unknown_tool"


def test_tool_registry_filters_schemas_by_workflow_scope():
    registry = ToolRegistry()

    names = {schema["name"] for schema in registry.schemas({"portfolio", "ask_user_question"})}

    assert names == {"portfolio", "ask_user_question"}


def test_ask_user_question_uses_question_callback():
    registry = ToolRegistry()
    observed = {}

    def _answer(question, options, allow_free_text, default_answer):
        observed["question"] = question
        observed["options"] = options
        observed["allow_free_text"] = allow_free_text
        observed["default_answer"] = default_answer
        return "近一年"

    registry.set_ask_user_question_callback(_answer)

    result = registry.execute(
        "ask_user_question",
        {
            "question": "回测区间？",
            "options": ["近半年", "近一年"],
            "allow_free_text": False,
            "default_answer": "近半年",
        },
    )

    assert result["status"] == "answered"
    assert result["answer"] == "近一年"
    assert observed == {
        "question": "回测区间？",
        "options": ["近半年", "近一年"],
        "allow_free_text": False,
        "default_answer": "近半年",
    }


def test_check_background_tasks_schema_mentions_completed_result_summary():
    schema = next(item for item in TOOL_SCHEMAS if item["name"] == "check_background_tasks")

    assert "completed 任务会带 result_summary" in schema["description"]


def test_ai_report_schema_allows_screen_handoff_continuation():
    schema = next(item for item in TOOL_SCHEMAS if item["name"] == "generate_ai_report")

    assert "上一跳筛股候选" in schema["description"]
    assert "stock_codes" not in schema["parameters"].get("required", [])


def test_screen_stocks_schema_exposes_non_bse_a_share_board():
    schema = next(item for item in TOOL_SCHEMAS if item["name"] == "screen_stocks")
    properties = schema["parameters"]["properties"]
    description = properties["board"]["description"]

    assert "main_chinext_star" in description
    assert "不含北交所" in description
    assert "theme" in properties
    assert "机器人" in properties["theme"]["description"]


def test_background_status_includes_completed_result_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    manager = BackgroundTaskManager()
    result = {
        "ok": True,
        "selection_brief": {
            "status": "ready_for_ai_review",
            "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
            "best_codes": ["300750"],
        },
        "trigger_groups": {"huge": [{"code": f"{idx:06d}", "blob": "x" * 200} for idx in range(80)]},
    }
    manager._tasks["bg_screen"] = BackgroundTask(
        id="bg_screen",
        tool_name="screen_stocks",
        status="completed",
        result=result,
        submitted_at=time.monotonic(),
        completed_at=time.monotonic(),
    )

    status = manager.get_status("bg_screen")

    assert status is not None
    assert status["status"] == "completed"
    assert "result_ref:" in status["result_summary"]
    assert "本轮首选可进入 AI 研报复核: 300750 宁德时代" in status["result_summary"]
    assert '"trigger_groups"' not in status["result_summary"]
    assert len(list((tmp_path / "tool-results").glob("*.json"))) == 1

    manager.get_status("bg_screen")

    assert len(list((tmp_path / "tool-results").glob("*.json"))) == 1


def test_sub_agent_prompts_require_background_result_when_needed():
    assert "check_background_tasks 读取 completed 任务的 result_summary" in WORKFLOW_TASK_AGENT_PROMPT
    assert "候选、结论或决策" in RESEARCH_AGENT_PROMPT
