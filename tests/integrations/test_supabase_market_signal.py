import integrations.supabase_market_signal as market_signal_module
from integrations.supabase_market_signal import _merge_latest_market_signal_rows, upsert_market_signal_daily


def test_merge_latest_market_signal_rows_uses_latest_available_source_blocks():
    merged = _merge_latest_market_signal_rows(
        [
            {
                "trade_date": "2026-06-20",
                "premarket_regime": "NORMAL",
                "banner_title": "自定义标题",
                "banner_message": "自定义正文",
                "banner_tone": "custom",
            },
            {
                "trade_date": "2026-06-19",
                "benchmark_regime": "RISK_OFF",
                "main_index_code": "000001.SH",
                "main_index_close": 3000.12,
                "main_index_ma50": 3050.0,
                "main_index_ma200": 2900.0,
            },
            {
                "trade_date": "2026-06-18",
                "a50_value_date": "2026-06-18",
                "a50_close": 13200.5,
                "a50_pct_chg": -0.8,
                "vix_value_date": "2026-06-18",
                "vix_close": 18.2,
                "vix_pct_chg": 3.1,
            },
        ]
    )

    assert merged is not None
    assert merged["trade_date"] == "2026-06-19"
    assert merged["benchmark_regime"] == "RISK_OFF"
    assert merged["premarket_regime"] == "NORMAL"
    assert merged["a50_close"] == 13200.5
    assert merged["vix_close"] == 18.2
    assert merged["banner_title"] == "自定义标题"
    assert merged["banner_message"] == "自定义正文"
    assert merged["banner_tone"] == "custom"


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, table: "_FakeMarketSignalTable", op: str, payload=None):
        self._table = table
        self._op = op
        self._payload = payload

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, _column, value):
        self._filter_value = value
        return self

    def limit(self, _n):
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        return self

    def execute(self):
        if self._op == "upsert":
            self._table.on_upsert(self._payload)
            return _FakeResponse([self._payload])
        row = self._table.row_for(self._filter_value)
        return _FakeResponse([row] if row else [])


class _FakeMarketSignalTable:
    """Simulates a single market_signal_daily row plus a concurrent writer that mutates
    updated_at right after this process's first read, to exercise the optimistic-lock retry."""

    def __init__(self, initial_row: dict, concurrent_write_after_reads: int | None = None):
        self.row = dict(initial_row)
        self.reads = 0
        self.writes: list[dict] = []
        self._concurrent_write_after_reads = concurrent_write_after_reads

    def table(self, _name):
        return _FakeQuery(self, "select")

    def row_for(self, trade_date):
        self.reads += 1
        if self._concurrent_write_after_reads == self.reads:
            self.row["updated_at"] = "concurrent-writer-timestamp"
        return dict(self.row) if self.row.get("trade_date") == trade_date else None

    def on_upsert(self, payload: dict) -> None:
        self.writes.append(dict(payload))
        self.row = dict(payload)


def test_upsert_market_signal_daily_retries_on_concurrent_update(monkeypatch):
    fake_client = _FakeMarketSignalTable(
        {"trade_date": "2026-06-20", "benchmark_regime": "RISK_ON", "updated_at": "t0"},
        concurrent_write_after_reads=1,
    )
    monkeypatch.setattr(market_signal_module, "is_supabase_admin_configured", lambda: True)
    monkeypatch.setattr(market_signal_module, "require_server_write_context", lambda *_a, **_k: None)
    monkeypatch.setattr(market_signal_module, "_get_supabase_admin_client", lambda: fake_client)

    ok = upsert_market_signal_daily("2026-06-20", {"vix_close": 18.5})

    assert ok is True
    # First read observes updated_at=t0; the injected concurrent write then changes it before
    # the pre-write check, so the stale merge must be discarded and retried with fresh data.
    assert fake_client.reads >= 2
    assert len(fake_client.writes) == 1
    assert fake_client.writes[0]["vix_close"] == 18.5
    assert fake_client.writes[0]["benchmark_regime"] == "RISK_ON"
