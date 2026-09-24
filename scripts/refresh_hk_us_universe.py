"""Refresh HK and US symbol pools, then rebuild their metadata."""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from workflows.hk_us_universe_refresh import (
    hk_pool,
    listing_names,
    names_from_us_basic,
    parse_pipe_table,
    us_pool,
    write_pools,
)
from workflows.market_universe_meta import (
    DEFAULT_UNIVERSE_DIR,
    MarketUniverseMetaRequest,
    run_market_universe_meta_build,
)

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
US_PAGE_SIZE = 6000
US_MAX_OFFSET = 60000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh HK and US symbol universes")
    parser.add_argument("--universe-dir", type=Path, default=DEFAULT_UNIVERSE_DIR)
    return parser.parse_args()


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def fetch_us_basic(pro) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    offset = 0
    while offset <= US_MAX_OFFSET:
        frame = pro.us_basic(offset=offset, limit=US_PAGE_SIZE)
        if frame is None or frame.empty:
            break
        frames.append(frame)
        if len(frame) < US_PAGE_SIZE:
            break
        offset += US_PAGE_SIZE
    if not frames:
        raise RuntimeError("us_basic 返回空数据")
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")


def refresh(universe_dir: Path) -> None:
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise SystemExit("需要 TUSHARE_TOKEN")
    hk_symbols, hk_names = hk_pool(pro.hk_basic(list_status="L"))
    listed = listing_names(parse_pipe_table(fetch_text(NASDAQ_LISTED)), "Symbol")
    listed.update(listing_names(parse_pipe_table(fetch_text(OTHER_LISTED)), "ACT Symbol"))
    us_symbols, us_names = us_pool(listed, names_from_us_basic(fetch_us_basic(pro)))
    write_pools(universe_dir, hk_symbols, hk_names, us_symbols, us_names)
    print(f"[universe] hk={len(hk_symbols)} us={len(us_symbols)}")


def main() -> int:
    args = parse_args()
    refresh(args.universe_dir.resolve())
    return run_market_universe_meta_build(MarketUniverseMetaRequest(universe_dir=args.universe_dir.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
