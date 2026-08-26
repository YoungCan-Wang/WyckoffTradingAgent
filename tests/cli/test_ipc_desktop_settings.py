"""桌面端外观配置与语气注入。

核心不变量：默认语气档必须让系统提示词保持字节不变，桌面端因此与 TUI 一致。
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.ipc.methods import DESKTOP_APPEARANCE_DEFAULTS, MethodError, dispatch
from cli.ipc.tone import tone_suffix


def _result(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for event in dispatch(method, params or {}):
        if event.get("type") == "result":
            return event
    raise AssertionError(f"{method} 没有产生 result 事件")


class TestToneSuffix:
    def test_default_appends_nothing(self) -> None:
        """默认档不动提示词——桌面端语气与 TUI 完全一致。"""
        from core.prompts import CHAT_AGENT_SYSTEM_PROMPT

        assert tone_suffix("default") == ""
        assert CHAT_AGENT_SYSTEM_PROMPT + tone_suffix("default") == CHAT_AGENT_SYSTEM_PROMPT

    def test_unknown_tone_falls_back_to_nothing(self) -> None:
        assert tone_suffix("nonsense") == ""

    @pytest.mark.parametrize("tone", ["brief", "detailed", "evidence"])
    def test_presets_append_text(self, tone: str) -> None:
        assert tone_suffix(tone).strip()

    def test_presets_never_relax_risk_rules(self) -> None:
        """语气档只管怎么说，不得给出放松风险提示的许可。"""
        for tone in ("brief", "detailed", "evidence"):
            text = tone_suffix(tone)
            assert "不需要风险" not in text
            assert "省略风险" not in text

    def test_custom_blank_appends_nothing(self) -> None:
        assert tone_suffix("custom", "   \n ") == ""

    def test_custom_text_is_injected_with_guardrail(self) -> None:
        out = tone_suffix("custom", "多用比喻")
        assert "多用比喻" in out
        # 自定义文本前必须有护栏说明，否则用户可写出削弱纪律的指令。
        assert "不得放松风险提示" in out


class TestDesktopSettings:
    def test_defaults_are_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("integrations.local_auth.load_config", lambda: {})
        monkeypatch.setattr("integrations.local_auth.load_model_configs", lambda: [])
        result = _result("settings_get")
        for key, default in DESKTOP_APPEARANCE_DEFAULTS.items():
            assert result[key] == default

    def test_stored_values_override_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "integrations.local_auth.load_config",
            lambda: {"desktop_appearance": "dark", "desktop_font_scale": 120},
        )
        monkeypatch.setattr("integrations.local_auth.load_model_configs", lambda: [])
        result = _result("settings_get")
        assert result["desktop_appearance"] == "dark"
        assert result["desktop_font_scale"] == 120

    def test_appearance_rejects_bad_choice(self) -> None:
        with pytest.raises(MethodError) as excinfo:
            list(dispatch("settings_set", {"key": "desktop_appearance", "value": "neon"}))
        assert excinfo.value.code == "invalid_value"

    def test_font_scale_is_clamped_not_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saved: dict[str, Any] = {}
        monkeypatch.setattr(
            "integrations.local_auth.save_config_key",
            lambda key, value: saved.__setitem__(key, value),
        )
        _result("settings_set", {"key": "desktop_font_scale", "value": 9999})
        assert saved["desktop_font_scale"] == 140
        _result("settings_set", {"key": "desktop_font_scale", "value": 10})
        assert saved["desktop_font_scale"] == 80

    def test_font_scale_rejects_non_numeric(self) -> None:
        with pytest.raises(MethodError) as excinfo:
            list(dispatch("settings_set", {"key": "desktop_font_scale", "value": "big"}))
        assert excinfo.value.code == "invalid_value"

    def test_custom_tone_is_length_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自定义语气会拼进系统提示词，必须限长以免挤掉真正的指令。"""
        saved: dict[str, Any] = {}
        monkeypatch.setattr(
            "integrations.local_auth.save_config_key",
            lambda key, value: saved.__setitem__(key, value),
        )
        _result("settings_set", {"key": "desktop_tone_custom", "value": "长" * 5000})
        assert len(saved["desktop_tone_custom"]) == 600

    def test_booleans_are_coerced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saved: dict[str, Any] = {}
        monkeypatch.setattr(
            "integrations.local_auth.save_config_key",
            lambda key, value: saved.__setitem__(key, value),
        )
        _result("settings_set", {"key": "desktop_reduce_motion", "value": 1})
        assert saved["desktop_reduce_motion"] is True

    def test_desktop_keys_do_not_collide_with_tui_theme(self) -> None:
        """TUI 用 theme 存 textual-dark，桌面端必须用自己的键。"""
        assert "theme" not in DESKTOP_APPEARANCE_DEFAULTS
        assert all(k.startswith("desktop_") for k in DESKTOP_APPEARANCE_DEFAULTS)
