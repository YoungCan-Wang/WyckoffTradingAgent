from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from core.tail_buy.models import DECISION_BUY, DECISION_SKIP, TailBuyCandidate
from workflows import tail_buy_llm_overlay as overlay
from workflows.tail_buy_utils import current_time


class FakeDepthClient:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    def get_depth(self, code: str) -> dict:
        row = self.rows.get(code)
        if row is None:
            raise RuntimeError("missing depth")
        return row


def _candidate(code: str = "000001") -> TailBuyCandidate:
    return TailBuyCandidate(
        code=code,
        name="平安银行",
        signal_date="2026-06-21",
        status="confirmed",
        signal_type="sos",
        signal_score=80.0,
        rule_score=72.0,
        rule_decision=DECISION_BUY,
        features={"last_close": 12.3, "close_pos": 0.9, "dist_vwap_pct": 1.2},
    )


def test_fetch_depth_features_calculates_weibi() -> None:
    client = FakeDepthClient({"000001": {"bid_volumes": [10, 20], "ask_volumes": [5, 5]}})

    result = overlay.fetch_depth_features(client, [_candidate()], max_symbols=1, concurrency=1)

    assert result["000001"] == {"bid_total": 30, "ask_total": 10, "weibi": 50.0}


def test_apply_tail_buy_depth_filter_marks_heavy_sell_pressure_skip(monkeypatch) -> None:
    monkeypatch.setattr(overlay, "log_line", lambda *_args, **_kwargs: None)
    candidate = _candidate()
    client = FakeDepthClient({"000001": {"bid_volumes": [5], "ask_volumes": [95]}})
    config = SimpleNamespace(deadline_at=current_time() + timedelta(minutes=5), max_llm_symbols=5, logs_path="")

    depth_map = overlay.apply_tail_buy_depth_filter([candidate], tickflow_client=client, config=config)

    assert depth_map["000001"]["weibi"] == -90.0
    assert candidate.rule_decision == DECISION_SKIP
    assert candidate.rule_reasons == ["五档委比=-90.0%，卖压过重"]


def test_run_llm_overlay_parses_route_decision(monkeypatch) -> None:
    monkeypatch.setattr(overlay, "log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        overlay,
        "call_llm",
        lambda **_kwargs: '{"decision":"BUY","reason":"尾盘强势","risk":"回落","confidence":0.8}',
    )
    route = {"name": "eff:test", "provider": "efficiency", "model": "test", "api_key": "k", "base_url": "https://x"}

    decisions, total, ok_count, route_hits = overlay.run_llm_overlay(
        [_candidate()],
        llm_routes=[route],
        style="hybrid",
        max_llm_symbols=3,
        min_rule_score=60.0,
        allowed_rule_decisions=(DECISION_BUY,),
        llm_concurrency=1,
        deadline_at=current_time() + timedelta(minutes=5),
        logs_path="",
    )

    assert total == 1
    assert ok_count == 1
    assert decisions["000001"]["decision"] == DECISION_BUY
    assert route_hits == {"eff:test": 1}
