"""Tests for exit-recommendation attribution."""

from __future__ import annotations

import pytest

from core.exit_attribution import (
    NEAR_TRIGGER_PCT,
    STALE_DEVIATION_PCT,
    ExitRecord,
    as_report_dict,
    build_attribution,
    classify_origin,
    parse_stop_loss,
)


def _record(**kwargs) -> ExitRecord:
    base = {
        "code": "600000",
        "name": "测试",
        "action": "EXIT",
        "trade_date": "2026-08-01",
        "price": 10.0,
        "sequence": 1,
    }
    base.update(kwargs)
    return ExitRecord(**base)


class TestParsing:
    def test_extracts_stop_loss(self):
        assert parse_stop_loss("audit=inherit_pos_stop(67.78); sell_with_slippage") == 67.78

    def test_missing_stop_returns_none(self):
        assert parse_stop_loss("audit=hold") is None
        assert parse_stop_loss(None) is None

    def test_origin_prefers_forced_stop(self):
        assert classify_origin("系统强制止损: 现价跌破...") == "系统强制止损"

    def test_origin_detects_stop_driven(self):
        assert classify_origin("已穿止损，结构破位") == "止损驱动"
        assert classify_origin("audit=inherit_pos_stop(35.10)") == "止损驱动"

    def test_origin_defaults_to_model(self):
        assert classify_origin("均线空头排列，减仓防守") == "模型判断"


class TestStopBands:
    def test_normal_stop_below_price(self):
        assert _record(stop_loss=9.0).stop_band == "止损低于现价"

    def test_just_triggered(self):
        record = _record(stop_loss=10.2)
        assert record.stop_deviation_pct == pytest.approx(2.0)
        assert record.stop_band == f"倒挂 0~{NEAR_TRIGGER_PCT:.0f}%"

    def test_stale_stop(self):
        """回归：昊华科技止损 67.78 / 现价 40.06，倒挂 69% 应判为陈旧。"""
        record = _record(price=40.06, stop_loss=67.78)
        assert record.stop_deviation_pct > STALE_DEVIATION_PCT
        assert record.stop_band == f"陈旧 >{STALE_DEVIATION_PCT:.0f}%"

    def test_missing_stop_band(self):
        assert _record().stop_band == "无止损信息"

    def test_zero_price_is_safe(self):
        assert _record(price=0.0, stop_loss=5.0).stop_deviation_pct is None


class TestExcess:
    def test_subtracts_benchmark(self):
        record = _record(after_pct=14.0, benchmark_pct=2.0)
        assert record.excess_pct == 12.0

    def test_none_without_benchmark(self):
        assert _record(after_pct=14.0).excess_pct is None


class TestAttribution:
    def test_empty_is_safe(self):
        report = build_attribution([])
        assert report.overall is None
        assert as_report_dict(report)["records"] == 0

    def test_sold_correctly_counts_declines(self):
        """卖出后下跌才算卖对了。"""
        records = [
            _record(code="A", after_pct=-5.0, benchmark_pct=0.0),
            _record(code="B", after_pct=+8.0, benchmark_pct=0.0),
        ]
        report = build_attribution(records)
        assert report.overall.count == 2
        assert report.overall.sold_correctly == 1
        assert report.overall.after_pct == 1.5

    def test_splits_first_and_repeat(self):
        records = [
            _record(code="A", sequence=1, after_pct=10.0),
            _record(code="A", sequence=2, after_pct=20.0),
            _record(code="A", sequence=3, after_pct=30.0),
        ]
        report = build_attribution(records)
        labels = {stat.label: stat for stat in report.by_sequence}
        assert labels["首次触发"].count == 1
        assert labels["重复触发"].count == 2
        assert labels["重复触发"].after_pct == 25.0

    def test_groups_by_origin(self):
        records = [
            _record(code="A", origin="止损驱动", after_pct=20.0),
            _record(code="B", origin="模型判断", after_pct=5.0),
        ]
        report = build_attribution(records)
        by_origin = {stat.label: stat.after_pct for stat in report.by_origin}
        assert by_origin["止损驱动"] == 20.0
        assert by_origin["模型判断"] == 5.0

    def test_report_dict_is_serializable(self):
        import json

        records = [_record(after_pct=1.0, benchmark_pct=0.5, stop_loss=9.0)]
        payload = as_report_dict(build_attribution(records))
        assert json.loads(json.dumps(payload, ensure_ascii=False))["codes"] == 1

    def test_records_without_outcome_do_not_break_stats(self):
        records = [_record(code="A", after_pct=None), _record(code="B", after_pct=4.0)]
        report = build_attribution(records)
        assert report.overall.count == 2
        assert report.overall.after_pct == 4.0
