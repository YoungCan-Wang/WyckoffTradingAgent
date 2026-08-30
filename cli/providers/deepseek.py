"""Official DeepSeek V4 provider over the Chat Completions API."""

from __future__ import annotations

from typing import Any

from cli.providers.openai import OpenAIProvider
from core.deepseek import (
    DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
    DEEPSEEK_CONTEXT_WINDOW,
    DEEPSEEK_OFFICIAL_ORIGIN,
    deepseek_chat_extra_body,
    normalize_deepseek_reasoning_level,
)


class DeepSeekProvider(OpenAIProvider):
    context_window = DEEPSEEK_CONTEXT_WINDOW

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-flash",
        base_url: str = f"{DEEPSEEK_OFFICIAL_ORIGIN}/v1",
        thinking_level: str = "high",
    ) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url)
        self._thinking_level = normalize_deepseek_reasoning_level(thinking_level)

    @property
    def name(self) -> str:
        return f"DeepSeek ({self._model})"

    def _request_options(self) -> dict[str, Any]:
        return {
            "max_tokens": DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
            "extra_body": deepseek_chat_extra_body(self._thinking_level),
        }
