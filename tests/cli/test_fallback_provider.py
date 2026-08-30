from unittest.mock import Mock

import pytest

from cli.providers.fallback import FallbackProvider


def _config(model_id: str, provider_name: str, model: str) -> dict:
    return {
        "id": model_id,
        "provider_name": provider_name,
        "api_key": "test-key",
        "model": model,
        "base_url": "https://example.com/v1",
    }


def test_fallback_exposes_the_provider_that_actually_ran(monkeypatch):
    provider = FallbackProvider(
        [
            _config("primary", "openai", "primary-model"),
            _config("backup", "deepseek", "backup-model"),
        ],
        "primary",
    )
    primary = Mock()
    primary.chat.side_effect = TimeoutError("primary timeout")
    backup = Mock()
    backup.chat.return_value = {"type": "text", "text": "ok"}
    monkeypatch.setattr(provider, "_get_provider", lambda model_id: {"primary": primary, "backup": backup}[model_id])

    assert provider.chat([], []) == {"type": "text", "text": "ok"}
    assert provider.active_provider_name == "deepseek"
    assert provider.active_model == "backup-model"


def test_unusable_backup_does_not_mask_primary_network_error(monkeypatch):
    provider = FallbackProvider(
        [
            _config("primary", "openai", "primary-model"),
            _config("broken", "missing-provider", "broken-model"),
        ],
        "primary",
    )
    primary = Mock()
    primary.chat.side_effect = TimeoutError("primary timeout")

    def get_provider(model_id: str):
        if model_id == "primary":
            return primary
        raise RuntimeError("backup is not configured")

    monkeypatch.setattr(provider, "_get_provider", get_provider)

    with pytest.raises(TimeoutError, match="primary timeout"):
        provider.chat([], [])


def test_streaming_fallback_updates_active_model(monkeypatch):
    provider = FallbackProvider(
        [
            _config("primary", "openai", "primary-model"),
            _config("backup", "deepseek", "backup-model"),
        ],
        "primary",
    )
    primary = Mock()
    primary.chat_stream.side_effect = TimeoutError("primary timeout")
    backup = Mock()
    backup.chat_stream.return_value = iter([{"type": "text_delta", "text": "ok"}])
    monkeypatch.setattr(provider, "_get_provider", lambda model_id: {"primary": primary, "backup": backup}[model_id])

    assert list(provider.chat_stream([], [])) == [{"type": "text_delta", "text": "ok"}]
    assert provider.active_provider_name == "deepseek"
    assert provider.active_model == "backup-model"


def test_unusable_streaming_backup_preserves_primary_error(monkeypatch):
    provider = FallbackProvider(
        [
            _config("primary", "openai", "primary-model"),
            _config("broken", "missing-provider", "broken-model"),
        ],
        "primary",
    )
    primary = Mock()
    primary.chat_stream.side_effect = TimeoutError("primary timeout")

    def get_provider(model_id: str):
        if model_id == "primary":
            return primary
        raise RuntimeError("backup is not configured")

    monkeypatch.setattr(provider, "_get_provider", get_provider)

    with pytest.raises(TimeoutError, match="primary timeout"):
        list(provider.chat_stream([], []))
