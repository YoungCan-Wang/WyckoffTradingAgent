from __future__ import annotations

import pandas as pd

from core.compliance_report import ComplianceLLMConfig
from core.x_social_summary import build_x_summary_payload, generate_x_social_summary


def _selected_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"code": "000002", "name": "低分股", "priority_score": 60.0, "funnel_score": 95.0},
            {"code": "000001", "name": "高分股", "priority_score": 88.0, "funnel_score": 70.0},
        ]
    )


def test_x_summary_payload_ranks_candidates_by_priority_score() -> None:
    payload = build_x_summary_payload(
        benchmark_context={"trade_date": "2026-06-30", "regime": "RISK_ON", "close": 3100.12},
        selected_df=_selected_df(),
        ops_codes=["000001"],
        report_text="## 🏹 处于起跳板\n- 000001 高分股",
    )

    assert payload["trade_date"] == "2026-06-30"
    assert [item["code"] for item in payload["candidates"]] == ["000001", "000002"]
    assert payload["candidates"][0]["confirmed"] is True
    assert payload["market"]["close"] == "3100.12"
    assert "000001 高分股" in payload["report_digest"]


def test_x_summary_falls_back_without_llm_config() -> None:
    text = generate_x_social_summary(
        benchmark_context={"regime": "CAUTION", "close": 3000, "main_today_pct": 1.2},
        selected_df=_selected_df(),
        ops_codes=["000001"],
    )

    assert text.startswith("## 🧵 X直白版总结")
    assert "000001 高分股" in text
    assert "不追高" in text


def test_x_summary_uses_llm_and_normalizes_title() -> None:
    calls: list[dict] = []
    cfg = ComplianceLLMConfig(
        provider="efficiency",
        api_key="key",
        model="model",
        base_url="https://example.test/v1",
        source="test",
        max_output_tokens=4096,
    )

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return "今天修复有力度，重点看高分股能否继续放量承接。"

    text = generate_x_social_summary(
        benchmark_context={"regime": "RISK_ON"},
        selected_df=_selected_df(),
        llm_config=cfg,
        llm_caller=fake_llm,
    )

    assert text.startswith("## 🧵 X直白版总结")
    assert "继续放量承接" in text
    assert calls[0]["provider"] == "efficiency"
    assert calls[0]["max_output_tokens"] == 1600
    assert "000001" in calls[0]["user_message"]


def test_x_summary_llm_failure_keeps_fallback() -> None:
    cfg = ComplianceLLMConfig(
        provider="efficiency",
        api_key="key",
        model="model",
        base_url="https://example.test/v1",
        source="test",
    )

    def broken_llm(**_kwargs):
        raise RuntimeError("down")

    text = generate_x_social_summary(
        benchmark_context={"regime": "NEUTRAL"},
        selected_df=pd.DataFrame(),
        llm_config=cfg,
        llm_caller=broken_llm,
    )

    assert "无明确个股" in text
    assert "先观察，不硬买" in text
