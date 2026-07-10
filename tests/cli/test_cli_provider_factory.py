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
