from __future__ import annotations

import inspect

import pytest

from agents.portfolio_tools import _normalize_stop_rows, set_stop_loss


class TestNarrowByConstruction:
    def test_signature_cannot_move_positions(self):
        """auto 档的安全性依赖这个签名——它不该能改股数、成本或现金。"""
        params = set(inspect.signature(set_stop_loss).parameters)
        assert params == {"code", "stop_loss", "items", "tool_context"}
        for forbidden in ("shares", "cost_price", "free_cash", "action", "table", "codes"):
            assert forbidden not in params

    def test_registered_as_auto_tool(self):
        from cli.approval_policy import AUTO, classify

        assert classify("set_stop_loss", {"code": "002270", "stop_loss": 1.0}) == AUTO

    def test_still_requires_approval_when_a_human_is_present(self):
        """auto 只是 daemon 无人时放行；有 UI 时仍要确认。"""
        from cli.tools import CONFIRM_TOOLS

        assert "set_stop_loss" in CONFIRM_TOOLS


class TestNormalization:
    def test_single_code(self):
        rows, error = _normalize_stop_rows("002270", 33.15, None)
        assert error is None
        assert rows == [{"code": "002270", "stop_loss": 33.15}]

    def test_batch_items(self):
        items = [{"code": "002270", "stop_loss": 33.15}, {"code": "605007", "stop_loss": 13.0}]
        rows, error = _normalize_stop_rows("", 0, items)
        assert error is None
        assert [r["code"] for r in rows] == ["002270", "605007"]

    def test_rejects_zero_price(self):
        _rows, error = _normalize_stop_rows("002270", 0, None)
        assert error is not None and "大于 0" in error["error"]

    def test_rejects_negative_price(self):
        _rows, error = _normalize_stop_rows("002270", -5.0, None)
        assert error is not None

    @pytest.mark.parametrize("code", ["", "12", "   "])
    def test_rejects_unnormalizable_code(self, code):
        """normalize_portfolio_code 把未知字符串当美股代码，只有它返回空才是真无效。"""
        _rows, error = _normalize_stop_rows(code, 10.0, None)
        assert error is not None and "无效" in error["error"]

    def test_rejects_non_numeric_price(self):
        _rows, error = _normalize_stop_rows("", 0, [{"code": "002270", "stop_loss": "abc"}])
        assert error is not None

    def test_rejects_empty_items(self):
        _rows, error = _normalize_stop_rows("", 0, [])
        assert error is not None

    def test_rejects_oversized_batch(self):
        items = [{"code": "002270", "stop_loss": 1.0}] * 201
        _rows, error = _normalize_stop_rows("", 0, items)
        assert error is not None and "最多" in error["error"]

    def test_accepts_189_missing_stops(self):
        """189 条缺失止损要能一次提交，这是这个工具存在的理由。"""
        items = [{"code": f"{600000 + i}", "stop_loss": 10.0 + i} for i in range(189)]
        rows, error = _normalize_stop_rows("", 0, items)
        assert error is None and len(rows) == 189

    def test_rejects_non_dict_item(self):
        _rows, error = _normalize_stop_rows("", 0, ["002270"])
        assert error is not None

    def test_normalizes_hk_code(self):
        rows, error = _normalize_stop_rows("00700.HK", 300.0, None)
        assert error is None
        assert rows[0]["code"] == "00700.HK"


class TestWritePath:
    @pytest.fixture
    def local_only(self, monkeypatch):
        monkeypatch.setattr("agents.portfolio_tools.has_cloud", lambda _ctx: False)
        monkeypatch.setattr("agents.portfolio_tools._portfolio_id", lambda _ctx: "TEST_PF")

    def test_reports_codes_not_in_portfolio(self, monkeypatch, local_only):
        monkeypatch.setattr("integrations.local_db.set_local_position_stop", lambda *_a: 0)
        result = set_stop_loss(code="002270", stop_loss=33.15)
        assert result["updated_count"] == 0
        assert result["not_in_portfolio"] == ["002270"]

    def test_counts_applied_rows(self, monkeypatch, local_only):
        monkeypatch.setattr("integrations.local_db.set_local_position_stop", lambda *_a: 1)
        items = [{"code": "002270", "stop_loss": 33.15}, {"code": "605007", "stop_loss": 13.0}]
        result = set_stop_loss(items=items)
        assert result["updated_count"] == 2
        assert "not_in_portfolio" not in result

    def test_validation_error_writes_nothing(self, monkeypatch, local_only):
        def _boom(*_a):
            raise AssertionError("must not write when validation failed")

        monkeypatch.setattr("integrations.local_db.set_local_position_stop", _boom)
        assert "error" in set_stop_loss(code="002270", stop_loss=-1)
