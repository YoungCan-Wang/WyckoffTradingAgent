from __future__ import annotations

import pytest

from workflows.backtest_cli import build_backtest_parser, parse_hold_days_list
from workflows.backtest_defaults import DEFAULT_HOLD_DAYS


def test_parse_hold_days_list_deduplicates_and_sorts() -> None:
    assert parse_hold_days_list("15, 10，15") == [10, 15]


def test_parse_hold_days_list_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="hold_days_list 中存在非法值"):
        parse_hold_days_list("10,0")


def test_backtest_parser_keeps_core_defaults() -> None:
    args = build_backtest_parser().parse_args([])

    assert args.hold_days == DEFAULT_HOLD_DAYS
    assert args.board == "all"
    assert args.top_n == 0
    assert args.pending_mode == "both"
    assert args.entry_price_mode == "open"
    assert args.cash_portfolio is False


def test_backtest_parser_accepts_close_entry_price_mode() -> None:
    args = build_backtest_parser().parse_args(["--entry-price-mode", "close"])

    assert args.entry_price_mode == "close"


def test_backtest_parser_help_renders_percent_text() -> None:
    help_text = build_backtest_parser().format_help()

    assert "止损线(%)" in help_text
    assert "跌破 9% 止损" in help_text
