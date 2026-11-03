"""Unit tests for dynamic policy discovery and self-iterating cache."""

from __future__ import annotations

from pathlib import Path

from integrations.policy_discovery import (
    analyze_policy_headlines_with_deepseek,
    build_funnel_policy_events,
    load_dynamic_policy_themes,
    refresh_dynamic_policy_themes,
)


def test_refresh_and_load_dynamic_policy_themes_with_ttl(tmp_path: Path) -> None:
    cache_path = tmp_path / "dynamic_policy.json"
    discovered = {
        "dynamic_aliases": {
            "红利低波": ["SFISF互换便利", "10bp招标", "AI", "银行"],
            "未知野生主题": ["野生概念词"],
        },
        "boost_themes": [
            {
                "theme": "SFISF互换便利",
                "weight": 0.95,
                "title": "央行开展1000亿互换便利操作，中标费率10bp",
                "source": "央行官网",
            }
        ],
        "squeeze_themes": [
            {
                "theme": "算力租赁",
                "penalty": 0.65,
                "title": "金管总局警示AI投资过度集中推高杠杆",
                "source": "金管总局",
            }
        ],
    }

    saved = refresh_dynamic_policy_themes(
        trade_date="2026-11-03",
        discovered_policy=discovered,
        path=cache_path,
    )
    assert saved["boost_themes"][0]["theme"] == "红利低波"
    assert saved["boost_themes"][0]["weight"] == 0.85
    assert saved["squeeze_themes"][0]["theme"] == "服务器算力"
    assert "未知野生主题" not in saved["dynamic_aliases"]
    assert saved["dynamic_aliases"]["红利低波"] == ["SFISF互换便利", "10bp招标"]

    loaded_fresh = load_dynamic_policy_themes(trade_date="2026-11-10", path=cache_path)
    assert loaded_fresh["dynamic_aliases"]["红利低波"] == ["SFISF互换便利", "10bp招标"]

    loaded_expired = load_dynamic_policy_themes(trade_date="2026-11-25", path=cache_path)
    assert loaded_expired["boost_themes"] == []
    assert loaded_expired["dynamic_aliases"] == {}


def test_policy_decay_and_cluster_confirmation_renewal(tmp_path: Path) -> None:
    cache_path = tmp_path / "dynamic_policy.json"
    refresh_dynamic_policy_themes(
        trade_date="2026-11-03",
        discovered_policy={
            "boost_themes": [
                {"theme": "红利低波", "weight": 0.80, "title": "SFISF落地", "source": "央行"},
                {"theme": "机器人", "weight": 0.50, "title": "展会消息", "source": "快讯"},
            ]
        },
        path=cache_path,
    )

    updated = refresh_dynamic_policy_themes(
        trade_date="2026-11-04",
        discovered_policy={},
        cluster_watch=[{"theme": "红利低波", "cluster_score": 0.62}],
        path=cache_path,
    )
    weights = {item["theme"]: item["weight"] for item in updated["boost_themes"]}
    assert weights["红利低波"] == 0.80
    assert weights["机器人"] == 0.46


def test_analyze_policy_headlines_with_deepseek_parses_fenced_json(monkeypatch) -> None:
    monkeypatch.setattr(
        "integrations.policy_discovery.get_provider_credentials",
        lambda provider: ("sk-test", "deepseek-v4-flash", "https://api.deepseek.com"),
    )
    monkeypatch.setattr(
        "integrations.policy_discovery.call_llm",
        lambda **kwargs: (
            "```json\n"
            "{\n"
            '  "dynamic_aliases": {"价值蓝筹": ["挤压传统行业融资空间"]},\n'
            '  "boost_themes": [{"theme": "价值蓝筹", "weight": 0.82, "title": "修复传统行业融资", "source": "金管总局"}],\n'
            '  "squeeze_themes": [{"theme": "服务器算力", "penalty": 0.68, "title": "警示AI高杠杆", "source": "金管总局"}]\n'
            "}\n"
            "```"
        ),
    )
    result = analyze_policy_headlines_with_deepseek(
        [{"title": "金管总局：AI投资过度集中推高杠杆、挤压传统行业融资空间", "source": "财联社"}],
        trade_date="2026-11-03",
    )
    assert result["boost_themes"][0]["theme"] == "价值蓝筹"
    assert result["squeeze_themes"][0]["theme"] == "服务器算力"


def test_build_funnel_policy_events_normalizes_ths_events_with_dynamic_aliases() -> None:
    events = build_funnel_policy_events(
        {"events": [{"title": "央行SFISF互换便利1000亿落地", "themes": ["SFISF互换便利"]}]},
        {"dynamic_aliases": {"红利低波": ["SFISF互换便利"]}},
    )
    assert events[0]["themes"] == ["红利低波"]
    assert events[0]["weight"] == 0.55


def test_policy_reversal_evicts_opposite_direction(tmp_path: Path) -> None:
    cache_path = tmp_path / "dynamic_policy.json"
    refresh_dynamic_policy_themes(
        trade_date="2026-11-03",
        discovered_policy={
            "boost_themes": [{"theme": "大金融", "weight": 0.85, "title": "互换便利落地", "source": "央行"}],
            "squeeze_themes": [{"theme": "服务器算力", "penalty": 0.70, "title": "警示AI高杠杆", "source": "金管总局"}],
        },
        path=cache_path,
    )

    reversed_policy = refresh_dynamic_policy_themes(
        trade_date="2026-11-10",
        discovered_policy={
            "boost_themes": [{"theme": "服务器算力", "weight": 0.82, "title": "算力专项补贴出台", "source": "工信部"}],
            "squeeze_themes": [{"theme": "大金融", "penalty": 0.60, "title": "严查两融绕道配资", "source": "证监会"}],
        },
        path=cache_path,
    )

    assert [r["theme"] for r in reversed_policy["boost_themes"]] == ["服务器算力"]
    assert [r["theme"] for r in reversed_policy["squeeze_themes"]] == ["大金融"]
