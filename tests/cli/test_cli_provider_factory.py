from cli.provider_factory import create_provider
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
    assert isinstance(provider, OpenAIProvider)
    assert str(provider._client.base_url) == "https://api.deepseek.com/v1/"


def test_deepseek_provider_defaults_to_official_endpoint():
    provider, err = create_provider("deepseek", "test-key", model="deepseek-v4-pro")

    assert err is None
    assert isinstance(provider, OpenAIProvider)
    assert str(provider._client.base_url) == "https://api.deepseek.com/v1/"
