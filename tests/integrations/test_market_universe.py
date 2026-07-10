from __future__ import annotations

import json

from integrations.market_universe import load_hk_symbols, load_us_symbols


def test_load_us_symbols_reads_real_universe_file() -> None:
    symbols, name_map = load_us_symbols()

    assert isinstance(symbols, list)
    assert isinstance(name_map, dict)
    if symbols:
        assert all(isinstance(s, str) and s for s in symbols)


def test_load_hk_symbols_reads_real_universe_file() -> None:
    symbols, name_map = load_hk_symbols()

    assert isinstance(symbols, list)
    assert isinstance(name_map, dict)
    if symbols:
        assert all(s.endswith(".HK") for s in symbols)


def test_load_name_map_reads_symbol_and_name_fields(tmp_path) -> None:
    from integrations.market_universe import _load_name_map

    meta_path = tmp_path / "hk_meta.json"
    meta_path.write_text(json.dumps([{"symbol": "00700.HK", "name": "Tencent"}]), encoding="utf-8")

    assert _load_name_map(meta_path) == {"00700.HK": "Tencent"}


def test_load_name_map_missing_file_returns_empty(tmp_path) -> None:
    from integrations.market_universe import _load_name_map

    assert _load_name_map(tmp_path / "missing.json") == {}
