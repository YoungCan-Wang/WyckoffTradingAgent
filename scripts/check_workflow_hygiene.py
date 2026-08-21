#!/usr/bin/env python3
"""Check GitHub workflow hygiene rules that keep scheduled jobs maintainable."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
AUTOMATION_TRIGGER_NAMES = {"schedule", "workflow_dispatch"}


def _workflow_on(data: dict[str, Any]) -> Any:
    return data.get("on", data.get(True))


def _trigger_names(raw_on: Any) -> set[str]:
    if isinstance(raw_on, str):
        return {raw_on}
    if isinstance(raw_on, list):
        return {str(item) for item in raw_on}
    if isinstance(raw_on, dict):
        return {str(key) for key in raw_on}
    return set()


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    return [step for step in steps if isinstance(step, dict)]


def _has_upload_artifact(job: dict[str, Any]) -> bool:
    return any(str(step.get("uses", "")).startswith("actions/upload-artifact@") for step in _steps(job))


def _prepares_logs(job: dict[str, Any]) -> bool:
    for step in _steps(job):
        name = str(step.get("name", "")).lower()
        run = str(step.get("run", "")).lower()
        if "prepare logs" in name or "mkdir -p logs" in run:
            return True
    return False


def _has_direct_input_interpolation(job: dict[str, Any]) -> bool:
    return any("${{ inputs." in str(step.get("run", "")) for step in _steps(job))


# 官方 action 的最低大版本。
#
# 低于这些的版本跑在已废弃的 Node 20 运行时上 —— GitHub 目前会强制降级到 Node 24
# 并只发一条警告，但那是过渡期行为，随时会变成硬失败。而这个仓库有 22 个
# workflow、上百处 uses，靠人肉 grep 迟早会漏一处、然后在某个只在夜里跑的定时
# 任务里炸掉。
#
# 加新 workflow 时如果 uses 写了更低的版本，这条会红。要升上界就连同破坏性变更
# 一起核对（例如 checkout v7 会拒绝签出 fork PR、setup-node v5 会按
# packageManager 字段自动缓存）。
MIN_ACTION_MAJORS = {
    "actions/checkout": 7,
    "actions/setup-python": 7,
    "actions/setup-node": 7,
    "actions/upload-artifact": 7,
    "actions/download-artifact": 8,
}


def _check_action_versions(path: Path, data: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return failures
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in _steps(job):
            uses = str(step.get("uses", ""))
            if "@" not in uses:
                continue
            action, _, ref = uses.partition("@")
            minimum = MIN_ACTION_MAJORS.get(action)
            if minimum is None:
                continue
            # 只认 vN / vN.M.P 形式；sha 固定的先放过（那是更强的固定方式）。
            if not ref.startswith("v") or not ref[1:2].isdigit():
                continue
            major = int(ref[1:].split(".")[0])
            if major < minimum:
                failures.append(
                    f"{path}: job {job_name} 用了 {uses}，"
                    f"低于要求的 {action}@v{minimum}（Node 20 运行时已废弃）"
                )
    return failures


def _check_workflow(path: Path) -> list[str]:
    failures: list[str] = []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return [f"{path}: workflow must be a mapping"]

    raw_on = _workflow_on(data)
    triggers = _trigger_names(raw_on)
    if not triggers:
        failures.append(f"{path}: missing workflow trigger")

    is_ci = path.name == "ci.yml"
    is_automation = bool(triggers.intersection(AUTOMATION_TRIGGER_NAMES)) and not triggers.intersection(
        {"pull_request", "push"}
    )
    if is_automation and not data.get("concurrency"):
        failures.append(f"{path}: automation workflow must define top-level concurrency")
    permissions = data.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("contents") != "read":
        failures.append(f"{path}: workflow must declare top-level permissions: contents: read")

    failures.extend(_check_action_versions(path, data))

    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return failures + [f"{path}: missing jobs"]

    for job_name, raw_job in jobs.items():
        if not isinstance(raw_job, dict):
            failures.append(f"{path}: job {job_name} must be a mapping")
            continue
        if is_automation and not is_ci and _prepares_logs(raw_job) and not _has_upload_artifact(raw_job):
            failures.append(f"{path}: job {job_name} prepares logs but does not upload artifacts")
        if _has_direct_input_interpolation(raw_job):
            failures.append(f"{path}: job {job_name} must pass workflow inputs through env before shell use")
    return failures


def main() -> int:
    failures: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        failures.extend(_check_workflow(path))

    shared_group = "step4-oms-a-share-${{ github.ref }}"
    for name in ("wyckoff_funnel.yml", "step4_from_supabase.yml"):
        data = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
        if data.get("concurrency", {}).get("group") != shared_group:
            failures.append(f"{name}: Step4/OMS entrypoints must share concurrency group {shared_group}")

    if failures:
        print("Workflow hygiene: FAIL")
        for failure in failures:
            print(f"  FAIL {failure}")
        return 2

    print("Workflow hygiene: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
