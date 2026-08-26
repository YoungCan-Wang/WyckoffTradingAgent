"""Top-level daily job orchestration."""

from __future__ import annotations

from typing import Any

from workflows.daily_job_common import log_line
from workflows.daily_job_lifecycle import load_daily_job_steps, log_daily_summary, log_job_start
from workflows.daily_job_runtime import daily_job_preflight_exit_code as _daily_job_preflight_exit_code
from workflows.daily_job_runtime import resolve_daily_job_config
from workflows.daily_job_step2 import run_step2_block
from workflows.daily_job_step3 import persist_step3_signal_observations, run_step3_block
from workflows.daily_job_step4 import run_step4_stage


def run_daily_job(args: Any) -> int:
    cfg = resolve_daily_job_config(args)
    preflight_exit = _daily_job_preflight_exit_code(args, cfg, log_line)
    if preflight_exit is not None:
        return preflight_exit

    run_step2, run_step3 = load_daily_job_steps()
    summary: list[dict] = []
    log_job_start(cfg)

    step2, has_blocking_failure, recommend_date, recommendation_payload = run_step2_block(run_step2, cfg, summary)
    step3 = run_step3_block(
        run_step3=run_step3,
        cfg=cfg,
        step2=step2,
        recommend_trade_date_int=recommend_date,
        recommendation_payload=recommendation_payload,
        summary=summary,
    )
    has_blocking_failure = has_blocking_failure or step3.blocking_failure
    if not persist_step3_signal_observations(step2, step3, cfg):
        has_blocking_failure = True
    funnel_ok = not has_blocking_failure

    step4_summary = run_step4_stage(
        cfg=cfg,
        symbols_info=step2.symbols_info,
        step3_springboard_codes=step3.springboard_codes,
        step3_report_text=step3.report_text,
        benchmark_context=step2.benchmark_context,
    )
    summary.append(step4_summary)
    has_blocking_failure = has_blocking_failure or not bool(step4_summary.get("ok"))
    if funnel_ok:
        from workflows.shadow_ledger_job import run_shadow_ledger_stage

        summary.append(
            run_shadow_ledger_stage(
                cfg=cfg,
                step2_details=step2.details or {},
                symbols_info=step2.symbols_info,
                step3_report_text=step3.report_text,
                benchmark_context=step2.benchmark_context or {},
            )
        )
    log_daily_summary(summary, cfg.logs_path)
    return 1 if has_blocking_failure else 0
