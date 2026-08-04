from types import SimpleNamespace

from cli.usage_metrics import (
    as_int,
    cache_hit_rate_pct,
    enrich_usage,
    extract_openai_cache_tokens,
    format_usage_footer,
    generation_seconds,
    normalize_anthropic_usage,
    openai_cache_reported,
    output_tok_per_s,
)


def test_as_int_respects_default_for_none():
    assert as_int(None, default=-1) == -1
    assert as_int(None) == 0
    assert as_int("12") == 12
    assert as_int("x", default=7) == 7


def test_extract_openai_cache_prefers_deepseek_hit_tokens():
    usage = SimpleNamespace(
        prompt_cache_hit_tokens=120,
        prompt_tokens_details=SimpleNamespace(cached_tokens=99, cache_write_tokens=8),
    )
    assert extract_openai_cache_tokens(usage) == (120, 8)
    assert openai_cache_reported(usage) is True


def test_extract_openai_cache_uses_cached_tokens_and_ignores_completion_cached():
    usage = {
        "prompt_tokens_details": {"cached_tokens": 3840, "cache_write_tokens": 0},
        "completion_tokens_details": {"cached_tokens": 29},  # reasoning, not cache write
    }
    assert extract_openai_cache_tokens(usage) == (3840, 0)
    assert openai_cache_reported(usage) is True


def test_openai_cache_not_reported_without_fields():
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
    assert openai_cache_reported(usage) is False
    assert openai_cache_reported({"prompt_tokens": 100}) is False


def test_normalize_anthropic_usage_folds_cache_into_input():
    usage = normalize_anthropic_usage(input_tokens=200, output_tokens=50, cache_read=800, cache_write=0)
    assert usage["input_tokens"] == 1000
    assert usage["cache_read_tokens"] == 800
    assert cache_hit_rate_pct(usage["cache_read_tokens"], usage["input_tokens"]) == 80


def test_output_tok_per_s_and_generation_window():
    assert generation_seconds(stream_started=1.0, first_content_at=1.2, ended_at=1.7) == 0.5
    assert generation_seconds(stream_started=1.0, first_content_at=None, ended_at=1.4) == 0.4
    assert output_tok_per_s(56, 0.32) == 175.0
    assert output_tok_per_s(0, 1.0) is None


def test_enrich_usage_and_footer():
    usage = enrich_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 80,
            "cache_write_tokens": 0,
        },
        generation_ms=250,
        cache_reported=True,
    )
    assert usage["output_tok_per_s"] == 200.0
    assert usage["cache_hit_rate"] == 80
    line = format_usage_footer(
        input_tokens=100,
        output_tokens=50,
        elapsed_s=2.3,
        output_tok_per_s=usage["output_tok_per_s"],
        cache_hit_rate=usage["cache_hit_rate"],
        cache_reported=True,
    )
    assert line == "↑100 ↓50 · 200tok/s · cache 80% · 2.3s"


def test_enrich_usage_hides_cache_when_key_absent():
    usage = enrich_usage({"input_tokens": 10, "output_tokens": 5}, generation_ms=100)
    assert "cache_hit_rate" not in usage
    assert not usage.get("cache_reported")


def test_enrich_usage_skips_tok_s_when_generation_ms_missing():
    usage = enrich_usage({"input_tokens": 10, "output_tokens": 50, "generation_ms": None})
    assert "output_tok_per_s" not in usage


def test_gemini_usage_event_omits_unset_cache_fields():
    from cli.providers.gemini import _gemini_usage_event

    class Meta:
        model_fields_set = {"prompt_token_count", "candidates_token_count"}
        prompt_token_count = 10
        candidates_token_count = 5
        cached_content_token_count = 0  # default placeholder, not in fields_set

    event = _gemini_usage_event(Meta())
    assert event["input_tokens"] == 10
    assert event["output_tokens"] == 5
    assert "cache_read_tokens" not in event


def test_gemini_usage_event_keeps_explicit_zero_cache():
    from cli.providers.gemini import _gemini_usage_event

    class Meta:
        model_fields_set = {"prompt_token_count", "candidates_token_count", "cached_content_token_count"}
        prompt_token_count = 10
        candidates_token_count = 5
        cached_content_token_count = 0

    event = _gemini_usage_event(Meta())
    assert event["cache_read_tokens"] == 0
