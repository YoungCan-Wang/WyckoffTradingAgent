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
    def test_default_factor_names_exist_in_scanner(self):
        """**最关键的一条**：权重里的因子名必须真实存在于 build_factors。

        2026-08-24 生产失败就源于此——默认权重写 dry_vol_min10_q250 / vol_ratio_5_20，
        而 main 上的键是 dry_vol_q250 / vol_ratio，脚本直接 SystemExit「未知因子」。
        那两个名字来自未合并的本地版本，我按它写了权重却没在 main 上验证。
        """
        import pandas as pd

        from scripts.scan_factor_ic import build_factors

        # 造一份最小行情，只为取出因子键集，不关心数值。
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        frame = pd.DataFrame(
            {
                "ts_code": ["000001"] * 3,
                "d": dates,
                "open": [10.0] * 3,
                "close": [10.0] * 3,
                "high": [10.0] * 3,
                "low": [10.0] * 3,
                "vol": [100.0] * 3,
                "amount": [1000.0] * 3,
            }
        )
        available = set(build_factors(frame)[0])
        missing = {w.name for w in ShadowScoreConfig().weights} - available
        assert not missing, f"权重引用了不存在的因子: {sorted(missing)}；可选 {sorted(available)}"

    def test_defaults_are_the_stable_factors(self):
        names = {w.name for w in ShadowScoreConfig().weights}
        assert names == {"ret60", "dry_vol_q250"}

    def test_does_not_double_count_ret60_and_rps_slow(self):
        """rps_slow 与 ret60 不得同时入选——两者共享同一份 60 日动量。

        2026-08-30 前 rps_slow 是 ret60 的**全市场**分位，即单调变换，故 Rank IC 逐位
        相同；此后改为**行业内**分位（见 scan_factor_ic._within_sector_rank），IC 已不再
        相同（实测差 +0.019）。但底层信号仍是同一个 60 日涨幅，一起加权仍属重复计权，
        故本约束保留——只是理由从「IC 相同」变成「同源」。
        """
        names = {w.name for w in ShadowScoreConfig().weights}
        assert not {"ret60", "rps_slow"} <= names

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
            "ret60": {"A": 5.0, "B": 50.0, "C": 95.0},
            "dry_vol_q250": {"A": 2.0, "B": 50.0, "C": 96.0},
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
        panels["ret60"]["D"] = 1.0  # D 只有一个因子有值
        picks = combine_scores(panels, ShadowScoreConfig(top_n=10))
        assert "D" not in {p.code for p in picks}

    def test_nan_treated_as_missing(self):
        panels = self._panels()
        panels["ret60"]["A"] = float("nan")
        assert "A" not in {p.code for p in combine_scores(panels, ShadowScoreConfig(top_n=3))}

    def test_unknown_factor_ignored(self):
        """只提供部分因子面板时，按可用部分打分（缺全部因子才返回空）。"""
        picks = combine_scores({"ret60": {"A": 1.0}}, ShadowScoreConfig(top_n=3))
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

        pick = ShadowPick("600363.SH", -1.06, 1, {"ret60": 0.0, "dry_vol_q250": 3.0})
        payload = json.loads(to_rows([pick], "2026-08-14", ShadowScoreConfig())[0]["features_json"])
        assert payload["ic_shadow_rank"] == 1
        assert payload["factor_percentiles"]["dry_vol_q250"] == pytest.approx(3.0)

    def test_strategy_version_carries_weights(self):
        """便于事后区分不同权重版本写入的行。"""
        from scripts.run_ic_shadow_pool import to_rows

        row = to_rows([ShadowPick("600363.SH", -1.0, 1)], "2026-08-14", ShadowScoreConfig())[0]
        assert "ret60" in row["strategy_version"]


class TestInlineInFunnel:
    """影子池已改为在漏斗内联计算（复用 all_df_map），不再有独立 workflow。

    原独立 workflow 每天自抓 560 天快照，实测 45 分钟；而漏斗本就抓
    FunnelConfig.trading_days=320 个交易日，足够覆盖最长的 250 日滚动分位。
    """

    def test_standalone_workflow_removed(self):
        from pathlib import Path

        assert not Path(".github/workflows/ic_shadow_pool.yml").exists()

    def test_funnel_computes_shadow_pool(self):
        from pathlib import Path

        src = Path("workflows/wyckoff_funnel.py").read_text(encoding="utf-8")
        assert "_build_ic_shadow_pool" in src
        assert 'metrics["ic_shadow"]' in src

    def test_daily_job_persists_shadow_pool(self):
        from pathlib import Path

        src = Path("workflows/daily_job_step3.py").read_text(encoding="utf-8")
        assert "persist_ic_shadow_pool" in src

    def test_funnel_window_covers_longest_factor(self):
        """漏斗窗口必须够长，否则 dry_vol_q250 的 250 日滚动分位算不出来。"""
        from core.wyckoff_engine import FunnelConfig

        assert FunnelConfig().trading_days >= 270

    def test_shadow_failure_does_not_break_funnel(self):
        """影子池是研究支线，异常必须被吞掉。"""
        from pathlib import Path

        src = Path("workflows/wyckoff_funnel.py").read_text(encoding="utf-8")
        block = src.split("def _build_ic_shadow_pool")[1].split("def run(")[0]
        assert "except Exception" in block
        assert "return []" in block


class TestRequiredColumns:
    """signal_observations 的 NOT NULL 列必须齐全。

    2026-08-24 首次实盘落库被 Postgres 拒绝：
        null value in column "track" violates not-null constraint
    当时容错生效、漏斗主流程未受影响，但影子样本丢了一天。
    """

    def _row(self) -> dict:
        from core.ic_shadow_score import to_rows

        picks = [ShadowPick("002121.SZ", -1.65, 1, {"ret60": 2.0, "dry_vol_q250": 1.0})]
        return to_rows(picks, "2026-08-24", ShadowScoreConfig())[0]

    def test_track_present_and_valid(self):
        """track 仅接受 Trend / Accum。影子池选低位缩量股，语义属吸筹。"""
        assert self._row()["track"] == "Accum"

    def test_no_none_values(self):
        """任何 None 都可能撞上 NOT NULL 约束。"""
        nulls = [k for k, v in self._row().items() if v is None]
        assert not nulls, f"这些字段为 None，可能违反 NOT NULL: {nulls}"

    def test_upsert_conflict_keys_all_present(self):
        """upsert 的 on_conflict 是 market,trade_date,code,signal_type——缺一个就报错。"""
        row = self._row()
        for key in ("market", "trade_date", "code", "signal_type"):
            assert row.get(key), key
