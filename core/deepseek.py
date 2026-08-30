"""DeepSeek V4 capability and request-policy definitions."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

DEEPSEEK_OFFICIAL_ORIGIN = "https://api.deepseek.com"
DEEPSEEK_CONTEXT_WINDOW = 1_000_000
DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS = 32_768
DEEPSEEK_BACKGROUND_MIN_OUTPUT_TOKENS = 4_096
DEEPSEEK_REASONING_LEVELS = ("off", "low", "high", "max")

DeepSeekReasoningLevel = Literal["off", "low", "high", "max"]


def is_deepseek_v4_model(model: str) -> bool:
    model_id = str(model or "").strip().lower()
    return model_id.startswith(("deepseek-v4-flash", "deepseek-v4-pro"))


def is_official_deepseek_url(base_url: str) -> bool:
    try:
        parsed = urlparse(str(base_url or ""))
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.deepseek.com"
        and port
        in {
            None,
            443,
        }
    )


def normalize_deepseek_reasoning_level(
    value: str | None, *, default: DeepSeekReasoningLevel = "high"
) -> DeepSeekReasoningLevel:
    level = str(value or "").strip().lower()
    if level in {"medium", "xhigh"}:
        return "high"
    if level in DEEPSEEK_REASONING_LEVELS:
        return level  # type: ignore[return-value]
    return default


def deepseek_chat_extra_body(level: str | None) -> dict[str, Any]:
    normalized = normalize_deepseek_reasoning_level(level)
    if normalized == "off":
        return {"thinking": {"type": "disabled"}}
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": normalized,
    }
