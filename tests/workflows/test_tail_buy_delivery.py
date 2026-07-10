from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from core.tail_buy.models import DECISION_BUY, TailBuyCandidate
from workflows import tail_buy_delivery as delivery
from workflows.tail_buy_runtime import TailBuyCandidateRun
from workflows.tail_buy_utils import TZ


def _candidate() -> TailBuyCandidate:
    return TailBuyCandidate(
        code="000001",
        name="平安银行",
        signal_date="2026-06-21",
        status="confirmed",
        signal_type="candidate",
        signal_score=81.0,
        rule_score=72.5,
        rule_decision=DECISION_BUY,
        rule_reasons=["站稳VWAP", "尾盘放量"],
        final_decision=DECISION_BUY,
        priority_score=88.0,
        features={"last_close": 12.34, "vwap": 12.1},
    )


def test_resolve_market_reminder_prefers_trade_date_row(monkeypatch) -> None:
    monkeypatch.setattr(
        delivery,
        "load_market_signal_daily",
        lambda _date: {"benchmark_regime": "risk_on", "premarket_regime": "neutral", "banner_message": "强势\n震荡"},
    )
    monkeypatch.setattr(delivery, "load_latest_market_signal_daily", lambda: None)

    assert delivery.resolve_market_reminder("2026-06-22") == "RISK_ON/NEUTRAL | 禁止新开仓（尾盘不买新票） | 强势 震荡"


def test_tail_buy_persist_row_serializes_reasons_and_features() -> None:
    started_at = datetime(2026, 6, 22, 14, 0, tzinfo=TZ)
    candidate = _candidate()
    candidate.features["daily_trap_reason"] = "日线放量上影(2.6x)"

    row = delivery.tail_buy_persist_row(candidate, started_at)

    assert row["initial_price"] == 12.34
    assert json.loads(row["rule_reasons"]) == ["站稳VWAP", "尾盘放量"]
    features = json.loads(row["features_json"])
    assert features["vwap"] == 12.1
    assert features["daily_trap_reason"] == "日线放量上影(2.6x)"
    assert features["execution_label"] == "可执行买入"
    assert features["execution_status"] == "executable_buy"
    assert features["orderable"] is True


def test_tail_buy_persist_row_downgrades_limit_up_candidate_to_watch_only() -> None:
    started_at = datetime(2026, 7, 8, 14, 44, tzinfo=TZ)
    candidate = _candidate()
    candidate.features["limit_up_touched"] = True

    row = delivery.tail_buy_persist_row(candidate, started_at)

    features = json.loads(row["features_json"])
    assert features["execution_label"] == "观察买入"
    assert features["execution_status"] == "watch_buy"
    assert features["orderable"] is False


def test_send_tail_buy_notifications_falls_back_from_rich_card(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("FEISHU_TAIL_BUY_RICH_CARD", "1")
    monkeypatch.setattr(delivery, "send_tail_buy_card", lambda *_args, **_kwargs: calls.append("rich") and False)
    monkeypatch.setattr(delivery, "send_feishu_notification", lambda *_args, **_kwargs: calls.append("text") or True)
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *_args, **_kwargs: calls.append("tg") or True)
    monkeypatch.setattr(delivery, "log_line", lambda *_args, **_kwargs: None)

    result = delivery.send_tail_buy_notifications(
        feishu_webhook="https://feishu.example",
        tg_bot_token="tg-token",
        tg_chat_id="chat",
        title="Tail Buy",
        report="report",
    )

    assert result == (True, True)
    assert calls == ["rich", "text", "tg"]


def test_send_tail_buy_report_builds_buy_only_report(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_markdown(**kwargs):
        captured.update(kwargs)
        return "markdown"

    monkeypatch.setattr(delivery, "build_tail_buy_markdown", fake_markdown)
    monkeypatch.setattr(delivery, "send_tail_buy_notifications", lambda **_kwargs: (True, False))

    config = SimpleNamespace(
        started_at=datetime(2026, 6, 22, 14, 0, tzinfo=TZ),
        feishu_webhook="feishu",
        tg_bot_token="",
        tg_chat_id="",
        logs_path="",
    )
    run_result = TailBuyCandidateRun(
        [_candidate()],
        1,
        1,
        {"efficiency:model": 1},
        "2026-06-22 14:00:00",
        {"lps": 0.5},
        {
            "source": "远端",
            "report_date": "2026-07-04",
            "horizon": "5",
            "age_days": 0,
            "execution_policy": "shadow",
            "execution_scope": "tail_buy_and_funnel_shadow",
            "next_action": "manual_review_dynamic_on",
            "formal_dynamic_allowed": True,
            "tail_buy_weights_active": True,
            "funnel_shadow_weights_active": True,
            "funnel_formal_weights_active": False,
        },
    )

    assert delivery.send_tail_buy_report(
        config=config,
        target_signal_date="2026-06-21",
        market_reminder="NEUTRAL",
        candidate_source_desc="signal_pending",
        holdings_section="holdings",
        run_result=run_result,
        elapsed=3.2,
    ) == (True, False)
    assert captured["buy_only"] is True
    assert captured["extra_sections"] == ["holdings"]
    assert captured["policy_weights"] == {"lps": 0.5}
    assert captured["policy_weight_meta"] == {
        "source": "远端",
        "report_date": "2026-07-04",
        "horizon": "5",
        "age_days": 0,
        "execution_policy": "shadow",
        "execution_scope": "tail_buy_and_funnel_shadow",
        "next_action": "manual_review_dynamic_on",
        "formal_dynamic_allowed": True,
        "tail_buy_weights_active": True,
        "funnel_shadow_weights_active": True,
        "funnel_formal_weights_active": False,
    }
