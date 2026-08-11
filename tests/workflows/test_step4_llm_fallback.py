"""Step4 决策模型的备用 provider 与降级路径。

2026-08-09/10 漏斗连续两天失败，都倒在 Step4 的
「OpenAI 兼容接口返回内容为空」——一次异常就 return llm_failed，整条 OMS exit 1，
期间连持仓与止损状态都看不到。Step3 早有 fallback 机制，Step4 反而没有，而 Step4
的输出是直接驱动下单的那一环。
"""

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


def test_empty_content_falls_back_to_next_provider(monkeypatch):
    """主 provider 返回空串时应继续尝试备用，而不是直接失败。"""
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        return "" if len(calls) == 1 else '{"decisions": []}'

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    raw = mod._call_with_fallback(_options(), _context())

    assert raw == '{"decisions": []}'
    assert len(calls) >= 2


def test_exception_falls_back(monkeypatch):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        if len(calls) == 1:
            raise RuntimeError("OpenAI 兼容接口返回内容为空")
        return '{"decisions": []}'

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    assert mod._call_with_fallback(_options(), _context()) == '{"decisions": []}'


def test_all_providers_failing_returns_none(monkeypatch):
    monkeypatch.setattr(mod, "call_llm", lambda **kw: "")
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    assert mod._call_with_fallback(_options(), _context()) is None


def test_providers_without_key_are_skipped(monkeypatch):
    """缺配置的 provider 不应被计入尝试，避免无谓的失败日志。"""
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        return ""

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("", "", None))

    mod._call_with_fallback(_options(), _context())

    assert calls == ["efficiency"]
