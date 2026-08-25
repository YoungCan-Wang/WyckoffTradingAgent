"""影子账本默认关：迁移未 apply 时不该每天在 summary 里留 failed_soft。"""

from __future__ import annotations

import pytest

from workflows import shadow_ledger_job as slj


class _Cfg:
    preview_only = False
    historical_replay = False
    logs_path = None


def _run(monkeypatch: pytest.MonkeyPatch) -> dict:
    return slj.run_shadow_ledger_stage(
        cfg=_Cfg(),
        step2_details={},
        symbols_info=[],
        step3_report_text="",
        benchmark_context={},
    )


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHADOW_LEDGER_ENABLED", raising=False)
    called: list[int] = []
    monkeypatch.setattr(slj, "_run_shadow_session", lambda *a, **k: called.append(1))

    result = _run(monkeypatch)

    assert "skipped" in str(result.get("output", "")) or "skipped" in str(result)
    assert called == [], "默认关时不应触碰 supabase"


def test_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_LEDGER_ENABLED", "1")
    monkeypatch.setattr(slj, "_run_shadow_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table")))

    result = _run(monkeypatch)

    # 开启后即便建表未完成，也只能软失败，不得阻断漏斗
    assert result["ok"] is True
    assert "no table" in str(result.get("err", ""))


def test_preview_still_skips_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_LEDGER_ENABLED", "1")

    class _Preview(_Cfg):
        preview_only = True

    called: list[int] = []
    monkeypatch.setattr(slj, "_run_shadow_session", lambda *a, **k: called.append(1))

    result = slj.run_shadow_ledger_stage(
        cfg=_Preview(),
        step2_details={},
        symbols_info=[],
        step3_report_text="",
        benchmark_context={},
    )

    assert "preview" in str(result)
    assert called == []
