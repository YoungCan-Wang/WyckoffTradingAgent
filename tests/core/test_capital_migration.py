from __future__ import annotations

from core.capital_migration import build_capital_migration_report


def test_capital_migration_identifies_flow_from_old_theme_to_new_mainline() -> None:
    report = build_capital_migration_report(
        trade_date="2026-06-29",
        concept_heat=[
            {"name": "CPO", "pct": 3.8, "net_inflow": 1_200_000_000},
            {"name": "存储芯片", "pct": 2.6, "net_inflow": 800_000_000},
        ],
        concept_history={
            "2026-06-29": {"CPO": {"pct": 3.8, "inflow": 1_200_000_000}},
            "2026-06-26": {"医药": {"pct": 4.1, "inflow": 600_000_000}},
        },
        sector_rotation={
            "state_map": {
                "医药": {
                    "state": "DISTRIBUTION_RISK",
                    "label": "退潮派发风险",
                    "ret_3d": -2.2,
                    "amount_ratio_3d": 1.18,
                    "breakdown_pct": 31.0,
                }
            }
        },
        theme_radar={"themes": [{"theme": "光模块", "score": 0.78, "state": "confirmed"}]},
    )

    assert report["confidence"] == "high"
    assert report["inflow"][0]["theme"] == "光模块"
    assert any(item["theme"] == "医药" for item in report["outflow"])
    assert "转向光模块" in report["summary"]


def test_capital_migration_stays_empty_without_evidence() -> None:
    report = build_capital_migration_report(
        trade_date="2026-06-29",
        concept_heat=[],
        concept_history={},
        sector_rotation={"state_map": {}},
        theme_radar={},
    )

    assert report["confidence"] == "low"
    assert report["summary"] == "暂无明确资金迁徙信号"
    assert report["rotation"] == []


def test_capital_migration_accepts_concept_heat_in_yi_units() -> None:
    report = build_capital_migration_report(
        trade_date="2026-06-29",
        concept_heat=[{"name": "共封装光学(CPO)", "pct": 2.8, "net_inflow": 11.98}],
        concept_history={},
        sector_rotation={"state_map": {}},
        theme_radar={},
    )

    assert report["inflow"][0]["theme"] == "光模块"
    assert "12.0亿" in report["inflow"][0]["evidence"]


def test_capital_migration_requires_positive_inflow_for_inflow_bucket() -> None:
    report = build_capital_migration_report(
        trade_date="2026-06-29",
        concept_heat=[{"name": "光模块", "pct": 7.0, "net_inflow": -2.0}],
        concept_history={},
        sector_rotation={"state_map": {}},
        theme_radar={"themes": [{"theme": "光模块", "score": 0.9, "state": "confirmed"}]},
    )

    assert report["inflow"] == []
    assert report["summary"] == "暂无明确资金迁徙信号"


def test_capital_migration_exposes_stock_level_theme_activity() -> None:
    report = build_capital_migration_report(
        trade_date="2026-06-30",
        concept_heat=[{"name": "芯片", "pct": 5.0, "net_inflow": 500.0}],
        concept_history={},
        sector_rotation={"state_map": {}},
        theme_radar={},
        theme_activity={
            "themes": [
                {
                    "theme": "机器人",
                    "score": 0.66,
                    "median_ret": 4.2,
                    "up_ratio": 0.82,
                    "strong_count": 18,
                }
            ]
        },
    )

    assert report["activity"][0]["theme"] == "机器人"
    assert "上涨占比82%" in report["activity"][0]["evidence"]
