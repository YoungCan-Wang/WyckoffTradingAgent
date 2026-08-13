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

    def test_schema_lists_every_supported_type(self) -> None:
        """schema 里的 enum 必须和存储层支持的 type 一致，否则模型会画出被拒的形状。"""
        from cli.tools import TOOL_SCHEMAS

        entry = next(t for t in TOOL_SCHEMAS if t["name"] == "annotate_chart")
        enum = entry["parameters"]["properties"]["annotations"]["items"]["properties"]["type"]["enum"]
        assert set(enum) == set(ca.REQUIRED)
