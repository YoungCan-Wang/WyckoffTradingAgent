from __future__ import annotations

import pandas as pd

from core.theme_radar import ThemeRadarConfig, build_theme_radar_snapshot, normalize_theme_name, summarize_theme_radar


def _trend_frame(start: float, step: float, days: int = 280) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=days, freq="B")
    close = [start + i * step for i in range(days)]
    return pd.DataFrame({"date": dates, "close": close, "volume": [1000 + i for i in range(days)]})


def _compound_frame(start: float, daily_rate: float, days: int = 280) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=days, freq="B")
    close = [start * ((1.0 + daily_rate) ** i) for i in range(days)]
    return pd.DataFrame({"date": dates, "close": close, "volume": [1000 + i for i in range(days)]})


def test_theme_radar_promotes_persistent_structural_theme() -> None:
    snapshot = build_theme_radar_snapshot(
        trade_date="2026-05-27",
        concept_heat=[{"name": "半导体", "pct": 4.2, "net_inflow": 600_000_000}],
        concept_history={
            "2026-05-27": {"半导体": {"pct": 4.2, "inflow": 600_000_000}},
            "2026-05-26": {"芯片": {"pct": 2.1, "inflow": 300_000_000}},
            "2026-05-25": {"先进封装": {"pct": 1.8, "inflow": 200_000_000}},
        },
        concept_map={"000001": ["半导体"], "000002": ["芯片"], "000003": ["白酒"]},
        sector_map={"000001": "半导体", "000002": "电子", "000003": "食品饮料"},
        df_map={
            "000001": _trend_frame(10, 0.08),
            "000002": _trend_frame(8, 0.06),
            "000003": _trend_frame(20, -0.01),
        },
        events=[{"title": "半导体 AI chip demand expands", "source": "test"}],
        name_map={"000001": "芯片A", "000002": "芯片B"},
    )

    top = snapshot["themes"][0]
    assert top["theme"] == "芯片半导体"
    assert top["score"] >= 0.45
    assert snapshot["strategic_candidates"][0]["theme"] == "芯片半导体"
    assert "芯片半导体" in summarize_theme_radar(snapshot)


def test_theme_radar_ranks_long_horizon_leaders_inside_theme() -> None:
    snapshot = build_theme_radar_snapshot(
        trade_date="2026-05-27",
        concept_heat=[{"name": "光模块", "pct": 3.5, "net_inflow": 500_000_000}],
        concept_history={"2026-05-27": {"CPO": {"pct": 3.5, "inflow": 500_000_000}}},
        concept_map={"000001": ["CPO"], "000002": ["光通信"], "000003": ["白酒"]},
        sector_map={"000001": "通信设备", "000002": "通信设备", "000003": "食品饮料"},
        df_map={
            "000001": _compound_frame(10, 0.006),
            "000002": _compound_frame(10, 0.003),
            "000003": _trend_frame(20, -0.01),
        },
        name_map={"000001": "光模块龙头", "000002": "光通信跟随"},
    )

    candidates = [item for item in snapshot["strategic_candidates"] if item["theme"] == "光模块"]
    assert candidates[0]["code"] == "000001"
    assert candidates[0]["theme_rank"] == 1
    assert candidates[0]["leader_score"] > candidates[1]["leader_score"]
    assert candidates[0]["ret120"] > 100
    assert candidates[0]["near_high_120d"] is True


def test_theme_radar_keeps_one_best_theme_per_strategic_candidate() -> None:
    snapshot = build_theme_radar_snapshot(
        trade_date="2026-05-27",
        concept_heat=[
            {"name": "半导体", "pct": 5.0, "net_inflow": 900_000_000},
            {"name": "光模块", "pct": 2.0, "net_inflow": 100_000_000},
        ],
        concept_history={"2026-05-27": {"半导体": {"pct": 5.0, "inflow": 900_000_000}}},
        concept_map={
            "000001": ["半导体", "CPO"],
            "000002": ["光模块"],
        },
        sector_map={"000001": "半导体", "000002": "通信设备"},
        df_map={
            "000001": _compound_frame(10, 0.005),
            "000002": _compound_frame(10, 0.004),
        },
        config=ThemeRadarConfig(min_theme_score=0.0, min_stock_score=0.0),
        name_map={"000001": "跨主题龙头", "000002": "跟随股"},
    )

    rows = [item for item in snapshot["strategic_candidates"] if item["code"] == "000001"]

    assert len(rows) == 1
    assert rows[0]["theme"] == "芯片半导体"


def test_theme_radar_filters_non_actionable_index_noise() -> None:
    snapshot = build_theme_radar_snapshot(
        trade_date="2026-05-27",
        concept_heat=[
            {"name": "日经225", "pct": 9.0, "net_inflow": 900_000_000},
            {"name": "半导体", "pct": 3.8, "net_inflow": 500_000_000},
        ],
        concept_history={"2026-05-27": {"日经225": {"pct": 9.0, "inflow": 900_000_000}}},
        concept_map={"000001": ["日经225"], "000002": ["半导体"]},
        sector_map={"000001": "日经225", "000002": "半导体"},
        df_map={"000001": _trend_frame(10, 0.10), "000002": _trend_frame(10, 0.08)},
        name_map={"000001": "指数噪声", "000002": "芯片A"},
        config=ThemeRadarConfig(min_theme_score=0.0, min_stock_score=0.0),
    )

    theme_names = {item["theme"] for item in snapshot["themes"]}
    assert "日经225" not in theme_names
    assert "芯片半导体" in theme_names


def test_theme_radar_normalizes_defensive_value_aliases() -> None:
    assert normalize_theme_name("高股息央企红利") == "红利低波"
    assert normalize_theme_name("银行保险走强") == "大金融"
    assert normalize_theme_name("火电公用事业") == "公用事业"


def test_theme_radar_normalizes_mainline_aliases() -> None:
    assert normalize_theme_name("CPO 800G 光模块") == "光模块"
    assert normalize_theme_name("MLCC被动元件") == "MLCC被动元件"
    assert normalize_theme_name("陶瓷电容扩产") == "MLCC被动元件"
    assert normalize_theme_name("国产CPU替代") == "国产CPU"
    assert normalize_theme_name("创新药进入商保目录") == "创新药医药"
    assert normalize_theme_name("可控核聚变人造太阳") == "核聚变核电"


def test_theme_radar_snapshot_round_trip_local_db(tmp_path, monkeypatch) -> None:
    from integrations import local_db

    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "theme.db")
    try:
        local_db.init_db()
        local_db.save_theme_radar_snapshot({"trade_date": "2026-05-27", "themes": [], "strategic_candidates": []})
        assert local_db.load_latest_theme_radar_snapshot()["trade_date"] == "2026-05-27"
    finally:
        if local_db._conn is not None:
            local_db._conn.close()
        local_db._conn = None
