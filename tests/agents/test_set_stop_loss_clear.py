"""止损的清除语义。

存储层一直支持把 stop_loss 写成 null，但工具层对每个值都做 float()，于是
None 被当成「无效」挡掉——止损填错了就只能删掉整条持仓重建。这里锁住三种
输入的区别：正数是设置，None 是清除，0/负数是错误（那不是清除，是无效价格）。
"""

from __future__ import annotations

from agents.portfolio_tools import _normalize_stop_rows


class TestClearSemantics:
    def test_none_means_clear(self) -> None:
        rows, error = _normalize_stop_rows("600519", None, None)
        assert error is None
        assert rows == [{"code": "600519", "stop_loss": None}]

    def test_positive_sets_price(self) -> None:
        rows, error = _normalize_stop_rows("600519", 1550.5, None)
        assert error is None
        assert rows == [{"code": "600519", "stop_loss": 1550.5}]

    def test_zero_is_rejected(self) -> None:
        """0 不是「清除」——把它当清除会让手滑输入 0 静默生效。"""
        rows, error = _normalize_stop_rows("600519", 0, None)
        assert rows == []
        assert error is not None
        assert "必须大于 0" in error["error"]

    def test_negative_is_rejected(self) -> None:
        _rows, error = _normalize_stop_rows("600519", -10, None)
        assert error is not None
        assert "必须大于 0" in error["error"]

    def test_unparseable_is_rejected(self) -> None:
        _rows, error = _normalize_stop_rows("600519", "abc", None)  # type: ignore[arg-type]
        assert error is not None
        assert "无效" in error["error"]


class TestBatchClearSemantics:
    def test_mixed_batch(self) -> None:
        """一批里可以同时有设置和清除。"""
        rows, error = _normalize_stop_rows(
            "",
            0,
            [
                {"code": "600519", "stop_loss": 1550},
                {"code": "000001", "stop_loss": None},
            ],
        )
        assert error is None
        assert rows == [
            {"code": "600519", "stop_loss": 1550.0},
            {"code": "000001", "stop_loss": None},
        ]

    def test_one_bad_row_fails_whole_batch(self) -> None:
        """一条非法就整批拒绝，不要部分写入——部分成功很难让用户看懂。"""
        rows, error = _normalize_stop_rows(
            "",
            0,
            [{"code": "600519", "stop_loss": 1550}, {"code": "000001", "stop_loss": 0}],
        )
        assert rows == []
        assert error is not None

    def test_missing_stop_key_is_treated_as_clear(self) -> None:
        """缺 key 与显式 None 同义：item.get() 都返回 None。

        这是刻意接受的——调用方省略字段时的意图只能是「不要止损」。
        """
        rows, error = _normalize_stop_rows("", 0, [{"code": "600519"}])
        assert error is None
        assert rows == [{"code": "600519", "stop_loss": None}]


class TestCodeValidation:
    def test_invalid_code_rejected_before_price(self) -> None:
        _rows, error = _normalize_stop_rows("123", 1550, None)
        assert error is not None
        assert "无效" in error["error"]

    def test_bare_short_code_rejected(self) -> None:
        """1-5 位裸代码不接受：补零是猜测，猜错会写到别人的持仓上。"""
        _rows, error = _normalize_stop_rows("597", 5.5, None)
        assert error is not None

    def test_code_is_normalized(self) -> None:
        """港股统一成补零加后缀的形式，否则匹配不到持仓。"""
        rows, error = _normalize_stop_rows("700.HK", 300, None)
        assert error is None
        assert rows[0]["code"] == "00700.HK"
