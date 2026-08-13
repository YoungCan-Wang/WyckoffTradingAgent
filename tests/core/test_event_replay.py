"""威科夫事件历史重放。

重点：
- 重放必须复用生产检测器（规则只有一份），所以这里用打桩验证"喂进去的是
  扩展窗口、第 i 根是检测器眼里的最后一根"这个契约。
- 锚定价位：spring/LPS 锚最低价（影线尖），SOS/EVR 锚收盘价 —— 锚错了标注
  会飘离 K 线。
- 畸形数据只能少画标注，不能让整条时间线炸掉。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from core import event_replay


def _frame(n: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y-%m-%d"),
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [float(9 - i * 0.01) for i in range(n)],
            "close": [float(10 + i * 0.01) for i in range(n)],
            "volume": [1_000_000] * n,
        }
    )


class _Cfg:
    """窗口开小一点，测试不用喂几百根。"""

    spring_support_window = 10
    lps_vol_ref_window = 5
    lps_lookback = 3
    sos_vol_window = 5
    evr_vol_window = 5


def _only(kind: str, hit_at: set[int]):
    """构造一个只在指定索引命中的假检测器，用于验证重放契约。"""
    seen: list[int] = []

    def detect(window: pd.DataFrame, _cfg: Any, *_rest: Any) -> float | None:
        index = len(window) - 1
        seen.append(index)
        return 1.5 if index in hit_at else None

    detect.seen = seen  # type: ignore[attr-defined]
    detect.kind = kind  # type: ignore[attr-defined]
    return detect


@pytest.fixture
def stub_detectors(monkeypatch: pytest.MonkeyPatch):
    """替掉真实检测器，让断言只针对重放逻辑本身。"""

    def install(specs):
        monkeypatch.setattr(event_replay, "_detectors", lambda: specs)

    return install


class TestReplayContract:
    def test_feeds_expanding_windows(self, stub_detectors) -> None:
        """第 i 根必须是检测器眼里的最后一根 —— 这是复用规则的前提。"""
        detect = _only("spring", set())
        stub_detectors([("spring", detect, "low")])
        event_replay.replay_events(_frame(30), cfg=_Cfg())
        # 每次调用看到的窗口长度应严格递增，且以全长收尾。
        assert detect.seen == sorted(detect.seen)
        assert detect.seen[-1] == 29

    def test_event_carries_date_and_score(self, stub_detectors) -> None:
        stub_detectors([("sos", _only("sos", {20}), "close")])
        events = event_replay.replay_events(_frame(30), cfg=_Cfg())
        assert len(events) == 1
        assert events[0]["date"] == _frame(30)["date"].iloc[20]
        assert events[0]["kind"] == "sos"
        assert events[0]["score"] == 1.5


class TestAnchorPrice:
    def test_low_anchor_uses_bar_low(self, stub_detectors) -> None:
        """spring 是下探动作，标在影线尖上才对得上视觉。"""
        frame = _frame(30)
        stub_detectors([("spring", _only("spring", {25}), "low")])
        events = event_replay.replay_events(frame, cfg=_Cfg())
        assert events[0]["price"] == frame["low"].iloc[25]

    def test_close_anchor_uses_bar_close(self, stub_detectors) -> None:
        frame = _frame(30)
        stub_detectors([("sos", _only("sos", {25}), "close")])
        events = event_replay.replay_events(frame, cfg=_Cfg())
        assert events[0]["price"] == frame["close"].iloc[25]


class TestClusterThinning:
    def test_adjacent_same_kind_collapses_to_best(self, stub_detectors) -> None:
        """检测器常在连续几根都成立；逐根都标会糊成一片。"""

        def detect(window: pd.DataFrame, _cfg: Any, *_rest: Any) -> float | None:
            index = len(window) - 1
            scores = {20: 1.0, 21: 9.0, 22: 2.0}
            return scores.get(index)

        stub_detectors([("spring", detect, "low")])
        events = event_replay.replay_events(_frame(30), cfg=_Cfg(), min_gap=5)
        assert len(events) == 1
        assert events[0]["score"] == 9.0

    def test_far_apart_hits_both_kept(self, stub_detectors) -> None:
        def detect(window: pd.DataFrame, _cfg: Any, *_rest: Any) -> float | None:
            return 1.0 if len(window) - 1 in {15, 29} else None

        stub_detectors([("spring", detect, "low")])
        events = event_replay.replay_events(_frame(40), cfg=_Cfg(), min_gap=5)
        assert len(events) == 2

    def test_gap_is_measured_in_bars_not_hit_order(self, stub_detectors) -> None:
        """回归：曾用"命中序"算间隔，导致相隔 14 根的两次事件被误并成一簇。"""

        def detect(window: pd.DataFrame, _cfg: Any, *_rest: Any) -> float | None:
            return 1.0 if len(window) - 1 in {15, 29} else None

        stub_detectors([("spring", detect, "low")])
        events = event_replay.replay_events(_frame(40), cfg=_Cfg(), min_gap=5)
        assert [e["bar"] for e in events] == [15, 29]

    def test_different_kinds_never_merge(self, stub_detectors) -> None:
        """同一天可以既是 spring 又是 SOS（长下影后放量收高）。"""
        stub_detectors(
            [
                ("spring", _only("spring", {25}), "low"),
                ("sos", _only("sos", {25}), "close"),
            ]
        )
        events = event_replay.replay_events(_frame(30), cfg=_Cfg())
        assert {e["kind"] for e in events} == {"spring", "sos"}


class TestRobustness:
    def test_empty_frame_returns_empty(self) -> None:
        assert event_replay.replay_events(pd.DataFrame()) == []

    def test_missing_columns_returns_empty(self) -> None:
        bad = pd.DataFrame({"date": ["2026-01-01"], "close": [10.0]})
        assert event_replay.replay_events(bad) == []

    def test_too_short_returns_empty(self) -> None:
        assert event_replay.replay_events(_frame(5), cfg=_Cfg()) == []

    def test_detector_exception_does_not_break_timeline(self, stub_detectors) -> None:
        """单个检测器炸掉不该让整张图没有标注。"""

        def boom(*_a: Any, **_k: Any) -> float | None:
            raise ValueError("bad bar")

        stub_detectors([("spring", boom, "low"), ("sos", _only("sos", {25}), "close")])
        events = event_replay.replay_events(_frame(30), cfg=_Cfg())
        assert [e["kind"] for e in events] == ["sos"]

    def test_nan_price_is_skipped(self, stub_detectors) -> None:
        """NaN 价位画不出来，也不能塞进 JSON。"""
        frame = _frame(30)
        frame.loc[25, "low"] = float("nan")
        stub_detectors([("spring", _only("spring", {25}), "low")])
        assert event_replay.replay_events(frame, cfg=_Cfg()) == []


class TestRealDetectors:
    def test_real_detectors_are_wired(self) -> None:
        """默认检测器就是生产用的那几个 —— 规则不能分叉。"""
        from core import wyckoff_engine as we

        wired = {kind: fn for kind, fn, _anchor in event_replay._detectors()}
        assert wired["spring"] is we._detect_spring
        assert wired["lps"] is we._detect_lps
        assert wired["sos"] is we._detect_sos
        assert wired["evr"] is we._detect_evr
