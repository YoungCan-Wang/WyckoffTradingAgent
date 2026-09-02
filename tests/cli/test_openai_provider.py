from types import SimpleNamespace

import pytest

from cli.providers.openai import (
    OpenAIProvider,
    OpenAIStreamState,
    _apply_usage_from_chunk,
    _create_openai_stream,
    _reraise_if_fatal_openai,
)
from integrations._llm_types import normalize_openai_compatible_base_url


def test_normalize_oneroute_root_url_adds_v1():
    assert normalize_openai_compatible_base_url("https://api.1route.dev") == "https://api.1route.dev/v1"
    assert normalize_openai_compatible_base_url("https://api.1route.dev/") == "https://api.1route.dev/v1"
    assert normalize_openai_compatible_base_url("https://api.1route.dev/v1") == "https://api.1route.dev/v1"
    assert normalize_openai_compatible_base_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"


def test_openai_provider_rewrites_oneroute_root_base_url():
    provider = OpenAIProvider(api_key="sk-test", model="gpt-5.6-luna", base_url="https://api.1route.dev")
    assert str(provider._client.base_url).rstrip("/") == "https://api.1route.dev/v1"


def test_apply_usage_from_chunk_reads_input_output_aliases():
    state = OpenAIStreamState()
    _apply_usage_from_chunk(state, SimpleNamespace(usage={"input_tokens": 41, "output_tokens": 7}))
    assert state.input_tokens == 41
    assert state.output_tokens == 7


def test_reraise_if_fatal_openai_keeps_auth_and_rate_limit():
    class AuthenticationError(Exception):
        status_code = 401

    with pytest.raises(AuthenticationError):
        _reraise_if_fatal_openai(AuthenticationError("invalid api key"))

    class RateLimitError(Exception):
        status_code = 429

    with pytest.raises(RateLimitError):
        _reraise_if_fatal_openai(RateLimitError("too many requests"))


def test_create_openai_stream_does_not_retry_401():
    class AuthenticationError(Exception):
        status_code = 401

    class Client:
        def __init__(self) -> None:
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **_kwargs):
            self.calls += 1
            raise AuthenticationError("401 invalid api key")

    client = Client()
    with pytest.raises(AuthenticationError, match="401"):
        _create_openai_stream(client, {"model": "gpt-5.6-luna", "stream": True})
    assert client.calls == 1
