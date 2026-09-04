from __future__ import annotations

from pathlib import Path

from scripts.check_pr_policy import _dependency_only_change, _is_dependabot_event, validate_policy
from scripts.check_workflow_hygiene import _check_workflow


def test_pr_policy_accepts_bilingual_summary_and_validation():
    body = "## 变更摘要\n\n- 拆分 CI\n\n## 验证\n\n- pytest"

    result = validate_policy(body, ["scripts/check_pr_policy.py"])

    assert result.ok is True


def test_pr_policy_accepts_slash_separated_bilingual_headings():
    """``## Summary / 变更摘要`` 曾被当成单个标题名而误判缺少章节（PR #266 因此 CI 红）。"""
    body = "## Summary / 变更摘要\n\n- fix\n\n## Validation / 验证\n\n- pytest"

    assert validate_policy(body, ["agents/portfolio_tools.py"]).ok is True


def test_pr_policy_accepts_pipe_separated_bilingual_headings():
    body = "## Summary | 变更摘要\n\n- fix\n\n## Validation ｜ 验证\n\n- pytest"

    assert validate_policy(body, ["agents/portfolio_tools.py"]).ok is True


def test_pr_policy_still_rejects_missing_sections_after_split():
    """拆分不能放宽判定：真的缺章节仍须 FAIL。"""
    result = validate_policy("## Notes / 备注\n\n- nothing", ["agents/portfolio_tools.py"])

    assert result.ok is False
    assert any("Summary" in item for item in result.failures)
    assert any("Validation" in item for item in result.failures)


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
    event = {"pull_request": {"user": {"login": "dependabot[bot]"}}, "sender": {"login": "YoungCan-Wang"}}

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
      - uses: actions/checkout@v7
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
permissions:
  contents: read
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Prepare logs dir
        run: mkdir -p logs
      - uses: actions/upload-artifact@v7
        with:
          name: logs
          path: logs/*
""".lstrip(),
        encoding="utf-8",
    )

    assert _check_workflow(workflow) == []


def test_signal_feedback_manual_dynamic_approval_is_explicit():
    workflow = Path(".github/workflows/signal_feedback.yml").read_text(encoding="utf-8")

    assert "formal_dynamic_approved:" in workflow
    assert "type: boolean" in workflow
    assert "formal_dynamic_approval_reason:" in workflow
    assert "formal_dynamic_approval_reason is required" in workflow
    assert '"approved_by": os.environ.get("GITHUB_ACTOR", "")' in workflow
    assert "--formal-dynamic-approval-json formal_dynamic_approval.json" in workflow


def test_ci_runs_python_suite_once_and_reuses_it_for_coverage():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert workflow.count("python -m coverage run -m pytest tests/ -x -q") == 1
    assert "\n  coverage-report:" not in workflow
    assert "name: coverage-report-${{ github.run_number }}" in workflow


def test_weekly_reflection_gate_reads_trigger_cron_not_wall_clock():
    """周度反思的「今天是不是周五」只能看哪条 cron 触发，不能在步骤里读 date -u。

    原先这里是 ``[ "$(date -u +%u)" = "5" ]``，把周几绑在了 GitHub 的排队延迟上。实测
    同一周里两头都错了：周四那班 cron 被推到 2026-08-28T00:29Z（UTC 已跨到周五），
    ``date -u +%u`` 读成 5，于是多跑了一次周度反思；周五那班被推到 2026-08-29T00:02Z
    （UTC 已跨到周六）读成 6，run 33222336351 的 ``Refresh weekly strategy reflection``
    被标成 skipped，整周静默丢了一轮。近期 run 起始时间已经从 15:48Z 漂到 18:48~20:56Z，
    延迟是常态而不是偶发。

    ``github.event.schedule`` 记的是排队那一刻的 cron 表达式，延迟多久都不变，所以周五
    单独拆一条 cron、按表达式判断才是与调度意图同源的写法。
    """
    workflow = Path(".github/workflows/signal_feedback.yml").read_text(encoding="utf-8")

    assert "$(date -u +%u)" not in workflow
    assert 'cron: "30 15 * * 1-4"' in workflow
    assert 'cron: "30 15 * * 5"' in workflow
    assert "TRIGGER_CRON: ${{ github.event.schedule }}" in workflow
    assert '[ "${TRIGGER_CRON}" = "30 15 * * 5" ]' in workflow


def test_review_shadow_backtest_collects_traces_from_failed_funnel_runs_too():
    """影子回测按样本天数攒 trace，不能按 conclusion 过滤掉失败的漏斗运行。

    漏斗失败的 run 因为 ``if: always()`` 照样上传 artifact，里面的 trace 往往是完整的
    （实测 2026-08-10 那次 failure 有 5331 只票、data_quality.status=normal），因为失败
    多发生在下单/通知这些 trace 落盘之后。而这个任务的瓶颈恰恰是样本天数：artifact
    retention 30 天 ≈ 22 个交易日，压在 MIN_DAYS=20 线上，少两天就可能跨不过判定门槛。

    review_list_replay.yml 用 ``--status success`` 是对的——它要的是某个特定交易日那份
    trace，宁缺勿错。两个任务口径不同，别把那份写法照抄过来。
    """
    workflow = Path(".github/workflows/review_shadow_backtest.yml").read_text(encoding="utf-8")

    assert "--status success" not in workflow
    assert "select(.conclusion" not in workflow
    assert "/actions/workflows/wyckoff_funnel.yml/runs?branch=" in workflow


def test_review_shadow_backtest_reads_min_days_from_core():
    """样本门槛只能有一处来源。

    workflow 里写死 20 就会与 core.funnel_effect_eval.MIN_DAYS 漂移（见 memory
    two-gates-must-share-one-source）：改了 core 而 workflow 照旧，日志会说「够了」
    而报告说「不下判定」。
    """
    workflow = Path(".github/workflows/review_shadow_backtest.yml").read_text(encoding="utf-8")

    assert "from core.funnel_effect_eval import MIN_DAYS" in workflow
    # 「不下判定」是样本不足，不是对照未通过——这两句结论完全不同，必须写在日志里。
    assert "不是对照未通过" in workflow


def test_worker_deploy_invokes_the_package_script_explicitly():
    workflow = Path(".github/workflows/worker_deploy.yml").read_text(encoding="utf-8")

    assert "pnpm --filter @wyckoff/api run deploy --message" in workflow
    assert "pnpm --filter @wyckoff/api deploy --" not in workflow
    assert "wrangler deployments status --json" in workflow
    assert "select(.percentage == 100).version_id" in workflow
    assert '.annotations["workers/message"] == $message' in workflow
    assert 'select(.tag? == "v2")' not in workflow
