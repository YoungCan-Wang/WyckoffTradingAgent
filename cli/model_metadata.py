"""Shared model metadata inference helpers."""

from __future__ import annotations

import re

from core.deepseek import DEEPSEEK_CONTEXT_WINDOW

UNKNOWN_MODEL_CONTEXT_WINDOW = 64_000

_CONTEXT_WINDOW_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"gemini-2", re.I), 1_000_000),
    (re.compile(r"gemini-3", re.I), 128_000),
    (re.compile(r"claude-(?:opus|sonnet|haiku)|claude", re.I), 200_000),
    (re.compile(r"minimax-m3", re.I), 1_000_000),
    (re.compile(r"gpt-4o|gpt-4\.1|gpt-4|\bo[34](?:-|$)|gpt-5|reasoning", re.I), 128_000),
    (re.compile(r"gpt-3\.5", re.I), 16_000),
    (re.compile(r"deepseek-v4", re.I), DEEPSEEK_CONTEXT_WINDOW),
    (re.compile(r"deepseek", re.I), 64_000),
    (re.compile(r"qwen|kimi|moonshot|minimax|mistral", re.I), 128_000),
    (re.compile(r"longcat|step", re.I), 64_000),
)


def infer_context_window(model_name: str) -> int:
    """Infer context window from local model metadata, including unknown-model default."""

    for pattern, window in _CONTEXT_WINDOW_PATTERNS:
        if pattern.search(model_name):
            return window
    return UNKNOWN_MODEL_CONTEXT_WINDOW
