"""图表标注存储。

重点：
- 校验必须整批成功或整批拒绝 —— 半套标注画在图上比不画更糟。
- NaN/Inf 不能进存储（JSON 没这两个值，前端 parse 会拿到非法值）。
- 原子写：进程被杀不能留下截断的 JSON 把已有标注全丢掉。
- 未知字段不进存储 —— 模型输出什么都不该原样落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations import chart_annotations as ca


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "annotations.json"


def _rect(**over):
    base = {"type": "rectangle", "start_date": "2026-01-05", "end_date": "2026-02-10", "low": 10.0, "high": 11.5}
    base.update(over)
    return base


class TestChartId:
    def test_symbol_uppercased_with_timeframe(self) -> None:
        assert ca.make_chart_id("600519") == "600519:1d"
        assert ca.make_chart_id("00700.hk", "1w") == "00700.HK:1w"

    def test_blank_timeframe_defaults(self) -> None:
        assert ca.make_chart_id("600519", "") == "600519:1d"


class TestValidate:
    def test_accepts_each_supported_type(self) -> None:
        samples = [
            _rect(),
            {"type": "price_line", "price": 12.3},
            {
                "type": "trendline",
                "start_date": "2026-01-01",
                "start_price": 9.0,
                "end_date": "2026-02-01",
                "end_price": 11.0,
            },
            {"type": "marker", "date": "2026-01-20", "price": 10.1},
            {"type": "text", "date": "2026-01-20", "price": 10.1, "text": "SOS"},
        ]
        for sample in samples:
            assert ca.validate(sample)["type"] == sample["type"]

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ca.AnnotationError, match="不支持的 type"):
            ca.validate({"type": "gann_fan", "price": 1})

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ca.AnnotationError, match="缺少必填字段"):
            ca.validate({"type": "price_line"})

    def test_bad_date_format_rejected(self) -> None:
        with pytest.raises(ca.AnnotationError, match="YYYY-MM-DD"):
            ca.validate({"type": "marker", "date": "2026/01/20", "price": 10.0})

    def test_non_numeric_price_rejected(self) -> None:
        with pytest.raises(ca.AnnotationError, match="必须是数字"):
            ca.validate({"type": "price_line", "price": "cheap"})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_inf_rejected(self, bad: float) -> None:
        """JSON 没有 NaN/Inf；漏进去前端会拿到非法值，也画不出来。"""
        with pytest.raises(ca.AnnotationError, match="NaN"):
            ca.validate({"type": "price_line", "price": bad})

    def test_inverted_rectangle_is_corrected(self) -> None:
        """上下写反是常见笔误，扶正而不是报错。"""
        out = ca.validate(_rect(low=11.5, high=10.0))
        assert (out["low"], out["high"]) == (10.0, 11.5)

    def test_unknown_fields_are_dropped(self) -> None:
        out = ca.validate({"type": "price_line", "price": 1.0, "evil": "<script>", "zzz": 9})
        assert set(out) == {"type", "price"}

    def test_long_label_truncated(self) -> None:
        out = ca.validate({"type": "price_line", "price": 1.0, "label": "长" * 500})
        assert len(out["label"]) == ca.MAX_TEXT

    def test_newlines_stripped_from_label(self) -> None:
        out = ca.validate({"type": "price_line", "price": 1.0, "label": "a\nb"})
        assert "\n" not in out["label"]

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(ca.AnnotationError):
            ca.validate("rectangle")


class TestReplace:
    def test_round_trip(self, store: Path) -> None:
        ca.replace("600519:1d", [_rect()], path=store)
        loaded = ca.load("600519:1d", path=store)
        assert len(loaded) == 1
        assert loaded[0]["high"] == 11.5

    def test_replace_is_not_append(self, store: Path) -> None:
        """重画即编辑：第二次 draw 换掉整组，不累加。"""
        ca.replace("600519:1d", [_rect(), _rect()], path=store)
        ca.replace("600519:1d", [{"type": "price_line", "price": 5.0}], path=store)
        loaded = ca.load("600519:1d", path=store)
        assert [i["type"] for i in loaded] == ["price_line"]

    def test_charts_are_independent(self, store: Path) -> None:
        ca.replace("600519:1d", [_rect()], path=store)
        ca.replace("000001:1d", [{"type": "price_line", "price": 1.0}], path=store)
        assert len(ca.load("600519:1d", path=store)) == 1
        assert len(ca.load("000001:1d", path=store)) == 1

    def test_all_or_nothing_on_invalid_item(self, store: Path) -> None:
        """一条不合法就整批拒绝 —— 半套标注比不画更糟。"""
        ca.replace("600519:1d", [_rect()], path=store)
        with pytest.raises(ca.AnnotationError):
            ca.replace("600519:1d", [_rect(), {"type": "price_line"}], path=store)
        # 原有标注必须完好无损。
        assert len(ca.load("600519:1d", path=store)) == 1

    def test_empty_list_removes_chart(self, store: Path) -> None:
        ca.replace("600519:1d", [_rect()], path=store)
        ca.replace("600519:1d", [], path=store)
        assert ca.load("600519:1d", path=store) == []

    def test_over_limit_rejected(self, store: Path) -> None:
        too_many = [_rect() for _ in range(ca.MAX_PER_CHART + 1)]
        with pytest.raises(ca.AnnotationError, match="最多"):
            ca.replace("600519:1d", too_many, path=store)

    def test_written_file_is_valid_json(self, store: Path) -> None:
        ca.replace("600519:1d", [_rect()], path=store)
        assert "600519:1d" in json.loads(store.read_text(encoding="utf-8"))


class TestLoadRobustness:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert ca.load_all(tmp_path / "nope.json") == {}

    def test_corrupt_file_is_empty_not_crash(self, store: Path) -> None:
        """截断的 JSON 只该让标注消失，不该让整个请求失败。"""
        store.write_text("{not json", encoding="utf-8")
        assert ca.load_all(store) == {}

    def test_non_dict_root_is_empty(self, store: Path) -> None:
        store.write_text("[1,2,3]", encoding="utf-8")
        assert ca.load_all(store) == {}


class TestClear:
    def test_clear_reports_count(self, store: Path) -> None:
        ca.replace("600519:1d", [_rect(), _rect()], path=store)
        assert ca.clear("600519:1d", path=store) == 2
        assert ca.load("600519:1d", path=store) == []

    def test_clear_unknown_chart_is_zero(self, store: Path) -> None:
        assert ca.clear("nope:1d", path=store) == 0
