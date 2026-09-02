"""Runtime config and preflight checks for the daily job."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from integrations._llm_types import OPENAI_COMPATIBLE_BASE_URLS
from integrations.llm_client import get_provider_credentials, provider_fallbacks, resolve_provider_name
from utils.trading_clock import CN_TZ, is_a_share_trading_day, resolve_end_calendar_day

TRUE_TEXTS = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DailyJobConfig:
    webhook: str
    wecom_webhook: str
    dingtalk_webhook: str
    provider: str
    api_key: str
    model: str
    llm_base_url: str
    base_url_env_key: str
    step4_provider: str
    step4_api_key: str
    step4_model: str
    step4_base_url: str
    step3_skip_llm: bool
    skip_step4: bool
    historical_replay: bool
    preview_only: bool
    logs_path: str


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_TEXTS


def default_daily_job_logs_path() -> str:
    return os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"daily_job_{datetime.now(CN_TZ).strftime('%Y%m%d_%H%M%S')}.log",
    )


def resolve_daily_job_config(args: argparse.Namespace) -> DailyJobConfig:
    provider = resolve_provider_name("STEP3_LLM_PROVIDER", "efficiency")
    api_key, model, llm_base_url = get_provider_credentials(provider)
    step4_provider = resolve_provider_name("STEP4_LLM_PROVIDER", "efficiency")
    step4_api_key, step4_model, step4_base_url = get_provider_credentials(step4_provider)
    preview_only = env_flag("DAILY_JOB_PREVIEW_ONLY")
    historical_replay = bool(os.getenv("END_CALENDAR_DAY", "").strip())
    if preview_only:
        os.environ["STEP3_SKIP_LLM"] = "1"
        os.environ["DAILY_JOB_SKIP_STEP4"] = "1"
    return DailyJobConfig(
        webhook=os.getenv("FEISHU_WEBHOOK_URL", "").strip(),
        wecom_webhook=os.getenv("WECOM_WEBHOOK_URL", "").strip(),
        dingtalk_webhook=os.getenv("DINGTALK_WEBHOOK_URL", "").strip(),
        provider=provider,
        api_key=api_key,
        model=model,
        llm_base_url=llm_base_url,
        base_url_env_key=f"{provider.upper()}_BASE_URL",
        step4_provider=step4_provider,
        step4_api_key=step4_api_key,
        step4_model=step4_model,
        step4_base_url=step4_base_url,
        step3_skip_llm=env_flag("STEP3_SKIP_LLM") or preview_only,
        skip_step4=env_flag("DAILY_JOB_SKIP_STEP4") or preview_only or historical_replay,
        historical_replay=historical_replay,
        preview_only=preview_only,
        logs_path=args.logs or default_daily_job_logs_path(),
    )


def non_trading_skip_message(today: date) -> str | None:
    next_day = today + timedelta(days=1)
    if is_a_share_trading_day(next_day):
        return None
    return f"📅 明日 {next_day} 非 A 股交易日，任务跳过"


def notify_skip(msg: str, feishu: str = "", wecom: str = "", dingtalk: str = "") -> None:
    if feishu:
        with suppress(Exception):
            from utils.feishu import send_feishu_notification

            send_feishu_notification(feishu, "定时任务跳过", msg)
    if wecom:
        with suppress(Exception):
            from utils.markdown_webhooks import send_wecom_notification

            send_wecom_notification(wecom, "定时任务跳过", msg)
    if dingtalk:
        with suppress(Exception):
            from utils.markdown_webhooks import send_dingtalk_notification

            send_dingtalk_notification(dingtalk, "定时任务跳过", msg)


def missing_llm_config(provider: str, step3_skip_llm: bool, skip_step4: bool, step4_provider: str) -> list[str]:
    missing = []
    step3_fallbacks = provider_fallbacks("STEP3_LLM_FALLBACK_PROVIDERS", _step3_fallback_default(provider))
    if not step3_skip_llm and not _provider_ready(provider) and not any(_provider_ready(p) for p in step3_fallbacks):
        missing.append(f"STEP3_LLM_PROVIDER={provider} 缺少可用 API Key / Model / Base URL")
    if not skip_step4 and not _provider_ready(step4_provider):
        missing.append(f"STEP4_LLM_PROVIDER={step4_provider} 缺少可用 API Key / Model / Base URL")
    return list(dict.fromkeys(missing))


def log_llm_config(provider: str, llm_base_url: str, base_url_env_key: str, logs_path: str | None, log_fn) -> None:
    if provider in OPENAI_COMPATIBLE_BASE_URLS:
        log_fn(f"LLM base_url: {llm_base_url or '(empty)'} (env={base_url_env_key})", logs_path)
    fallback = _step3_fallback_default(provider)
    if _provider_ready(fallback):
        _, model, _ = get_provider_credentials(fallback)
        label = {"gemini": "Gemini", "efficiency": "Efficiency"}.get(fallback, fallback)
        log_fn(f"Step3 {label} 兜底已配置: model={model}", logs_path)


def daily_job_preflight_exit_code(args: argparse.Namespace, cfg: DailyJobConfig, log_fn: Callable) -> int | None:
    missing = missing_llm_config(cfg.provider, cfg.step3_skip_llm, cfg.skip_step4, cfg.step4_provider)
    if missing:
        log_fn(f"配置缺失: {', '.join(missing)}", cfg.logs_path)
        return 1
    if not cfg.webhook and not cfg.wecom_webhook and not cfg.dingtalk_webhook:
        log_fn("未配置任何 IM 渠道（飞书/企微/钉钉），筛选与研报仍会执行，推送将被跳过", cfg.logs_path)
    if args.dry_run:
        log_fn("--dry-run: 配置校验通过，退出", cfg.logs_path)
        return 0

    skip_msg = non_trading_skip_message(resolve_end_calendar_day())
    if skip_msg:
        log_fn(skip_msg, cfg.logs_path)
        notify_skip(skip_msg, cfg.webhook, cfg.wecom_webhook, cfg.dingtalk_webhook)
        return 0

    log_llm_config(cfg.provider, cfg.llm_base_url, cfg.base_url_env_key, cfg.logs_path, log_fn)
    log_fn(f"Step4 LLM: provider={cfg.step4_provider}, model={cfg.step4_model or '(missing)'}", cfg.logs_path)
    return None


def _provider_ready(provider: str) -> bool:
    api_key, model, base_url = get_provider_credentials(provider)
    if provider in OPENAI_COMPATIBLE_BASE_URLS and not base_url:
        return False
    return bool(api_key and model)


def _step3_fallback_default(provider: str) -> str:
    return "efficiency" if provider == "gemini" else "gemini"
