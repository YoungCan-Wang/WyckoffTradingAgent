"""
股票池解析工具。

根据环境变量选择股票来源（手动指定 / 板块筛选 / 全市场默认）。
"""

from __future__ import annotations

import os

from integrations.fetch_a_share_csv import (
    get_stocks_by_board,
    normalize_symbols,
)
from utils.env import parse_int_env


def load_stock_name_map() -> dict[str, str]:
    """获取 A 股代码→名称映射（如失败返回空 dict）。不含 ETF。"""
    try:
        from integrations.fetch_a_share_csv import get_all_stocks

        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}
    except Exception:
        return {}


def _pool_stats(
    mode: str,
    *,
    main: int,
    chinext: int,
    star: int,
    bse: int,
    merged: int,
    st_excluded: int,
    limit: int,
) -> dict[str, int | str]:
    return {
        "pool_mode": mode,
        "pool_main": main,
        "pool_chinext": chinext,
        "pool_star": star,
        "pool_bse": bse,
        "pool_merged": merged,
        "pool_st_excluded": st_excluded,
        "pool_limit": limit,
    }


def _merge_code_to_name(items: list[dict[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for item in items:
        code = str(item.get("code", "")).strip()
        if code and code not in merged:
            merged[code] = str(item.get("name", "")).strip()
    return merged


def _symbols_from_map(
    code_to_name: dict[str, str],
    limit_count: int,
) -> tuple[list[str], dict[str, str], int, int]:
    merged_symbols = normalize_symbols(list(code_to_name.keys()))
    st_set = _st_symbol_set(merged_symbols, code_to_name)
    filtered_symbols = [sym for sym in merged_symbols if sym not in st_set]
    symbols = filtered_symbols[:limit_count] if limit_count > 0 else filtered_symbols
    return symbols, {code: code_to_name.get(code, "") for code in symbols}, len(merged_symbols), len(st_set)


def _st_symbol_set(symbols: list[str], code_to_name: dict[str, str]) -> set[str]:
    return {sym for sym in symbols if "ST" in code_to_name.get(sym, "").upper()}


def _board_items(board: str) -> list[dict[str, str]]:
    try:
        return get_stocks_by_board(board)
    except Exception:
        return []


def _board_counts() -> tuple[int, int, int, int]:
    return (
        len(_board_items("main")),
        len(_board_items("chinext")),
        len(_board_items("star")),
        len(_board_items("bse")),
    )


def _resolve_board_pool(
    board_name: str,
    limit_count: int,
) -> tuple[list[str], dict[str, str], dict[str, int | str]]:
    if board_name in {"main_chinext", "main_chinext_star"}:
        items = _board_items("main") + _board_items("chinext") + _board_items("star")
    else:
        items = get_stocks_by_board(board_name)
    symbols, name_map, merged, st_excluded = _symbols_from_map(_merge_code_to_name(items), limit_count)
    if board_name == "all":
        main, chinext, star, bse = _board_counts()
    else:
        main = len(items) if board_name == "main" else 0
        chinext = len(items) if board_name == "chinext" else 0
        star = len(items) if board_name == "star" else 0
        bse = len(items) if board_name == "bse" else 0
        if board_name in {"main_chinext", "main_chinext_star"}:
            main, chinext, star = (
                len(_board_items("main")),
                len(_board_items("chinext")),
                len(_board_items("star")),
            )
            bse = 0
    return (
        symbols,
        name_map,
        _pool_stats(
            "board",
            main=main,
            chinext=chinext,
            star=star,
            bse=bse,
            merged=merged,
            st_excluded=st_excluded,
            limit=limit_count,
        ),
    )


def _resolve_default_pool(limit_count: int) -> tuple[list[str], dict[str, str], dict[str, int | str]]:
    main_items = get_stocks_by_board("main")
    chinext_items = get_stocks_by_board("chinext")
    star_items = get_stocks_by_board("star")
    bse_items = _board_items("bse")
    code_to_name = _merge_code_to_name(main_items + chinext_items + star_items + bse_items)
    merged_symbols = normalize_symbols(list(code_to_name.keys()))
    st_set = _st_symbol_set(merged_symbols, code_to_name)
    all_symbols = [sym for sym in merged_symbols if sym not in st_set]
    if limit_count > 0:
        all_symbols = all_symbols[:limit_count]
    stats = _pool_stats(
        "default",
        main=len(main_items),
        chinext=len(chinext_items),
        star=len(star_items),
        bse=len(bse_items),
        merged=len(merged_symbols),
        st_excluded=len(st_set),
        limit=limit_count,
    )
    return all_symbols, {code: code_to_name.get(code, "") for code in all_symbols}, stats


def resolve_symbol_pool(
    *,
    pool_mode: str = "",
    board_name: str = "",
    manual_symbols: str = "",
    limit_count: int = 0,
) -> tuple[list[str], dict[str, str], dict[str, int | str]]:
    """解析给定参数对应的股票池，避免调用方通过全局环境变量传参。"""
    pool_mode = str(pool_mode or "").strip().lower()
    board_name = str(board_name or "").strip().lower()
    manual_raw = str(manual_symbols or "")
    limit_count = max(int(limit_count or 0), 0)

    if pool_mode == "manual":
        all_name_map = load_stock_name_map()
        symbols = normalize_symbols([x.strip() for x in manual_raw.replace(";", ",").replace("\n", ",").split(",")])
        name_map = {code: all_name_map.get(code, "") for code in symbols}
        return (
            symbols,
            name_map,
            _pool_stats(
                "manual",
                main=0,
                chinext=0,
                star=0,
                bse=0,
                merged=len(symbols),
                st_excluded=0,
                limit=limit_count,
            ),
        )

    if pool_mode == "board" and board_name in {
        "main",
        "chinext",
        "star",
        "bse",
        "all",
        "main_chinext",
        "main_chinext_star",
    }:
        return _resolve_board_pool(board_name, limit_count)

    return _resolve_default_pool(limit_count)


def resolve_symbol_pool_from_env() -> tuple[list[str], dict[str, str], dict[str, int | str]]:
    """
    根据环境变量 FUNNEL_POOL_MODE / FUNNEL_POOL_MANUAL_SYMBOLS 等
    解析当前使用的股票池。

    返回: (symbols, name_map, pool_stats)
    """
    return resolve_symbol_pool(
        pool_mode=os.getenv("FUNNEL_POOL_MODE", ""),
        board_name=os.getenv("FUNNEL_POOL_BOARD", ""),
        manual_symbols=os.getenv("FUNNEL_POOL_MANUAL_SYMBOLS", ""),
        limit_count=parse_int_env("FUNNEL_POOL_LIMIT_COUNT", 0),
    )
