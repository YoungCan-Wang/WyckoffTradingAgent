from __future__ import annotations

import time


def _reset_local_db(local_db) -> None:
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


def _screen_result() -> dict:
    return {
        "ok": True,
        "selection_brief": {
            "status": "ready_for_ai_review",
            "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
            "best_codes": ["300750"],
        },
        "top_candidates": [{"code": "300750", "name": "宁德时代", "track": "Trend"}],
        "symbols_for_report": [{"code": "300750", "name": "宁德时代", "tag": "trend"}],
        "trigger_groups": {"huge": [{"code": f"{idx:06d}", "blob": "x" * 200} for idx in range(80)]},
    }


def _funnel_screen_blocked_result() -> dict:
    reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    return {
        "ok": True,
        "job_kind": "funnel_screen",
        "summary": {"report_candidates": 0},
        "symbols_for_report": [],
        "watch_candidates": [{"code": "000013", "name": "低质量候选", "action_status": "watch_only"}],
        "quality_gate": {"status": "blocked_by_quality_gate", "reason": reason, "blocked_count": 1},
    }


def _recommendation_event_eval_result() -> dict:
    return {
        "ok": True,
        "job_kind": "recommendation_event_eval",
        "result_summary": (
            "推荐事件评估: ready=12/20, hit=60%, ranking_decision=candidate\n"
            "排序接入候选: candidate_shadow_then_score top1 已通过样本/lift/风险门槛\n"
            "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代"
        ),
        "summary": {
            "all": {"rows_ready": 12, "rows_total": 20, "hit_rate_pct": 60.0},
            "ranking_decision": {
                "status": "candidate",
                "recommended_strategy": "candidate_shadow_then_score",
                "recommended_top_k": 1,
                "reason": "candidate_shadow_then_score top1 passed lift and risk gates",
                "candidates": {
                    "candidate_shadow_then_score": {
                        "status": "candidate",
                        "top_k": "1",
                        "decision_score": 101.0,
                        "rows_ready": 12,
                        "hit_rate_delta_pct": 100.0,
                        "avg_mfe_delta_pct": 8.0,
                        "avg_mae_delta_pct": 0.0,
                        "sample_ok": True,
                        "lift_ok": True,
                        "risk_ok": True,
                    }
                },
            },
        },
        "policy_selection": {
            "status": "candidate",
            "selection_strategy": "candidate_shadow_then_score",
            "top_k": 1,
            "recommend_date": 20260601,
            "uses_promoted_ranking": True,
            "action_plan": {
                "primary_action": "generate_ai_report",
                "candidate_action": "generate_ai_report",
                "new_buy_allowed": False,
                "ai_review_allowed": True,
                "trade_readiness": "research_only",
                "review_status": "ready_for_ai_review",
                "reason": "只读推荐事件评估已通过排序接入门槛，可进入 AI 研报；不直接触发买入",
                "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                "next_tool": {"tool": "generate_ai_report", "args": {"stock_codes": ["300750"]}},
            },
            "picks": [
                {
                    "rank": 1,
                    "code": "300750",
                    "name": "宁德时代",
                    "candidate_shadow_grade": "S",
                    "action_status": "ready_for_ai_review",
                    "label_ready": False,
                    "label_status": "partial_window",
                    "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
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
                    "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                }
            ],
        },
        "events": [{"code": f"{idx:06d}", "blob": "x" * 200} for idx in range(80)],
    }


def _diagnosis_result() -> dict:
    return {
        "code": "002326",
        "name": "永太科技",
        "health": "🟢健康",
        "candidate_score": 83.04,
        "diagnosis_brief": {
            "status": "priority_watch",
            "label": "重点观察",
            "headline": "重点观察: 002326 永太科技",
            "risks": ["短线涨幅偏快"],
            "direct_buy_allowed": False,
            "next_step": "加入重点观察，等待市场闸门打开",
        },
        "data_status": "ok",
    }


def _wait_completed(manager, task_id: str) -> dict:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = manager.get_status(task_id)
        if status and status["status"] == "completed":
            return status
        time.sleep(0.01)
    raise AssertionError(f"background task did not complete: {task_id}")


def test_local_db_background_history_uses_tool_result_preview(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "background.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    try:
        local_db.init_db()

        local_db.save_background_task_result(
            "bg_screen",
            "screen_stocks",
            _screen_result(),
            session_id="s1",
        )
        row = local_db.load_background_task_results(limit=1)[0]

        assert row["task_id"] == "bg_screen"
        assert "本轮首选可进入 AI 研报复核: 300750 宁德时代" in row["summary"]
        assert "完整 trigger_groups 已保留在完整结果中" in row["summary"]
        assert '"trigger_groups"' not in row["summary"]
    finally:
        _reset_local_db(local_db)


def test_local_db_background_history_uses_dynamic_workflow_preview(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "background-workflow.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    try:
        local_db.init_db()

        local_db.save_background_task_result(
            "wfbg_screen",
            "dynamic_workflow",
            {
                "workflow_run_id": "wf_screen",
                "workflow": "dynamic_task",
                "final_text": "候选结论: 首选 300750 宁德时代\n风险边界: 跌破 20 日线转观察",
                "events": [
                    {
                        "type": "workflow_step_done",
                        "step": {
                            "title": "扫描候选",
                            "status": "completed",
                            "summary": "候选扫描完成",
                            "evidence": [
                                "候选结论: 首选 300750 宁德时代 · 可进入AI复核",
                                "候选护栏: 1只禁止直接买入",
                            ],
                        },
                    }
                ],
                "huge": [{"blob": "x" * 200} for _ in range(80)],
            },
            session_id="s1",
        )
        row = local_db.load_background_task_results(limit=1)[0]

        assert row["task_id"] == "wfbg_screen"
        assert "候选结论: 首选 300750 宁德时代" in row["summary"]
        assert "wf_screen" in row["summary"]
        assert "扫描候选" in row["summary"]
        assert "候选护栏: 1只禁止直接买入" in row["summary"]
        assert '"huge"' not in row["summary"]
    finally:
        _reset_local_db(local_db)


def test_local_db_dynamic_workflow_preview_prioritizes_candidate_conclusions(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "background-workflow-candidates.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    try:
        local_db.init_db()

        local_db.save_background_task_result(
            "wfbg_candidates",
            "dynamic_workflow",
            {
                "workflow_run_id": "wf_candidates",
                "workflow": "dynamic_task",
                "final_text": (
                    "长前言\n"
                    + ("市场说明。" * 500)
                    + "\n候选结论: 首选 000014 高质量候选\n"
                    + "候选结论: 观察候选 000013 观察候选\n"
                ),
                "events": [
                    {
                        "type": "workflow_step_done",
                        "step": {"title": "扫描候选", "status": "completed", "summary": "候选扫描完成"},
                    }
                ],
                "huge": [{"blob": "x" * 200} for _ in range(80)],
            },
            session_id="s1",
        )
        row = local_db.load_background_task_results(limit=1)[0]

        assert row["task_id"] == "wfbg_candidates"
        assert "candidate_conclusions" in row["summary"]
        assert "候选结论: 首选 000014 高质量候选" in row["summary"]
        assert "候选结论: 观察候选 000013 观察候选" in row["summary"]
        assert '"huge"' not in row["summary"]
    finally:
        _reset_local_db(local_db)


def test_local_db_background_history_uses_recommendation_event_eval_preview(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "background-eval.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    try:
        local_db.init_db()

        local_db.save_background_task_result(
            "bg_eval",
            "web_background_job",
            _recommendation_event_eval_result(),
            session_id="s1",
        )
        row = local_db.load_background_task_results(limit=1)[0]

        assert row["task_id"] == "bg_eval"
        assert "ranking_decision=candidate" in row["summary"]
        assert "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代" in row["summary"]
        assert "候选标签未成熟" in row["summary"]
        assert '"policy_selection"' in row["summary"]
        assert '"events"' not in row["summary"]
    finally:
        _reset_local_db(local_db)


def test_background_manager_completed_results_keeps_status_summary_compact():
    from cli.background import BackgroundTaskManager

    manager = BackgroundTaskManager()
    task_id = manager.submit("bg_screen", "screen_stocks", lambda: _screen_result(), {})

    status = _wait_completed(manager, task_id)
    payloads = manager.completed_results()

    assert status["task_id"] == "bg_screen"
    assert "result" not in status
    assert payloads[0][0] == "bg_screen"
    assert payloads[0][1] == "screen_stocks"
    assert payloads[0][2]["selection_brief"]["best_codes"] == ["300750"]


def test_tool_registry_wait_background_tasks_restores_handoff_state():
    from cli.background import BackgroundTaskManager
    from cli.tools import ToolRegistry

    manager = BackgroundTaskManager()
    registry = ToolRegistry()
    registry.set_background_manager(manager)
    task_id = manager.submit("bg_screen", "screen_stocks", lambda: _screen_result(), {})

    statuses = registry.wait_background_tasks([task_id], timeout_seconds=2)

    assert statuses[0]["status"] == "completed"
    assert statuses[0]["result_summary"]
    assert registry.state["last_screen_result"]["selection_brief"]["best_codes"] == ["300750"]


def test_check_background_tasks_restores_screen_handoff_state():
    from cli.background import BackgroundTaskManager
    from cli.tools import ToolRegistry

    manager = BackgroundTaskManager()
    registry = ToolRegistry()
    registry.set_background_manager(manager)
    task_id = manager.submit("bg_screen", "screen_stocks", lambda: _screen_result(), {})

    _wait_completed(manager, task_id)
    result = registry.execute("check_background_tasks", {})

    assert result["tasks"][0]["status"] == "completed"
    assert "result" not in result["tasks"][0]
    state = registry.state["last_screen_result"]
    assert state["selection_brief"]["best_codes"] == ["300750"]
    assert state["symbols_for_report"][0]["code"] == "300750"
    assert "trigger_groups" not in state


def test_check_background_tasks_restores_web_funnel_quality_gate():
    from cli.background import BackgroundTaskManager
    from cli.tools import ToolRegistry

    manager = BackgroundTaskManager()
    registry = ToolRegistry()
    registry.set_background_manager(manager)
    task_id = manager.submit("bg_web_screen", "web_background_job", lambda: _funnel_screen_blocked_result(), {})

    _wait_completed(manager, task_id)
    registry.execute("check_background_tasks", {})

    state = registry.state["last_screen_result"]
    assert state["job_kind"] == "funnel_screen"
    assert state["symbols_for_report"] == []
    assert state["watch_candidates"][0]["code"] == "000013"
    assert state["quality_gate"]["status"] == "blocked_by_quality_gate"


def test_check_background_tasks_restores_ai_report_handoff_state():
    from cli.background import BackgroundTaskManager
    from cli.tools import ToolRegistry

    report = {
        "ok": True,
        "reviewed_codes": ["300750"],
        "reviewed_symbols": [{"code": "300750", "name": "宁德时代"}],
    }
    manager = BackgroundTaskManager()
    registry = ToolRegistry()
    registry.set_background_manager(manager)
    task_id = manager.submit("bg_report", "generate_ai_report", lambda: report, {})

    _wait_completed(manager, task_id)
    registry.execute("check_background_tasks", {})

    assert registry.state["last_ai_report"]["reviewed_codes"] == ["300750"]


def test_check_background_tasks_restores_strategy_decision_handoff_state():
    from cli.background import BackgroundTaskManager
    from cli.tools import ToolRegistry

    decision = {
        "ok": True,
        "report_source": "last_ai_report",
        "reviewed_codes": ["300750"],
        "reviewed_symbols": [{"code": "300750", "name": "宁德时代"}],
        "next_action": "补充 Telegram 配置后可生成并发送 OMS 工单",
    }
    manager = BackgroundTaskManager()
    registry = ToolRegistry()
    registry.set_background_manager(manager)
    task_id = manager.submit("bg_strategy", "generate_strategy_decision", lambda: decision, {})

    _wait_completed(manager, task_id)
    result = registry.execute("check_background_tasks", {})

    assert result["tasks"][0]["status"] == "completed"
    assert "result" not in result["tasks"][0]
    assert registry.state["last_strategy_decision"]["reviewed_codes"] == ["300750"]
    assert registry.state["last_strategy_decision"]["report_source"] == "last_ai_report"


def test_check_background_tasks_restores_recommendation_eval_handoff_state():
    from cli.background import BackgroundTaskManager
    from cli.tools import ToolRegistry

    manager = BackgroundTaskManager()
    registry = ToolRegistry()
    registry.set_background_manager(manager)
    task_id = manager.submit(
        "bg_eval", "evaluate_recommendation_events", lambda: _recommendation_event_eval_result(), {}
    )

    _wait_completed(manager, task_id)
    result = registry.execute("check_background_tasks", {})

    assert result["tasks"][0]["status"] == "completed"
    assert registry.state["last_recommendation_event_eval"]["policy_selection"]["picks"][0]["code"] == "300750"
    assert registry.state["last_recommendation_event_eval"]["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
    handoff = registry.state["last_screen_result"]
    assert handoff["scan_scope"]["source"] == "recommendation_event_eval"
    assert handoff["symbols_for_report"][0]["code"] == "300750"
    assert handoff["symbols_for_report"][0]["candidate_shadow_grade"] == "S"
    assert handoff["candidate_guard_summary"]["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"


def test_tool_registry_remembers_recommendation_eval_handoff_without_status_poll():
    from cli.tools import ToolRegistry

    registry = ToolRegistry()

    registry.remember_tool_handoff("evaluate_recommendation_events", _recommendation_event_eval_result())

    assert registry.state["last_recommendation_event_eval"]["policy_selection"]["picks"][0]["code"] == "300750"
    assert registry.state["last_recommendation_event_eval"]["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
    handoff = registry.state["last_screen_result"]
    assert handoff["selection_brief"]["best_codes"] == ["300750"]
    assert handoff["symbols_for_report"][0]["selection_source"] == "recommendation_event_eval"
    assert handoff["candidate_guard_summary"]["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"


def test_tool_registry_remembers_stock_diagnosis_handoff_without_status_poll():
    from cli.tools import ToolRegistry

    registry = ToolRegistry()

    registry.remember_tool_handoff("analyze_stock", _diagnosis_result())

    handoff = registry.state["last_stock_diagnosis"]
    assert handoff["latest"]["code"] == "002326"
    assert handoff["latest"]["action_status"] == "priority_watch"
    assert handoff["latest"]["new_buy_allowed"] is False
    assert handoff["diagnosed_symbols"][0]["risk_factors"] == ["短线涨幅偏快"]


def test_local_db_chat_background_history_uses_shared_preview(tmp_path, monkeypatch):
    from integrations import local_db

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "background-chat.db")
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    try:
        local_db.init_db()

        local_db.save_background_task_result(
            "bg_screen_chat",
            "screen_stocks",
            _screen_result(),
            session_id="s1",
        )
        row = local_db.load_background_task_result("bg_screen_chat")

        assert row is not None
        assert "本轮首选可进入 AI 研报复核: 300750 宁德时代" in row["summary"]
        assert "完整 trigger_groups 已保留在完整结果中" in row["summary"]
        assert '"trigger_groups"' not in row["summary"]
    finally:
        _reset_local_db(local_db)
