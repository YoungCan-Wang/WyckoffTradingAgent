"""Tests for funnel throughput bottleneck attribution.

这个脚本存在的意义是别再靠猜调参数：2026-08 复盘时我曾误判
``FUNNEL_AI_TOTAL_CAP=8`` 是瓶颈，实测 12 天里它一次都没卡住，真瓶颈是水温闸门
（``allow_ai_review=False``）与该档 AI 配额为 0。所以这些用例重点守 ``_bottleneck``
的归因优先级，别让结论再次指错参数。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts import diagnose_funnel_throughput as mod


def _limits(**overrides):
    base = {
        "regime": "BEAR_REBOUND",
        "allow_ai_review": True,
        "trade_mode": "repair_review",
        "trend_quota": 5,
        "accum_quota": 1,
        "total_cap": 8,
        "max_per_sector": 2,
        "selection_mode": "tradeable_l4",
        "total_cap_binds_promotion": False,
        "buy_blocked": False,
    }
    base.update(overrides)
    return base


class TestBottleneckPriority:
    def test_no_observations_reported_first(self):
        stages = {"observations": 0, "formal_l4": 0, "selected_for_ai": 0}
        assert "无 observation" in mod._bottleneck(stages, _limits())

    def test_gate_outranks_quota(self):
        """闸门关闭时不该归因到配额——8/17 有 71 只 formal_l4 却因闸门丢光。"""
        stages = {"observations": 168, "formal_l4": 71, "selected_for_ai": 0}
        verdict = mod._bottleneck(stages, _limits(allow_ai_review=False, trade_mode="observe_only", regime="UNKNOWN"))
        assert "水温闸门" in verdict
        assert "配额" not in verdict

    def test_zero_formal_is_not_a_quota_problem(self):
        stages = {"observations": 120, "formal_l4": 0, "selected_for_ai": 0}
        verdict = mod._bottleneck(stages, _limits())
        assert "无 formal_l4" in verdict
        assert "非配额问题" in verdict

    def test_zero_quota_named_explicitly(self):
        """配额为 0 时要点名具体 env 变量，否则运维无从下手。"""
        stages = {"observations": 134, "formal_l4": 60, "selected_for_ai": 0}
        verdict = mod._bottleneck(stages, _limits(trend_quota=0, accum_quota=0))
        assert "FUNNEL_AI_BEAR_REBOUND_TREND/ACCUM" in verdict

    def test_quota_saturated(self):
        stages = {"observations": 140, "formal_l4": 30, "selected_for_ai": 6}
        assert "配额打满" in mod._bottleneck(stages, _limits())

    def test_total_cap_only_blamed_when_it_binds(self):
        """tradeable_l4 下 total_cap 不参与晋级，不能归因到它。"""
        stages = {"observations": 200, "formal_l4": 50, "selected_for_ai": 8}
        # 配额 5+1=6，selected 8 已超配额 -> 先归因配额
        assert "配额打满" in mod._bottleneck(stages, _limits())
        # 配额放大后，binds=False 时不应提 total_cap
        verdict = mod._bottleneck(stages, _limits(trend_quota=20, accum_quota=5))
        assert "FUNNEL_AI_TOTAL_CAP" not in verdict

    def test_total_cap_blamed_when_binding(self):
        stages = {"observations": 200, "formal_l4": 50, "selected_for_ai": 8}
        verdict = mod._bottleneck(
            stages, _limits(trend_quota=20, accum_quota=5, total_cap_binds_promotion=True, total_cap=8)
        )
        assert "FUNNEL_AI_TOTAL_CAP" in verdict

    def test_no_hard_cut_when_below_all_limits(self):
        stages = {"observations": 112, "formal_l4": 5, "selected_for_ai": 2}
        verdict = mod._bottleneck(stages, _limits())
        assert "未见硬性截断" in verdict
        assert "候选质量" in verdict


class TestRatioAndRender:
    def test_ratio_guards_zero_denominator(self):
        assert mod._ratio(3, 0) is None
        assert mod._ratio(3, 6) == 50.0

    def test_pct_formats_none(self):
        assert mod._pct(None) == "—"
        assert mod._pct(42.3) == "42.3%"

    def test_render_includes_bottleneck_and_limits(self):
        report = {
            "trade_date": "2026-08-17",
            "stages": {"observations": 168, "formal_l4": 71, "selected_for_ai": 0},
            "shrink": {"observations_to_formal": 42.3, "formal_to_ai": 0.0},
            "limits": _limits(allow_ai_review=False, regime="UNKNOWN", trade_mode="observe_only", buy_blocked=True),
            "formal_by_sector_top5": {"种植业": 3},
            "bottleneck": "水温闸门（UNKNOWN / observe_only）",
        }
        text = mod.render([report])
        assert "2026-08-17" in text
        assert "水温闸门" in text
        assert "**不参与**" in text  # tradeable_l4 下须标明 cap 不限制晋级
        assert "禁买" in text


class TestBuildDayReport:
    def test_counts_distinct_codes_per_stage(self, monkeypatch):
        monkeypatch.setattr(mod, "_limits_for", lambda regime: _limits(regime=regime))
        frame = pd.DataFrame(
            [
                {"trade_date": "2026-08-17", "code": "A", "candidate_status": "formal_l4", "selected_for_ai": True},
                {"trade_date": "2026-08-17", "code": "A", "candidate_status": "formal_l4", "selected_for_ai": True},
                {"trade_date": "2026-08-17", "code": "B", "candidate_status": "Lane", "selected_for_ai": False},
                {"trade_date": "2026-08-14", "code": "C", "candidate_status": "formal_l4", "selected_for_ai": False},
            ]
        )
        report = mod.build_day_report(frame, "2026-08-17", "BEAR_REBOUND")
        # 同一 code 多行只算一次；另一天不计入。
        assert report["stages"] == {"observations": 2, "formal_l4": 1, "selected_for_ai": 1}
        assert report["shrink"]["formal_to_ai"] == 100.0

    def test_missing_industry_column_is_safe(self, monkeypatch):
        monkeypatch.setattr(mod, "_limits_for", lambda regime: _limits())
        frame = pd.DataFrame(
            [{"trade_date": "2026-08-17", "code": "A", "candidate_status": "formal_l4", "selected_for_ai": False}]
        )
        report = mod.build_day_report(frame, "2026-08-17", "BEAR_REBOUND")
        assert report["formal_by_sector_top5"] == {}


def test_module_is_read_only():
    """诊断脚本不得写库或发通知。"""
    source = (Path(__file__).resolve().parents[2] / "scripts" / "diagnose_funnel_throughput.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("upsert", "insert(", "send_feishu", "require_server_write_context"):
        assert forbidden not in source, f"诊断脚本不应包含写入/通知调用: {forbidden}"


if __name__ == "__main__":
    pytest.main([__file__])
