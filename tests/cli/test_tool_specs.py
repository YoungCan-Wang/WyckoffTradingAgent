from __future__ import annotations

import time

from cli.background import BackgroundTask, BackgroundTaskManager
from cli.sub_agent_prompts import RESEARCH_AGENT_PROMPT, WORKFLOW_TASK_AGENT_PROMPT
from cli.tools import (
    ASK_USER_TIMEOUT_SENTINEL,
    BACKGROUND_TOOLS,
    CONCURRENCY_SAFE_TOOLS,
    CONFIRM_TOOLS,
    TOOL_DISPLAY_NAMES,
    TOOL_SCHEMAS,
    TOOL_SPECS,
    ToolRegistry,
    ask_user_question,
    stringify_json_schema_descriptions,
)


def _iter_descriptions(node):
    if isinstance(node, dict):
        if "description" in node:
            yield node["description"]
        for value in node.values():
            yield from _iter_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_descriptions(item)


def test_tool_specs_cover_all_public_schemas():
    schema_names = {schema["name"] for schema in TOOL_SCHEMAS}

    assert set(TOOL_SPECS) == schema_names
    assert "ask_user" not in schema_names


def test_tool_schema_descriptions_are_strings():
    """NVIDIA / Cohere 会拒掉 description 为 array 的 JSON Schema；括号尾逗号就会变成 tuple。"""
    for schema in TOOL_SCHEMAS:
        for description in _iter_descriptions(schema):
            assert isinstance(description, str), schema["name"]
    items = next(s for s in TOOL_SCHEMAS if s["name"] == "update_portfolio")["parameters"]["properties"]["items"]
    assert isinstance(items["description"], str)
    assert "批量 add/update/remove" in items["description"]
    coerced = stringify_json_schema_descriptions({"description": ("a", "b")})
    assert coerced["description"] == "a b"
    for schema in ToolRegistry().schemas():
        for description in _iter_descriptions(schema):
            assert isinstance(description, str), schema.get("name")


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


def test_confirm_timeout_is_not_reported_as_user_denial():
    """超时说成「用户拒绝」等于伪造一件没发生的事，用户会读到自己从没做过的决定。"""
    registry = ToolRegistry()
    registry.set_confirm_callback(lambda _name, _args: {"action": "timeout"})

    _args, error = registry._confirm_high_risk_call("update_portfolio", {"code": "002648"}, None)

    assert error is not None
    assert error["error"] != "用户拒绝执行此操作"
    assert "超时" in error["error"]
    assert "这不是拒绝" in error["error"]
    assert "重试" in error["error"]


def test_confirm_deny_still_reports_denial():
    registry = ToolRegistry()
    registry.set_confirm_callback(lambda _name, _args: {"action": "deny"})

    _args, error = registry._confirm_high_risk_call("update_portfolio", {"code": "002648"}, None)

    assert error == {"error": "用户拒绝执行此操作"}


def test_ask_user_question_timeout_is_not_reported_as_an_answer():
    """原先它以 status=answered 返回「已超时未作答」，模型只能把这句话当成用户的回答。"""
    registry = ToolRegistry()
    registry.set_ask_user_question_callback(lambda *_a, **_k: ASK_USER_TIMEOUT_SENTINEL)

    result = ask_user_question("要录入哪一条？", tool_context=registry._tool_context)

    assert result["status"] == "timeout"
    assert "answer" not in result
    assert ASK_USER_TIMEOUT_SENTINEL not in result["error"]
    assert "不要把超时当成用户的回答" in result["error"]


def test_ask_user_question_still_returns_real_answers():
    registry = ToolRegistry()
    registry.set_ask_user_question_callback(lambda *_a, **_k: "卫星化学 200股")

    result = ask_user_question("要录入哪一条？", tool_context=registry._tool_context)

    assert result["status"] == "answered"
    assert result["answer"] == "卫星化学 200股"


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
