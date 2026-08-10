from __future__ import annotations

from core.candidate_policy import cap_quality_candidates, is_tradeable_l4_trigger_combo


def test_is_tradeable_l4_trigger_combo_structural_and_naked_right_side():
    # 结构性触发（如 spring）单独出现即可交易
    assert is_tradeable_l4_trigger_combo(["spring"]) is True

    # 裸右侧信号（sos/evr）没有结构性触发时不可交易
    assert is_tradeable_l4_trigger_combo(["sos"]) is False

    # 裸右侧信号叠加结构性触发后可交易
    assert is_tradeable_l4_trigger_combo(["sos", "spring"]) is True

    # 空触发集合不可交易
    assert is_tradeable_l4_trigger_combo([]) is False


def test_quality_cap_prefers_score_and_limits_sector_concentration():
    codes = ["000001", "000002", "000003", "000004", "000005"]
    scores = {code: 100.0 - index for index, code in enumerate(codes)}
    sectors = {
        "000001": "科技",
        "000002": "科技",
        "000003": "科技",
        "000004": "医药",
        "000005": "消费",
    }

    selected, cap_dropped, sector_dropped = cap_quality_candidates(
        codes,
        scores,
        sectors,
        total_cap=3,
        max_per_sector=2,
    )

    assert selected == ["000001", "000002", "000004"]
    assert cap_dropped == ["000005"]
    assert sector_dropped == ["000003"]


class TestThresholdsAreStructurallyReachable:
    """min_score 阈值必须落在对应 detector 的实际值域内。

    2026-08-10 发现三个阈值结构上不可达，等于判据恒为真：
      _detect_trend_pullback 返回 1.0 - vol_ratio（vol_ratio > 0）→ score < 1.0 恒成立
      _detect_lps 返回 vol_ratio 且 vol_ratio > 0.65 即弃用      → score ∈ (0, 0.65]
    而原阈值 pure_trendpb_min_score=10.0 / pure_lps_min_score=6.0 /
    mix_trendpb_min_score=12.0 分别是上界的 10 倍、9.2 倍、12 倍以上。

    后果：主线 trend_pullback / lps 候选 100% 被拦（"主线跳过仅观察"的快速通道对
    它们实际关闭）；含 trend_pullback 的共振组合在五种弱回踩市况下被无条件拦掉
    ——共振组合没有 observe_only 兜底，本该是质量更高的一批。
    """

    #: detector 返回值的结构上界（见 core/wyckoff_engine.py 的 _detect_* 实现）
    TRENDPB_SCORE_MAX = 1.0
    LPS_SCORE_MAX = 0.65

    def test_trendpb_thresholds_below_structural_bound(self):
        from core.candidate_policy import CandidatePolicyConfig

        cfg = CandidatePolicyConfig()

        assert cfg.pure_trendpb_min_score < self.TRENDPB_SCORE_MAX
        assert cfg.mix_trendpb_min_score < self.TRENDPB_SCORE_MAX

    def test_lps_threshold_below_structural_bound(self):
        from core.candidate_policy import CandidatePolicyConfig

        assert CandidatePolicyConfig().pure_lps_min_score < self.LPS_SCORE_MAX

    def test_mainline_trendpb_can_pass(self):
        """主线候选跳过 observe_only 后，正常分数应能放行而非被低分拦。"""
        from core.candidate_policy import CandidatePolicyConfig, loss_guard_reason

        cfg = CandidatePolicyConfig()
        mainline = {"000001"}

        # 0.43 是实测 trendpb 分数中位数
        assert (
            loss_guard_reason(
                "000001", "NEUTRAL", {"trend_pullback"}, 0.43, "", {}, config=cfg, mainline_codes=mainline
            )
            == ""
        )
        # 极低分仍应被拦，否则阈值失去判别作用
        assert loss_guard_reason(
            "000001", "NEUTRAL", {"trend_pullback"}, 0.001, "", {}, config=cfg, mainline_codes=mainline
        )

    def test_resonant_trendpb_passes_in_weak_regimes(self):
        """含 trend_pullback 的共振组合不应被弱回踩判据无条件拦掉。"""
        from core.candidate_policy import WEAK_PULLBACK_REGIMES, CandidatePolicyConfig, loss_guard_reason

        cfg = CandidatePolicyConfig()

        for regime in WEAK_PULLBACK_REGIMES:
            reason = loss_guard_reason("000001", regime, {"trend_pullback", "sos"}, 0.43, "", {}, config=cfg)
            assert "弱趋势回踩" not in reason

    def test_pure_signal_still_observe_only(self):
        """阈值修正不应放松纯信号的 observe_only 约束。"""
        from core.candidate_policy import CandidatePolicyConfig, loss_guard_reason

        cfg = CandidatePolicyConfig()

        assert "仅观察" in loss_guard_reason("000001", "NEUTRAL", {"trend_pullback"}, 0.43, "", {}, config=cfg)
        assert "仅观察" in loss_guard_reason("000001", "NEUTRAL", {"lps"}, 0.58, "", {}, config=cfg)
