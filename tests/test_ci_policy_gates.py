from __future__ import annotations

from pathlib import Path

from scripts.check_pr_policy import _dependency_only_change, _is_dependabot_event, validate_policy
from scripts.check_workflow_hygiene import _check_workflow


def test_pr_policy_accepts_bilingual_summary_and_validation():
    body = "## 变更摘要\n\n- 拆分 CI\n\n## 验证\n\n- pytest"

    result = validate_policy(body, ["scripts/check_pr_policy.py"])

    assert result.ok is True


def test_pr_policy_blocks_logs_and_secret_like_body():
    body = "## Summary\n\nUses Bearer eyJabc.def.ghi\n\n## Validation\n\n- pytest"

    result = validate_policy(body, ["logs/run.log"])

    assert result.ok is False
    assert any("secret" in item for item in result.failures)
    assert any("local logs" in item for item in result.failures)


def test_pr_policy_allows_dependabot_dependency_body_without_manual_headings():
    body = "Bumps vite from 6.4.2 to 6.4.3.\n\n---\nupdated-dependencies:\n- dependency-name: vite"

    result = validate_policy(body, ["web/package.json", "web/pnpm-lock.yaml"], automated_dependency_pr=True)

    assert result.ok is True


def test_pr_policy_still_blocks_dependabot_secret_body():
    body = "Bumps vite.\n\nBearer eyJabc.def.ghi"

    result = validate_policy(body, ["web/package.json"], automated_dependency_pr=True)

    assert result.ok is False
    assert any("secret" in item for item in result.failures)


def test_dependabot_relaxation_requires_dependency_files():
    event = {"sender": {"login": "dependabot[bot]"}}

    assert _is_dependabot_event(event) is True
    assert _dependency_only_change(["web/package.json", "web/pnpm-lock.yaml"]) is True
    assert _dependency_only_change(["scripts/check_pr_policy.py"]) is False


def test_workflow_hygiene_requires_concurrency_for_manual_automation(tmp_path: Path):
    workflow = tmp_path / "manual.yml"
    workflow.write_text(
        """
name: Manual
on:
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
""".lstrip(),
        encoding="utf-8",
    )

    failures = _check_workflow(workflow)

    assert any("concurrency" in failure for failure in failures)


def test_workflow_hygiene_accepts_logs_with_artifact(tmp_path: Path):
    workflow = tmp_path / "manual.yml"
    workflow.write_text(
        """
name: Manual
on:
  workflow_dispatch:
concurrency:
  group: manual-${{ github.ref }}
  cancel-in-progress: true
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Prepare logs dir
        run: mkdir -p logs
      - uses: actions/upload-artifact@v4
        with:
          name: logs
          path: logs/*
""".lstrip(),
        encoding="utf-8",
    )

    assert _check_workflow(workflow) == []
