"""Lifecycle helpers for the daily job orchestrator."""

from __future__ import annotations

from functools import partial

from workflows.daily_job_common import log_line
from workflows.daily_job_runtime import DailyJobConfig


def load_daily_job_steps() -> tuple[object, object]:
    from workflows.step3_batch_report import run as run_step3
    from workflows.wyckoff_funnel import run as run_step2

    return partial(run_step2, include_financial_metrics=False), run_step3


def log_job_start(cfg: DailyJobConfig) -> None:
    log_line("开始定时任务", cfg.logs_path)
    if cfg.preview_only:
        log_line("预演模式: 仅生成 Step3 LLM input，跳过 Step2 通知和所有写库动作", cfg.logs_path)
    elif cfg.historical_replay:
        log_line(
            "历史回放: END_CALENDAR_DAY 已设置，跳过 Step2/3 共享表写库与通知，并隔离 Step4/影子账本",
            cfg.logs_path,
        )


def log_daily_summary(summary: list[dict], logs_path: str | None) -> None:
    total_elapsed = sum(item.get("elapsed_s", 0) for item in summary)
    log_line("", logs_path)
    log_line("=== 阶段汇总 ===", logs_path)
    for item in summary:
        status = "✅" if item["ok"] else "❌"
        line = f"  {status} {item['step']}: {item.get('elapsed_s', 0)}s, {item.get('output', '')}"
        log_line(line + (f" | {item['err']}" if item.get("err") else ""), logs_path)
    log_line(f"总耗时: {total_elapsed:.1f}s", logs_path)
    log_line("定时任务结束", logs_path)
