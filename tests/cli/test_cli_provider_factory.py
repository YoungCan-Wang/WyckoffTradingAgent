from cli.provider_factory import create_provider
from cli.providers.deepseek import DeepSeekProvider
from cli.providers.openai import OpenAIProvider


def test_openai_provider_accepts_minimax_compatible_endpoint():
    provider, err = create_provider(
        "openai",
        "test-key",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        context_window=1_000_000,
    )

    assert err is None
    assert isinstance(provider, OpenAIProvider)
    assert provider.context_window == 1_000_000


def test_deepseek_provider_uses_openai_compatible_transport():
    provider, err = create_provider(
        "deepseek",
        "test-key",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
    )

    assert err is None
    assert isinstance(provider, DeepSeekProvider)
    assert str(provider._client.base_url) == "https://api.deepseek.com/v1/"
    assert provider.context_window == 1_000_000
    assert provider._request_options()["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def test_deepseek_provider_defaults_to_official_endpoint():
    provider, err = create_provider("deepseek", "test-key", model="deepseek-v4-pro")

    assert err is None
    assert isinstance(provider, DeepSeekProvider)
    assert str(provider._client.base_url) == "https://api.deepseek.com/v1/"


def test_deepseek_provider_supports_off_reasoning_level():
    provider, err = create_provider(
        "deepseek",
        "test-key",
        model="deepseek-v4-pro",
        thinking_level="off",
    )

    assert err is None
    assert isinstance(provider, DeepSeekProvider)
    assert provider._request_options()["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_provider_keeps_custom_endpoint_generic():
    provider, err = create_provider(
        "deepseek",
        "test-key",
        model="deepseek-v4-flash",
        base_url="https://proxy.example.com/v1",
    )

    assert err is None
    assert isinstance(provider, DeepSeekProvider)
    assert provider.context_window == 64_000
    assert provider._request_options() == {"frequency_penalty": 0.3}


def test_deepseek_provider_migrates_retired_official_alias():
    provider, err = create_provider(
        "deepseek",
        "test-key",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
    )

    assert err is None
    assert isinstance(provider, DeepSeekProvider)
    assert provider._model == "deepseek-v4-flash"
    assert provider.context_window == 1_000_000
    assert provider._request_options()["extra_body"] == {"thinking": {"type": "disabled"}}
