from __future__ import annotations

import pandas as pd
import pytest


def test_call_track_report_falls_back_to_efficiency_after_gemini_failure(monkeypatch):
    import workflows.step3_llm as step3_llm
    from workflows.step3_llm import call_track_report
    from workflows.step3_runtime_config import Step3RuntimeConfig

    calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://efficiency.example/v1")

    def fake_call_llm(**kwargs):
        calls.append((kwargs["provider"], kwargs["model"], kwargs.get("base_url")))
        if kwargs["provider"] == "gemini":
            raise RuntimeError("gemini unavailable")
        return "## 💀 逻辑破产\n- 无\n\n## ⏳ 储备营地\n- 无\n\n## 🏹 处于起跳板\n- 000001"

    monkeypatch.setattr(step3_llm, "call_llm", fake_call_llm)

    ok, report, used_model = call_track_report(
        track="Trend",
        system_prompt="system",
        user_message="user",
        model="gemini-main",
        api_key="gem-key",
        selected_codes=["000001"],
        selected_df=pd.DataFrame([{"code": "000001"}]),
        provider="gemini",
        runtime_config=Step3RuntimeConfig(gemini_model_fallback="gemini-backup"),
    )

    assert ok is True
    assert "处于起跳板" in report
    assert used_model == "Efficiency:eff-model"
    assert calls == [
        ("gemini", "gemini-main", None),
        ("gemini", "gemini-backup", None),
        ("efficiency", "eff-model", "https://efficiency.example/v1"),
    ]


def test_call_track_report_falls_back_to_gemini_after_efficiency_failure(monkeypatch):
    import workflows.step3_llm as step3_llm
    from workflows.step3_llm import call_track_report
    from workflows.step3_runtime_config import Step3RuntimeConfig

    calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-main")

    def fake_call_llm(**kwargs):
        calls.append((kwargs["provider"], kwargs["model"], kwargs.get("base_url")))
        if kwargs["provider"] == "efficiency":
            raise RuntimeError("efficiency unavailable")
        return "## 💀 逻辑破产\n- 无\n\n## ⏳ 储备营地\n- 无\n\n## 🏹 处于起跳板\n- 000001"

    monkeypatch.setattr(step3_llm, "call_llm", fake_call_llm)

    ok, report, used_model = call_track_report(
        track="Trend",
        system_prompt="system",
        user_message="user",
        model="eff-model",
        api_key="eff-key",
        selected_codes=["000001"],
        selected_df=pd.DataFrame([{"code": "000001"}]),
        provider="efficiency",
        llm_base_url="https://efficiency.example/v1",
        runtime_config=Step3RuntimeConfig(),
    )

    assert ok is True
    assert "处于起跳板" in report
    assert used_model == "Gemini:gemini-main"
    assert calls == [
        ("efficiency", "eff-model", "https://efficiency.example/v1"),
        ("gemini", "gemini-main", None),
    ]


def test_step3_llm_routes_include_gemini_when_efficiency_primary(monkeypatch):
    from workflows.step3_llm import build_step3_llm_routes

    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-main")

    routes = build_step3_llm_routes(
        provider="efficiency",
        model="eff-model",
        api_key="eff-key",
        llm_base_url="https://efficiency.example/v1",
    )

    assert [route["provider"] for route in routes] == ["efficiency", "gemini"]
    assert routes[1]["model"] == "gemini-main"


def test_step3_llm_routes_allow_efficiency_when_gemini_key_missing(monkeypatch):
    from workflows.step3_llm import build_step3_llm_routes

    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://efficiency.example/v1")

    routes = build_step3_llm_routes(
        provider="gemini",
        model="gemini-main",
        api_key="",
        llm_base_url="",
    )

    assert routes == [
        {
            "provider": "efficiency",
            "model": "eff-model",
            "api_key": "eff-key",
            "base_url": "https://efficiency.example/v1",
        }
    ]


def test_step3_runtime_config_from_env_normalizes_values(monkeypatch):
    from workflows.step3_runtime_config import step3_runtime_config_from_env

    monkeypatch.setenv("STEP3_TRADING_DAYS", "0")
    monkeypatch.setenv("STEP3_MAX_OUTPUT_TOKENS", "bad")
    monkeypatch.setenv("STEP3_MAX_AI_INPUT", "-4")
    monkeypatch.setenv("STEP3_EMPTY_COMPRESSION_FALLBACK_CAP", "-2")
    monkeypatch.setenv("STEP3_ENTRY_QUALITY_TIE_BUCKET", "0.5")
    monkeypatch.setenv("STEP3_ENABLE_RAG_VETO", "off")
    monkeypatch.setenv("STEP3_SKIP_LLM", "yes")
    monkeypatch.setenv("STEP3_GEMINI_MODEL_FALLBACK", "gemini-backup")
    monkeypatch.setenv("STEP3_LLM_FALLBACK_PROVIDERS", "efficiency, gemini")
    monkeypatch.setenv("STEP3_HISTORY_MAX_WORKERS", "0")

    cfg = step3_runtime_config_from_env()

    assert cfg.trading_days == 1
    assert cfg.max_output_tokens == 6000
    assert cfg.max_ai_input == 0
    assert cfg.empty_compression_fallback_cap == 0
    assert cfg.entry_quality_tie_bucket == 0.5
    assert cfg.enable_rag_veto is False
    assert cfg.skip_llm is True
    assert cfg.gemini_model_fallback == "gemini-backup"
    assert cfg.llm_fallback_providers == ("efficiency", "gemini")
    assert cfg.history_max_workers == 1


def test_step3_runtime_config_rejects_legacy_report_style(monkeypatch):
    from workflows.step3_runtime_config import step3_runtime_config_from_env

    monkeypatch.setenv("STEP3_REPORT_STYLE", "legacy")

    with pytest.raises(RuntimeError, match="legacy 口径已禁用"):
        step3_runtime_config_from_env()


class TestModelFooter:
    """研报尾部标注实际使用模型。

    此前该信息只 print 到 stdout，要翻 CI 日志才能看到，导致"这份研报是哪个模型
    生成的、有没有走 fallback"在阅读时不可见。模型选型直接决定 API 成本与输出
    风格——2026-08 排查 Gemini 月费时就是因为看不到实际模型而只能靠猜。
    """

    @staticmethod
    def _result(used_models: dict[str, str]):
        from workflows.step3_models import Step3LlmResult

        return Step3LlmResult(ok=True, status="", report="x", used_models=used_models)

    def test_footer_lists_model_per_track(self):
        from workflows.step3_reporting import build_model_footer

        footer = build_model_footer(
            self._result({"accum": "Gemini:gemini-3-flash-preview", "trend": "Gemini:gemini-3-flash-preview"}),
            ["accum", "trend"],
            "fallback",
        )

        assert "gemini-3-flash-preview" in footer
        assert "accum=" in footer and "trend=" in footer
        # 各轨一致时不应报警
        assert "不一致" not in footer

    def test_footer_flags_track_level_fallback(self):
        """各轨模型不一致意味着有轨道回退了——这正是需要被看见的信号。"""
        from workflows.step3_reporting import build_model_footer

        footer = build_model_footer(
            self._result({"accum": "Gemini:gemini-3-pro-preview", "trend": "DeepSeek:deepseek-v4-flash"}),
            ["accum", "trend"],
            "fallback",
        )

        assert "不一致" in footer
        assert "gemini-3-pro-preview" in footer and "deepseek-v4-flash" in footer

    def test_footer_falls_back_to_options_model(self):
        from workflows.step3_reporting import build_model_footer

        footer = build_model_footer(self._result({}), ["accum"], "Gemini:gemini-2.5-flash-lite")

        assert "gemini-2.5-flash-lite" in footer

    def test_footer_empty_without_tracks(self):
        from workflows.step3_reporting import build_model_footer

        assert build_model_footer(self._result({}), [], "x") == ""
