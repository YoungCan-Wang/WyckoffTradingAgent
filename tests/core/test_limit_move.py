"""Tests for core.limit_move (A-share limit-up/limit-down classification)."""

from __future__ import annotations

from core.limit_move import (
    classify_limit_move,
    describe_limit_move,
    is_st_name,
    limit_pct,
)


class TestLimitPct:
    def test_main_board_default_10pct(self):
        assert limit_pct("600519", "贵州茅台") == 10.0

    def test_chinext_20pct(self):
        assert limit_pct("300750", "宁德时代") == 20.0

    def test_star_board_20pct(self):
        assert limit_pct("688981", "中芯国际") == 20.0

    def test_bse_20pct(self):
        assert limit_pct("430047", "诺思兰德") == 20.0

    def test_st_name_5pct_overrides_board(self):
        assert limit_pct("300123", "ST某某") == 5.0
        assert limit_pct("600123", "*ST某某") == 5.0


class TestIsStName:
    def test_st_prefix(self):
        assert is_st_name("ST同洲")
        assert is_st_name("*ST海投")

    def test_non_st(self):
        assert not is_st_name("贵州茅台")
        assert not is_st_name("")


class TestClassifyLimitMove:
    def test_insufficient_data_returns_none(self):
        assert classify_limit_move(code="600519", name="", prev_close=0.0, open_=10, high=10, low=10, close=10) is None

    def test_one_word_limit_down(self):
        prev_close = 10.0
        limit_down = round(prev_close * 0.9, 2)
        state = classify_limit_move(
            code="600519",
            name="贵州茅台",
            prev_close=prev_close,
            open_=limit_down,
            high=limit_down,
            low=limit_down,
            close=limit_down,
        )
        assert state is not None
        assert state.closed_limit_down
        assert state.one_word_board
        assert not state.opened_then_broke

    def test_opened_then_broke_limit_down(self):
        """跌停后打开又未封住（烂板）：不应被判定为一字板。"""
        prev_close = 10.0
        limit_down = round(prev_close * 0.9, 2)
        state = classify_limit_move(
            code="600519",
            name="贵州茅台",
            prev_close=prev_close,
            open_=9.8,
            high=9.9,
            low=limit_down,
            close=9.85,
        )
        assert state is not None
        assert state.touched_limit_down
        assert not state.closed_limit_down
        assert state.opened_then_broke
        assert not state.one_word_board

    def test_normal_trading_day_no_limit_touch(self):
        state = classify_limit_move(
            code="600519",
            name="贵州茅台",
            prev_close=10.0,
            open_=9.9,
            high=10.1,
            low=9.7,
            close=9.8,
        )
        assert state is not None
        assert not state.touched_limit_up
        assert not state.touched_limit_down

    def test_st_limit_pct_used(self):
        prev_close = 10.0
        st_limit_down = round(prev_close * 0.95, 2)
        state = classify_limit_move(
            code="600123",
            name="ST某某",
            prev_close=prev_close,
            open_=st_limit_down,
            high=st_limit_down,
            low=st_limit_down,
            close=st_limit_down,
        )
        assert state is not None
        assert state.limit_pct == 5.0
        assert state.closed_limit_down


class TestDescribeLimitMove:
    def test_none_state_returns_empty(self):
        assert describe_limit_move(None) == ""

    def test_one_word_down_description(self):
        state = classify_limit_move(
            code="600519", name="贵州茅台", prev_close=10.0, open_=9.0, high=9.0, low=9.0, close=9.0
        )
        desc = describe_limit_move(state)
        assert "一字跌停" in desc
        assert "不能视为有效" in desc

    def test_opened_then_broke_description(self):
        state = classify_limit_move(
            code="600519", name="贵州茅台", prev_close=10.0, open_=9.8, high=9.9, low=9.0, close=9.85
        )
        desc = describe_limit_move(state)
        assert "烂板" in desc

    def test_no_limit_touch_returns_empty(self):
        state = classify_limit_move(
            code="600519", name="贵州茅台", prev_close=10.0, open_=9.9, high=10.1, low=9.7, close=9.8
        )
        assert describe_limit_move(state) == ""
