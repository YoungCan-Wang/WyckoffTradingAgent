"""K 线 IPC 方法。

重点：列式结构（画图端按列消费，也省一半 payload）、天数裁剪、
NaN/Inf 必须变成 None（JSON 没有这两个值，漏出去前端会炸）、
数据源千奇百怪的异常要统一成一个可读错误。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from cli.ipc import methods


def _frame(n: int = 5) -> pd.DataFrame:
    """模拟 get_stock_hist 的返回：中文列名。"""
    return pd.DataFrame(
        {
            "日期": pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y-%m-%d"),
            "开盘": [10.0 + i for i in range(n)],
            "最高": [11.0 + i for i in range(n)],
            "最低": [9.0 + i for i in range(n)],
            "收盘": [10.5 + i for i in range(n)],
            "成交量": [1_000_000 + i for i in range(n)],
            "成交额": [10_500_000 + i for i in range(n)],
            "涨跌幅": [1.5] * n,
        }
    )


def _run(params: dict[str, Any]) -> dict[str, Any]:
    return list(methods.ohlcv(params))[0]


@pytest.fixture
def stub_hist(monkeypatch: pytest.MonkeyPatch):
    """替掉真实数据源 —— 测试不发网络请求。"""
    calls: list[dict[str, Any]] = []

    def fake_get(symbol, start_date, end_date, adjust="qfq"):
        calls.append({"symbol": symbol, "adjust": adjust, "start": start_date, "end": end_date})
        return _frame(fake_get.rows)

    fake_get.rows = 5
    monkeypatch.setattr("integrations.stock_hist_repository.get_stock_hist", fake_get)
    return calls, fake_get


class TestColumnarShape:
    def test_returns_columns_not_row_objects(self, stub_hist) -> None:
        bars = _run({"symbol": "600519"})["bars"]
        assert isinstance(bars, dict)
        assert bars["close"] == [10.5, 11.5, 12.5, 13.5, 14.5]
        assert len(bars["date"]) == 5

    def test_english_column_names(self, stub_hist) -> None:
        """前端按 open/high/low/close 消费，中文列名不能漏出去。"""
        bars = _run({"symbol": "600519"})["bars"]
        assert {"date", "open", "high", "low", "close", "volume"} <= set(bars)
        assert not [k for k in bars if not k.isascii()]

    def test_date_is_plain_iso_day(self, stub_hist) -> None:
        assert _run({"symbol": "600519"})["bars"]["date"][0] == "2026-01-01"

    def test_symbol_and_adjust_echoed(self, stub_hist) -> None:
        result = _run({"symbol": "00700.HK", "adjust": "hfq"})
        assert result["symbol"] == "00700.HK"
        assert result["adjust"] == "hfq"


class TestDayClamping:
    def test_tail_trims_to_requested_days(self, stub_hist) -> None:
        _calls, fake_get = stub_hist
        fake_get.rows = 50
        assert len(_run({"symbol": "600519", "days": 10})["bars"]["close"]) == 10

    def test_defaults_when_days_absent(self, stub_hist) -> None:
        assert _run({"symbol": "600519"})["bars"]["close"]

    def test_garbage_days_falls_back_to_default(self, stub_hist) -> None:
        """模型可能传 "abc"；不能让它把请求打挂。"""
        assert _run({"symbol": "600519", "days": "abc"})["bars"]["close"]

    def test_absurd_days_is_capped(self, stub_hist) -> None:
        assert methods._clamp_int(999_999, 320, 20, methods.MAX_OHLCV_DAYS) == methods.MAX_OHLCV_DAYS


class TestJsonSafety:
    def test_nan_becomes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JSON 没有 NaN —— 漏出去会让前端 JSON.parse 拿到非法值。"""
        frame = _frame(3)
        frame.loc[1, "收盘"] = float("nan")
        monkeypatch.setattr(
            "integrations.stock_hist_repository.get_stock_hist",
            lambda *a, **k: frame,
        )
        assert _run({"symbol": "600519"})["bars"]["close"][1] is None

    def test_inf_becomes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        frame = _frame(3)
        frame["成交额"] = frame["成交额"].astype(float)
        frame.loc[2, "成交额"] = float("inf")
        monkeypatch.setattr(
            "integrations.stock_hist_repository.get_stock_hist",
            lambda *a, **k: frame,
        )
        assert _run({"symbol": "600519"})["bars"]["amount"][2] is None


class TestFailures:
    def test_missing_symbol_rejected(self) -> None:
        with pytest.raises(methods.MethodError) as exc:
            _run({})
        assert exc.value.code == "invalid_params"

    def test_unknown_adjust_rejected(self) -> None:
        with pytest.raises(methods.MethodError) as exc:
            _run({"symbol": "600519", "adjust": "wild"})
        assert exc.value.code == "invalid_params"

    def test_empty_frame_is_data_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "integrations.stock_hist_repository.get_stock_hist",
            lambda *a, **k: pd.DataFrame(),
        )
        with pytest.raises(methods.MethodError) as exc:
            _run({"symbol": "000001"})
        assert exc.value.code == "data_unavailable"

    def test_source_exception_becomes_readable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """数据源可能抛任何东西；用户看到的必须是一句人话。"""

        def boom(*_a, **_k):
            raise RuntimeError("tushare 502 upstream")

        monkeypatch.setattr("integrations.stock_hist_repository.get_stock_hist", boom)
        with pytest.raises(methods.MethodError) as exc:
            _run({"symbol": "600519"})
        assert exc.value.code == "data_unavailable"
        assert "502" not in exc.value.message


class TestRegistration:
    def test_registered_in_methods_table(self) -> None:
        assert methods.METHODS["ohlcv"] is methods.ohlcv


class TestChartData:
    def test_fetches_once_for_bars_and_structure(self, stub_hist, monkeypatch: pytest.MonkeyPatch) -> None:
        calls, _fake_get = stub_hist
        monkeypatch.setattr("core.event_replay.replay_events", lambda *a, **k: [])

        result = list(methods.chart_data({"symbol": "600519", "days": 5}))[0]

        assert len(calls) == 1
        assert result["bars"]["close"][-1] == 14.5
        assert result["events"] == []
        assert {"trading_range", "targets", "annotations"} <= set(result)

    def test_registered_in_methods_table(self) -> None:
        assert methods.METHODS["chart_data"] is methods.chart_data


class TestSymbolNormalization:
    """用户现在能手输代码，所以图表也要走持仓账本那套正规化。"""

    def test_lowercase_us_ticker_is_normalized(self, stub_hist) -> None:
        calls, _fake_get = stub_hist

        result = _run({"symbol": "aapl", "days": 5})

        # 传给数据源的是规范码，不是用户原样输入。
        assert calls[0]["symbol"] == "AAPL.US"
        assert result["symbol"] == "AAPL.US"

    def test_short_hk_code_is_padded(self, stub_hist) -> None:
        calls, _fake_get = stub_hist

        _run({"symbol": "700.HK", "days": 5})

        assert calls[0]["symbol"] == "00700.HK"

    def test_a_share_code_passes_through(self, stub_hist) -> None:
        calls, _fake_get = stub_hist

        _run({"symbol": "600519", "days": 5})

        assert calls[0]["symbol"] == "600519"

    def test_whitespace_is_trimmed(self, stub_hist) -> None:
        calls, _fake_get = stub_hist

        _run({"symbol": "  600519  ", "days": 5})

        assert calls[0]["symbol"] == "600519"

    def test_unrecognized_symbol_is_rejected_before_fetching(self, stub_hist) -> None:
        """认不出的代码不该打到数据源 —— 那样错误会变成含糊的「取不到行情」。"""
        calls, _fake_get = stub_hist

        with pytest.raises(methods.MethodError) as error:
            _run({"symbol": "not a code!", "days": 5})

        assert error.value.code == "invalid_params"
        assert calls == []

    def test_bare_short_digits_are_rejected(self, stub_hist) -> None:
        """裸 1-5 位数字会和残缺 A 股码混淆，正规化那层故意不接受。"""
        calls, _fake_get = stub_hist

        with pytest.raises(methods.MethodError):
            _run({"symbol": "123", "days": 5})

        assert calls == []

    def test_missing_symbol_still_reported(self, stub_hist) -> None:
        with pytest.raises(methods.MethodError) as error:
            _run({"days": 5})
        assert error.value.code == "invalid_params"

    def test_wyckoff_events_shares_the_normalization(self, stub_hist, monkeypatch) -> None:
        """两个方法共用 _chart_frame，正规化不该只在其中一个生效。"""
        calls, _fake_get = stub_hist
        monkeypatch.setattr("core.event_replay.replay_events", lambda *a, **k: [])

        result = list(methods.wyckoff_events({"symbol": "aapl", "days": 5}))[0]

        assert calls[0]["symbol"] == "AAPL.US"
        assert result["symbol"] == "AAPL.US"
