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

    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return failures + [f"{path}: missing jobs"]

    for job_name, raw_job in jobs.items():
        if not isinstance(raw_job, dict):
            failures.append(f"{path}: job {job_name} must be a mapping")
            continue
        if is_automation and not is_ci and _prepares_logs(raw_job) and not _has_upload_artifact(raw_job):
            failures.append(f"{path}: job {job_name} prepares logs but does not upload artifacts")
    return failures


def main() -> int:
    failures: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        failures.extend(_check_workflow(path))

    if failures:
        print("Workflow hygiene: FAIL")
        for failure in failures:
            print(f"  FAIL {failure}")
        return 2

    print("Workflow hygiene: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
