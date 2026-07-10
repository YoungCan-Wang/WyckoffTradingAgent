from __future__ import annotations


def test_generate_public_premarket_brief_uses_efficiency():
    import core.premarket_public_brief as brief

    config = brief.PublicBriefLlmConfig(
        routes=(
            {
                "provider": "efficiency",
                "model": "eff-model",
                "api_key": "eff-key",
                "base_url": "https://eff.example/v1",
            },
        )
    )

    result = brief.generate_public_premarket_brief(
        a50={"ok": True, "date": "2026-06-19", "pct_chg": -0.2, "source": "a50"},
        vix={"ok": True, "date": "2026-06-19", "close": 18.2, "pct_chg": 1.2, "source": "vix"},
        regime="NORMAL",
        reasons=["A50/VIX 未触发风险阈值"],
        market_signal={"benchmark_regime": "NEUTRAL"},
        llm_config=config,
        llm_caller=lambda **_kwargs: (
            '{"banner_title":"隔夜扰动有限，盘前观察承接",'
            '"banner_message":"昨日场内水温中性，隔夜外部波动未明显放大。今日重点观察开盘承接、量能变化与风险偏好是否同步修复。",'
            '"banner_tone":"谨慎"}'
        ),
    )

    assert result["llm_used"] is True
    assert result["provider"] == "efficiency"
    assert result["banner_tone"] == "谨慎"
    assert "持仓" not in result["banner_message"]


def test_generate_public_premarket_brief_rejects_private_or_action_terms():
    import core.premarket_public_brief as brief

    config = brief.PublicBriefLlmConfig(
        routes=(
            {
                "provider": "efficiency",
                "model": "eff-model",
                "api_key": "eff-key",
                "base_url": "https://eff.example/v1",
            },
        )
    )

    result = brief.generate_public_premarket_brief(
        a50={"ok": True, "pct_chg": -1.2},
        vix={"ok": True, "close": 21.0, "pct_chg": 9.0},
        regime="RISK_OFF",
        reasons=["A50跌幅 -1.20% <= -1.00%"],
        market_signal={"benchmark_regime": "RISK_OFF"},
        llm_config=config,
        llm_caller=lambda **_kwargs: (
            '{"banner_title":"关注 600000","banner_message":"结合你的持仓，可以买入并设置止损。","banner_tone":"谨慎"}'
        ),
    )

    assert result["llm_used"] is False
    assert "600000" not in result["banner_title"]
    assert "持仓" not in result["banner_message"]
    assert "买入" not in result["banner_message"]
    assert result["validation_reasons"]


def test_validate_public_brief_blocks_stock_codes_and_private_terms():
    from core.premarket_public_brief import validate_public_brief

    ok, reasons = validate_public_brief(
        {
            "banner_title": "盘前关注 600000",
            "banner_message": "结合你的账户和持仓，今日可以加仓。",
            "banner_tone": "谨慎",
        }
    )

    assert ok is False
    assert "contains_stock_code" in reasons
    assert "contains_term:持仓" in reasons
    assert "contains_term:加仓" in reasons
