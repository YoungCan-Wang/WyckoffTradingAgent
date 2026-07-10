from __future__ import annotations

from core.funnel_report import FunnelReportMaps, build_symbol_report_row, candidate_reason_text


def test_build_symbol_report_row_combines_shared_fields() -> None:
    maps = FunnelReportMaps(
        name_map={"000001": "平安银行"},
        sector_map={"000001": "银行"},
        sector_rotation_map={"银行": {"state": "hot", "label": "升温", "note": "放量", "guidance": "关注"}},
        exit_signals={"000001": {"signal": "hold", "price": 12.3, "reason": "趋势未破"}},
        latest_close_map={"000001": 11.8},
        theme_candidate_map={
            "000001": {"theme": "大金融", "theme_score": 0.8, "stock_score": 0.7, "state": "confirmed"}
        },
        theme_bonus_map={"000001": 9.0},
        code_to_trigger_keys={"000001": ["sos", "evr", "sos"]},
        code_to_reasons={"000001": ["SOS"]},
        theme_badge_map={"000001": "主线:大金融"},
        capital_migration_bonus_map={"000001": 4.5},
        layer3_score_map={"000001": 0.82},
    )

    row = build_symbol_report_row(
        "000001",
        rank=1,
        tag=candidate_reason_text("000001", maps.code_to_reasons, maps.theme_badge_map),
        track="Trend",
        stage="Markup",
        score=12.0,
        priority_score=13.0,
        selection_source="l4_hit",
        selection_is_fill=False,
        market_regime="risk_on",
        maps=maps,
    )

    assert row["name"] == "平安银行"
    assert row["signal_types"] == ["sos", "evr"]
    assert row["market_regime"] == "RISK_ON"
    assert row["sector_state"] == "升温"
    assert row["exit_reason"] == "趋势未破"
    assert row["strategic_theme"] == "大金融"
    assert row["capital_migration_bonus"] == 4.5
    assert row["layer3_quality_score"] == 0.82
    assert row["tag"] == "SOS、主线:大金融"
