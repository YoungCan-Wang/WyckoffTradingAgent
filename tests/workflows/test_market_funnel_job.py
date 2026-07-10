from __future__ import annotations

import json

import pandas as pd

import workflows.market_funnel_data as market_data
import workflows.market_funnel_job as market_job
import workflows.market_funnel_tracking as market_tracking
from workflows.market_funnel_job import _candidate_rows, run_market_funnel
from workflows.market_funnel_runtime import runtime_config_from_env


def _daily_frame(rows: int = 230) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=rows, freq="B")
    close = pd.Series(range(rows), dtype="float64") * 0.2 + 100.0
    open_ = close - 0.5
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([1_000_000 + i * 1000 for i in range(rows)], dtype="float64")
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "pct_chg": close.pct_change().fillna(0.0) * 100.0,
        }
    )


def test_run_layers_passes_l2_channel_map_to_l4(monkeypatch):
    captured: dict[str, object] = {}
    bench = _daily_frame()
    sector_map = {"AAPL.US": "Technology"}

    def fake_layer2(symbols, _df_map, bench_df, *_args, **_kwargs):
        captured["bench_df"] = bench_df
        return (["AAPL.US"], {"AAPL.US": "趋势延续"}, [])

    def fake_layer3(symbols, sectors, *_args, **_kwargs):
        captured["sector_map"] = sectors
        return (symbols, [("Technology", 1)])

    def fake_layer4(symbols, _df_map, _cfg, *, channel_map=None, **_kwargs):
        captured["symbols"] = symbols
        captured["channel_map"] = channel_map
        return {"trend_pullback": [(symbols[0], 0.25)]}

    monkeypatch.setattr(market_job, "layer1_filter", lambda symbols, *_args, **_kwargs: symbols)
    monkeypatch.setattr(market_job, "layer2_strength_detailed", fake_layer2)
    monkeypatch.setattr(market_job, "layer3_sector_resonance", fake_layer3)
    monkeypatch.setattr(market_job, "layer4_triggers", fake_layer4)

    triggers, metrics = market_job._run_layers(
        ["AAPL.US"],
        {"AAPL.US": "Apple"},
        {"AAPL.US": _daily_frame()},
        runtime_config_from_env("us", None),
        bench_df=bench,
        sector_map=sector_map,
    )

    assert captured["bench_df"] is bench
    assert captured["sector_map"] == sector_map
    assert captured["symbols"] == ["AAPL.US"]
    assert captured["channel_map"] == {"AAPL.US": "趋势延续"}
    assert triggers["trend_pullback"] == [("AAPL.US", 0.25)]
    assert metrics["by_trigger"] == {"trend_pullback": 1}
    assert metrics["sector_coverage"] == 1


def test_candidate_rows_keep_raw_trigger_strength():
    rows = _candidate_rows(
        {"sos": [("BZFD.US", 535.7), ("WOK.US", 26.3), ("QUBT.US", 11.23)]},
        name_map={"BZFD.US": "BuzzFeed", "WOK.US": "WORK Medical Tech", "QUBT.US": "Quantum Computing"},
        df_map={
            "BZFD.US": pd.DataFrame({"date": ["2025-01-01"], "close": [1.39]}),
            "WOK.US": pd.DataFrame({"date": ["2025-01-01"], "close": [6.66]}),
            "QUBT.US": pd.DataFrame({"date": ["2025-01-01"], "close": [11.78]}),
        },
    )

    assert [row["symbol"] for row in rows] == ["BZFD.US", "WOK.US", "QUBT.US"]
    assert [row["score"] for row in rows] == [535.7, 26.3, 11.23]
    assert rows[0]["latest_trade_date"] == 20250101


def test_candidate_rows_sanitize_invalid_scores():
    rows = _candidate_rows(
        {"sos": [("BAD.US", "bad"), ("INF.US", float("inf")), ("NAN.US", float("nan")), ("GOOD.US", 4.25)]},
        name_map={},
        df_map={},
    )

    assert rows[0]["symbol"] == "GOOD.US"
    assert {row["symbol"]: row["score"] for row in rows} == {
        "BAD.US": 0.0,
        "INF.US": 0.0,
        "NAN.US": 0.0,
        "GOOD.US": 4.25,
    }


def test_rank_quotes_prefers_liquidity_not_absolute_change():
    ranked = market_data.rank_quotes(
        {
            "SAFE.US": {"last_price": 20, "amount": 100, "volume": 1, "ext": {"change_pct": 0.5}},
            "MOVE.US": {"last_price": 20, "amount": 90, "volume": 999, "ext": {"change_pct": -20}},
        },
        max_symbols=2,
        min_quote_amount=0.0,
        min_quote_price=1.0,
    )

    assert [row["symbol"] for row in ranked] == ["SAFE.US", "MOVE.US"]


def test_rank_quotes_filters_low_price_symbols():
    ranked = market_data.rank_quotes(
        {
            "PENNY.HK": {"last_price": 0.25, "amount": 99_000_000, "volume": 1_000_000},
            "BLUE.HK": {"last_price": 10, "amount": 1_000_000, "volume": 100_000},
        },
        max_symbols=5,
        min_quote_amount=0.0,
        min_quote_price=1.0,
    )

    assert [row["symbol"] for row in ranked] == ["BLUE.HK"]


def test_market_regime_crash_caps_candidates_and_tightens_strength():
    cfg = market_job.funnel_config_for_market("us", trading_days=230)
    bench = _daily_frame()
    bench.loc[bench.index[-1], "pct_chg"] = -4.0
    context = market_job._market_regime_context(bench, cfg)

    assert context["regime"] == "CRASH"
    assert context["candidate_cap"] == 0
    assert cfg.rps_fast_min >= 85.0


class FakeTickFlowClient:
    def __init__(self) -> None:
        self.quote_batches: list[list[str]] = []
        self.kline_batches: list[list[str]] = []

    def get_quotes(self, symbols=None, *, universes=None):
        assert universes is None
        self.quote_batches.append(list(symbols or []))
        quotes = {
            "00700.HK": {
                "symbol": "00700.HK",
                "last_price": 350.0,
                "amount": 9_000_000.0,
                "ext": {"name": "Tencent", "change_pct": 0.01},
            },
            "00005.HK": {
                "symbol": "00005.HK",
                "last_price": 65.0,
                "amount": 8_000_000.0,
                "ext": {"name": "HSBC", "change_pct": -0.005},
            },
            "09999.HK": {"symbol": "09999.HK", "last_price": 0.0, "amount": 10_000_000.0},
        }
        return {symbol: quotes[symbol] for symbol in symbols or [] if symbol in quotes}

    def get_klines_batch(self, symbols, *, period, count, adjust):
        self.kline_batches.append(list(symbols))
        assert period == "1d"
        assert count == 230
        assert adjust == "forward"
        return {symbol: _daily_frame() for symbol in symbols}

    def get_klines(self, symbol, *, period, count, adjust):
        assert period == "1d"
        assert count == 230
        assert adjust == "forward"
        return _daily_frame()


class ManyCandidateTickFlowClient:
    def __init__(self) -> None:
        self.symbol_count = 0

    def get_quotes(self, symbols=None, *, universes=None):
        assert universes is None
        rows = {}
        for index, symbol in enumerate(symbols or [], start=1):
            rows[symbol] = {
                "symbol": symbol,
                "last_price": 100.0 + index,
                "amount": 10_000_000.0 + index,
                "ext": {"name": f"Name {symbol}", "change_pct": 0.0},
            }
        self.symbol_count += len(rows)
        return rows

    def get_klines_batch(self, symbols, *, period, count, adjust):
        assert period == "1d"
        assert count == 230
        assert adjust == "forward"
        return {symbol: _daily_frame() for symbol in symbols}

    def get_klines(self, symbol, *, period, count, adjust):
        assert period == "1d"
        assert count == 230
        assert adjust == "forward"
        return _daily_frame()


def test_run_market_funnel_uses_quote_prefilter_and_batch_fetch(tmp_path, monkeypatch):
    symbol_file = tmp_path / "hk_symbols.txt"
    symbol_file.write_text("00700.HK\n00005.HK\n09999.HK\n", encoding="utf-8")
    monkeypatch.setenv("MARKET_FUNNEL_SYMBOL_FILE", str(symbol_file))
    monkeypatch.setenv("MARKET_FUNNEL_MAX_SYMBOLS", "2")
    monkeypatch.setenv("MARKET_FUNNEL_QUOTE_BATCH_SIZE", "1")
    monkeypatch.setenv("MARKET_FUNNEL_QUOTE_BATCH_SLEEP", "0")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_COUNT", "230")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_BATCH_SIZE", "1")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_BATCH_SLEEP", "0")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_QUOTE_AMOUNT", "0")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "220")
    output = tmp_path / "hk_result.json"
    client = FakeTickFlowClient()

    result = run_market_funnel("hk", output=str(output), client=client)

    assert result["ok"] is True
    assert result["market"] == "hk"
    assert result["quote_count"] == 3
    assert result["universe_symbol_count"] == 3
    assert result["selected_count"] == 2
    assert result["fetched_count"] == 2
    assert client.quote_batches == [["00700.HK"], ["00005.HK"], ["09999.HK"]]
    assert client.kline_batches == [["00700.HK"], ["00005.HK"]]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["limits"]["quote_batch_size"] == 1
    assert payload["limits"]["quote_batch_sleep"] == 0.0
    assert payload["limits"]["kline_batch_size"] == 1
    report = output.with_name("hk_report.md").read_text(encoding="utf-8")
    assert "Wyckoff Funnel 港股 最终报告" in report
    assert "## 筛选概览" in report
    assert "| 股票池 | 3 |" in report


def test_run_market_funnel_writes_all_candidates_to_db_when_enabled(tmp_path, monkeypatch):
    symbols = [f"S{i:03d}.US" for i in range(105)]
    symbol_file = tmp_path / "us_symbols.txt"
    symbol_file.write_text("\n".join(symbols), encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_layers(fetched_symbols, name_map, df_map, runtime, **_kwargs):
        hits = [(symbol, float(index)) for index, symbol in enumerate(fetched_symbols, start=1)]
        return {"sos": hits}, {"total_hits": len(hits), "by_trigger": {"sos": len(hits)}}

    def fake_upsert(recommend_date, rows, market):
        captured["rows"] = rows
        captured["market"] = market
        captured["recommend_date"] = recommend_date
        return True

    monkeypatch.setenv("MARKET_FUNNEL_SYMBOL_FILE", str(symbol_file))
    monkeypatch.setenv("MARKET_FUNNEL_MAX_SYMBOLS", "105")
    monkeypatch.setenv("MARKET_FUNNEL_QUOTE_BATCH_SIZE", "200")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_COUNT", "230")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_BATCH_SIZE", "200")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_QUOTE_AMOUNT", "0")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "220")
    monkeypatch.setenv("MARKET_FUNNEL_WRITE_DB", "1")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr("workflows.market_funnel_job._run_layers", fake_run_layers)
    monkeypatch.setattr("integrations.recommendation_global.upsert_global_recommendations", fake_upsert)

    result = run_market_funnel("us", output=str(tmp_path / "us_result.json"), client=ManyCandidateTickFlowClient())

    assert len(result["top_candidates"]) == 100
    assert result["metrics"]["total_hits"] == 105
    assert len(captured["rows"]) == 105
    assert captured["market"] == "us"
    assert captured["recommend_date"] == 20251118


def test_upsert_funnel_to_tracking_groups_by_trade_date(monkeypatch):
    calls: list[tuple[int, list[dict[str, object]], str]] = []

    def fake_upsert(recommend_date, rows, market):
        calls.append((recommend_date, rows, market))
        return True

    monkeypatch.setattr("integrations.recommendation_global.upsert_global_recommendations", fake_upsert)

    market_tracking.upsert_funnel_to_tracking(
        [
            {"symbol": "HALT.US", "latest_close": 10.0, "latest_trade_date": 20260513},
            {"symbol": "AAPL.US", "latest_close": 213.0, "latest_trade_date": 20260514},
        ],
        "us",
    )

    assert [(date, [row["code"] for row in rows], market) for date, rows, market in calls] == [
        (20260513, ["HALT.US"], "us"),
        (20260514, ["AAPL.US"], "us"),
    ]
