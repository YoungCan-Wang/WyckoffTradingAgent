from __future__ import annotations

from integrations.ths_hot_concept import (
    merge_concept_heat,
    summarize_ths_hot_events,
    ths_hot_events_to_concept_heat,
)


def test_ths_hot_events_convert_to_theme_heat_and_keep_stocks() -> None:
    snapshot = {
        "events": [
            {
                "event_id": "e1",
                "theme": "人形机器人",
                "title": "机器人催化",
                "heat": 677000,
                "rise_pct": 1.86,
                "limit_up_count": 38,
                "stocks": [{"code": "301279", "name": "金道科技", "reason": "人形机器人"}],
                "theme_table": [
                    {
                        "theme": "人形机器人",
                        "rise_pct": 2.08,
                        "limit_up_count": 38,
                        "stocks": [{"code": "300607", "name": "拓斯达", "reason": "加工设备"}],
                        "children": [{"theme": "减速器", "stocks": [{"code": "002896", "name": "中大力德"}]}],
                    }
                ],
            }
        ]
    }

    rows = ths_hot_events_to_concept_heat(snapshot)
    names = {row["name"] for row in rows}

    assert "机器人" in names
    assert rows[0]["source"] == "ths_hot_event"
    all_top_codes = {stock["code"] for row in rows for stock in row.get("top_stocks", [])}
    assert "301279" in all_top_codes
    assert "机器人" in summarize_ths_hot_events(snapshot)


def test_merge_concept_heat_prefers_event_heat_for_same_alias() -> None:
    merged = merge_concept_heat(
        [{"name": "机器人概念", "pct": 0.5, "net_inflow": 100.0}],
        [{"name": "人形机器人", "pct": 2.0, "event_heat": 500000, "source": "ths_hot_event"}],
    )

    assert len(merged) == 1
    assert merged[0]["source"] == "ths_hot_event"
