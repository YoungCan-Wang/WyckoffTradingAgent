"""Refresh the committed HK and US symbol pools from public listing sources."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
INSTRUMENT_RE = re.compile(
    r"\b(warrants?|rights?|units|preferred|preference|pfd|debentures?|bonds?|notes)\b",
    re.IGNORECASE,
)
HK_SYMBOL_RE = re.compile(r"^\d{5}\.HK$")
MIN_HK_SYMBOLS = 2000
MIN_US_SYMBOLS = 4000


def parse_pipe_table(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line and not line.startswith("File Creation Time")]
    if not lines:
        return []
    header = lines[0].split("|")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) >= len(header):
            rows.append(dict(zip(header, parts, strict=False)))
    return rows


def clean_listing_name(name: str) -> str:
    text = " ".join(name.replace("\xa0", " ").split())
    head = text.split(" - ", 1)[0]
    for suffix in (" Common Stock", " Ordinary Shares"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    return head.strip(" -")


def keep_us_listing(row: dict[str, str], symbol_key: str) -> bool:
    symbol = row.get(symbol_key, "").strip().upper()
    if row.get("Test Issue") == "Y" or row.get("ETF") == "Y" or row.get("NextShares") == "Y":
        return False
    if not US_SYMBOL_RE.match(symbol):
        return False
    return INSTRUMENT_RE.search(row.get("Security Name", "")) is None


def listing_names(rows: list[dict[str, str]], symbol_key: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in rows:
        if not keep_us_listing(row, symbol_key):
            continue
        symbol = row[symbol_key].strip().upper()
        names.setdefault(symbol, clean_listing_name(row.get("Security Name", "")))
    return names


def names_from_us_basic(frame: pd.DataFrame | None) -> dict[str, str]:
    if frame is None or frame.empty:
        return {}
    subset = frame[frame["classify"].isin(["EQ", "ADR", "GDR"])]
    names: dict[str, str] = {}
    for row in subset.itertuples(index=False):
        symbol = str(row.ts_code).strip().upper()
        name = str(getattr(row, "name", "") or "").strip() or str(getattr(row, "enname", "") or "").strip()
        if US_SYMBOL_RE.match(symbol) and name:
            names[symbol] = name
    return names


def us_pool(listing: dict[str, str], tushare_names: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    symbols = [f"{symbol}.US" for symbol in sorted(listing)]
    names = {symbol: tushare_names.get(symbol[:-3]) or listing[symbol[:-3]] for symbol in symbols}
    return symbols, names


def hk_pool(frame: pd.DataFrame | None) -> tuple[list[str], dict[str, str]]:
    if frame is None or frame.empty:
        raise RuntimeError("hk_basic 返回空数据")
    matched = frame[frame["ts_code"].astype(str).str.upper().str.match(HK_SYMBOL_RE)].copy()
    matched["ts_code"] = matched["ts_code"].astype(str).str.upper()
    matched["name"] = matched["name"].fillna("").astype(str).str.strip()
    symbols = sorted(matched["ts_code"].drop_duplicates())
    names = {row.ts_code: row.name for row in matched.itertuples(index=False) if row.name}
    return symbols, names


def require_pool_size(label: str, symbols: list[str], minimum: int) -> None:
    if len(symbols) < minimum:
        raise RuntimeError(f"{label} 名单只有 {len(symbols)} 只，低于 {minimum}，拒绝覆盖")


def write_symbol_file(path: Path, symbols: list[str]) -> None:
    path.write_text("".join(f"{symbol}\n" for symbol in symbols), encoding="utf-8")


def write_name_file(path: Path, names: dict[str, str]) -> None:
    payload = {symbol: names[symbol] for symbol in sorted(names)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_pools(
    universe_dir: Path, hk_symbols: list[str], hk_names: dict[str, str], us_symbols: list[str], us_names: dict[str, str]
) -> None:
    require_pool_size("港股", hk_symbols, MIN_HK_SYMBOLS)
    require_pool_size("美股", us_symbols, MIN_US_SYMBOLS)
    write_symbol_file(universe_dir / "hk.txt", hk_symbols)
    write_symbol_file(universe_dir / "us.txt", us_symbols)
    write_name_file(universe_dir / "hk_names.json", hk_names)
    write_name_file(universe_dir / "us_names.json", us_names)
