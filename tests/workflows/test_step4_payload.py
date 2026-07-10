from __future__ import annotations

from types import SimpleNamespace

from workflows.step4_models import PortfolioState, PositionItem
from workflows.step4_payload import (
    build_candidate_meta_map,
    candidate_context_line,
    collect_step4_candidates,
    extract_stock_codes,
    prepare_step4_payload_context,
    prepend_candidate_context,
)


def test_build_candidate_meta_map_keeps_capital_migration_bonus_and_source() -> None:
    meta_map = build_candidate_meta_map(
        [
            {
                "code": "000390",
                "name": "晨光",
                "priority_score": 91,
                "funnel_score": 88,
                "capital_migration_bonus": 4.5,
                "source_type": "supabase_recommendation_tracking",
                "action_status": "ready_for_ai_review",
                "trade_readiness": "research_only",
                "new_buy_allowed": False,
                "label_ready": False,
                "risk_factors": ["评估标签尚未成熟"],
                "entry_quality_risk_flags": ["追高延展"],
                "next_step": "生成 AI 研报并结合持仓形成攻防决策",
            }
        ],
        positions=[],
    )

    meta = meta_map["000390"]
    assert meta.funnel_score == 91
    assert meta.capital_migration_bonus == 4.5
    assert meta.source_type == "supabase_recommendation_tracking"
    assert meta.action_status == "ready_for_ai_review"
    assert meta.trade_readiness == "research_only"
    assert meta.new_buy_allowed is False
    assert meta.label_ready is False
    assert meta.risk_factors == ("评估标签尚未成熟", "追高延展")
    assert meta.next_step == "生成 AI 研报并结合持仓形成攻防决策"


def test_candidate_context_line_exposes_score_source_and_capital_migration() -> None:
    line = candidate_context_line(
        {
            "code": "000390",
            "priority_score": 91,
            "layer3_quality_score": 0.82,
            "capital_migration_bonus": 4.5,
            "selection_source": "二次确认",
            "candidate_lane": "mainline",
        }
    )

    assert "score=91.00" in line
    assert "L3质量=0.82" in line
    assert "资金迁移加分=+4.50" in line
    assert "来源=二次确认" in line
    assert "通道=mainline" in line


def test_candidate_context_line_exposes_quality_and_action_guards() -> None:
    line = candidate_context_line(
        {
            "code": "300750",
            "candidate_shadow_score": 92.0,
            "candidate_shadow_grade": "S",
            "entry_quality_score": 84.0,
            "entry_quality_grade": "A",
            "entry_quality_risk_flags": ["追高延展"],
            "selection_strategy": "candidate_shadow_then_score",
            "is_ai_recommended": True,
            "recommend_count": 2,
            "action_status": "ready_for_ai_review",
            "trade_readiness": "research_only",
            "new_buy_allowed": False,
            "label_ready": False,
            "risk_factors": ["评估标签尚未成熟"],
            "next_step": "生成 AI 研报并结合持仓形成攻防决策",
        }
    )

    assert "候选影子=S/92.00" in line
    assert "入场=A/84.00" in line
    assert "策略=candidate_shadow_then_score" in line
    assert "已AI推荐" in line
    assert "推荐次数=2" in line
    assert "动作=ready_for_ai_review" in line
    assert "交易就绪=research_only" in line
    assert "不允许新增买入" in line
    assert "标签未成熟" in line
    assert "风险=评估标签尚未成熟；追高延展" in line
    assert "下一步=生成 AI 研报并结合持仓形成攻防决策" in line


def test_prepend_candidate_context_keeps_payload_header_first() -> None:
    payload = "• 000390 晨光\n  [价格锚点] 最新收盘价:10.00\n"

    got = prepend_candidate_context(payload, {"priority_score": 91, "capital_migration_bonus": -3.25})

    assert got.startswith("• 000390 晨光\n  [候选归因]")
    assert "资金迁移扣分=-3.25" in got


def test_build_candidate_meta_map_preserves_existing_holding_source() -> None:
    meta_map = build_candidate_meta_map(
        [{"code": "000001", "name": "平安银行", "capital_migration_bonus": 4.5}],
        positions=[PositionItem(code="000001", name="平安银行", cost=10, buy_dt="2026-05-10", shares=1000)],
    )

    assert meta_map["000001"].source_type == "holding"


def test_collect_step4_candidates_promotes_external_report_codes_to_payload_items() -> None:
    portfolio = PortfolioState(
        free_cash=10000,
        total_equity=20000,
        positions=[PositionItem(code="000001", name="平安银行", cost=10, buy_dt="2026-05-10", shares=1000)],
    )

    candidate_codes, candidate_items, allowed_codes, meta_map, name_map = collect_step4_candidates(
        portfolio,
        candidate_meta=None,
        external_report="重点观察 000390，持仓 000001 不重复。",
        max_external_report_candidates=12,
    )

    assert candidate_codes == ["000390"]
    assert candidate_items == [
        {"code": "000390", "name": "000390", "tag": "外部报告候选", "source_type": "external_report"}
    ]
    assert allowed_codes == {"000001", "000390"}
    assert meta_map["000390"].source_type == "external_report"
    assert meta_map["000390"].tag == "外部报告候选"
    assert meta_map["000001"].source_type == "holding"
    assert name_map["000390"] == "000390"


def test_collect_step4_candidates_caps_external_report_fallback_codes() -> None:
    portfolio = PortfolioState(free_cash=10000, total_equity=20000, positions=[])

    candidate_codes, candidate_items, allowed_codes, meta_map, _name_map = collect_step4_candidates(
        portfolio,
        candidate_meta=None,
        external_report="000390 000391 000392 000393",
        max_external_report_candidates=2,
    )

    assert candidate_codes == ["000390", "000391"]
    assert [item["code"] for item in candidate_items] == ["000390", "000391"]
    assert allowed_codes == {"000390", "000391"}
    assert set(meta_map) == {"000390", "000391"}


def test_extract_stock_codes_filters_date_like_numeric_noise() -> None:
    text = "报告日 202606，批次 999999，候选 000390 159919 300750 600519 833575。"

    assert extract_stock_codes(text) == ["000390", "159919", "300750", "600519", "833575"]


def test_extract_stock_codes_accepts_exchange_prefixed_and_suffixed_symbols() -> None:
    text = "候选 SH600519、sz000001、000390.SZ、833575.BJ，重复 sh600519。"

    assert extract_stock_codes(text) == ["600519", "000001", "000390", "833575"]


def test_prepare_step4_payload_context_reports_external_candidate_truncation(monkeypatch) -> None:
    portfolio = PortfolioState(
        free_cash=10000,
        total_equity=10000,
        positions=[PositionItem(code="000001", name="平安银行", cost=10, buy_dt="2026-05-10", shares=1000)],
    )
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        "workflows.step4_payload.format_position_payload",
        lambda *_args, **_kwargs: ("", [], 0.0, {}, {}),
    )

    def fake_format_candidate_payload(items, *_args, **_kwargs):
        captured["codes"] = [item["code"] for item in items]
        return "candidate payload", [], {}, {}

    monkeypatch.setattr("workflows.step4_payload.format_candidate_payload", fake_format_candidate_payload)

    payload = prepare_step4_payload_context(
        portfolio,
        SimpleNamespace(),
        "000390 000391 000392 000001 000390",
        candidate_meta=None,
        atr_period=14,
        max_workers=1,
        enforce_target_trade_date=False,
        max_external_report_candidates=2,
    )

    assert captured["codes"] == ["000390", "000391"]
    assert payload.candidate_failures == ["external_report_candidates_truncated: kept=2, dropped=1, limit=2"]
