"""影子账本默认关：迁移未 apply 时不该每天在 summary 里留 failed_soft。"""

from __future__ import annotations

from datetime import date

import pytest

from core.shadow_ledger import ShadowBook, ShadowSession
from integrations import supabase_shadow
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


def test_shadow_buy_candidates_use_the_execution_regime() -> None:
    item = {
        "code": "000001",
        "name": "平安银行",
        "signal_type": "trend_breakout",
        "signal_status": "confirmed",
        "status": "confirmed",
        "selection_source": "signal_confirmed",
    }

    assert [row["code"] for row in slj._buy_candidates([item], "", {"regime": "NEUTRAL"})] == ["000001"]
    assert slj._buy_candidates([item], "", {"regime": "BEAR_REBOUND"}) == []


def test_shadow_buy_candidates_carry_the_route_rule() -> None:
    item = {
        "code": "000001",
        "name": "平安银行",
        "signal_type": "trend_lane_pullback",
        "signal_status": "confirmed",
        "status": "confirmed",
        "selection_source": "signal_confirmed",
    }
    context = {
        "regime": "NEUTRAL",
        "regime_route_shadow": {"mode": "shadow", "regime": "CAUTION", "rule": "bull_pullback"},
    }

    assert slj._buy_candidates([item], "", context)[0]["regime_route_rule"] == "bull_pullback"


def test_card_reads_closed_trades_after_annotation_and_survives_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Live(_Cfg):
        webhook = "https://example.test/hook"

    order: list[str] = []
    sent: list[str] = []
    trip = {"code": "002292", "name": "奥飞娱乐", "buy_date": "2026-09-01", "buy_price": 10.0}
    trip |= {"sell_date": "2026-09-10", "sell_price": 11.0}
    events = [{"event_type": "sell", "payload": {"round_trip": trip}}]
    session = ShadowSession(book=ShadowBook(cash=1.0, positions={}), fills=[], new_plans=[], nav={})
    monkeypatch.setenv("SHADOW_LEDGER_ENABLED", "1")
    monkeypatch.setattr(slj, "_as_of", lambda: date(2026, 9, 18))
    monkeypatch.setattr(slj, "_run_shadow_session", lambda *a, **k: session)
    monkeypatch.setattr(slj, "_persist_session", lambda _s: None)
    monkeypatch.setattr(slj, "_annotate_round_trips", lambda _cfg: order.append("annotate") or "ok")
    monkeypatch.setattr(slj, "send_feishu_notification", lambda _hook, _title, content: sent.append(content) or True)

    def _load(_account: str) -> list[dict]:
        order.append("load")
        return events

    monkeypatch.setattr(supabase_shadow, "load_filled_trade_events", _load)
    kwargs = {"step2_details": {}, "symbols_info": [], "step3_report_text": "", "benchmark_context": {}}
    assert slj.run_shadow_ledger_stage(cfg=_Live(), **kwargs)["err"] is None
    assert order == ["annotate", "load"]
    assert "002292 奥飞娱乐  买入 10.00 (2026-09-01)" in sent[-1]

    monkeypatch.setattr(
        supabase_shadow, "load_filled_trade_events", lambda _a: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert slj.run_shadow_ledger_stage(cfg=_Live(), **kwargs)["err"] is None
    assert "已清仓读取失败" in sent[-1]
