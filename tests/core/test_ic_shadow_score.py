"""Tests for the IC reverse-scoring shadow pool.

2026-08-22 的 IC 扫描（用生产 FunnelConfig 真实参数）显示生产四条通道方向都反了，
且三个因子同时满足「可用」与「三段方向全一致」：

    rps_fast            IC -0.0711  IR -0.38  各段 -0.084 -0.058 -0.072
    ret60               IC -0.0692  IR -0.37  各段 -0.086 -0.051 -0.071
    dry_vol_min10_q250  IC -0.0504  IR -0.35  各段 -0.070 -0.017 -0.063

影子池把这三个反向加权成横截面排序，只写 observation 不下单——因为 IC 只说明方向反了，
不说明该设什么阈值，而阈值化本身就是过拟合来源（参数网格 walk-forward 仅 1/16）。
"""

from __future__ import annotations

import pytest

from core.ic_shadow_score import (
    MIN_ABS_WEIGHT,
    SHADOW_CHANNEL,
    SHADOW_SOURCE,
    FactorWeight,
    ShadowPick,
    ShadowScoreConfig,
    combine_scores,
)


class TestConfig:
    def test_defaults_are_the_three_stable_factors(self):
        names = {w.name for w in ShadowScoreConfig().weights}
        assert names == {"rps_fast", "ret60", "dry_vol_min10_q250"}

    def test_all_defaults_are_reverse(self):
        """三个因子 IC 全为负，必须都反向使用。"""
        assert all(w.reversed_use for w in ShadowScoreConfig().weights)

    def test_weights_normalize_to_one(self):
        weights = ShadowScoreConfig().normalized()
        assert sum(abs(v) for v in weights.values()) == pytest.approx(1.0, abs=1e-9)

    def test_drops_negligible_weights(self):
        config = ShadowScoreConfig(weights=(FactorWeight("big", -0.4), FactorWeight("tiny", -MIN_ABS_WEIGHT / 2)))
        assert set(config.normalized()) == {"big"}

    def test_empty_weights_yield_nothing(self):
        assert ShadowScoreConfig(weights=()).normalized() == {}


class TestCombine:
    def _panels(self):
        # A 极弱势极缩量、C 极强势放量——反向打分应把 A 排首位、C 垫底。
        return {
            "rps_fast": {"A": 3.0, "B": 50.0, "C": 97.0},
            "ret60": {"A": 5.0, "B": 50.0, "C": 95.0},
            "dry_vol_min10_q250": {"A": 2.0, "B": 50.0, "C": 96.0},
        }

    def test_weak_and_dry_ranks_first(self):
        picks = combine_scores(self._panels(), ShadowScoreConfig(top_n=3))
        assert [p.code for p in picks] == ["A", "B", "C"]
        assert picks[0].rank == 1

    def test_respects_top_n(self):
        assert len(combine_scores(self._panels(), ShadowScoreConfig(top_n=2))) == 2

    def test_zero_top_n_returns_empty(self):
        assert combine_scores(self._panels(), ShadowScoreConfig(top_n=0)) == []

    def test_drops_codes_missing_any_factor(self):
        """缺值不补 50——否则无信息标的会被抬进 top-N。"""
        panels = self._panels()
        panels["rps_fast"]["D"] = 1.0  # D 只有一个因子有值
        picks = combine_scores(panels, ShadowScoreConfig(top_n=10))
        assert "D" not in {p.code for p in picks}

    def test_nan_treated_as_missing(self):
        panels = self._panels()
        panels["ret60"]["A"] = float("nan")
        assert "A" not in {p.code for p in combine_scores(panels, ShadowScoreConfig(top_n=3))}

    def test_unknown_factor_ignored(self):
        picks = combine_scores({"rps_fast": {"A": 1.0}}, ShadowScoreConfig(top_n=3))
        assert [p.code for p in picks] == ["A"]

    def test_no_panels_returns_empty(self):
        assert combine_scores({}, ShadowScoreConfig()) == []


class TestObservationRows:
    def test_rows_marked_as_non_tradeable(self):
        """影子池不得进推荐或下单链路——三个标记必须同时成立。"""
        from scripts.run_ic_shadow_pool import to_rows

        picks = [ShadowPick(code="600363.SH", score=-1.06, rank=1, factor_ranks={"ret60": 0.0})]
        row = to_rows(picks, "2026-08-14", ShadowScoreConfig())[0]
        assert row["ai_recommended"] is False
        assert row["selected_for_ai"] is False
        assert row["candidate_status"] == "shadow_observe"

    def test_source_and_channel_tagged(self):
        from scripts.run_ic_shadow_pool import to_rows

        row = to_rows([ShadowPick("600363.SH", -1.0, 1)], "2026-08-14", ShadowScoreConfig())[0]
        assert row["source"] == SHADOW_SOURCE
        assert row["channel"] == SHADOW_CHANNEL
        assert row["signal_type"] == SHADOW_SOURCE

    def test_code_stripped_of_suffix(self):
        from scripts.run_ic_shadow_pool import to_rows

        row = to_rows([ShadowPick("600363.SH", -1.0, 1)], "2026-08-14", ShadowScoreConfig())[0]
        assert row["code"] == "600363"

    def test_features_json_records_composition(self):
        import json

        from scripts.run_ic_shadow_pool import to_rows

        pick = ShadowPick("600363.SH", -1.06, 1, {"ret60": 0.0, "rps_fast": 3.0})
        payload = json.loads(to_rows([pick], "2026-08-14", ShadowScoreConfig())[0]["features_json"])
        assert payload["ic_shadow_rank"] == 1
        assert payload["factor_percentiles"]["rps_fast"] == pytest.approx(3.0)

    def test_strategy_version_carries_weights(self):
        """便于事后区分不同权重版本写入的行。"""
        from scripts.run_ic_shadow_pool import to_rows

        row = to_rows([ShadowPick("600363.SH", -1.0, 1)], "2026-08-14", ShadowScoreConfig())[0]
        assert "rps_fast" in row["strategy_version"]


class TestDailyWorkflow:
    """影子池已接每日定时——这些用例守住它不会误入下单链路或撞车其它任务。"""

    def _workflow(self) -> dict:
        from pathlib import Path

        import yaml

        data = yaml.safe_load(Path(".github/workflows/ic_shadow_pool.yml").read_text(encoding="utf-8"))
        # PyYAML 把 `on:` 解析成布尔 True，这是已知怪癖。
        return data

    def test_runs_after_main_funnel(self):
        """必须在主漏斗（北京 17:17 / UTC 9:17）之后，才能用同一交易日的收盘数据。"""
        data = self._workflow()
        on = data.get("on") or data.get(True)
        minute, hour, _dom, _mon, dow = on["schedule"][0]["cron"].split()
        assert int(hour) > 9 or (int(hour) == 9 and int(minute) > 17)
        # 与主漏斗同为周日至周四。
        assert dow == "0-4"

    def test_does_not_collide_with_review_replay(self):
        """review_list_replay 在 UTC 11:25；影子池须早于它，避免争 Tushare 配额。"""
        data = self._workflow()
        on = data.get("on") or data.get(True)
        minute, hour, *_ = on["schedule"][0]["cron"].split()
        assert (int(hour), int(minute)) < (11, 25)

    def test_write_context_is_server_job(self):
        env = self._workflow()["jobs"]["run"]["env"]
        assert env["WYCKOFF_WRITE_CONTEXT"] == "server_job"

    def test_has_timeout(self):
        """快照抓取 + 打分约 30 分钟；设上限避免卡死占用额度。"""
        assert self._workflow()["jobs"]["run"]["timeout-minutes"] <= 120
