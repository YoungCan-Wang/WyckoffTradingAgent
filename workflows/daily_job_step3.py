"""Step3 report execution and recommendation marking for the daily job."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import workflows.daily_job_persistence as daily_persistence
import workflows.daily_signal_observations as signal_observations
from tools.report_parser import extract_step3_verdicts
from workflows.daily_job_common import Step2StageResult, Step3StageResult, log_line, run_with_stdout_tee
from workflows.daily_job_runtime import DailyJobConfig
from workflows.step4_pipeline import TZ, is_confirmed_step4_candidate, latest_trade_date_str

STEP3_REASON_MAP = {
    "data_all_failed": "OHLCV 全部拉取失败",
    "llm_failed": "大模型调用失败",
    "feishu_failed": "飞书推送失败",
    "skipped_no_symbols": "无输入股票，已跳过",
    "no_data_but_no_error": "无可用数据",
    "ok_preview": "预演模式：未调用模型，仅展示输入",
}


def run_step3_block(
    *,
    run_step3,
    cfg: DailyJobConfig,
    step2: Step2StageResult,
    recommend_trade_date_int: int | None,
    recommendation_payload: list[dict],
    summary: list[dict],
) -> Step3StageResult:
    symbols_info = step3_symbols_info(step2)
    step3 = run_step3_stage(
        symbols_info=symbols_info,
        benchmark_context=step2.benchmark_context,
        run_step3=run_step3,
        cfg=cfg,
    )
    summary.append(step3.summary_item)
    mark_step3_outputs(recommend_trade_date_int, recommendation_payload, step3, cfg)
    return step3


def step3_symbols_info(step2: Step2StageResult) -> list[dict]:
    if step2.details and "step3_symbols_info" in step2.details:
        return list(step2.details.get("step3_symbols_info") or [])
    return step2.symbols_info


def run_step3_stage(
    *,
    symbols_info: list[dict],
    benchmark_context: dict,
    run_step3,
    cfg: DailyJobConfig,
) -> Step3StageResult:
    t0 = datetime.now(TZ)
    step3_ok, step3_err, report_text = _call_step3_report(run_step3, symbols_info, benchmark_context, cfg)
    springboard_codes, springboard_updates = ([], {})
    verdicts: dict[str, str] = {}
    if step3_ok and report_text:
        springboard_codes, springboard_updates = parse_step3_springboards(report_text, symbols_info, cfg.logs_path)
        verdicts = extract_step3_verdicts(
            report_text,
            [str(item.get("code", "")).strip() for item in symbols_info or []],
        )
    elapsed = (datetime.now(TZ) - t0).total_seconds()
    summary_item = {
        "step": "批量研报",
        "ok": step3_ok and step3_err is None,
        "err": step3_err,
        "elapsed_s": round(elapsed, 1),
        "output": f"{len(symbols_info)} symbols",
    }
    log_line(f"Step3 批量研报: ok={step3_ok}, elapsed={elapsed:.1f}s, err={step3_err}", cfg.logs_path)
    preview_codes = ", ".join(springboard_codes[:8]) if springboard_codes else "无"
    log_line(f"Step3 批量研报: 起跳板代码={len(springboard_codes)} ({preview_codes})", cfg.logs_path)
    log_line(f"Step3 LLM 判定: {dict(Counter(verdicts.values())) or '无'}", cfg.logs_path)
    return Step3StageResult(report_text, springboard_codes, springboard_updates, summary_item, verdicts)


def mark_step3_outputs(
    recommend_trade_date_int: int | None,
    recommendation_payload: list[dict],
    step3: Step3StageResult,
    cfg: DailyJobConfig,
) -> None:
    daily_persistence.mark_step3_recommendations(
        recommend_trade_date_int,
        step3.springboard_codes,
        step3.springboard_updates,
        cfg.logs_path,
        dry_run=cfg.preview_only,
        log_fn=log_line,
        step3_verdicts=step3.verdicts,
    )
    if recommend_trade_date_int and recommendation_payload:
        signal_observations.apply_step3_springboard_updates(recommendation_payload, step3.springboard_updates)
        daily_persistence.write_recommendation_backup(
            recommend_trade_date_int,
            recommendation_payload,
            cfg.logs_path,
            ai_codes=step3.springboard_codes,
            log_fn=log_line,
        )


def persist_ic_shadow_pool(step2: Step2StageResult, cfg: DailyJobConfig) -> None:
    """写入 IC 反向打分影子池。只观察不下单，失败不阻断主流程。

    影子行在漏斗内计算（复用其 all_df_map，见 wyckoff_funnel._build_ic_shadow_pool），
    在此统一落库——漏斗本身保持只读，写库集中在 daily_job 这一层。
    """
    rows = ((step2.details or {}).get("metrics") or {}).get("ic_shadow") or []
    if not rows:
        return
    if cfg.preview_only:
        log_line(f"  影子池 dry-run：{len(rows)} 行未写入")
        return
    try:
        from integrations.supabase_signal_feedback import upsert_signal_observations

        written = upsert_signal_observations(rows)
        log_line(f"  影子池已写入 {written}/{len(rows)} 行（source=ic_shadow）")
    except Exception as exc:  # noqa: BLE001 - 研究支线，不得阻断
        log_line(f"  影子池写入失败（不影响主流程）: {str(exc)[:120]}")


def persist_step3_signal_observations(
    step2: Step2StageResult,
    step3: Step3StageResult,
    cfg: DailyJobConfig,
) -> bool:
    persist_ic_shadow_pool(step2, cfg)
    if step2.ok and step2.details:
        return signal_observations.persist_signal_observations(
            step2.details,
            step2.benchmark_context,
            step3.springboard_codes,
            cfg.logs_path,
            trade_date=latest_trade_date_str(),
            dry_run=cfg.preview_only,
            log_fn=log_line,
        )
    return True


def filter_confirmed_step3_codes(codes: list[str], symbols_info: list[dict]) -> tuple[list[str], list[str]]:
    allowed = {
        str(item.get("code", "")).strip()
        for item in symbols_info
        if isinstance(item, dict) and is_confirmed_step4_candidate(item)
    }
    kept = [code for code in codes if str(code).strip() in allowed]
    blocked = [code for code in codes if str(code).strip() not in allowed]
    return kept, blocked


def parse_step3_springboards(
    report_text: str, symbols_info: list[dict], logs_path: str | None
) -> tuple[list[str], dict]:
    from tools.report_parser import extract_operation_pool_codes, extract_operation_pool_springboards

    allowed_codes = [str(item.get("code", "")).strip() for item in symbols_info if isinstance(item, dict)]
    try:
        codes = extract_operation_pool_codes(report=report_text, allowed_codes=allowed_codes)
        updates = extract_operation_pool_springboards(report=report_text, allowed_codes=allowed_codes)
        codes, blocked_unconfirmed = filter_confirmed_step3_codes(codes, symbols_info)
        confirmed_set = set(codes)
        updates = {code: fields for code, fields in updates.items() if code in confirmed_set}
        if blocked_unconfirmed:
            log_line(
                "Step3 批量研报: 未二次确认起跳板已拦截 "
                f"{len(blocked_unconfirmed)}只 ({', '.join(blocked_unconfirmed[:8])})",
                logs_path,
            )
        return codes, updates
    except Exception as e:
        log_line(f"Step3 批量研报: 起跳板解析失败，已降级为空。err={e}", logs_path)
        return [], {}


def _call_step3_report(run_step3, symbols_info: list[dict], benchmark_context: dict, cfg: DailyJobConfig):
    try:
        step3_ok, step3_reason, report_text = run_with_stdout_tee(
            cfg.logs_path,
            run_step3,
            symbols_info,
            cfg.webhook,
            cfg.api_key,
            cfg.model,
            benchmark_context=benchmark_context,
            provider=cfg.provider,
            llm_base_url=cfg.llm_base_url,
            wecom_webhook=cfg.wecom_webhook,
            dingtalk_webhook=cfg.dingtalk_webhook,
        )
        return step3_ok, None if step3_ok else STEP3_REASON_MAP.get(step3_reason, step3_reason), report_text
    except Exception as e:
        return False, str(e), ""
