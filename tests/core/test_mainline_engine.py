from __future__ import annotations

import pandas as pd

from core.mainline_engine import MainlineEngineConfig, build_mainline_candidates, mainline_candidate_entries
from core.wyckoff_engine import FunnelConfig, run_funnel


def _frame(values: list[float], *, volume_tail: float = 900.0, amount: float = 100_000_000.0) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(values), freq="B")
    volume = [1000.0] * max(len(values) - 5, 0) + [volume_tail] * min(5, len(values))
    return pd.DataFrame(
        {
            "date": dates,
            "open": [v * 0.99 for v in values],
            "high": [v * 1.01 for v in values],
            "low": [v * 0.98 for v in values],
            "close": values,
            "volume": volume,
            "amount": [amount] * len(values),
            "pct_chg": pd.Series(values).pct_change().fillna(0.0) * 100.0,
        }
    )


def _trend_values(days: int = 140) -> list[float]:
    return [10 + i * 0.05 for i in range(days - 8)] + [16.0, 16.2, 16.4, 16.6, 16.5, 16.4, 16.3, 16.2]


def _high_mainline_values() -> list[float]:
    base = [10 + i * (8 / 109) for i in range(110)]
    return base + [18.2 + i * 0.4 for i in range(20)]


def _event_reversal_values() -> list[float]:
    base = [25 - i * 0.07 for i in range(90)]
    repair_base = [18.4, 18.1, 17.9, 17.7, 17.5, 17.8, 18.0, 17.7, 17.9, 18.1]
    return base + repair_base * 5


def test_mainline_dynamic_theme_can_bypass_l2_but_requires_timing() -> None:
    candidates = build_mainline_candidates(
        l1_passed=["000001"],
        l2_passed=[],
        concept_map={"000001": ["军工信息化"]},
        concept_heat=[{"name": "军工信息化", "pct": 5.5, "net_inflow": 900_000_000}],
        theme_radar={"themes": [{"theme": "军工信息化", "score": 0.72}], "strategic_candidates": []},
        df_map={"000001": _frame(_trend_values())},
        financial_map={"000001": {"roe": 12, "debt_to_asset_ratio": 45, "revenue_yoy": 20}},
        name_map={"000001": "动态主线A"},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["theme"] == "军工信息化"
    assert candidates[0]["l2_passed"] is False
    assert candidates[0]["status"] == "主线买点候选"
    assert mainline_candidate_entries(candidates, max_count=3)[0]["signal_key"] == "mainline"


def test_mainline_event_reversal_theme_can_bypass_l2_with_liquidity() -> None:
    candidates = build_mainline_candidates(
        l1_passed=["000010"],
        l2_passed=[],
        concept_map={"000010": ["创新药"]},
        concept_heat=[{"name": "创新药", "pct": 5.2, "net_inflow": 900_000_000}],
        theme_radar={"themes": [{"theme": "创新药", "score": 0.70}], "strategic_candidates": []},
        df_map={"000010": _frame(_event_reversal_values(), amount=200_000_000.0)},
        financial_map={},
        name_map={"000010": "事件修复A"},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["theme"] == "创新药医药"
    assert candidates[0]["status"] == "事件主题修复候选"
    assert "事件主题低位修复" in candidates[0]["entry_type"]
    assert mainline_candidate_entries(candidates, max_count=3)


def test_mainline_can_seed_candidates_from_theme_activity() -> None:
    candidates = build_mainline_candidates(
        l1_passed=["000011"],
        l2_passed=[],
        concept_map={"000011": ["减速器"]},
        concept_heat=[],
        theme_radar={"themes": [], "strategic_candidates": []},
        theme_activity={"themes": [{"theme": "机器人", "score": 0.72}]},
        df_map={"000011": _frame(_event_reversal_values(), amount=200_000_000.0)},
        financial_map={},
        name_map={"000011": "活跃主题A"},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["theme"] == "机器人"
    assert candidates[0]["source"] == "concept_map"
    assert mainline_candidate_entries(candidates, max_count=3)


def test_mainline_can_seed_candidates_from_ths_hot_event_without_concept_map() -> None:
    candidates = build_mainline_candidates(
        l1_passed=["000012"],
        l2_passed=[],
        concept_map={},
        concept_heat=[],
        theme_radar={"themes": [], "strategic_candidates": []},
        hot_events={
            "events": [
                {
                    "event_id": "evt-robot",
                    "theme": "人形机器人",
                    "title": "机器人主题发酵",
                    "heat": 650000,
                    "rise_pct": 2.1,
                    "limit_up_count": 20,
                    "stocks": [{"code": "000012", "name": "事件机器人", "reason": "灵巧手"}],
                }
            ]
        },
        df_map={"000012": _frame(_trend_values())},
        financial_map={},
        name_map={},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["theme"] == "机器人"
    assert candidates[0]["source"] == "ths_hot_event"
    assert candidates[0]["theme_event_id"] == "evt-robot"
    entry = mainline_candidate_entries(candidates, max_count=3)[0]
    assert entry["opportunity"] == "事件主线: 机器人"
    assert entry["metrics"]["theme_event_reason"] == "灵巧手"


def test_mainline_blocks_candidate_without_timing_gate() -> None:
    weak = [10 + i * 0.03 for i in range(120)] + [10.0, 9.8, 9.5, 9.2, 9.1]
    candidates = build_mainline_candidates(
        l1_passed=["000002"],
        l2_passed=[],
        concept_map={"000002": ["机器人"]},
        concept_heat=[{"name": "机器人", "pct": 4.0, "net_inflow": 700_000_000}],
        theme_radar={"themes": [{"theme": "机器人", "score": 0.70}], "strategic_candidates": []},
        df_map={"000002": _frame(weak)},
        financial_map={},
        name_map={},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["status"] == "主线观察"
    assert mainline_candidate_entries(candidates, max_count=3) == []


def test_mainline_theme_activity_can_seed_alias_theme_without_concept_heat() -> None:
    candidates = build_mainline_candidates(
        l1_passed=["000011"],
        l2_passed=[],
        concept_map={"000011": ["减速器"]},
        concept_heat=[],
        theme_radar={"themes": [], "strategic_candidates": []},
        theme_activity={"themes": [{"theme": "机器人", "score": 0.74}]},
        df_map={"000011": _frame(_trend_values())},
        financial_map={},
        name_map={"000011": "机器人链A"},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["theme"] == "机器人"
    assert candidates[0]["status"] == "主线买点候选"


def test_mainline_high_bias_can_enter_divergence_pool() -> None:
    # 更陡的高位延伸，确保触发高位抱团并进入分歧可交易池。
    steep = [10 + i * (10 / 109) for i in range(110)] + [20.5 + i * 0.55 for i in range(20)]
    candidates = build_mainline_candidates(
        l1_passed=["000003"],
        l2_passed=["000003"],
        concept_map={"000003": ["消费电子"]},
        concept_heat=[{"name": "消费电子", "pct": 7.2, "net_inflow": 1_100_000_000}],
        theme_radar={"themes": [{"theme": "消费电子", "score": 0.76}], "strategic_candidates": []},
        df_map={"000003": _frame(steep)},
        financial_map={},
        name_map={},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["status"] in {"强主线分歧", "主线买点候选"}
    assert candidates[0]["status"] != "过热不追"
    assert mainline_candidate_entries(candidates, max_count=3)
    assert mainline_candidate_entries(candidates, max_count=3)[0]["signal_key"] == "mainline"


def test_mainline_event_reversal_enters_tradeable_pool_before_observe() -> None:
    candidates = build_mainline_candidates(
        l1_passed=["000006", "000007"],
        l2_passed=[],
        concept_map={"000006": ["创新药"], "000007": ["创新药"]},
        concept_heat=[{"name": "创新药", "pct": 4.2, "net_inflow": 900_000_000}],
        theme_radar={"themes": [{"theme": "创新药", "score": 0.70}], "strategic_candidates": []},
        df_map={
            "000006": _frame(_event_reversal_values(), amount=180_000_000.0),
            "000007": _frame([10 + i * 0.02 for i in range(90)] + [10.2] * 30),
        },
        financial_map={},
        name_map={"000006": "修复候选", "000007": "观察候选"},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["code"] == "000006"
    assert candidates[0]["status"] == "事件主题修复候选"
    assert "事件主题低位修复" in candidates[0]["entry_type"]
    assert mainline_candidate_entries(candidates, max_count=3)[0]["code"] == "000006"


def test_mainline_fish_tail_does_not_enter_tradeable_pool() -> None:
    fish_tail = [10 + i * 0.03 for i in range(110)] + [18 + i * 0.9 for i in range(20)]
    candidates = build_mainline_candidates(
        l1_passed=["000004"],
        l2_passed=["000004"],
        concept_map={"000004": ["消费电子"]},
        concept_heat=[{"name": "消费电子", "pct": 7.2, "net_inflow": 1_100_000_000}],
        theme_radar={"themes": [{"theme": "消费电子", "score": 0.76}], "strategic_candidates": []},
        df_map={"000004": _frame(fish_tail, volume_tail=2600)},
        financial_map={},
        name_map={},
        config=MainlineEngineConfig(),
    )

    assert candidates[0]["status"] == "过热不追"
    assert "鱼尾加速" in candidates[0]["risk_flags"]
    assert mainline_candidate_entries(candidates, max_count=3) == []


def test_mainline_configured_themes_are_optional_not_default_targets() -> None:
    cfg = MainlineEngineConfig()
    assert cfg.themes == ()
    assert cfg.core_basket == ()


def test_run_funnel_merges_mainline_entries_when_configured() -> None:
    frame = _frame(_trend_values())
    cfg = FunnelConfig()
    cfg.ma_long = 60

    result = run_funnel(
        all_symbols=["000001"],
        df_map={"000001": frame},
        bench_df=frame,
        name_map={"000001": "动态主线A"},
        market_cap_map={},
        sector_map={"000001": "通信设备"},
        cfg=cfg,
        concept_map={"000001": ["军工信息化"]},
        concept_heat=[{"name": "军工信息化", "pct": 5.5, "net_inflow": 900_000_000}],
        financial_map={"000001": {"roe": 12, "debt_to_asset_ratio": 45, "revenue_yoy": 20}},
        mainline_config=MainlineEngineConfig(max_ai_candidates=3),
    )

    assert any(item["signal_key"] == "mainline" for item in result.candidate_entries)
