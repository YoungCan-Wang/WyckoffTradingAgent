"""Tests for the RS-divergence volume thresholds extracted from hardcoded literals.

背景：`_rs_divergence_volume_ok` 原先把 1.2 / 0.8 硬编码在函数体里，既不能调也不能关。
实测（两年 / 100 个大盘创 60 日新低的交易日）大盘近 20 日均量 > 前 40 日均量 ×1.2 的
满足天数为 **0 天**，导致「暗中护盘」通道在生产回测 66 个交易日里命中恒为 0——指数成交量
是 5000+ 只股票的加总，20 日尺度被平滑，不会像个股那样放量 20%。

本次仅提取为可配置字段，**默认值保持不变**（通道仍失效），故这些用例的第一职责是
守住「重构不改行为」，第二职责是证明新字段真的能调。
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from core.layer2_strength import _rs_divergence_volume_ok
from core.wyckoff_engine import FunnelConfig


def _frames(bench_change: float, stock_change: float, ref_len: int = 40, recent_len: int = 20):
    """构造前 ref_len 日基准量 100、近 recent_len 日按给定比例变化的量能序列。"""
    bench = pd.DataFrame({"volume": [100.0] * ref_len + [100.0 * bench_change] * recent_len})
    stock = pd.DataFrame({"volume": [100.0] * ref_len + [100.0 * stock_change] * recent_len})
    return stock, bench


class TestDefaultsUnchanged:
    def test_field_defaults_match_previous_literals(self):
        cfg = FunnelConfig()
        assert cfg.rs_div_bench_vol_expand_ratio == pytest.approx(1.2)
        assert cfg.rs_div_stock_vol_shrink_ratio == pytest.approx(0.8)

    def test_bench_expand_and_stock_shrink_passes(self):
        stock, bench = _frames(1.3, 0.7)
        assert _rs_divergence_volume_ok(stock, bench, FunnelConfig()) is True

    def test_bench_expand_below_threshold_fails(self):
        """大盘只放量 10% —— 这正是实盘 100% 的触发日所处的区间。"""
        stock, bench = _frames(1.1, 0.7)
        assert _rs_divergence_volume_ok(stock, bench, FunnelConfig()) is False

    def test_stock_not_shrinking_enough_fails(self):
        stock, bench = _frames(1.3, 0.9)
        assert _rs_divergence_volume_ok(stock, bench, FunnelConfig()) is False

    def test_boundary_is_strict_on_bench(self):
        """恰好等于 1.2 倍应判否（原实现用 > 而非 >=），重构不得放宽。"""
        stock, bench = _frames(1.2, 0.7)
        assert _rs_divergence_volume_ok(stock, bench, FunnelConfig()) is False

    def test_boundary_is_strict_on_stock(self):
        """恰好等于 0.8 倍应判否（原实现用 < 而非 <=）。"""
        stock, bench = _frames(1.3, 0.8)
        assert _rs_divergence_volume_ok(stock, bench, FunnelConfig()) is False


class TestNowConfigurable:
    def test_lowering_bench_ratio_admits_realistic_index_volume(self):
        """把大盘阈值降到 1.05 后，+10% 的现实指数放量即可通过——证明字段生效。"""
        stock, bench = _frames(1.1, 0.7)
        cfg = replace(FunnelConfig(), rs_div_bench_vol_expand_ratio=1.05)
        assert _rs_divergence_volume_ok(stock, bench, cfg) is True

    def test_raising_stock_ratio_admits_milder_shrink(self):
        stock, bench = _frames(1.3, 0.9)
        cfg = replace(FunnelConfig(), rs_div_stock_vol_shrink_ratio=0.95)
        assert _rs_divergence_volume_ok(stock, bench, cfg) is True

    def test_both_ratios_independent(self):
        stock, bench = _frames(1.1, 0.9)
        loosened = replace(FunnelConfig(), rs_div_bench_vol_expand_ratio=1.05, rs_div_stock_vol_shrink_ratio=0.95)
        assert _rs_divergence_volume_ok(stock, bench, loosened) is True
        # 只放宽一侧仍应被另一侧拦住。
        assert (
            _rs_divergence_volume_ok(stock, bench, replace(FunnelConfig(), rs_div_bench_vol_expand_ratio=1.05)) is False
        )

    def test_missing_attrs_fall_back_to_previous_literals(self):
        """getattr 回退：传入不带新字段的配置对象时仍按 1.2/0.8 行为。"""

        class Legacy:
            rs_div_bench_window = 20
            rs_div_bench_ref_window = 60
            rs_div_stock_window = 20

        stock, bench = _frames(1.3, 0.7)
        assert _rs_divergence_volume_ok(stock, bench, Legacy()) is True
        stock2, bench2 = _frames(1.1, 0.7)
        assert _rs_divergence_volume_ok(stock2, bench2, Legacy()) is False


class TestDegenerateInputs:
    def test_empty_volume_passes_through(self):
        """量能缺失时不应因此否决——保持原实现的宽松回退。"""
        empty = pd.DataFrame({"volume": []})
        stock, _ = _frames(1.3, 0.7)
        assert _rs_divergence_volume_ok(stock, empty, FunnelConfig()) is True
        assert _rs_divergence_volume_ok(empty, stock, FunnelConfig()) is True

    def test_zero_reference_volume_passes_through(self):
        bench = pd.DataFrame({"volume": [0.0] * 40 + [130.0] * 20})
        stock = pd.DataFrame({"volume": [0.0] * 40 + [70.0] * 20})
        assert _rs_divergence_volume_ok(stock, bench, FunnelConfig()) is True
