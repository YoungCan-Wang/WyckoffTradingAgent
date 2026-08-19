"""推荐跟踪记录的列名映射。

存储层的列名和记录字段名对不上（表里是 initial_price / change_pct / mfe_pct /
candidate_status），历史上这里按字段名直接取，于是四个字段恒为空 —— 数据是满的，
只是读错了列。CLI 的 recommend 表格和桌面端跟踪页都吃这个亏，且失败是静默的：
没有报错，只是永远显示「-」。这些测试锁住映射，避免再次跑偏。
"""

from __future__ import annotations

from core.pattern_review.records import PatternReviewRecord


def _record(row: dict) -> dict:
    return PatternReviewRecord.from_row(row).to_tool_record()


class TestStorageColumnNames:
    def test_maps_actual_table_columns(self) -> None:
        """真实列名（Supabase / 本地 SQLite 实际使用的那套）必须取到值。"""
        out = _record(
            {
                "code": "600699",
                "name": "均胜电子",
                "recommend_date": "20260818",
                "initial_price": 21.23,
                "current_price": 21.10,
                "change_pct": -0.61,
                "mfe_pct": 0.24,
                "mae_pct": -1.66,
                "candidate_status": "禁新仓-影子观察",
            }
        )
        assert out["recommend_price"] == 21.23
        assert out["current_price"] == 21.10
        assert out["pnl_pct"] == -0.61
        assert out["max_pnl_pct"] == 0.24
        assert out["min_pnl_pct"] == -1.66
        assert out["status"] == "禁新仓-影子观察"

    def test_falls_back_to_legacy_names(self) -> None:
        """旧名仍要能读：别的写入路径或历史行可能用的是这一套。"""
        out = _record(
            {
                "code": "000001",
                "recommend_price": 11.80,
                "pnl_pct": 3.20,
                "max_pnl_pct": 5.00,
                "min_pnl_pct": -1.10,
                "status": "已止盈",
            }
        )
        assert out["recommend_price"] == 11.80
        assert out["pnl_pct"] == 3.20
        assert out["max_pnl_pct"] == 5.00
        assert out["min_pnl_pct"] == -1.10
        assert out["status"] == "已止盈"

    def test_actual_column_wins_over_legacy(self) -> None:
        """两套同时存在时以真实列为准。"""
        out = _record({"code": "1", "initial_price": 10.0, "recommend_price": 99.0})
        assert out["recommend_price"] == 10.0


class TestMissingValues:
    def test_missing_numbers_stay_none(self) -> None:
        """缺失必须是 None，不能填 0 —— 「涨跌 0%」和「没数据」是两件事。"""
        out = _record({"code": "600519", "name": "贵州茅台"})
        assert out["recommend_price"] is None
        assert out["current_price"] is None
        assert out["pnl_pct"] is None
        assert out["max_pnl_pct"] is None
        assert out["min_pnl_pct"] is None

    def test_zero_is_preserved_as_zero(self) -> None:
        """真实的 0 要保留 —— 不能被当成缺失而回退到旧列或变 None。"""
        out = _record({"code": "1", "change_pct": 0, "initial_price": 0.0, "pnl_pct": 7.7})
        assert out["pnl_pct"] == 0
        # 0.0 是有效价格（虽然罕见），不该被 _first 跳过。
        assert out["recommend_price"] == 0.0

    def test_empty_string_status_is_treated_as_missing(self) -> None:
        """空串状态应继续往后找，而不是把空串当有效值。"""
        out = _record({"code": "1", "candidate_status": "", "status": "已清仓"})
        assert out["status"] == "已清仓"

    def test_all_missing_status_is_empty_string(self) -> None:
        assert _record({"code": "1"})["status"] == ""


class TestStatusIsFreeText:
    def test_does_not_constrain_status_vocabulary(self) -> None:
        """状态是自由文本，没有枚举 —— 原样带过去，不要试图归一化。"""
        for raw in ["禁新仓-影子观察", "已止盈", "跟踪中", "任意后端新增的说法"]:
            assert _record({"code": "1", "candidate_status": raw})["status"] == raw


class TestEntryRole:
    def test_ai_flag_drives_role(self) -> None:
        assert _record({"code": "1", "is_ai_recommended": True})["entry_role"] == "AI推荐"
        assert _record({"code": "1", "is_ai_recommended": 1})["entry_role"] == "AI推荐"
        # 本地 SQLite 存的是 0/1 整数，云端可能是布尔或字符串。
        assert _record({"code": "1", "is_ai_recommended": "true"})["entry_role"] == "AI推荐"

    def test_defaults_to_review_role(self) -> None:
        assert _record({"code": "1"})["entry_role"] == "观察/信号复盘"
        assert _record({"code": "1", "is_ai_recommended": 0})["entry_role"] == "观察/信号复盘"
