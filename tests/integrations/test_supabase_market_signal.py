import integrations.supabase_market_signal as market_signal_module
from core.market_trade_mode import PROBE_ONLY_REGIMES, merge_premarket_regime, resolve_market_trade_mode
from integrations.supabase_market_signal import (
    _merge_latest_market_signal_rows,
    compose_market_state,
    market_signal_readiness,
    upsert_market_signal_daily,
)


def test_missing_benchmark_is_unknown_and_fails_closed():
    state = compose_market_state({"benchmark_regime": None, "premarket_regime": "NORMAL"})

    assert state["benchmark_slot"] == "UNKNOWN"
    assert state["market_posture_code"] == "DATA_HOLD"
    assert "禁止新开仓" in state["action_phrase"]


def test_risk_on_banner_matches_overheat_execution_gate():
    state = compose_market_state({"benchmark_regime": "RISK_ON", "premarket_regime": "NORMAL"})

    assert state["market_posture_code"] == "OVERHEAT_HOLD"
    assert "禁止新开仓" in state["action_phrase"]


def test_benchmark_caution_is_not_collapsed_to_risk_off():
    state = compose_market_state({"benchmark_regime": "CAUTION", "premarket_regime": "NORMAL"})

    assert state["benchmark_slot"] == "CAUTION"
    assert state["market_posture_code"] == "PATIENT_OBSERVE"
    assert "PROBE" in state["action_phrase"]
    assert "禁止 ATTACK" in state["action_phrase"]


ALL_BENCHMARK_REGIMES = (
    "RISK_ON",
    "NEUTRAL",
    "CAUTION",
    "BEAR_REBOUND",
    "PANIC_REPAIR",
    "PANIC_REPAIR_CONFIRMED",
    "PANIC_REPAIR_INTRADAY",
    "RISK_OFF",
    "CRASH",
    "BLACK_SWAN",
)
ALL_PREMARKET_SLOTS = ("NORMAL", "CAUTION", "RISK_OFF", "BLACK_SWAN", "UNKNOWN", "")


def _assert_banner_matches_trade_mode(benchmark: str, premarket: str) -> None:
    state = compose_market_state({"benchmark_regime": benchmark, "premarket_regime": premarket})
    mode = resolve_market_trade_mode(merge_premarket_regime(benchmark, premarket))
    action = state["action_phrase"]
    where = f"{benchmark}+{premarket}"
    if not mode.allow_recommendation_write:
        assert "禁止新开仓" in action, f"{where} 横幅越过执行层闸门"
    else:
        assert "禁止新开仓" not in action, f"{where} 横幅比执行层更严"
        if mode.regime in PROBE_ONLY_REGIMES:
            assert "PROBE" in action and "禁止 ATTACK" in action, where


def test_banner_action_never_exceeds_effective_market_permission(monkeypatch):
    """横幅口径必须与执行层同源：oracle 走 trade mode，而不是自算 stricter + 硬集合。"""
    monkeypatch.delenv("STEP4_BUY_ALLOW_REGIMES", raising=False)
    for premarket in ALL_PREMARKET_SLOTS:
        for benchmark in ALL_BENCHMARK_REGIMES:
            _assert_banner_matches_trade_mode(benchmark, premarket)


def test_banner_follows_step4_buy_allow_regimes_exemption(monkeypatch):
    """运维豁免必须同时作用于横幅。

    生产 ``STEP4_BUY_ALLOW_REGIMES=BEAR_REBOUND``。此前横幅直接比
    ``EXECUTE_BLOCK_NEW_BUY_REGIMES``（BEAR_REBOUND 仍在其中），于是 08-07/08-17
    两天 trade mode 判「可执行买入」、横幅却写「禁止新开仓」。
    """
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")
    for premarket in ALL_PREMARKET_SLOTS:
        for benchmark in ALL_BENCHMARK_REGIMES:
            _assert_banner_matches_trade_mode(benchmark, premarket)

    state = compose_market_state({"benchmark_regime": "BEAR_REBOUND", "premarket_regime": "UNKNOWN"})
    assert "禁止新开仓" not in state["action_phrase"]


def test_banner_permission_reads_raw_regime_not_collapsed_slot(monkeypatch):
    """slot 会把 BEAR_REBOUND 压成 RISK_OFF，权限判定必须用未压缩的水温。"""
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")
    bear = compose_market_state({"benchmark_regime": "BEAR_REBOUND", "premarket_regime": "NORMAL"})
    risk_off = compose_market_state({"benchmark_regime": "RISK_OFF", "premarket_regime": "NORMAL"})

    assert bear["benchmark_slot"] == risk_off["benchmark_slot"] == "RISK_OFF"
    assert "禁止新开仓" not in bear["action_phrase"]
    assert "禁止新开仓" in risk_off["action_phrase"]


def test_premarket_gap_banner_follows_benchmark_not_data_hold():
    """盘前取数失败不再自己加闸门——执行层已回落 benchmark，文案必须跟上。

    08-07/08-17 两天就是这个组合（a50_pct_chg 缺失 → 盘前 UNKNOWN），此前横幅写
    「禁止新开仓，等待关键数据恢复」，与 trade mode 的「可执行买入」直接相反。
    """
    state = compose_market_state({"benchmark_regime": "NEUTRAL", "premarket_regime": "UNKNOWN"})

    assert state["premarket_slot"] == "UNKNOWN"
    assert state["market_posture_code"] == "PATIENT_OBSERVE"
    assert "禁止新开仓" not in state["action_phrase"]
    assert "隔夜" in state["wind_phrase"]


def test_premarket_gap_still_defers_to_hard_benchmark():
    state = compose_market_state({"benchmark_regime": "CRASH", "premarket_regime": ""})

    assert state["premarket_slot"] == "UNKNOWN"
    assert "禁止新开仓" in state["action_phrase"]


def test_invalid_benchmark_display_fails_closed_as_unknown():
    state = compose_market_state({"benchmark_regime": "typo", "premarket_regime": "NORMAL"})

    assert state["benchmark_slot"] == "UNKNOWN"
    assert state["market_posture_code"] == "DATA_HOLD"


def test_market_signal_readiness_distinguishes_partial_and_stale():
    assert market_signal_readiness({"trade_date": "2026-07-10"}, "2026-07-10")["status"] == "partial"
    assert (
        market_signal_readiness(
            {"trade_date": "2026-07-10", "benchmark_regime": "NEUTRAL"},
            "2026-07-10",
        )["reason"]
        == "当日盘前风险尚未就绪"
    )
    assert (
        market_signal_readiness(
            {"trade_date": "2026-07-09", "benchmark_regime": "NEUTRAL"},
            "2026-07-10",
        )["status"]
        == "stale"
    )
    assert (
        market_signal_readiness(
            {"trade_date": "2026-07-10", "benchmark_regime": "NEUTRAL", "premarket_regime": "UNKNOWN"},
            "2026-07-10",
        )["status"]
        == "partial"
    )


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

    def __init__(
        self,
        initial_row: dict,
        concurrent_write_after_reads: int | None = None,
    ):
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


def test_upsert_market_signal_daily_writes_repair_regimes_without_fallback(monkeypatch):
    fake_client = _FakeMarketSignalTable({"trade_date": "2026-06-20", "updated_at": "t0"})
    monkeypatch.setattr(market_signal_module, "is_supabase_admin_configured", lambda: True)
    monkeypatch.setattr(market_signal_module, "require_server_write_context", lambda *_a, **_k: None)
    monkeypatch.setattr(market_signal_module, "_get_supabase_admin_client", lambda: fake_client)

    ok = upsert_market_signal_daily("2026-06-20", {"benchmark_regime": "PANIC_REPAIR_CONFIRMED"})

    assert ok is True
    assert fake_client.writes[0]["benchmark_regime"] == "PANIC_REPAIR_CONFIRMED"


def test_upsert_market_signal_daily_scrubs_non_finite_numeric_fields(monkeypatch):
    """A NaN/Inf slipping in from an upstream calculation (e.g. a division by
    zero in benchmark breadth stats) must not reach Postgres as-is: some
    numeric columns/clients choke on NaN/Infinity in JSON. finite_float
    coerces it to NULL instead of writing a poisoned value.
    """
    fake_client = _FakeMarketSignalTable({"trade_date": "2026-06-20", "updated_at": "t0"})
    monkeypatch.setattr(market_signal_module, "is_supabase_admin_configured", lambda: True)
    monkeypatch.setattr(market_signal_module, "require_server_write_context", lambda *_a, **_k: None)
    monkeypatch.setattr(market_signal_module, "_get_supabase_admin_client", lambda: fake_client)

    ok = upsert_market_signal_daily(
        "2026-06-20",
        {"main_index_close": float("nan"), "vix_close": float("inf"), "a50_close": 13200.5},
    )

    assert ok is True
    written = fake_client.writes[0]
    assert written["main_index_close"] is None
    assert written["vix_close"] is None
    assert written["a50_close"] == 13200.5
