from __future__ import annotations

import pytest

from integrations.tickflow_client import TickFlowClient


class FakeResponse:
    def __init__(self, status_code, text="", payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_request_honors_tickflow_rate_limit_wait(monkeypatch):
    client = TickFlowClient(api_key="test-key", max_retries=2)
    sleeps = []
    calls = []

    def fake_get(url, *, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        if len(calls) == 1:
            return FakeResponse(429, '{"code":"RATE_LIMITED","message":"实时行情限流 (60/min)，请 1234ms 后重试"}')
        return FakeResponse(200, payload={"data": []})

    monkeypatch.setattr("integrations.tickflow_client.requests.get", fake_get)
    monkeypatch.setattr("integrations.tickflow_client.time.sleep", sleeps.append)

    payload = client._request("/v1/quotes", params={"symbols": "AAPL.US"})

    assert payload == {"data": []}
    assert len(calls) == 2
    assert sleeps == [pytest.approx(1.734)]


def test_get_quotes_accepts_universe(monkeypatch):
    client = TickFlowClient(api_key="test-key")
    calls = []

    def fake_request(path, *, params=None, json_body=None, method="GET"):
        calls.append((path, params, json_body, method))
        return {"data": [{"symbol": "AAPL.US", "last_price": 205.0}]}

    monkeypatch.setattr(client, "_request", fake_request)

    quotes = client.get_quotes(universes=["US_Equity"])

    assert calls == [("/v1/quotes", {"universes": "US_Equity"}, None, "GET")]
    assert quotes["AAPL.US"]["last_price"] == 205.0


def test_get_quotes_chunks_symbols_at_tickflow_limit(monkeypatch):
    client = TickFlowClient(api_key="test-key")
    calls = []

    def fake_request(path, *, params=None, json_body=None, method="GET"):
        calls.append((path, params, json_body, method))
        assert method == "GET" and json_body is None
        return {"data": [{"symbol": symbol, "last_price": 1.0} for symbol in params["symbols"].split(",")]}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("integrations.tickflow_client.time.sleep", lambda _: None)

    symbols = [f"SYM{idx:03d}.US" for idx in range(121)]
    quotes = client.get_quotes(symbols)

    assert [len(call[1]["symbols"].split(",")) for call in calls] == [50, 50, 21]
    assert len(quotes) == 121


def test_get_klines_batch_parses_payload(monkeypatch):
    client = TickFlowClient(api_key="test-key")
    calls = []

    def fake_request(path, *, params=None):
        calls.append((path, params))
        return {
            "data": {
                "AAPL.US": {
                    "timestamp": [1704067200000, 1704153600000],
                    "open": [100.0, 101.0],
                    "high": [102.0, 103.0],
                    "low": [99.0, 100.0],
                    "close": [101.0, 102.0],
                    "prev_close": [99.0, 101.0],
                    "volume": [1000, 1200],
                    "amount": [101000.0, 122400.0],
                }
            }
        }

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.get_klines_batch(["AAPL.US"], period="1d", count=2, adjust="forward")

    assert calls == [
        (
            "/v1/klines/batch",
            {"symbols": "AAPL.US", "period": "1d", "count": 2, "adjust": "forward"},
        )
    ]
    assert list(result) == ["AAPL.US"]
    assert result["AAPL.US"]["close"].tolist() == [101.0, 102.0]


def test_get_klines_batch_splits_at_default_two_hundred(monkeypatch):
    """日K批次默认 200，不要压到 100。

    2026-09-04 用真 key 实测过 `/v1/klines/batch`：请求 100/101/150/200 只，返回数量与
    请求数一一相等，厂商在 100 这个数上没有任何限制。按生产真实参数 count=260 再测，
    100 只耗时 1.9s / 2101KB，200 只耗时 1.9s / 4114KB，客户端默认超时 12s，离得很远，
    所以超时也不构成压批次的理由。

    压到 100 是有代价的：调用次数翻倍，而 A 股漏斗配了
    ``TICKFLOW_KLINE_RATE_LIMIT_PER_MIN: 110``、每批之间还有 0.55s sleep。美股通道自己
    在 ``wyckoff_funnel_us.yml`` 里设了 ``TICKFLOW_KLINE_BATCH_SIZE: "100"``，那是那条
    通道的选择，不该提成客户端硬顶——硬顶会静默吃掉运维设的任何更大值，且不打日志。
    """
    client = TickFlowClient(api_key="test-key")
    sizes = []

    def fake_request(path, *, params=None):
        assert path == "/v1/klines/batch"
        sizes.append(len(params["symbols"].split(",")))
        return {"data": {}}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("integrations.tickflow_client.time.sleep", lambda _: None)
    client.get_klines_batch([f"{idx:06d}.SZ" for idx in range(220)], count=260, adjust="forward")
    assert sizes == [200, 20]


def test_get_quotes_deduplicates_universe_and_symbol_requests(monkeypatch):
    client = TickFlowClient(api_key="test-key")
    calls = []

    def fake_request(path, *, params=None):
        calls.append(params)
        return {"data": []}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("integrations.tickflow_client.time.sleep", lambda _: None)
    client.get_quotes(["00285.HK", "00285.HK"], universes=["CN_Equity_A", "CN_Equity_A"])
    assert calls == [{"universes": "CN_Equity_A"}, {"symbols": "00285.HK"}]


def test_get_financial_metrics_chunks_at_one_hundred(monkeypatch):
    client = TickFlowClient(api_key="test-key")
    calls = []

    def fake_request(path, *, params=None, json_body=None, method="GET"):
        calls.append((path, params, json_body, method))
        first_symbol = str(params["symbols"]).split(",", 1)[0]
        return {"data": {first_symbol: [{"roe": 0.12}]}}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("integrations.tickflow_client.time.sleep", lambda _: None)

    symbols = [f"{idx:06d}.SZ" for idx in range(205)]
    result = client.get_financial_metrics(symbols)

    assert len(calls) == 3
    assert [len(call[1]["symbols"].split(",")) for call in calls] == [100, 100, 5]
    assert len(result) == 3
