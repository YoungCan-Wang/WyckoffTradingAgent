"""Model metadata and local usage cost helpers for Wyckoff CLI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cli.model_catalog import catalog_context_window, looks_like_openrouter
from cli.model_metadata import infer_context_window
from core.deepseek import DEEPSEEK_REASONING_LEVELS


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    context_window: int
    supports_reasoning: bool
    thinking_levels: tuple[str, ...] = ("off",)
    input_cost_per_1m: float | None = None
    output_cost_per_1m: float | None = None
    cache_read_cost_per_1m: float | None = None
    cache_write_cost_per_1m: float | None = None
    source: str = "inferred"
    window_source: str = "inferred"


@dataclass(frozen=True)
class UsageSummary:
    provider: str
    model: str
    requests: int
    tokens_in: int
    tokens_out: int
    elapsed_s: float
    estimated_cost: float | None


@dataclass(frozen=True)
class _ModelPattern:
    pattern: re.Pattern[str]
    supports_reasoning: bool = False
    thinking_levels: tuple[str, ...] = ("off",)


_MODEL_PATTERNS: tuple[_ModelPattern, ...] = (
    _ModelPattern(re.compile(r"gemini-2\.5|gemini-3", re.I), True, ("off", "low", "medium", "high")),
    _ModelPattern(re.compile(r"gemini-2|gemini", re.I), False),
    _ModelPattern(re.compile(r"claude-(?:opus|sonnet|haiku)", re.I), True, ("off", "low", "medium", "high")),
    _ModelPattern(
        re.compile(r"\bo[34](?:-|$)|gpt-5|reasoning", re.I), True, ("off", "minimal", "low", "medium", "high")
    ),
    _ModelPattern(re.compile(r"gpt-4o|gpt-4\.1|gpt-4", re.I), False),
    _ModelPattern(re.compile(r"deepseek-v4", re.I), True, DEEPSEEK_REASONING_LEVELS),
    _ModelPattern(re.compile(r"minimax-m3", re.I), True, ("off", "adaptive")),
    _ModelPattern(re.compile(r"qwen|kimi|moonshot|minimax|mistral", re.I), False),
    _ModelPattern(re.compile(r"longcat|step", re.I), False),
)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num >= 0 else None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def infer_model_info(config: dict[str, Any]) -> ModelInfo:
    """Infer model metadata from a saved model config.

    Cost values are deliberately read from config, not guessed from volatile
    provider price pages. Users can set them with `wyckoff model cost`.
    """

    provider = str(config.get("provider_name", "") or "")
    model = str(config.get("model", "") or "")
    pattern_info = next((item for item in _MODEL_PATTERNS if item.pattern.search(model)), None)
    context_window = _int_or_none(config.get("context_window"))
    window_source = "config" if context_window is not None else ""
    if context_window is None:
        context_window, window_source = _resolve_window(model, str(config.get("base_url", "") or ""))
    supports_reasoning = (
        bool(config.get("supports_reasoning"))
        if "supports_reasoning" in config
        else bool(pattern_info.supports_reasoning if pattern_info else False)
    )
    levels = tuple(str(v) for v in config.get("thinking_levels", []) if str(v)) or (
        pattern_info.thinking_levels if pattern_info else ("off",)
    )
    if not supports_reasoning:
        levels = ("off",)
    return ModelInfo(
        provider=provider,
        model=model,
        context_window=context_window,
        supports_reasoning=supports_reasoning,
        thinking_levels=levels,
        input_cost_per_1m=_float_or_none(config.get("input_cost_per_1m")),
        output_cost_per_1m=_float_or_none(config.get("output_cost_per_1m")),
        cache_read_cost_per_1m=_float_or_none(config.get("cache_read_cost_per_1m")),
        cache_write_cost_per_1m=_float_or_none(config.get("cache_write_cost_per_1m")),
        source=_metadata_source(config, window_source),
        window_source=window_source,
    )


def _resolve_window(model: str, base_url: str) -> tuple[int, str]:
    """Prefer the provider-reported window; fall back to name patterns.

    只对 OpenRouter 走目录：其它 base_url 的模型 id 命名空间不同，用它的目录反而会误配。
    """
    if looks_like_openrouter(base_url):
        window = catalog_context_window(model)
        if window:
            return window, "catalog"
    return infer_context_window(model), "inferred"


def _metadata_source(config: dict[str, Any], window_source: str) -> str:
    if window_source == "config" or config.get("input_cost_per_1m"):
        return "config"
    return window_source or "inferred"


def estimate_cost_usd(info: ModelInfo, *, tokens_in: int = 0, tokens_out: int = 0) -> float | None:
    """Estimate request cost in USD from configured per-1M token prices."""

    if info.input_cost_per_1m is None and info.output_cost_per_1m is None:
        return None
    input_cost = (info.input_cost_per_1m or 0) * max(tokens_in, 0) / 1_000_000
    output_cost = (info.output_cost_per_1m or 0) * max(tokens_out, 0) / 1_000_000
    return input_cost + output_cost


def format_token_window(tokens: int) -> str:
    if tokens >= 1_000_000:
        value = tokens / 1_000_000
        return f"{value:g}M"
    if tokens >= 1_000:
        value = tokens / 1_000
        return f"{value:g}K"
    return str(tokens)


def format_cost_pair(info: ModelInfo) -> str:
    if info.input_cost_per_1m is None and info.output_cost_per_1m is None:
        return "unknown"
    input_s = "?" if info.input_cost_per_1m is None else f"${info.input_cost_per_1m:g}"
    output_s = "?" if info.output_cost_per_1m is None else f"${info.output_cost_per_1m:g}"
    return f"{input_s}/{output_s} per 1M"


def format_model_metadata(info: ModelInfo) -> str:
    reasoning = "on" if info.supports_reasoning else "off"
    return f"ctx={format_token_window(info.context_window)}  reasoning={reasoning}  cost={format_cost_pair(info)}"


def summarize_model_usage(*, days: int = 7, configs: list[dict[str, Any]] | None = None) -> list[UsageSummary]:
    """Aggregate local chat_log usage for model cost visibility."""

    from integrations.local_db import get_db, init_db

    init_db()
    config_by_key: dict[tuple[str, str], ModelInfo] = {}
    for config in configs or []:
        info = infer_model_info(config)
        config_by_key[(info.provider, info.model)] = info

    cutoff = (datetime.now() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d %H:%M:%S")
    rows = get_db().execute(
        """SELECT provider, model,
                  COUNT(*) AS requests,
                  SUM(tokens_in) AS tokens_in,
                  SUM(tokens_out) AS tokens_out,
                  SUM(elapsed_s) AS elapsed_s
           FROM chat_log
           WHERE role='assistant'
             AND created_at >= ?
             AND (tokens_in > 0 OR tokens_out > 0)
           GROUP BY provider, model
           ORDER BY SUM(tokens_in + tokens_out) DESC""",
        (cutoff,),
    )
    summaries: list[UsageSummary] = []
    for row in rows.fetchall():
        provider = str(row["provider"] or "")
        model = str(row["model"] or "")
        info = config_by_key.get((provider, model)) or infer_model_info({"provider_name": provider, "model": model})
        tokens_in = int(row["tokens_in"] or 0)
        tokens_out = int(row["tokens_out"] or 0)
        summaries.append(
            UsageSummary(
                provider=provider,
                model=model,
                requests=int(row["requests"] or 0),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                elapsed_s=float(row["elapsed_s"] or 0),
                estimated_cost=estimate_cost_usd(info, tokens_in=tokens_in, tokens_out=tokens_out),
            )
        )
    return summaries
