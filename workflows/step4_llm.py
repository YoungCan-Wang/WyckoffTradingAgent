"""Step4 LLM decision service."""

from __future__ import annotations

import logging
from collections.abc import Callable

from core.prompts import PRIVATE_PM_DECISION_JSON_PROMPT
from integrations.llm_client import call_llm, get_provider_credentials
from tools.debug_io import dump_model_input
from workflows.step4_decision_parser import parse_decisions
from workflows.step4_models import Step4DecisionResult, Step4InputContext, Step4RunOptions

logger = logging.getLogger(__name__)


#: Step4 决策模型的备用 provider。主 provider 返回空内容或抛错时依次尝试。
#:
#: 2026-08-10 补齐：此前 Step4 只调一个 provider，一次异常就 return llm_failed，
#: 整条 OMS 链路随之 exit 1——8/9 与 8/10 连续两天因「OpenAI 兼容接口返回内容为空」
#: 完全停摆，期间连持仓与止损状态都看不到。Step3 早有 llm_fallback_providers 机制，
#: Step4 反而没有，而 Step4 的输出是直接驱动下单的那一环。
_STEP4_FALLBACK_PROVIDERS = ("gemini", "efficiency", "1route", "deepseek")


def _call_with_fallback(options: Step4RunOptions, context: Step4InputContext) -> str | None:
    """按主 provider → 备用 provider 顺序调用，全部失败返回 None。"""
    attempts: list[tuple[str, str, str, str | None]] = [
        (options.provider, options.model, options.api_key, options.llm_base_url or None)
    ]
    seen = {str(options.provider).strip().lower()}
    for name in _STEP4_FALLBACK_PROVIDERS:
        if name in seen:
            continue
        try:
            key, model, base_url = get_provider_credentials(name)
        except Exception:  # noqa: BLE001 - 缺该 provider 配置时跳过即可
            continue
        if not key:
            continue
        seen.add(name)
        attempts.append((name, model, key, base_url))

    for idx, (provider, model, api_key, base_url) in enumerate(attempts):
        try:
            raw = call_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=PRIVATE_PM_DECISION_JSON_PROMPT,
                user_message=context.user_message,
                base_url=base_url,
                timeout=300,
                max_output_tokens=options.runtime_config.max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - 逐个 provider 容错，最后统一报失败
            logger.error("模型调用失败 (%s:%s): %s", provider, model, exc, exc_info=idx == 0)
            continue
        if str(raw or "").strip():
            if idx:
                logger.warning("Step4 决策已回退到备用模型 %s:%s", provider, model)
            return raw
        logger.error("模型返回内容为空 (%s:%s)", provider, model)
    return None


def call_step4_decision_model(
    options: Step4RunOptions,
    context: Step4InputContext,
    report_progress: Callable[[str, str, float], None],
) -> tuple[bool, str, Step4DecisionResult | None]:
    dump_model_input(
        step_prefix="step4",
        model=f"{options.provider}:{options.model}",
        system_prompt=PRIVATE_PM_DECISION_JSON_PROMPT,
        user_message=context.user_message,
        symbols=sorted(context.allowed_codes),
    )
    report_progress("LLM决策", "计算中", 0.5)
    raw = _call_with_fallback(options, context)
    if raw is None:
        return False, "llm_failed", None

    market_view, decisions, parse_err = parse_decisions(raw, context.allowed_codes, context.name_map)
    if parse_err:
        logger.error("决策 JSON 解析失败: %s", parse_err)
        return False, "llm_failed", None
    if not decisions:
        logger.info("模型未产出有效决策，跳过")
        return True, "skipped_no_decisions", None
    return True, "ok", Step4DecisionResult(market_view=market_view, decisions=decisions)
