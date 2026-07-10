"""core/wyckoff_engine.py 冒烟测试。"""

from __future__ import annotations

import pandas as pd

from core.wyckoff_engine import (
    FunnelConfig,
    _attach_price_targets,
    _board_vol_ratio_scale,
    _build_sector_groups,
    _compute_stop_loss,
    _detect_compression,
    _detect_evr,
    _detect_lps,
    _detect_sos,
    _detect_spring,
    _effective_entry_max_bias_200,
    _is_holiday_grace,
    _latest_trade_date,
    build_candidate_entries,
    detect_accum_stage,
    detect_leader_radar,
    dollar_volume_series,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    sort_by_date_if_needed,
)

MAIN_BOARD_CODE = "600001"
CHINEXT_CODE = "300001"
STAR_CODE = "688001"


def _make_df(dates, closes, volumes=None, amounts=None) -> pd.DataFrame:
    n = len(dates)
    opens = closes
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    vols = volumes or [1_000_000] * n
    amount = amounts if amounts is not None else [100_000_000] * n
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": opens,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": vols,
            "amount": amount,
        }
    )


class TestSortedIfNeeded:
    def test_already_sorted(self):
        df = _make_df(["2024-01-01", "2024-01-02", "2024-01-03"], [10, 11, 12])
        result = sort_by_date_if_needed(df)
        assert list(result["close"]) == [10, 11, 12]

    def test_reverse_sorted(self):
        df = _make_df(["2024-01-03", "2024-01-02", "2024-01-01"], [12, 11, 10])
        result = sort_by_date_if_needed(df)
        assert list(result["close"]) == [10, 11, 12]


class TestDollarVolumeSeries:
    def test_uses_amount_when_available(self):
        df = _make_df(["2024-01-01", "2024-01-02"], [10.0, 11.0], volumes=[100, 200], amounts=[1000.0, 2200.0])
        result = dollar_volume_series(df)
        assert list(result) == [1000.0, 2200.0]

    def test_falls_back_to_close_times_volume_when_amount_all_zero(self):
        df = _make_df(["2024-01-01", "2024-01-02"], [10.0, 11.0], volumes=[100, 200], amounts=[0.0, 0.0])
        result = dollar_volume_series(df)
        assert list(result) == [1000.0, 2200.0]

    def test_falls_back_when_amount_column_missing(self):
        df = _make_df(["2024-01-01", "2024-01-02"], [10.0, 11.0], volumes=[100, 200]).drop(columns=["amount"])
        result = dollar_volume_series(df)
        assert list(result) == [1000.0, 2200.0]


class TestLatestTradeDate:
    def test_returns_last_date(self):
        df = _make_df(["2024-01-01", "2024-01-02", "2024-01-03"], [10, 11, 12])
        result = _latest_trade_date(df)
        assert pd.Timestamp(result) == pd.Timestamp("2024-01-03")

    def test_empty_df_returns_none(self):
        df = pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])
        result = _latest_trade_date(df)
        assert result is None


class TestLayer1Filter:
    def test_production_defaults_use_broader_cap_and_liquidity_thresholds(self):
        cfg = FunnelConfig()

        assert cfg.min_market_cap_yi == 25.0
        assert cfg.min_avg_amount_wan == 4000.0

    def test_filters_st_stocks(self):
        """L1 应剔除 ST 股票（名称含 ST）。"""
        cfg = FunnelConfig()
        # 准备一只正常股和一只 ST 股
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        closes = [10 + i * 0.01 for i in range(100)]
        df = _make_df(dates.strftime("%Y-%m-%d").tolist(), closes)

        name_map = {"000001": "平安银行", "000002": "ST 万科"}
        # 给足够大的市值和成交额，让非 ST 股通过
        mcap = {"000001": 5e10, "000002": 5e10}
        df_map = {"000001": df.copy(), "000002": df.copy()}

        result = layer1_filter(["000001", "000002"], name_map, mcap, df_map, cfg)
        assert "000002" not in result  # ST 被剔除

    def test_accepts_star_and_bse_by_default(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        closes = [10 + i * 0.01 for i in range(100)]
        df = _make_df(dates.strftime("%Y-%m-%d").tolist(), closes)

        name_map = {"688001": "华兴源创", "689009": "科创样本", "830001": "北交样本"}
        mcap = {code: 100.0 for code in name_map}
        df_map = {code: df.copy() for code in name_map}

        result = layer1_filter(list(name_map), name_map, mcap, df_map, cfg)

        assert "688001" in result
        assert "689009" in result
        assert "830001" in result

    def test_can_disable_bse_board_in_layer1(self):
        cfg = FunnelConfig(include_bse_board=False)
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        closes = [10 + i * 0.01 for i in range(100)]
        df = _make_df(dates.strftime("%Y-%m-%d").tolist(), closes)

        name_map = {"830001": "北交样本"}
        result = layer1_filter(["830001"], name_map, {"830001": 100.0}, {"830001": df}, cfg)

        assert result == []

    def test_rejects_single_day_amount_spike_distortion(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        closes = [10.0] * 20
        stable = _make_df(dates.strftime("%Y-%m-%d").tolist(), closes, amounts=[60_000_000] * 20)
        distorted = _make_df(
            dates.strftime("%Y-%m-%d").tolist(),
            closes,
            amounts=[1_000_000] * 19 + [1_000_000_000],
        )

        result = layer1_filter(
            ["000001", "000002"],
            {"000001": "稳定成交", "000002": "单日巨量"},
            {},
            {"000001": stable, "000002": distorted},
            cfg,
        )

        assert result == ["000001"]


def test_sector_groups_stably_deduplicate_concepts_per_symbol() -> None:
    counts, sym_sectors = _build_sector_groups(
        ["000001"],
        {},
        {"000001": ["机器人", "人工智能", "机器人", "", "人工智能"]},
        True,
    )

    assert sym_sectors == {"000001": ["机器人", "人工智能"]}
    assert counts == {"机器人": 1, "人工智能": 1}

    def test_amount_all_zero_falls_back_to_close_times_volume(self):
        """TickFlow 港股/美股历史 K 线 amount 字段恒为 0，L1 流动性过滤必须回退为 close*volume，
        否则所有标的都会被误判为流动性不足而全部剔除（真实生产回归 bug）。

        require_cn_main_or_chinext=False 跳过板块限制，聚焦测试 _amount_liquidity_ok
        本身的回退逻辑（该函数是港股/美股/A股共用的 L1 硬过滤）。
        """
        cfg = FunnelConfig(min_avg_amount_wan=800.0, require_cn_main_or_chinext=False)
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        closes = [10.0] * 100
        # amount 全 0 模拟 TickFlow 港股数据源限制；volume 足够大，close*volume 应能通过门槛
        df = _make_df(
            dates.strftime("%Y-%m-%d").tolist(),
            closes,
            volumes=[2_000_000] * 100,
            amounts=[0.0] * 100,
        )

        result = layer1_filter(["00700.HK"], {"00700.HK": "腾讯"}, {}, {"00700.HK": df}, cfg)

        assert result == ["00700.HK"]

    def test_rejects_low_price_and_hard_market_cap_floor(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        normal = _make_df(dates.strftime("%Y-%m-%d").tolist(), [3.0] * 20)
        low_price = _make_df(dates.strftime("%Y-%m-%d").tolist(), [1.8] * 20)

        result = layer1_filter(
            ["000001", "000002", "000003"],
            {"000001": "正常股", "000002": "低价股", "000003": "硬市值风险"},
            {"000001": 40.0, "000002": 40.0, "000003": 8.0},
            {"000001": normal, "000002": low_price, "000003": normal.copy()},
            cfg,
        )

        assert result == ["000001"]


class TestIsHolidayGrace:
    def test_normal_day_no_grace(self):
        df = _make_df(["2024-01-02", "2024-01-03"], [10, 11])
        assert _is_holiday_grace(df, 1) is False

    def test_weekend_no_grace(self):
        df = _make_df(["2024-01-05", "2024-01-08"], [10, 11])
        assert _is_holiday_grace(df, 1) is True

    def test_holiday_gap_triggers_grace(self):
        df = _make_df(["2024-09-27", "2024-10-08"], [10, 11])
        assert _is_holiday_grace(df, 1) is True

    def test_grace_disabled(self):
        df = _make_df(["2024-09-27", "2024-10-08"], [10, 11])
        assert _is_holiday_grace(df, 0) is False

    def test_grace_day2_still_active(self):
        df = _make_df(["2024-09-27", "2024-10-08", "2024-10-09"], [10, 11, 12])
        assert _is_holiday_grace(df, 1) is False
        assert _is_holiday_grace(df, 2) is True

    def test_grace_day3_expired(self):
        df = _make_df(
            ["2024-09-27", "2024-10-08", "2024-10-09", "2024-10-10"],
            [10, 11, 12, 13],
        )
        assert _is_holiday_grace(df, 2) is False
        assert _is_holiday_grace(df, 3) is True


class TestComputeStopLoss:
    def test_markup_trailing_stop(self):
        cfg = FunnelConfig()
        n = 250
        closes = pd.Series([10.0 + i * 0.05 for i in range(n)])
        lows = closes * 0.99
        highs = closes * 1.01
        price, reason = _compute_stop_loss(closes, lows, highs, "Markup", cfg)
        assert price is not None
        assert "主升趋势破位" in reason

    def test_accum_bottom_stop(self):
        cfg = FunnelConfig()
        n = 250
        closes = pd.Series([10.0] * n)
        lows = pd.Series([9.5] * n)
        highs = pd.Series([10.5] * n)
        price, reason = _compute_stop_loss(closes, lows, highs, "Accum_B", cfg)
        assert price is not None
        assert "吸筹底线" in reason


class TestAccumStage:
    def test_detects_accum_b_bottom_tests(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=260, freq="B")
        close = [10.0] * 260
        low = [9.9] * 200 + [9.8 if i % 10 in {0, 1, 2} else 9.95 for i in range(60)]
        volume = [1_000_000] * 200 + [500_000] * 60
        df = _make_df(dates.strftime("%Y-%m-%d").tolist(), close, volumes=volume)
        df["low"] = low

        result = detect_accum_stage(["000001"], {"000001": df}, cfg)

        assert result["000001"] == "Accum_B"


class TestLeaderRadar:
    def test_detects_independent_markup_watchlist(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=140, freq="B")
        strong_closes = [5.0 + i * 0.08 for i in range(140)]
        flat_closes = [10.0] * 140
        strong = _make_df(
            dates.strftime("%Y-%m-%d").tolist(), strong_closes, volumes=[1_000_000] * 135 + [1_200_000] * 5
        )
        flat = _make_df(dates.strftime("%Y-%m-%d").tolist(), flat_closes)

        rows = detect_leader_radar(
            ["000001", "000002"],
            {"000001": strong, "000002": flat},
            {"000001": "机器人"},
            {"000001": "主升通道"},
            cfg,
        )

        assert [row["code"] for row in rows] == ["000001"]
        assert rows[0]["risk"] == "主升跟踪"
        assert "60日" in rows[0]["reason"]


class TestAlphaCandidateBoard:
    def test_builds_launchpad_candidate_before_extreme_overheat(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=150, freq="B")
        launchpad_closes = [8.0 + i * 0.045 for i in range(90)] + [12.1 + i * 0.055 for i in range(60)]
        flat_closes = [10.0] * 150
        entries = build_candidate_entries(
            alpha_symbols=["000001", "000002"],
            df_map={
                "000001": _make_df(dates.strftime("%Y-%m-%d").tolist(), launchpad_closes),
                "000002": _make_df(dates.strftime("%Y-%m-%d").tolist(), flat_closes),
            },
            sector_map={"000001": "机器人"},
            channel_map={"000001": "主升通道"},
            triggers={},
            stage_map={},
            exit_signals={},
            cfg=cfg,
        )

        assert [item["code"] for item in entries] == ["000001"]
        assert entries[0]["entry_type"] == "launchpad"
        assert entries[0]["track"] == "future_leader"

    def test_builds_recent_supported_breakout_candidate(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=150, freq="B")
        closes = [10.0 + i * 0.01 for i in range(90)]
        closes += [11.0 + i * 0.035 for i in range(55)]
        closes += [13.8, 13.7, 13.9, 13.75, 14.0]
        volumes = [1_000_000] * 145 + [2_200_000, 1_200_000, 1_100_000, 1_100_000, 1_200_000]
        df = _make_df(dates.strftime("%Y-%m-%d").tolist(), closes, volumes=volumes)
        df.loc[df.index[145], "high"] = closes[145] * 1.002

        entries = build_candidate_entries(
            alpha_symbols=["000001"],
            df_map={"000001": df},
            sector_map={"000001": "机器人"},
            channel_map={"000001": "点火破局"},
            triggers={},
            stage_map={},
            exit_signals={},
            cfg=cfg,
        )

        assert [item["code"] for item in entries] == ["000001"]
        assert entries[0]["entry_type"] == "early_breakout"
        assert any("承接" in reason for reason in entries[0]["reasons"])

    def test_builds_volatile_pullback_candidate_for_a_share_wave(self):
        cfg = FunnelConfig()
        dates = pd.date_range("2024-01-01", periods=150, freq="B")
        closes = [8.0 + i * 0.02 for i in range(90)]
        closes += [10.0 + i * 0.08 for i in range(40)]
        closes += [
            13.5,
            12.2,
            14.0,
            12.8,
            14.5,
            13.1,
            15.0,
            13.6,
            15.5,
            14.0,
            16.0,
            14.4,
            16.5,
            15.0,
            17.0,
            15.5,
            17.5,
            16.0,
            18.0,
            17.2,
        ]

        entries = build_candidate_entries(
            alpha_symbols=["000001"],
            df_map={"000001": _make_df(dates.strftime("%Y-%m-%d").tolist(), closes)},
            sector_map={"000001": "机器人"},
            channel_map={"000001": "主升通道"},
            triggers={},
            stage_map={},
            exit_signals={},
            cfg=cfg,
        )

        assert [item["code"] for item in entries] == ["000001"]
        assert entries[0]["entry_type"] == "volatile_pullback"
        assert entries[0]["track"] == "future_leader"


class TestDetectCompression:
    def _build_compression_df(self):
        n = 60
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        closes = [10.0] * n
        highs = [10.0 + (0.5 if i < 40 else 0.5 * (0.7 ** (i - 40))) for i in range(n)]
        lows = [10.0 - (0.5 if i < 40 else 0.5 * (0.7 ** (i - 40))) for i in range(n)]
        vols = [1_000_000 if i < 40 else int(600_000 * (0.9 ** (i - 40))) for i in range(n)]
        return pd.DataFrame(
            {
                "date": dates,
                "open": closes,
                "close": closes,
                "high": highs,
                "low": lows,
                "volume": vols,
                "pct_chg": [0.0] * n,
            }
        )

    def test_detects_compression(self):
        cfg = FunnelConfig()
        df = self._build_compression_df()
        result = _detect_compression(df, cfg)
        assert result is not None
        assert result < 1.0

    def test_rejects_high_position(self):
        cfg = FunnelConfig()
        n = 250
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        closes = [10.0 + i * 0.2 for i in range(n)]
        df = pd.DataFrame(
            {
                "date": dates,
                "open": closes,
                "close": closes,
                "high": [c * 1.001 for c in closes],
                "low": [c * 0.999 for c in closes],
                "volume": [1_000_000] * n,
                "pct_chg": [0.0] * n,
            }
        )
        result = _detect_compression(df, cfg)
        assert result is None

    def test_rejects_downtrend_compression(self):
        cfg = FunnelConfig()
        n = 60
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        closes = [12.0 - i * 0.03 for i in range(n)]
        highs = [c + (0.45 if i < 40 else 0.45 * (0.7 ** (i - 40))) for i, c in enumerate(closes)]
        lows = [c - (0.45 if i < 40 else 0.45 * (0.7 ** (i - 40))) for i, c in enumerate(closes)]
        vols = [1_000_000 if i < 40 else int(600_000 * (0.9 ** (i - 40))) for i in range(n)]
        df = pd.DataFrame(
            {
                "date": dates,
                "open": closes,
                "close": closes,
                "high": highs,
                "low": lows,
                "volume": vols,
                "pct_chg": [0.0] * n,
            }
        )

        assert _detect_compression(df, cfg) is None

    def test_effective_bias_limit_uses_star_and_trend_overrides(self):
        cfg = FunnelConfig()

        assert _effective_entry_max_bias_200("000001", "", cfg) == 25.0
        assert _effective_entry_max_bias_200("688001", "", cfg) == 40.0
        assert _effective_entry_max_bias_200("000001", "趋势延续", cfg) == 35.0
        # 仅有高 RPS 无绝对收益地板时，不得放宽。
        assert _effective_entry_max_bias_200("000001", "趋势延续", cfg, rps_slow=95.0) == 35.0
        assert _effective_entry_max_bias_200("000001", "趋势延续", cfg, rps_slow=95.0, ret120_pct=45.0) == 60.0
        assert _effective_entry_max_bias_200("688001", "趋势延续", cfg, rps_slow=95.0, ret120_pct=45.0) == 80.0

    def test_sos_bypass_requires_minimum_slow_rps(self):
        cfg = FunnelConfig()
        cfg.enable_ambush_channel = False
        cfg.enable_accumulation_channel = False
        cfg.enable_dry_vol_channel = False
        cfg.enable_rs_divergence_channel = False
        cfg.enable_trend_cont_channel = False
        cfg.enable_breakout_accel_channel = False
        cfg.enable_pre_ignition_watch = False
        cfg.sos_bypass_rps_slow_min = 75.0
        dates = pd.date_range("2024-01-01", periods=220, freq="B")
        weak_close = [10.0] * 219 + [10.7]
        strong_close = [10.0 + i * 0.08 for i in range(220)]

        weak = pd.DataFrame(
            {
                "date": dates,
                "open": [10.0] * 220,
                "close": weak_close,
                "high": [10.1] * 219 + [10.8],
                "low": [9.9] * 219 + [10.0],
                "volume": [1_000_000] * 219 + [4_000_000],
                "pct_chg": [0.0] * 219 + [7.0],
            }
        )
        strong = pd.DataFrame(
            {
                "date": dates,
                "open": strong_close,
                "close": strong_close,
                "high": [c * 1.01 for c in strong_close],
                "low": [c * 0.99 for c in strong_close],
                "volume": [1_000_000] * 220,
                "pct_chg": [0.5] * 220,
            }
        )

        passed, channel_map, _ = layer2_strength_detailed(
            ["LOW"],
            {"LOW": weak, "HIGH": strong},
            None,
            cfg,
            rps_universe=["LOW", "HIGH"],
        )

        assert passed == []
        assert channel_map == {}


class TestSectorHeatBypass:
    def test_heat_bypass_includes_sector(self):
        cfg = FunnelConfig()
        cfg.sector_heat_bypass_min_count = 2
        n = 30
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        base_closes = [10.0 + i * 0.1 for i in range(n)]
        df_map = {}
        for code in ["A1", "A2", "B1"]:
            df_map[code] = pd.DataFrame(
                {
                    "date": dates,
                    "open": base_closes,
                    "close": base_closes,
                    "high": [c * 1.02 for c in base_closes],
                    "low": [c * 0.98 for c in base_closes],
                    "volume": [1_000_000] * n,
                }
            )
        sector_map = {"A1": "电气设备", "A2": "电气设备", "B1": "食品饮料"}
        result, top = layer3_sector_resonance(
            ["A1", "A2", "B1"],
            sector_map,
            cfg,
            base_symbols=["A1", "A2", "B1"],
            df_map=df_map,
        )
        assert "A1" in result
        assert "A2" in result

    def test_hot_concept_matches_normalized_aliases(self):
        cfg = FunnelConfig()
        cfg.sector_min_count = 1
        cfg.l3_keep_strength_min = 0.0
        n = 30
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        closes = [10.0 + i * 0.1 for i in range(n)]
        df_map = {
            code: pd.DataFrame(
                {
                    "date": dates,
                    "open": closes,
                    "close": closes,
                    "high": [c * 1.02 for c in closes],
                    "low": [c * 0.98 for c in closes],
                    "volume": [1_000_000] * n,
                }
            )
            for code in ["R1", "R2", "B1"]
        }
        result, top = layer3_sector_resonance(
            ["R1", "R2", "B1"],
            {"B1": "银行"},
            cfg,
            base_symbols=["R1", "R2", "B1"],
            df_map=df_map,
            concept_map={"R1": ["减速器"], "R2": ["机器视觉"], "B1": ["银行"]},
            hot_concepts=["机器人"],
        )

        assert {"R1", "R2"} <= set(result)
        assert {"减速器", "机器视觉"} & set(top)

    def test_hot_concepts_match_normalized_theme_aliases(self):
        cfg = FunnelConfig()
        cfg.sector_min_count = 2
        cfg.top_n_sectors = 1
        cfg.l3_hot_leader_strength_min = 0.50
        dates = pd.date_range("2024-01-01", periods=30, freq="B")

        def frame(start: float, step: float) -> pd.DataFrame:
            closes = [start + i * step for i in range(30)]
            return pd.DataFrame(
                {
                    "date": dates,
                    "open": closes,
                    "close": closes,
                    "high": [c * 1.02 for c in closes],
                    "low": [c * 0.98 for c in closes],
                    "volume": [1_000_000] * 30,
                }
            )

        df_map = {
            "A1": frame(10.0, 0.30),
            "B1": frame(10.0, 0.01),
            "B2": frame(10.0, 0.01),
            "B3": frame(10.0, 0.01),
        }
        result, top = layer3_sector_resonance(
            ["A1", "B1", "B2", "B3"],
            {},
            cfg,
            base_symbols=["A1", "B1", "B2", "B3"],
            df_map=df_map,
            concept_map={
                "A1": ["减速器"],
                "B1": ["银行"],
                "B2": ["银行"],
                "B3": ["银行"],
            },
            hot_concepts=["机器人"],
        )

        assert top == ["银行"]
        assert "A1" in result


def _flat_board_history(n: int, base: float) -> pd.DataFrame:
    """构造一段窄幅横盘历史（用于满足 Spring 的交易区间上下文要求）。"""
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    closes = [base + ((i % 5) - 2) * 0.01 * base for i in range(n)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c * 1.003 for c in closes],
            "low": [c * 0.997 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
            "pct_chg": [0.0] * n,
        }
    )


def _append_board_row(df: pd.DataFrame, *, open_, high, low, close, volume, pct_chg=0.0) -> pd.DataFrame:
    next_date = df["date"].iloc[-1] + pd.tseries.offsets.BDay(1)
    row = pd.DataFrame(
        {
            "date": [next_date],
            "open": [open_],
            "high": [high],
            "low": [low],
            "close": [close],
            "volume": [volume],
            "pct_chg": [pct_chg],
        }
    )
    return pd.concat([df, row], ignore_index=True)


class TestBoardVolRatioScale:
    """20% 涨跌停板块（创业板/科创板）的量能阈值应比 10% 主板/北交所更宽松。"""

    def test_main_board_scale_is_one(self):
        assert _board_vol_ratio_scale(MAIN_BOARD_CODE) == 1.0

    def test_bse_scale_is_one(self):
        assert _board_vol_ratio_scale("430001") == 1.0

    def test_chinext_scale_is_amplified(self):
        assert _board_vol_ratio_scale(CHINEXT_CODE) > 1.0

    def test_star_scale_is_amplified(self):
        assert _board_vol_ratio_scale(STAR_CODE) > 1.0

    def test_unknown_code_defaults_to_one(self):
        assert _board_vol_ratio_scale("") == 1.0


def _spring_setup_by_board(base: float = 10.0) -> tuple[pd.DataFrame, float, float]:
    """构造一个满足 Spring 支撑测试基础条件的历史：横盘 + 前一日跌破支撑。"""
    cfg = FunnelConfig()
    history = _flat_board_history(cfg.spring_support_window + 5, base)
    support_level = float(history["close"].tail(cfg.spring_support_window).min())
    vol_avg = float(history["volume"].tail(5).mean())
    history = _append_board_row(
        history,
        open_=support_level * 1.01,
        high=support_level * 1.02,
        low=support_level * 0.97,
        close=support_level * 1.005,
        volume=vol_avg,
    )
    return history, support_level, vol_avg


class TestSpringVolScaleByBoard:
    def test_volume_between_main_and_registration_threshold_only_passes_main_board(self):
        """量能刚好越过主板门槛、但不足以越过创业板/科创板放大后门槛时，仅主板应判定为 Spring。"""
        cfg = FunnelConfig()
        history, support_level, vol_avg = _spring_setup_by_board()
        # 主板门槛：vol_avg * spring_vol_ratio；放大后门槛：* _board_vol_ratio_scale (~1.41)
        borderline_volume = vol_avg * cfg.spring_vol_ratio * 1.15
        df = _append_board_row(
            history,
            open_=support_level * 0.99,
            high=support_level * 1.06,
            low=support_level * 0.96,
            close=support_level * 1.05,
            volume=borderline_volume,
        )
        assert _detect_spring(df, cfg, code=MAIN_BOARD_CODE) is not None
        assert _detect_spring(df, cfg, code=CHINEXT_CODE) is None
        assert _detect_spring(df, cfg, code=STAR_CODE) is None


class TestLpsVolScaleByBoard:
    def test_vol_ratio_between_main_and_registration_threshold(self):
        cfg = FunnelConfig()
        n = max(cfg.lps_vol_ref_window, cfg.lps_ma) + cfg.lps_lookback + cfg.lps_ma_rising_window + 5
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        base = 10.0
        # 缓慢上升的均线，制造 MA20 抬升 + 价格回踩 MA20 的场景。
        closes = [base + i * 0.01 for i in range(n)]
        volumes = [1_000_000.0] * (n - cfg.lps_lookback)
        # 参考窗口最大量能为 1_000_000；近 lookback 日最大量能设为主板门槛和创业板门槛之间。
        borderline_vol = 1_000_000.0 * (cfg.lps_vol_dry_ratio + 0.10)
        volumes += [borderline_vol] * cfg.lps_lookback
        df = pd.DataFrame(
            {
                "date": dates,
                "open": closes,
                "high": [c * 1.001 for c in closes],
                "low": closes,  # low 紧贴 MA20，满足 lps_ma_tolerance
                "close": closes,
                "volume": volumes,
                "pct_chg": [0.0] * n,
            }
        )
        # 确保最后几日 low 恰好等于当时的 MA20（人为对齐），否则 lps 会因为 tolerance 被拒绝。
        ma20 = df["close"].rolling(cfg.lps_ma).mean()
        for idx in df.index[-cfg.lps_lookback :]:
            df.loc[idx, "low"] = float(ma20.loc[idx])

        # borderline_vol/1_000_000 = 0.6，介于主板阈值 0.5 与创业板放宽阈值 0.705 之间。
        assert _detect_lps(df, cfg, code=MAIN_BOARD_CODE) is None
        assert _detect_lps(df, cfg, code=CHINEXT_CODE) is not None
        assert _detect_lps(df, cfg, code=STAR_CODE) is not None


class TestEvrVolScaleByBoard:
    def test_day_pct_between_main_and_registration_threshold(self):
        cfg = FunnelConfig()
        n = cfg.evr_vol_window + 10
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        base = 10.0
        # 事件日是候选索引 -2（confirm_days=1），随后一日（-1）需确认收盘不跌破事件日最低价。
        closes = [base] * (n - 2) + [base, base]
        volumes = [1_000_000.0] * (n - 2) + [1_000_000.0 * (cfg.evr_vol_ratio + 0.5), 1_000_000.0]
        # 当日涨幅刚好越过主板 evr_max_rise，但小于放大后的科创板门槛。
        borderline_pct = cfg.evr_max_rise * 1.15
        pct_chg = [0.0] * (n - 2) + [borderline_pct, 0.0]
        lows = closes
        df = pd.DataFrame(
            {
                "date": dates,
                "open": closes,
                "high": closes,
                "low": lows,
                "close": closes,
                "volume": volumes,
                "pct_chg": pct_chg,
            }
        )
        assert _detect_evr(df, cfg, code=MAIN_BOARD_CODE) is None
        assert _detect_evr(df, cfg, code=STAR_CODE) is not None


class TestSosVolScaleByBoard:
    def test_day_pct_between_main_and_registration_threshold(self):
        cfg = FunnelConfig()
        n = max(cfg.sos_vol_window, cfg.sos_breakout_window, 200) + 5
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        base = 10.0
        closes = [base] * (n - 1)
        # 当日涨幅刚好越过主板 sos_pct_min，但不足以越过放大后的科创板门槛。
        borderline_pct = cfg.sos_pct_min * 1.15
        last_close = base * (1 + borderline_pct / 100.0)
        closes.append(last_close)
        volumes = [1_000_000.0] * (n - 1) + [1_000_000.0 * (cfg.sos_vol_ratio + 1.0)]
        pct_chg = [0.0] * (n - 1) + [borderline_pct]
        highs = list(closes)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": closes,
                "high": highs,
                "low": closes,
                "close": closes,
                "volume": volumes,
                "pct_chg": pct_chg,
            }
        )
        # 6.9% 涨幅越过主板门槛(6.0%)，但达不到创业板/科创板放大后的门槛(6.0%*1.41≈8.46%)，
        # 说明同样的涨幅"含金量"在20%涨跌停板块更低，点火判定理应更严格。
        assert _detect_sos(df, cfg, code=MAIN_BOARD_CODE) is not None
        assert _detect_sos(df, cfg, code=STAR_CODE) is None


class TestFrozenBoardExcludedFromSpring:
    """一字涨跌停日不应被误判为有效 Spring 支撑。"""

    def test_normal_recovery_day_detects_spring(self):
        """非一字板的正常收回日，应能检测出有效 Spring。"""
        history, support_level, _ = _spring_setup_by_board()
        cfg = FunnelConfig()
        vol_avg = float(history["volume"].tail(5).iloc[:-1].mean())
        # last day: recovers with a real trading range and expanded volume (valid Spring).
        df = _append_board_row(
            history,
            open_=support_level * 0.99,
            high=support_level * 1.06,
            low=support_level * 0.96,
            close=support_level * 1.05,
            volume=vol_avg * (cfg.spring_vol_ratio + 0.5),
        )
        score = _detect_spring(df, cfg)
        assert score is not None
        assert score > 0

    def test_frozen_limit_down_day_returns_none(self):
        """最后一天若是一字跌停（开=高=低=收，无真实波动），不能算有效 Spring 收回。"""
        history, support_level, _ = _spring_setup_by_board()
        cfg = FunnelConfig()
        vol_avg = float(history["volume"].tail(5).iloc[:-1].mean())
        frozen_price = support_level * 1.05
        df = _append_board_row(
            history,
            open_=frozen_price,
            high=frozen_price,
            low=frozen_price,
            close=frozen_price,
            volume=vol_avg * (cfg.spring_vol_ratio + 0.5),
        )
        assert _detect_spring(df, cfg) is None

    def test_frozen_prev_day_also_excluded(self):
        """若"跌破支撑"那一天（prev）本身就是一字板（无真实换手的跌停），同样排除。"""
        cfg = FunnelConfig()
        history = _flat_board_history(cfg.spring_support_window + 5, base=10.0)
        support_level = float(history["close"].tail(cfg.spring_support_window).min())
        vol_avg = float(history["volume"].tail(5).mean())
        frozen_price = support_level * 0.97
        history = _append_board_row(
            history, open_=frozen_price, high=frozen_price, low=frozen_price, close=frozen_price, volume=vol_avg
        )
        df = _append_board_row(
            history,
            open_=support_level * 0.99,
            high=support_level * 1.06,
            low=support_level * 0.96,
            close=support_level * 1.05,
            volume=vol_avg * (cfg.spring_vol_ratio + 0.5),
        )
        assert _detect_spring(df, cfg) is None


class TestAttachPriceTargets:
    """给候选条目原地补充技术位目标价。"""

    def test_attaches_price_targets_and_metrics_for_matching_code(self):
        dates = pd.date_range("2024-01-01", periods=260, freq="B")
        # 前259天在[10, 12]箱体内震荡，最后一天放量突破至13。
        closes = [10.0 + (i % 5) * 0.4 for i in range(259)] + [13.0]
        df = _make_df(dates, closes)
        entries = [{"code": MAIN_BOARD_CODE, "score": 80.0}]

        _attach_price_targets(entries, {MAIN_BOARD_CODE: df})

        entry = entries[0]
        assert "price_targets" in entry
        assert entry["price_targets"]["last_close"] == 13.0
        assert entry["metrics"]["target_last_close"] == 13.0
        # 原有字段不应被覆盖
        assert entry["score"] == 80.0

    def test_missing_df_leaves_entry_untouched(self):
        entries = [{"code": "999999", "score": 50.0}]

        _attach_price_targets(entries, {})

        assert "price_targets" not in entries[0]
        assert entries[0] == {"code": "999999", "score": 50.0}

    def test_preserves_existing_metrics(self):
        dates = pd.date_range("2024-01-01", periods=260, freq="B")
        closes = [10.0 + (i % 5) * 0.4 for i in range(259)] + [13.0]
        df = _make_df(dates, closes)
        entries = [{"code": MAIN_BOARD_CODE, "metrics": {"existing_key": 1.23}}]

        _attach_price_targets(entries, {MAIN_BOARD_CODE: df})

        assert entries[0]["metrics"]["existing_key"] == 1.23
        assert "target_last_close" in entries[0]["metrics"]
