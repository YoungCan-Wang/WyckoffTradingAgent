from __future__ import annotations

import json

from integrations import market_metadata


def test_fetch_sector_map_reads_fresh_cache(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "sector_map_cache.json"
    cache.write_text(json.dumps({"000001": "银行"}), encoding="utf-8")
    monkeypatch.setattr(market_metadata, "SECTOR_CACHE", cache)

    assert market_metadata.fetch_sector_map() == {"000001": "银行"}


def test_fetch_market_cap_map_normalizes_cached_values(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "market_cap_cache.json"
    cache.write_text(json.dumps({"000001": "123.4"}), encoding="utf-8")
    monkeypatch.setattr(market_metadata, "MARKET_CAP_CACHE", cache)

    assert market_metadata.fetch_market_cap_map() == {"000001": 123.4}


def test_fetch_float_share_map_converts_wan_shares_to_shares(tmp_path, monkeypatch) -> None:
    import pandas as pd

    class _FakePro:
        def daily_basic(self, trade_date: str, fields: str):  # noqa: ARG002
            return pd.DataFrame({"ts_code": ["000001.SZ"], "float_share": [1_940_000.0]})

    monkeypatch.setattr(market_metadata, "FLOAT_SHARE_CACHE", tmp_path / "float_share_cache.json")
    monkeypatch.setattr(market_metadata, "_tushare_pro", lambda: _FakePro())

    assert market_metadata.fetch_float_share_map() == {"000001": 1.94e10}


def test_fetch_historical_market_cap_map_retries(monkeypatch) -> None:
    import pandas as pd

    class _FlakyPro:
        calls = 0

        def daily_basic(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary")
            return pd.DataFrame({"ts_code": ["000001.SZ"], "total_mv": [1_234_000.0]})

    pro = _FlakyPro()
    monkeypatch.setenv("MARKET_METADATA_MAX_RETRIES", "2")
    monkeypatch.setenv("MARKET_METADATA_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setattr(market_metadata, "_tushare_pro", lambda: pro)

    assert market_metadata.fetch_historical_market_cap_map("2026-07-15") == {"000001": 123.4}
    assert pro.calls == 2


def test_detect_theme_lines_uses_consecutive_recent_history(tmp_path, monkeypatch) -> None:
    history = tmp_path / "concept_heat_history.json"
    history.write_text(
        json.dumps(
            {
                "2026-06-20": {"AI算力": {}, "机器人": {}},
                "2026-06-19": {"AI算力": {}, "机器人": {}},
                "2026-06-18": {"AI算力": {}, "电力": {}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(market_metadata, "CONCEPT_HEAT_HISTORY", history)

    assert market_metadata.detect_theme_lines(min_days=3) == ["AI算力"]


def test_detect_theme_lines_accepts_remote_history_without_local_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(market_metadata, "CONCEPT_HEAT_HISTORY", tmp_path / "missing.json")
    history = {
        "2026-06-20": {"机器人": {}},
        "2026-06-19": {"机器人": {}},
        "2026-06-18": {"机器人": {}},
    }

    assert market_metadata.detect_theme_lines(min_days=3, history=history) == ["机器人"]


def test_fetch_suspended_symbols_keeps_only_suspend_rows(monkeypatch) -> None:
    import pandas as pd

    class _FakePro:
        def suspend_d(self, trade_date: str):
            assert trade_date == "20260715"
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "suspend_type": "S"},
                    {"ts_code": "000002.SZ", "suspend_type": "R"},
                ]
            )

    monkeypatch.setattr(market_metadata, "_tushare_pro", lambda: _FakePro())

    assert market_metadata.fetch_suspended_symbols(market_metadata.date(2026, 7, 15)) == {"000001"}


def test_update_concept_heat_history_keeps_pct_and_inflow_leaders(tmp_path, monkeypatch) -> None:
    history = tmp_path / "concept_heat_history.json"
    monkeypatch.setattr(market_metadata, "CONCEPT_HEAT_HISTORY", history)
    monkeypatch.setattr(market_metadata, "_upsert_concept_heat_history", lambda *_args, **_kwargs: None)

    market_metadata.update_concept_heat_history(
        "2026-06-30",
        [
            {"name": "资金强", "pct": 1.0, "net_inflow": 100.0},
            {"name": "机器人", "pct": 6.0, "net_inflow": 5.0},
        ],
        top_n=1,
    )

    data = json.loads(history.read_text(encoding="utf-8"))
    assert set(data["2026-06-30"]) == {"资金强", "机器人"}
