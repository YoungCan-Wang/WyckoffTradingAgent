from __future__ import annotations

from integrations.llm_client import provider_fallbacks, provider_route_chain, resolve_provider_name


def test_provider_route_chain_prefers_efficiency(monkeypatch):
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://eff.example/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    routes = provider_route_chain("efficiency", ("gemini",))

    assert [route["provider"] for route in routes] == ["efficiency", "gemini"]
    assert routes[0]["base_url"] == "https://eff.example/v1"


def test_provider_route_chain_skips_openai_compatible_without_base(monkeypatch):
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
    monkeypatch.delenv("EFFICIENCY_BASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    routes = provider_route_chain("efficiency", ("gemini",))

    assert [route["provider"] for route in routes] == ["gemini"]


def test_resolve_provider_name_uses_role_before_global(monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("TAIL_BUY_LLM_PROVIDER", "efficiency")

    assert resolve_provider_name("TAIL_BUY_LLM_PROVIDER", "gemini") == "efficiency"


def test_provider_fallbacks_empty_by_default(monkeypatch):
    monkeypatch.delenv("TAIL_BUY_LLM_FALLBACK_PROVIDERS", raising=False)

    assert provider_fallbacks("TAIL_BUY_LLM_FALLBACK_PROVIDERS") == ()
