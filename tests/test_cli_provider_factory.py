from cli._provider_factory import _create_provider
from cli.providers.openai import OpenAIProvider


def test_openai_provider_accepts_minimax_compatible_endpoint():
    provider, err = _create_provider(
        "openai",
        "test-key",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
    )

    assert err is None
    assert isinstance(provider, OpenAIProvider)


def test_minimax_provider_alias_uses_openai_compatible_client():
    provider, err = _create_provider("minimax", "test-key", model="MiniMax-M3")

    assert err is None
    assert isinstance(provider, OpenAIProvider)
