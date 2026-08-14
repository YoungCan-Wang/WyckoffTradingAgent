"""annotate_chart 工具。

重点：
- fail-closed：写盘失败必须回报错误，不能让界面出现"幽灵画线"。
- 标注是纯展示，不该进审批队列（它不动持仓、不下单）。
- 校验失败要给模型可操作的提示，而不是一句"参数错误"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.chart_annotation_tools import annotate_chart
from integrations import chart_annotations as ca


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """标注写在 tmp 下，测试不碰用户真实的 ~/.wyckoff。"""
    monkeypatch.setattr(ca, "STORE_PATH", tmp_path / "annotations.json")


def _rect():
    return {"type": "rectangle", "start_date": "2026-01-05", "end_date": "2026-02-10", "low": 10.0, "high": 11.5}


class TestDraw:
    def test_draw_reports_count_and_chart_id(self) -> None:
        result = annotate_chart("600519", [_rect()])
        assert result["drawn"] == 1
        assert result["chart_id"] == "600519:1d"
        assert "error" not in result

    def test_draw_is_replace_not_append(self) -> None:
        annotate_chart("600519", [_rect(), _rect()])
        annotate_chart("600519", [{"type": "price_line", "price": 9.0}])
        listed = annotate_chart("600519", action="list")
        assert listed["count"] == 1
        assert listed["annotations"][0]["type"] == "price_line"

    def test_draw_without_annotations_is_rejected(self) -> None:
        """空 draw 大概率是模型忘了带参数，而不是想清空。"""
        result = annotate_chart("600519", [])
        assert "error" in result
        assert "clear" in result["error"]

    def test_non_list_annotations_rejected(self) -> None:
        assert "error" in annotate_chart("600519", {"type": "price_line", "price": 1})

    def test_missing_code_rejected(self) -> None:
        assert "error" in annotate_chart("")


class TestValidationFeedback:
    def test_invalid_item_returns_actionable_hint(self) -> None:
        """模型要能从错误里看出该补什么字段。"""
        result = annotate_chart("600519", [{"type": "rectangle", "low": 1.0}])
        assert "error" in result
        assert "rectangle" in result["hint"]

    def test_nothing_saved_when_validation_fails(self) -> None:
        annotate_chart("600519", [_rect()])
        annotate_chart("600519", [_rect(), {"type": "bogus"}])
        # 整批拒绝，原有标注保持不变。
        assert annotate_chart("600519", action="list")["count"] == 1


class TestFailClosed:
    def test_write_failure_reports_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """落盘失败必须回报失败 —— 否则图上出现重开就消失的幽灵画线。"""

        def boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("agents.chart_annotation_tools.replace", boom)
        result = annotate_chart("600519", [_rect()])
        assert "error" in result
        assert "drawn" not in result


class TestListAndClear:
    def test_list_empty_chart(self) -> None:
        assert annotate_chart("000001", action="list")["count"] == 0

    def test_clear_reports_removed(self) -> None:
        annotate_chart("600519", [_rect(), _rect()])
        assert annotate_chart("600519", action="clear")["cleared"] == 2
        assert annotate_chart("600519", action="list")["count"] == 0

    def test_unknown_action_rejected(self) -> None:
        assert "error" in annotate_chart("600519", action="paint")


class TestRegistration:
    def test_registered_and_dispatchable(self) -> None:
        from cli.tools import TOOL_SCHEMAS

        entry = next((t for t in TOOL_SCHEMAS if t["name"] == "annotate_chart"), None)
        assert entry is not None
        assert "code" in entry["parameters"]["required"]

    def test_is_not_an_approval_tool(self) -> None:
        """纯展示：不动持仓、不下单，不该弹审批。"""
        from cli.tools import TOOL_SPECS, ToolRegistry

        assert TOOL_SPECS["annotate_chart"].requires_approval is False
        # 走真实注册表的判定，而不是只看元数据字段。
        assert ToolRegistry().requires_approval("annotate_chart") is False

    def test_is_not_marked_concurrency_safe(self) -> None:
        from cli.tools import ToolRegistry

        assert ToolRegistry().concurrency_safe("annotate_chart") is False

    def test_schema_lists_every_supported_type(self) -> None:
        """schema 里的 enum 必须和存储层支持的 type 一致，否则模型会画出被拒的形状。"""
        from cli.tools import TOOL_SCHEMAS

        entry = next(t for t in TOOL_SCHEMAS if t["name"] == "annotate_chart")
        enum = entry["parameters"]["properties"]["annotations"]["items"]["properties"]["type"]["enum"]
        assert set(enum) == set(ca.REQUIRED)


class TestSymbolAgreement:
    """标注按 symbol 存、图表按正规化后的 symbol 查，两边必须用同一套规则。

    不一致的症状是标注静默消失（图少画一层但不报错），所以这里把契约钉住。
    """

    def test_stores_under_the_normalized_symbol(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ca, "STORE_PATH", tmp_path / "annotations.json")
        annotate_chart(
            code="aapl",
            annotations=[{"type": "marker", "date": "2026-01-05", "price": 10.0, "label": "spring"}],
        )

        # agent 传小写裸 ticker，落盘的 chart_id 用规范码。
        assert "AAPL.US" in ca.STORE_PATH.read_text(encoding="utf-8")

    def test_chart_query_finds_annotations_written_with_loose_input(self, tmp_path, monkeypatch) -> None:
        """端到端：agent 用 '700.HK' 写，图表用 '700.hk' 查，也要对上。"""
        from cli.ipc import methods

        monkeypatch.setattr(ca, "STORE_PATH", tmp_path / "annotations.json")
        annotate_chart(
            code="700.HK",
            annotations=[{"type": "marker", "date": "2026-01-05", "price": 10.0, "label": "spring"}],
        )

        found = methods._annotations_payload("00700.HK")

        assert len(found) == 1
        assert found[0]["label"] == "spring"

    def test_unrecognized_code_is_refused(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ca, "STORE_PATH", tmp_path / "annotations.json")
        result = annotate_chart(code="not a code!", annotations=[])

        assert "认不出" in result["error"]
        assert not ca.STORE_PATH.exists()
