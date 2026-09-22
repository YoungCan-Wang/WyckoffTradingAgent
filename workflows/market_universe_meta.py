"""Build structured market universe metadata from executable txt pools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE_DIR = ROOT / "data" / "market_universes"


@dataclass(frozen=True)
class MarketUniverseMetaRequest:
    universe_dir: Path = DEFAULT_UNIVERSE_DIR


def run_market_universe_meta_build(request: MarketUniverseMetaRequest) -> int:
    universe_dir = request.universe_dir.resolve()
    meta = build_metadata(universe_dir)
    for key, rows in meta.items():
        out = universe_dir / f"{key}_meta.json"
        write_json(out, rows)
        print(f"[meta] wrote {out} rows={len(rows)}")
    return 0


def clean_symbol_lines(path: Path) -> list[str]:
    out: list[str] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip().upper()
        if text:
            out.append(text)
    return sorted(dict.fromkeys(out))


def load_name_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {
        str(key).strip().upper(): str(value).strip()
        for key, value in payload.items()
        if str(key).strip() and str(value).strip()
    }


def market_entry(symbol: str, market: str, name: str = "") -> dict[str, Any]:
    code = symbol.split(".", 1)[0]
    currency = "USD" if market == "us" else "HKD"
    return {
        "symbol": symbol,
        "code": code,
        "name": name,
        "market": market,
        "asset_type": "stock",
        "currency": currency,
        "active": True,
    }


def etf_entries(path: Path) -> list[dict[str, Any]]:
    group = ""
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("# ----") and stripped.endswith("----"):
            group = stripped.strip("#- ")
            continue
        text = stripped.split("#", 1)[0].strip()
        if not text:
            continue
        parts = text.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 6 or not parts[0].isdigit():
            continue
        code, sector_tag = parts
        out.append(
            {
                "symbol": cn_fund_symbol(code),
                "code": code,
                "name": f"{sector_tag}ETF",
                "market": "cn",
                "asset_type": "etf",
                "currency": "CNY",
                "sector_tag": sector_tag,
                "group": group,
                "active": True,
            }
        )
    return out


def build_metadata(universe_dir: Path) -> dict[str, list[dict[str, Any]]]:
    us_names = load_name_map(universe_dir / "us_names.json")
    hk_names = load_name_map(universe_dir / "hk_names.json")
    us_symbols = clean_symbol_lines(universe_dir / "us.txt")
    hk_symbols = clean_symbol_lines(universe_dir / "hk.txt")
    return {
        "us": [market_entry(symbol, "us", us_names.get(symbol, "")) for symbol in us_symbols],
        "hk": [market_entry(symbol, "hk", hk_names.get(symbol, "")) for symbol in hk_symbols],
        "etf_cn": etf_entries(universe_dir / "etf_cn.txt"),
    }


def cn_fund_symbol(code: str) -> str:
    if code.startswith(("15", "16", "18")):
        return f"{code}.SZ"
    return f"{code}.SH"


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
