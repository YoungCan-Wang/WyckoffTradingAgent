from __future__ import annotations

import json

from cli.workflows.saved import list_saved_workflows, load_saved_workflow, save_workflow_script
from cli.workflows.store import load_workflow_script_payload


def test_save_and_load_workflow_script(monkeypatch, tmp_path):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    run = {
        "run_id": "wf_1",
        "workflow": "stock_diagnosis",
        "label": "个股诊断",
        "plan": {
            "allowed_tools": ["analyze_stock"],
            "route": {"reason": "test"},
            "script": {
                "title": "诊断脚本",
                "phases": [
                    {
                        "id": "diagnose",
                        "tasks": [{"id": "check", "title": "诊断", "agent": "analysis", "prompt": "诊断 {args}"}],
                    }
                ],
            },
        },
    }

    path = save_workflow_script("Daily Diagnose", run)
    loaded = load_saved_workflow("daily-diagnose")

    assert path == tmp_path / "workflows" / "daily-diagnose.json"
    assert loaded
    assert loaded["source_run_id"] == "wf_1"
    assert loaded["script"]["title"] == "诊断脚本"
    names = [row["name"] for row in list_saved_workflows()]
    assert "daily-diagnose" in names
    assert "deep-research" in names


def test_save_workflow_script_strips_run_specific_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    run = {
        "run_id": "wf_restart",
        "workflow": "dynamic_task",
        "label": "重跑任务",
        "plan": {
            "script": {
                "title": "可复用脚本",
                "runtime": {
                    "planner": "stored_script",
                    "adaptive": True,
                    "script_path": "/tmp/wf_restart.json",
                    "rerun_of": "wf_old",
                    "only_step_id": "scan",
                    "only_step_missing": "missing",
                    "args": "旧参数",
                },
                "tasks": [{"id": "scan", "title": "扫描候选", "tools": ["screen_stocks"], "prompt": "扫描 {args}"}],
            }
        },
    }

    save_workflow_script("Reusable", run)
    loaded = load_saved_workflow("reusable")

    assert loaded
    assert loaded["script"]["runtime"] == {"adaptive": True}


def test_load_workflow_script_payload_accepts_persisted_plan(tmp_path):
    path = tmp_path / "wf.json"
    path.write_text(
        json.dumps({"script": {"title": "edited", "phases": []}}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_workflow_script_payload(str(path)) == {"title": "edited", "phases": []}


def test_builtin_deep_research_workflow(monkeypatch, tmp_path):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))

    loaded = load_saved_workflow("deep-research")

    assert loaded
    assert loaded["workflow"] == "dynamic_task"
    assert loaded["script"]["phases"][0]["tasks"][0]["agent"] == "research"
