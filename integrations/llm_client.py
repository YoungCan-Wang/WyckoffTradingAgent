"""
统一 LLM 调用层。

根据 provider/model/api_key/base_url 路由到 Gemini、OpenAI 兼容接口或 LiteLLM 适配层。
带图片输入时使用原生 Gemini 路径，避免 LiteLLM 文本路由误处理多模态 payload。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from integrations._llm_types import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_MODELS,
    OPENAI_COMPATIBLE_BASE_URLS,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "GEMINI_MODELS",
    "OPENAI_COMPATIBLE_BASE_URLS",
    "PROVIDER_LABELS",
    "SUPPORTED_PROVIDERS",
    "call_llm",
    "get_provider_credentials",
    "provider_fallbacks",
    "provider_route_chain",
    "resolve_provider_name",
]

GEMINI_MAX_OUTPUT_TOKENS_DEFAULT = 32768
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY = 2.0

# 推理与正文共享输出预算；只允许一次有上限的扩容，避免把截断误判为成功，
# 也避免复杂提示通过无限抬高预算掩盖任务本身需要拆分的问题。
OPENAI_COMPATIBLE_BUDGET_RETRY_FACTOR = 2
OPENAI_COMPATIBLE_MAX_BUDGET = 32768


@dataclass(frozen=True)
class _CompatibleCompletion:
    text: str
    finish_reason: str
    completion_tokens: int
    reasoning_tokens: int
    reasoning_length: int


def get_provider_credentials(provider: str) -> tuple[str, str, str]:
    """
    根据 provider 从环境变量取 (api_key, model, base_url)。

    Streamlit MVP 已下线，主分支不再从页面 session_state 读取模型配置。
    """
    provider = str(provider or "").strip().lower()
    key_suffix = provider
    env_prefix = key_suffix.upper()
    api_key = os.getenv(f"{env_prefix}_API_KEY", "").strip()
    model = os.getenv(f"{env_prefix}_MODEL", "").strip()
    base_url = os.getenv(f"{env_prefix}_BASE_URL", "").strip()
    if not base_url and provider in OPENAI_COMPATIBLE_BASE_URLS:
        base_url = (OPENAI_COMPATIBLE_BASE_URLS.get(provider, "") or "").strip()
    if not model and provider == "gemini":
        model = DEFAULT_GEMINI_MODEL
    if not model and provider == "1route":
        model = "gpt-5.5"
    if not model and provider == "deepseek":
        model = "deepseek-v4-flash"
    return (api_key, model or "", base_url)


def resolve_provider_name(role_env: str, default_provider: str) -> str:
    provider = os.getenv(role_env, "").strip() if role_env else ""
    provider = provider or os.getenv("DEFAULT_LLM_PROVIDER", "").strip() or default_provider
    return provider.lower() or default_provider


def provider_fallbacks(env_name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(env_name, default).strip()
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def provider_route_chain(primary_provider: str, fallback_providers: tuple[str, ...] = ()) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for provider in (primary_provider, *fallback_providers):
        provider = str(provider or "").strip().lower()
        api_key, model, base_url = get_provider_credentials(provider)
        key = (provider, model, base_url)
        missing_base = provider in OPENAI_COMPATIBLE_BASE_URLS and not base_url
        if not provider or not api_key or not model or missing_base or key in seen:
            continue
        seen.add(key)
        routes.append(
            {
                "name": f"{provider}:{model}",
                "provider": provider,
                "model": model,
                "api_key": api_key,
                "base_url": base_url,
            }
        )
    return routes


# Gemini finish_reason 在不同 SDK/模型下可能是字符串或数字枚举，这里统一兜底识别“输出被截断”。
_GEMINI_TRUNCATION_REASONS = {
    "MAX_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "TOKEN_LIMIT",
    "LENGTH",
    "2",  # 兼容部分枚举输出
}


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _validate_llm_request(provider: str, api_key: str) -> None:
    if not api_key or not api_key.strip():
        raise ValueError("API Key 未配置，请先在设置页录入。")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的供应商: {provider}，当前仅支持: {SUPPORTED_PROVIDERS}")


def _litellm_enabled() -> bool:
    return os.environ.get("LITELLM_ENABLED", "").strip() in ("1", "true", "yes")


def _call_litellm_if_enabled(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    *,
    images: list | None,
    base_url: str | None,
    timeout: int,
    max_output_tokens: int | None,
    allow_truncated_text: bool,
) -> str | None:
    if not _litellm_enabled():
        return None
    if provider == "deepseek":
        logger.info("[llm] DeepSeek uses the official native adapter even when LiteLLM is enabled")
        return None
    if images:
        logger.info("[llm] LITELLM_ENABLED=1 but images present, using native Gemini implementation")
        return None
    try:
        from integrations.llm_adapter import call_llm_via_litellm

        logger.info("[llm] LITELLM_ENABLED=1, routing to LiteLLM: provider=%s model=%s", provider, model)
        return call_llm_via_litellm(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=system_prompt,
            user_message=user_message,
            base_url=base_url,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            allow_truncated_text=allow_truncated_text,
        )
    except ImportError:
        logger.warning("[llm] LiteLLM not installed, falling back to native implementation")
        return None


def _call_native_llm(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    *,
    images: list | None,
    base_url: str | None,
    timeout: int,
    max_output_tokens: int | None,
    allow_truncated_text: bool,
) -> str:
    if provider == "gemini":
        return _call_gemini(
            model=model,
            api_key=api_key.strip(),
            system_prompt=system_prompt,
            user_message=user_message,
            images=images,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            allow_truncated_text=allow_truncated_text,
            base_url=(base_url or "").strip(),
        )
    if provider in OPENAI_COMPATIBLE_BASE_URLS:
        base = (base_url or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "") or "").rstrip("/")
        if not base:
            raise ValueError(f"未配置 {provider} 的 base_url")
        return _call_openai_compatible(
            base_url=base,
            api_key=api_key.strip(),
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            provider=provider,
            allow_truncated_text=allow_truncated_text,
        )
    raise ValueError(f"未实现的供应商: {provider}")


def call_llm(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    *,
    images: list | None = None,
    base_url: str | None = None,
    timeout: int = 120,
    max_output_tokens: int | None = None,
    allow_truncated_text: bool = False,
) -> str:
    """
    调用大模型，返回回复文本。

    Args:
        provider: 供应商名称。
        model: 模型名，如 gemini-3.1-flash-lite-preview。
        api_key: 对应供应商的 API Key。
        system_prompt: 系统提示词（Alpha 投委会等）。
        user_message: 用户消息（拼装好的 OHLCV 等）。
        images: 可选图片列表（PIL Image 或 bytes），仅部分模型支持。
        base_url: 可选代理地址，Gemini 和 OpenAI 兼容均支持。
        timeout: 请求超时秒数。
        allow_truncated_text: 当供应商返回“输出被截断”但已有非空文本时，是否直接返回文本。

    Returns:
        模型回复的纯文本。

    Raises:
        ValueError: provider 不支持或参数无效。
        RuntimeError: 调用失败或返回为空。
    """
    _validate_llm_request(provider, api_key)
    routed = _call_litellm_if_enabled(
        provider,
        model,
        api_key,
        system_prompt,
        user_message,
        images=images,
        base_url=base_url,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
        allow_truncated_text=allow_truncated_text,
    )
    if routed is not None:
        return routed
    return _call_native_llm(
        provider,
        model,
        api_key,
        system_prompt,
        user_message,
        images=images,
        base_url=base_url,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
        allow_truncated_text=allow_truncated_text,
    )


def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_output_tokens: int | None,
    provider: str = "",
    allow_truncated_text: bool = False,
) -> str:
    """通过 OpenAI 兼容的 /chat/completions 接口调用（OpenAI/智谱/DeepSeek/Qwen 等）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    from core.deepseek import (
        DEEPSEEK_BACKGROUND_MIN_OUTPUT_TOKENS,
        is_deepseek_v4_model,
        is_official_deepseek_url,
        resolve_official_deepseek_model,
    )

    resolved_model, legacy_level = resolve_official_deepseek_model(model, base_url)
    official_deepseek = (
        provider == "deepseek" and is_official_deepseek_url(base_url) and is_deepseek_v4_model(resolved_model)
    )
    minimum = DEEPSEEK_BACKGROUND_MIN_OUTPUT_TOKENS if official_deepseek else 256
    max_tokens = max(minimum, int(max_output_tokens) if max_output_tokens is not None else 8192)
    completion = _openai_compatible_attempt(
        url,
        headers,
        resolved_model,
        system_prompt,
        user_message,
        timeout,
        max_tokens,
        official_deepseek=official_deepseek,
        reasoning_level=legacy_level or "low",
    )
    if _completion_is_acceptable(completion, official_deepseek, allow_truncated_text):
        return completion.text
    retry_budget = _compatible_retry_budget(completion, max_tokens, official_deepseek)
    if retry_budget:
        completion = _openai_compatible_attempt(
            url,
            headers,
            resolved_model,
            system_prompt,
            user_message,
            timeout,
            retry_budget,
            official_deepseek=official_deepseek,
            reasoning_level=legacy_level or "low",
        )
        if _completion_is_acceptable(completion, official_deepseek, allow_truncated_text):
            return completion.text
    if completion.text and completion.finish_reason == "length":
        raise RuntimeError("OpenAI 兼容接口输出被截断，未返回完整正文")
    raise RuntimeError("OpenAI 兼容接口返回内容为空")


def _completion_is_acceptable(
    completion: _CompatibleCompletion,
    strict_completion: bool,
    allow_truncated_text: bool,
) -> bool:
    if not completion.text:
        return False
    return not strict_completion or completion.finish_reason != "length" or allow_truncated_text


def _compatible_retry_budget(
    completion: _CompatibleCompletion,
    max_tokens: int,
    strict_completion: bool,
) -> int:
    if completion.finish_reason != "length":
        return 0
    if completion.text and not strict_completion:
        return 0
    if not completion.text and completion.reasoning_tokens <= 0 and completion.reasoning_length <= 0:
        return 0
    budget = min(max_tokens * OPENAI_COMPATIBLE_BUDGET_RETRY_FACTOR, OPENAI_COMPATIBLE_MAX_BUDGET)
    return budget if budget > max_tokens else 0


def _openai_compatible_attempt(
    url: str,
    headers: dict[str, str],
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_tokens: int,
    *,
    official_deepseek: bool = False,
    reasoning_level: str = "low",
) -> _CompatibleCompletion:
    """发一次请求并保留截断诊断，是否重试由调用场景决定。"""
    import requests

    payload = _openai_compatible_payload(
        model,
        system_prompt,
        user_message,
        max_tokens,
        official_deepseek,
        reasoning_level,
    )
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI 兼容接口 HTTP {resp.status_code}: {resp.text[:500]}")
    return _parse_openai_compatible_response(resp.json(), model, max_tokens)


def _openai_compatible_payload(
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    official_deepseek: bool,
    reasoning_level: str,
) -> dict:
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    if official_deepseek:
        from core.deepseek import deepseek_chat_extra_body

        payload.pop("temperature", None)
        payload.update(deepseek_chat_extra_body(reasoning_level))
    return payload


def _parse_openai_compatible_response(
    data: dict,
    model: str,
    max_tokens: int,
) -> _CompatibleCompletion:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI 兼容接口返回无 choices")
    choice = choices[0] or {}
    msg = choice.get("message") or {}
    text = str(msg.get("content") or "").strip()
    finish_reason = str(choice.get("finish_reason") or "unknown")
    usage = data.get("usage") or {}
    reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    completion = _CompatibleCompletion(
        text=text,
        finish_reason=finish_reason.lower(),
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        reasoning_length=len(str(msg.get("reasoning_content") or "")),
    )
    if not text or completion.finish_reason == "length":
        log = logger.warning if text else logger.error
        log(
            "openai_compatible model=%s 返回%s正文: finish_reason=%s max_tokens=%s "
            "completion_tokens=%s reasoning_tokens=%s reasoning_len=%s",
            model,
            "截断" if text else "空",
            completion.finish_reason,
            max_tokens,
            completion.completion_tokens,
            completion.reasoning_tokens,
            completion.reasoning_length,
        )
    return completion


def _gemini_http_options(timeout: int, base_url: str) -> dict:
    opts: dict = {"timeout": timeout * 1000}
    if base_url:
        opts["base_url"] = base_url.rstrip("/")
    return opts


def _gemini_config(types, system_prompt: str, max_output_tokens: int | None):
    resolved = int(max_output_tokens) if max_output_tokens is not None else GEMINI_MAX_OUTPUT_TOKENS_DEFAULT
    return types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.4,
        top_p=0.95,
        top_k=40,
        max_output_tokens=max(1024, resolved),
    )


def _gemini_contents(user_message: str, images: list | None) -> list:
    contents = [user_message]
    if images:
        contents.extend(images)
    return contents


def _gemini_text(response) -> str:
    text = getattr(response, "text", None) or ""
    if text or not getattr(response, "candidates", None):
        return text.strip()
    parts = []
    for candidate in response.candidates:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(part_text)
    return "".join(parts).strip()


def _gemini_finish_reason(response) -> str:
    if not getattr(response, "candidates", None):
        return ""
    if len(response.candidates) <= 0:
        return ""
    reason = getattr(response.candidates[0], "finish_reason", "")
    if reason is None:
        return ""
    return getattr(reason, "name", str(reason))


def _log_gemini_usage(model: str, finish_reason: str, response, max_output_tokens: int) -> None:
    if not _env_enabled("LLM_LOG_USAGE", True):
        return
    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    completion_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    total_tokens = getattr(usage, "total_token_count", None) if usage else None
    logger.info(
        "gemini model=%s finish_reason=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s max_output_tokens=%s",
        model,
        finish_reason or "unknown",
        prompt_tokens,
        completion_tokens,
        total_tokens,
        max_output_tokens,
    )


def _handle_gemini_truncation(text: str, finish_reason: str, allow_truncated_text: bool) -> str | None:
    if finish_reason.strip().upper() not in _GEMINI_TRUNCATION_REASONS:
        return None
    if allow_truncated_text and text.strip():
        if _env_enabled("LLM_LOG_USAGE", True):
            logger.warning("gemini truncation tolerated: using returned text because allow_truncated_text=1")
        return text
    raise RuntimeError(f"Gemini 输出被截断(finish_reason={finish_reason or 'unknown'})，请缩短输入或提升输出上限后重试")


def _gemini_retry_sleep(attempt: int) -> float:
    return min(GEMINI_RETRY_DELAY * (2 ** (attempt - 1)), 30.0)


def _call_gemini(
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    images: list | None,
    timeout: int,
    max_output_tokens: int | None,
    allow_truncated_text: bool,
    base_url: str = "",
) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key, http_options=_gemini_http_options(timeout, base_url))
    config = _gemini_config(types, system_prompt, max_output_tokens)
    contents = _gemini_contents(user_message, images)
    last_err: Exception | None = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            if response is None:
                raise RuntimeError("Gemini 返回空响应")

            text = _gemini_text(response)
            if not text:
                raise RuntimeError("Gemini 返回内容为空")

            finish_reason = _gemini_finish_reason(response)
            _log_gemini_usage(model, finish_reason, response, config.max_output_tokens)
            truncated = _handle_gemini_truncation(text, finish_reason, allow_truncated_text)
            if truncated is not None:
                return truncated
            return text
        except Exception as e:
            last_err = e
            if attempt >= GEMINI_MAX_RETRIES:
                break
            sleep_s = _gemini_retry_sleep(attempt)
            if _env_enabled("LLM_LOG_RETRY_ERRORS", True):
                logger.warning(
                    "gemini attempt %s/%s failed: %s; retry in %.1fs",
                    attempt,
                    GEMINI_MAX_RETRIES,
                    e,
                    sleep_s,
                )
            time.sleep(sleep_s)

    raise RuntimeError(f"Gemini 调用失败: {last_err}")
