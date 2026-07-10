from __future__ import annotations

import pandas as pd

from core.theme_activity import ThemeActivityConfig, build_theme_activity_snapshot, summarize_theme_activity


def _bars(prev: float, last: float, volume_ratio: float = 1.0) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=22, freq="B")
    close = [prev] * 21 + [last]
    volume = [1000.0] * 21 + [1000.0 * volume_ratio]
    return pd.DataFrame({"date": dates, "close": close, "volume": volume})


def test_theme_activity_aggregates_robot_aliases_from_stock_bars() -> None:
    snapshot = build_theme_activity_snapshot(
        trade_date="2026-06-30",
        df_map={
            "300024": _bars(10, 11.0, 2.0),
            "000001": _bars(10, 10.6, 1.6),
            "000002": _bars(10, 10.5, 1.4),
            "000003": _bars(10, 10.4, 1.3),
            "000004": _bars(10, 10.3, 1.2),
            "600000": _bars(10, 9.8, 0.8),
        },
        concept_map={
            "300024": ["机器人概念"],
            "000001": ["减速器"],
            "000002": ["机器视觉"],
            "000003": ["人形机器人"],
            "000004": ["伺服系统"],
            "600000": ["银行"],
        },
        sector_map={},
        concept_heat=[{"name": "减速器", "pct": 3.5, "net_inflow": 60.0}],
        config=ThemeActivityConfig(top_themes=5, min_members=3, min_score=0.0),
    )

    robot = next(item for item in snapshot["themes"] if item["theme"] == "机器人")
    assert robot["member_count"] == 5
    assert robot["strong_count"] == 3
    assert robot["median_ret"] > 4.0
    assert "机器人" in summarize_theme_activity(snapshot)


def test_theme_activity_filters_noise_before_top_limit() -> None:
    snapshot = build_theme_activity_snapshot(
        trade_date="2026-06-30",
        df_map={
            "NOISE": _bars(10, 11.0, 2.5),
            "R1": _bars(10, 10.6, 1.5),
            "R2": _bars(10, 10.5, 1.4),
            "R3": _bars(10, 10.4, 1.3),
        },
        concept_map={
            "NOISE": ["单点题材"],
            "R1": ["减速器"],
            "R2": ["机器视觉"],
            "R3": ["人形机器人"],
        },
        sector_map={},
        config=ThemeActivityConfig(top_themes=1, min_members=3, min_score=0.0),
    )

    assert [row["theme"] for row in snapshot["themes"]] == ["机器人"]
