"""integrations/llm_client.py 的 LiteLLM 开关测试。"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestLiteLLMSwitch:
    """验证 LITELLM_ENABLED 环境变量路由逻辑。"""

    def test_one_route_defaults_to_api_base_url(self):
        """1Route 默认使用 API 网关域名。"""
        with patch.dict(os.environ, {"1ROUTE_API_KEY": "one-route-key"}, clear=True):
            from integrations.llm_client import get_provider_credentials

            api_key, model, base_url = get_provider_credentials("1route")

        assert api_key == "one-route-key"
        assert model == "gpt-5.5"
        assert base_url == "https://api.1route.dev/v1"

    def test_litellm_disabled_by_default(self):
        """不设 LITELLM_ENABLED 时，不走 LiteLLM。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LITELLM_ENABLED", None)
            with patch("integrations.llm_client._call_gemini", return_value="native reply") as mock_native:
                from integrations.llm_client import call_llm

                result = call_llm(
                    provider="gemini",
                    model="gemini-3.1-flash-lite-preview",
                    api_key="fake-key",
                    system_prompt="test",
                    user_message="hello",
                )
                assert result == "native reply"
                mock_native.assert_called_once()

    def test_litellm_enabled_routes_to_litellm(self):
        """LITELLM_ENABLED=1 且无 images 时，走 LiteLLM。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "1"}),
            patch(
                "integrations.llm_adapter.call_llm_via_litellm",
                return_value="litellm reply",
            ) as mock_litellm,
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="gemini",
                model="gemini-3.1-flash-lite-preview",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )
            assert result == "litellm reply"
            mock_litellm.assert_called_once()

    def test_litellm_enabled_true_string(self):
        """DeepSeek 始终走官方适配器，避免 LiteLLM 丢失推理契约。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "true"}),
            patch(
                "integrations.llm_adapter.call_llm_via_litellm",
                return_value="litellm reply",
            ) as mock_litellm,
            patch("integrations.llm_client._call_openai_compatible", return_value="native reply") as mock_native,
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="deepseek",
                model="deepseek-v4-flash",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )
            assert result == "native reply"
            mock_litellm.assert_not_called()
            mock_native.assert_called_once()

    def test_litellm_enabled_with_images_falls_back(self):
        """LITELLM_ENABLED=1 但带 images 时，降级为原生实现。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "1"}),
            patch("integrations.llm_client._call_gemini", return_value="native with images") as mock_native,
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="gemini",
                model="gemini-3.1-flash-lite-preview",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
                images=[b"fake_image_bytes"],
            )
            assert result == "native with images"
            mock_native.assert_called_once()

    def test_litellm_import_error_falls_back(self):
        """LiteLLM 未安装时，降级为原生实现。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "1"}),
            patch(
                "integrations.llm_adapter.call_llm_via_litellm",
                side_effect=ImportError("No module named 'litellm'"),
            ),
            patch("integrations.llm_client._call_gemini", return_value="fallback reply") as mock_native,
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="gemini",
                model="gemini-3.1-flash-lite-preview",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )
            assert result == "fallback reply"
            mock_native.assert_called_once()

    def test_validation_still_works_with_litellm(self):
        """即使 LITELLM_ENABLED=1，空 api_key 仍然报错。"""
        with patch.dict(os.environ, {"LITELLM_ENABLED": "1"}):
            from integrations.llm_client import call_llm

            with pytest.raises(ValueError, match="API Key 未配置"):
                call_llm(
                    provider="gemini",
                    model="test",
                    api_key="",
                    system_prompt="test",
                    user_message="hello",
                )

    def test_unsupported_provider_still_raises(self):
        """不支持的 provider 仍然报错，即使 LiteLLM 开关开启。"""
        with patch.dict(os.environ, {"LITELLM_ENABLED": "1"}):
            from integrations.llm_client import call_llm

            with pytest.raises(ValueError, match="不支持的供应商"):
                call_llm(
                    provider="unknown_provider",
                    model="test",
                    api_key="fake-key",
                    system_prompt="test",
                    user_message="hello",
                )

    def test_efficiency_requires_base_url(self):
        from integrations.llm_client import call_llm

        with pytest.raises(ValueError, match="base_url"):
            call_llm(
                provider="efficiency",
                model="cheap-model",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )


class TestGeminiTruncationHandling:
    @staticmethod
    def _install_fake_google_genai(response):
        google_mod = ModuleType("google")
        genai_mod = ModuleType("google.genai")
        types_mod = ModuleType("google.genai.types")
        generate = MagicMock(side_effect=response) if isinstance(response, list) else MagicMock(return_value=response)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.models = SimpleNamespace(generate_content=generate)
                genai_mod.last_client = self

        class FakeConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class FakeThinking:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        genai_mod.Client = FakeClient
        types_mod.GenerateContentConfig = FakeConfig
        types_mod.ThinkingConfig = FakeThinking
        genai_mod.types = types_mod
        google_mod.genai = genai_mod
        return {
            "google": google_mod,
            "google.genai": genai_mod,
            "google.genai.types": types_mod,
        }

    def test_gemini_truncation_can_return_text_when_allowed(self):
        from integrations.llm_client import _call_gemini

        response = SimpleNamespace(
            text='{"decision":"BUY","reason":"ok","risk":"low","confidence":0.8}',
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                total_token_count=120,
            ),
        )
        fake_modules = self._install_fake_google_genai(response)
        with patch.dict(sys.modules, fake_modules, clear=False):
            out = _call_gemini(
                model="gemini-3.8-flash",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
                images=None,
                timeout=30,
                max_output_tokens=256,
                allow_truncated_text=True,
                base_url="",
            )
        assert '"decision":"BUY"' in out
        config = fake_modules["google.genai"].last_client.models.generate_content.call_args.kwargs["config"]
        assert "temperature" not in config.kwargs
        assert "top_p" not in config.kwargs
        assert "top_k" not in config.kwargs

    def test_gemini_truncation_still_raises_by_default(self):
        from integrations.llm_client import _call_gemini

        response = SimpleNamespace(
            text='{"decision":"BUY"}',
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                total_token_count=120,
            ),
        )
        fake_modules = self._install_fake_google_genai(response)
        with (
            patch.dict(sys.modules, fake_modules, clear=False),
            patch("integrations.llm_client.time.sleep", return_value=None),
            pytest.raises(RuntimeError, match="Gemini 调用失败"),
        ):
            _call_gemini(
                model="gemini-3.8-flash",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
                images=None,
                timeout=30,
                max_output_tokens=256,
                allow_truncated_text=False,
                base_url="",
            )
        generate = fake_modules["google.genai"].last_client.models.generate_content
        assert generate.call_count == 2
        budgets = [call.kwargs["config"].max_output_tokens for call in generate.call_args_list]
        assert budgets == [1024, 2048]


def test_gemini_config_body_omits_sampling_params():
    from google.genai import types

    from integrations.llm_client import _accepted_gemini_thinking_level, _gemini_config

    level = _accepted_gemini_thinking_level("gemini-3.8-flash", "low")
    dumped = _gemini_config(types, "sys", 2048, level).model_dump(exclude_none=True)

    assert dumped["max_output_tokens"] == 2048
    assert dumped["thinking_config"]["thinking_level"] == types.ThinkingLevel.LOW
    assert "temperature" not in dumped
    assert "top_p" not in dumped
    assert "top_k" not in dumped
    plain = _gemini_config(types, "sys", 6000, None).model_dump(exclude_none=True)
    assert "temperature" not in plain
    assert "top_p" not in plain
    assert "top_k" not in plain
    assert "thinking_config" not in plain
    assert _accepted_gemini_thinking_level("gemini-3.8-flash", "minimal") is None
    assert _accepted_gemini_thinking_level("gemini-3.8-flash", "off") is None
    assert _accepted_gemini_thinking_level("gemini-2.5-flash-lite", "low") is None


def test_gemini_model_defaults_to_38_flash(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    from integrations._llm_types import DEFAULT_GEMINI_MODEL, GEMINI_MODELS
    from integrations.llm_client import get_provider_credentials

    assert DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert all("pro" not in model for model in GEMINI_MODELS)
    _key, model, _base = get_provider_credentials("gemini")
    assert model == "gemini-3.8-flash"


def test_empty_visible_text_expands_budget_once_then_returns_reply():
    from integrations.llm_client import _call_gemini

    truncated = SimpleNamespace(text="", candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")], usage_metadata=None)
    done = SimpleNamespace(
        text="次日推演：缩量止跌前只观察。",
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=None,
    )
    fake_modules = TestGeminiTruncationHandling._install_fake_google_genai([truncated, done])
    with patch.dict(sys.modules, fake_modules, clear=False):
        out = _call_gemini(
            model="gemini-3.8-flash",
            api_key="fake-key",
            system_prompt="test",
            user_message="hello",
            images=None,
            timeout=30,
            max_output_tokens=200,
            allow_truncated_text=False,
            base_url="",
            thinking_level="low",
        )
    assert out == "次日推演：缩量止跌前只观察。"
    generate = fake_modules["google.genai"].last_client.models.generate_content
    configs = [call.kwargs["config"] for call in generate.call_args_list]
    assert [config.max_output_tokens for config in configs] == [1024, 2048]
    assert all("temperature" not in config.kwargs for config in configs)
    assert all("top_p" not in config.kwargs for config in configs)
    assert all("top_k" not in config.kwargs for config in configs)
    assert configs[0].kwargs["thinking_config"].kwargs["thinking_level"] == "low"
