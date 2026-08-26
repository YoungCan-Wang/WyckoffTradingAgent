"""威科夫结构 IPC 方法 —— 图表标注的数据来源。

重点：结构缺失时给 None 而不是抛错（图少画一层可以，打不开不行），
以及不把上游数据源的错误细节透给前端。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from cli.ipc import methods


def _frame(n: int = 200) -> pd.DataFrame:
    """中文列名，模拟 get_stock_hist 的原始返回。"""
    return pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=n, freq="B").strftime("%Y-%m-%d"),
            "开盘": [10.0] * n,
            "最高": [11.0] * n,
            "最低": [9.0] * n,
            "收盘": [10.5] * n,
            "成交量": [1_000_000] * n,
            "成交额": [10_500_000] * n,
            "涨跌幅": [0.5] * n,
        }
    )


def _run(params: dict[str, Any]) -> dict[str, Any]:
    return list(methods.wyckoff_events(params))[0]


@pytest.fixture
def stub_hist(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "integrations.stock_hist_repository.get_stock_hist",
        lambda *a, **k: _frame(),
    )


class TestPayload:
    def test_returns_events_range_and_targets(self, stub_hist) -> None:
        result = _run({"symbol": "600519"})
        assert result["symbol"] == "600519"
        assert isinstance(result["events"], list)
        # 平坦数据上结构可能不成立，但这三个键必须存在。
        assert "trading_range" in result
        assert "targets" in result

    def test_events_carry_chartable_fields(self, monkeypatch: pytest.MonkeyPatch, stub_hist) -> None:
        """画图端要靠 date/price/kind 定位，bar 用来直接索引数组。"""
        fake = [{"date": "2026-01-05", "price": 10.0, "kind": "spring", "score": 2.0, "bar": 7}]
        monkeypatch.setattr("core.event_replay.replay_events", lambda *a, **k: fake)
        assert _run({"symbol": "600519"})["events"] == fake


class TestMissingStructure:
    def test_range_none_when_detector_finds_nothing(self, monkeypatch: pytest.MonkeyPatch, stub_hist) -> None:
        monkeypatch.setattr("core.wyckoff_structure.identify_trading_range", lambda *a, **k: None)
        assert _run({"symbol": "600519"})["trading_range"] is None

    def test_range_none_when_detector_raises(self, monkeypatch: pytest.MonkeyPatch, stub_hist) -> None:
        """结构检测炸掉只能少画一层，不能让整个请求失败。"""

        def boom(*_a: Any, **_k: Any):
            raise ValueError("bad frame")

        monkeypatch.setattr("core.wyckoff_structure.identify_trading_range", boom)
        assert _run({"symbol": "600519"})["trading_range"] is None

    def test_targets_none_when_detector_raises(self, monkeypatch: pytest.MonkeyPatch, stub_hist) -> None:
        def boom(*_a: Any, **_k: Any):
            raise ValueError("bad series")

        monkeypatch.setattr("core.price_targets.compute_price_targets", boom)
        assert _run({"symbol": "600519"})["targets"] is None


class TestFailures:
    def test_missing_symbol_rejected(self) -> None:
        with pytest.raises(methods.MethodError) as exc:
            _run({})
        assert exc.value.code == "invalid_params"

    def test_empty_frame_is_data_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "integrations.stock_hist_repository.get_stock_hist",
            lambda *a, **k: pd.DataFrame(),
        )
        with pytest.raises(methods.MethodError) as exc:
            _run({"symbol": "000001"})
        assert exc.value.code == "data_unavailable"

    def test_upstream_detail_not_leaked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any):
            raise RuntimeError("tushare token invalid abc123")

        monkeypatch.setattr("integrations.stock_hist_repository.get_stock_hist", boom)
        with pytest.raises(methods.MethodError) as exc:
            _run({"symbol": "600519"})
        assert "abc123" not in exc.value.message


class TestRegistration:
    def test_registered_in_methods_table(self) -> None:
        assert methods.METHODS["wyckoff_events"] is methods.wyckoff_events
