from __future__ import annotations

import json
import sys
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

from scripts import a_share_1455_quote_snapshot as snapshot_module
from scripts.a_share_1455_quote_snapshot import fetch_snapshot

_BEIJING = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 9, 29, 14, 55, 30, tzinfo=_BEIJING)


class _Response:
    def __init__(self, status: int, payload: dict, *, cf_ray: str = "ray-test") -> None:
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.headers = {"CF-RAY": cf_ray, "Date": "Tue, 29 Sep 2026 06:55:30 GMT"}

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.trust_env = True
        self.calls: list[dict] = []
        self.closed = False

    def get(self, url: str, **kwargs) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.response

    def close(self) -> None:
        self.closed = True


def _quote(index: int, *, price: float = 10, timestamp: datetime = _NOW) -> dict:
    return {
        "symbol": f"{index:06d}.SZ",
        "last_price": price,
        "prev_close": 10,
        "amount": 1_000_000 + index,
        "timestamp": int(timestamp.timestamp() * 1000),
        "region": "CN",
        "ext": {"type": "cn_equity", "name": f"公司{index}", "change_pct": (price / 10 - 1) * 100},
    }


def test_full_pool_is_one_direct_get_with_compact_first_output():
    rows = [_quote(index, price=9 if index < 30 else 10) for index in range(5300)]
    session = _Session(_Response(200, {"data": rows}))

    summary, candidates = fetch_snapshot("private-test-key", now=_NOW, session_factory=lambda: session)

    assert summary["ok"] is True
    assert summary["candidate_count"] == 30
    assert summary["coverage"] == 5300
    assert summary["valid_quote_count"] == 5300
    assert summary["candidate_count_basis"] == "provisional_price_screen_pending_corporate_action_check"
    assert summary["industry_counts"] == {"unmapped": 30}
    assert len(summary["candidates"]) == 20
    assert len(candidates) == 30
    assert len(json.dumps(summary, ensure_ascii=False).encode()) < 10_000
    assert "private-test-key" not in json.dumps(summary)
    assert session.trust_env is False
    assert session.closed is True
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://api.tickflow.org/v1/quotes"
    assert session.calls[0]["params"] == {"universes": "CN_Equity_A"}
    assert session.calls[0]["headers"] == {"x-api-key": "private-test-key"}
    assert session.calls[0]["allow_redirects"] is False


def test_403_stops_after_one_request_and_reports_safe_diagnostics():
    session = _Session(_Response(403, {"code": "FORBIDDEN", "message": "private-test-key"}))

    summary, candidates = fetch_snapshot("private-test-key", now=_NOW, session_factory=lambda: session)

    assert candidates == []
    assert summary["ok"] is False
    assert summary["failure"] == "HTTP_ERROR"
    assert summary["http_status"] == 403
    assert summary["error_code"] == "FORBIDDEN"
    assert summary["cf_ray"] == "ray-test"
    assert "private-test-key" not in json.dumps(summary)
    assert len(session.calls) == 1


def test_partial_or_duplicate_pool_is_not_accepted():
    rows = [_quote(index) for index in range(5299)] + [_quote(0)]
    session = _Session(_Response(200, {"data": rows}))

    summary, _ = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["ok"] is False
    assert summary["duplicate_count"] == 1
    assert summary["failure"] == "DATA_INCOMPLETE"


def test_old_illiquid_candidate_does_not_stale_the_active_market():
    old = datetime(2026, 9, 29, 14, 50, tzinfo=_BEIJING)
    rows = [_quote(index) for index in range(5300)]
    rows[0] = _quote(0, price=9, timestamp=old)
    session = _Session(_Response(200, {"data": rows}))

    summary, candidates = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["ok"] is True
    assert summary["candidates"][0]["symbol"] == "000000.SZ"
    assert summary["candidates"][0]["timestamp"] == old.isoformat(timespec="seconds")
    assert candidates[0]["needs_recheck"] is True
    assert candidates[0]["age_seconds"] == 330


def test_vendor_pct_disagreement_requires_recheck():
    rows = [_quote(index) for index in range(5300)]
    rows[0] = _quote(0, price=9)
    rows[0]["ext"]["change_pct"] = -1
    session = _Session(_Response(200, {"data": rows}))

    summary, candidates = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["quote_pct_mismatch_count"] == 1
    assert candidates[0]["needs_recheck"] is True


def test_malformed_payload_cannot_look_like_zero_candidates():
    session = _Session(_Response(200, {"data": {"rows": []}}))

    summary, candidates = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["ok"] is False
    assert summary["failure"] == "INVALID_PAYLOAD"
    assert candidates == []


def test_small_pool_fails_coverage_check_even_without_duplicates():
    session = _Session(_Response(200, {"data": [_quote(index) for index in range(100)]}))

    summary, _ = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["failure"] == "DATA_INCOMPLETE"
    assert summary["coverage"] == 100


def test_malformed_quotes_cannot_fill_the_coverage_floor():
    rows = [_quote(index) for index in range(5200)]
    rows[0]["last_price"] = None
    session = _Session(_Response(200, {"data": rows}))

    summary, _ = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["coverage"] == 5200
    assert summary["valid_quote_count"] == 5199
    assert summary["failure"] == "DATA_INCOMPLETE"


def test_non_equity_row_is_not_a_deep_drop_candidate():
    rows = [_quote(index) for index in range(5301)]
    rows[0]["ext"]["type"] = "cn_etf"
    rows[0]["last_price"] = 9
    session = _Session(_Response(200, {"data": rows}))

    summary, candidates = fetch_snapshot("test", now=_NOW, session_factory=lambda: session)

    assert summary["ok"] is True
    assert summary["coverage"] == 5300
    assert summary["valid_quote_count"] == 5300
    assert summary["null_count"] == 1
    assert candidates == []


def test_interactive_pages_keep_full_candidates_in_memory(monkeypatch, capsys):
    candidates = [{"symbol": f"{index:06d}.SZ"} for index in range(25)]
    summary = {"ok": True, "candidates": candidates[:20], "candidate_count": 25}
    monkeypatch.setenv("TICKFLOW_API_KEY", "private-test-key")
    monkeypatch.setattr(snapshot_module, "fetch_snapshot", lambda key: (summary, candidates))
    monkeypatch.setattr(sys, "stdin", StringIO("page 1\nquit\n"))
    monkeypatch.setattr(sys, "argv", ["snapshot", "--interactive"])

    assert snapshot_module.main() == 0

    first, second = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert len(first["candidates"]) == 20
    assert second["page"] == 1
    assert len(second["candidates"]) == 5
