"""Step4 决策模型的备用 provider 与降级路径。"""

from __future__ import annotations

import workflows.step4_llm as mod


def _options(provider="efficiency", model="m1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        provider=provider,
        model=model,
        api_key="k",
        llm_base_url="",
        runtime_config=SimpleNamespace(max_output_tokens=1024),
    )


def _context():
    from types import SimpleNamespace

    return SimpleNamespace(user_message="u", allowed_codes={"000001"}, name_map={})


def test_errors_and_empty_content_fall_back_to_next_provider(monkeypatch):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return "" if len(calls) == 2 else '{"decisions": []}'

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    raw = mod._call_with_fallback(_options(), _context())

    assert raw == '{"decisions": []}'
    assert len(calls) == 3


def test_all_providers_failing_returns_none(monkeypatch):
    monkeypatch.setattr(mod, "call_llm", lambda **kw: "")
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    assert mod._call_with_fallback(_options(), _context()) is None


def test_providers_without_key_are_skipped(monkeypatch):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        return ""

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("", "", None))

    mod._call_with_fallback(_options(), _context())

    assert calls == ["efficiency"]
