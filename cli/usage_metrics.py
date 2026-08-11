"""Token throughput and cache-hit helpers shared by runtime / TUI."""

from __future__ import annotations

from typing import Any


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_dump(usage: Any) -> dict[str, Any]:
    """Best-effort dict view for OpenAI SDK / OpenRouter / MiniMax usage objects."""
    if isinstance(usage, dict):
        return usage
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=False)
            if isinstance(data, dict):
                return data
        except TypeError:
            try:
                data = dump()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        except Exception:
            pass
    extra = getattr(usage, "model_extra", None)
    return extra if isinstance(extra, dict) else {}


def _usage_field(usage: Any, name: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(name)
    value = getattr(usage, name, None)
    if value is not None:
        return value
    return _usage_dump(usage).get(name)


def _details_field(details: Any, name: str) -> Any:
    if details is None:
        return None
    if isinstance(details, dict):
        return details.get(name)
    value = getattr(details, name, None)
    if value is not None:
        return value
    dump = getattr(details, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=False)
            if isinstance(data, dict):
                return data.get(name)
        except Exception:
            pass
    return None


def _prompt_token_details(usage: Any) -> Any:
    details = _usage_field(usage, "prompt_tokens_details")
    if details is not None:
        return details
    return _usage_dump(usage).get("prompt_tokens_details")


def openai_cache_reported(usage: Any) -> bool:
    """True only when the gateway actually returned cache-related fields."""
    if _usage_field(usage, "prompt_cache_hit_tokens") is not None:
        return True
    details = _prompt_token_details(usage)
    if details is None:
        return False
    if isinstance(details, dict):
        return "cached_tokens" in details or "cache_write_tokens" in details
    if getattr(details, "cached_tokens", None) is not None:
        return True
    if getattr(details, "cache_write_tokens", None) is not None:
        return True
    # Pydantic may expose the object even when fields are unset; require dump keys.
    dump = getattr(details, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=True)
            if isinstance(data, dict):
                return "cached_tokens" in data or "cache_write_tokens" in data
        except Exception:
            pass
    return False


def extract_openai_cache_tokens(usage: Any) -> tuple[int, int]:
    """Return (cache_read, cache_write) from an OpenAI-compatible usage object."""
    hit = _usage_field(usage, "prompt_cache_hit_tokens")
    details = _prompt_token_details(usage)
    if hit is not None:
        cache_read = as_int(hit)
    else:
        cache_read = as_int(_details_field(details, "cached_tokens"))
    cache_write = as_int(_details_field(details, "cache_write_tokens"))
    return cache_read, cache_write


def normalize_anthropic_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict[str, int]:
    """Anthropic input_tokens exclude cache; fold cache into input for hit-rate math."""
    read = max(0, as_int(cache_read))
    write = max(0, as_int(cache_write))
    return {
        "input_tokens": max(0, as_int(input_tokens)) + read + write,
        "output_tokens": max(0, as_int(output_tokens)),
        "cache_read_tokens": read,
        "cache_write_tokens": write,
    }


def generation_seconds(*, stream_started: float | None, first_content_at: float | None, ended_at: float) -> float:
    """Model-only generation window: first content → end; else full stream wall time."""
    if stream_started is None:
        return 0.0
    start = first_content_at if first_content_at is not None else stream_started
    return max(0.0, round(ended_at - start, 6))


def output_tok_per_s(output_tokens: int, generation_s: float) -> float | None:
    if output_tokens <= 0 or generation_s <= 0:
        return None
    return round(output_tokens / generation_s, 1)


def cache_hit_rate_pct(cache_read_tokens: int, input_tokens: int) -> int | None:
    if input_tokens <= 0:
        return None
    return min(100, int(round(100.0 * max(0, cache_read_tokens) / input_tokens)))


def enrich_usage(
    usage: dict[str, Any],
    *,
    generation_ms: int | None = None,
    cache_reported: bool | None = None,
) -> dict[str, Any]:
    """Attach tok/s and cache hit % onto a usage dict (mutates a copy)."""
    out = dict(usage)
    input_tokens = as_int(out.get("input_tokens"))
    output_tokens = as_int(out.get("output_tokens"))
    cache_read = as_int(out.get("cache_read_tokens"))
    if generation_ms is not None:
        out["generation_ms"] = max(0, int(generation_ms))
    gen_ms = as_int(out.get("generation_ms"), default=-1)
    if gen_ms >= 0:
        tok_s = output_tok_per_s(output_tokens, gen_ms / 1000.0)
        if tok_s is not None:
            out["output_tok_per_s"] = tok_s
    hit = cache_hit_rate_pct(cache_read, input_tokens)
    reported = bool(cache_reported) if cache_reported is not None else "cache_read_tokens" in usage
    if hit is not None and reported:
        out["cache_hit_rate"] = hit
        out["cache_reported"] = True
    elif reported:
        out["cache_reported"] = True
    return out


def merge_usage_totals(*usages: dict[str, Any]) -> dict[str, Any]:
    total_in = sum(as_int(u.get("input_tokens")) for u in usages)
    total_out = sum(as_int(u.get("output_tokens")) for u in usages)
    total_cache_read = sum(as_int(u.get("cache_read_tokens")) for u in usages)
    total_cache_write = sum(as_int(u.get("cache_write_tokens")) for u in usages)
    total_gen_ms = sum(as_int(u.get("generation_ms")) for u in usages)
    any_cache = any(bool(u.get("cache_reported")) or "cache_read_tokens" in u for u in usages)
    merged: dict[str, Any] = {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "generation_ms": total_gen_ms,
    }
    if any_cache:
        merged["cache_read_tokens"] = total_cache_read
        merged["cache_write_tokens"] = total_cache_write
    return enrich_usage(merged, cache_reported=any_cache)


def format_usage_footer(
    *,
    input_tokens: int,
    output_tokens: int,
    elapsed_s: float,
    output_tok_per_s: float | None = None,
    cache_hit_rate: int | None = None,
    cache_reported: bool = False,
) -> str:
    parts: list[str] = []
    if input_tokens or output_tokens:
        parts.append(f"↑{input_tokens:,} ↓{output_tokens:,}")
    if output_tok_per_s is not None and output_tok_per_s > 0:
        parts.append(f"{output_tok_per_s:g}tok/s")
    if cache_reported and cache_hit_rate is not None and input_tokens > 0:
        parts.append(f"cache {cache_hit_rate}%")
    parts.append(f"{elapsed_s:.1f}s")
    return " · ".join(parts)
