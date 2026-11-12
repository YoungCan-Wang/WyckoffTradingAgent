"""Offline unit tests for the limit-up-hunter skill scanner (no network)."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "limit-up-hunter" / "scripts" / "ladder_scanner.py"
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location("ladder_scanner", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


LS = _load_scanner()


def _raw_quote(
    symbol: str,
    name: str,
    prev_close: float,
    open_p: float,
    high_p: float,
    low_p: float,
    last_p: float,
    vwap: float,
    amount_yi: float = 5.0,
    timestamp: int = 1773110520000,
) -> dict[str, Any]:
    amt = amount_yi * 1e8
    vol = amt / (vwap * 100.0)
    change_pct = (last_p - prev_close) / prev_close
    return {
        "symbol": symbol,
        "prev_close": prev_close,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "last_price": last_p,
        "volume": vol,
        "amount": amt,
        "timestamp": timestamp,
        "ext": {"name": name, "change_pct": change_pct},
    }


def test_parse_quote_filters_st_bj_new_ipo_and_checks_exact_one_word():
    assert LS.parse_quote(_raw_quote("000091.SZ", "*ST样本", 1.10, 1.07, 1.09, 1.06, 1.09, 1.07)) is None
    assert LS.parse_quote(_raw_quote("920001.BJ", "北交样本", 10.0, 25.0, 28.0, 22.0, 25.1, 25.0)) is None
    assert LS.parse_quote(_raw_quote("300991.SZ", "N新股甲", 10.0, 25.0, 28.0, 22.0, 25.1, 25.0)) is None
    assert LS.parse_quote(_raw_quote("688991.SH", "C新股乙", 20.0, 35.0, 38.0, 32.0, 35.1, 35.0)) is None

    near_ow = LS.parse_quote(_raw_quote("000001.SZ", "样本高开", 10.00, 10.96, 11.00, 10.94, 11.00, 10.98))
    assert near_ow is not None
    assert near_ow.is_limit_up is True
    assert near_ow.is_one_word is False

    ow_q = _raw_quote("600001.SH", "样本一字", 18.85, 20.74, 20.74, 20.74, 20.74, 20.7400002, 1.20)
    ow = LS.parse_quote(ow_q)
    assert ow is not None
    assert ow.is_limit_up is True
    assert ow.is_one_word is True
    assert ow.above_vwap is True
    assert ">=VWAP: 20.74" in LS.format_row(ow)


def test_compute_ladder_from_daily_rows_and_5m_analysis(capsys, tmp_path, monkeypatch):
    nine_board_rows: list[tuple[str, float, float]] = [("20260301", 10.00, 10.00)]
    p = 10.00
    for i in range(1, 10):
        nxt = LS.calc_limit_up_price(p, "600001.SH")
        nine_board_rows.append((f"202603{1 + i:02d}", p, nxt))
        p = nxt

    rows_by_sym = {
        "600001.SH": nine_board_rows,
        "000002.SZ": [("20260303", 5.00, 5.00), ("20260304", 5.00, 5.50), ("20260305", 5.50, 6.05)],
        "600003.SH": [
            ("20260301", 2.50, 2.75),
            ("20260302", 2.75, 3.03),
            ("20260303", 3.03, 3.33),
            ("20260304", 3.33, 3.40),
            ("20260305", 3.40, 3.28),
            ("20260306", 3.28, 3.61),
        ],
    }
    ladder = LS.compute_ladder_from_daily_rows(rows_by_sym)
    assert ladder["600001.SH"] == {"prev_boards": 9, "m_days_n_boards": ""}
    assert ladder["000002.SZ"] == {"prev_boards": 2, "m_days_n_boards": ""}
    assert ladder["600003.SH"] == {"prev_boards": 1, "m_days_n_boards": "6天4板"}

    monkeypatch.setattr(LS.Path, "home", lambda: tmp_path)
    assert LS.load_prev_ladder("20990101", None) == {}
    assert "[WARN]" in capsys.readouterr().err

    bars_df = pd.DataFrame(
        [
            {"date": "2026-03-10", "open": 5.75, "high": 6.18, "close": 6.12, "amount": 1000.0},
            {"date": "2026-03-10", "open": 6.12, "high": 6.15, "close": 6.08, "amount": 120.0},
            {"date": "2026-03-10", "open": 6.08, "high": 6.25, "close": 6.25, "amount": 600.0},
        ]
    )
    summary = LS.analyze_5m_bars(bars_df, prev_close=6.06)
    assert summary is not None
    assert summary.first_5m_red is True
    assert summary.first_5m_surge_pct == pytest.approx((6.18 - 5.75) / 6.06 * 100.0)
    assert summary.min_vol_ratio == pytest.approx(0.12)
    assert summary.broke_morning_high is True
    assert "(<=15%锁仓)" in summary.format_tag()
    assert "过早高" in summary.format_tag()


def test_classify_ladder_buckets_retains_prev_pool_and_scales_20cm():
    ladder_info = {
        "600001.SH": {"prev_boards": 3, "m_days_n_boards": ""},
        "000002.SZ": {"prev_boards": 2, "m_days_n_boards": ""},
        "000003.SZ": {"prev_boards": 2, "m_days_n_boards": ""},
        "600004.SH": {"prev_boards": 0, "m_days_n_boards": "10天5板"},
        "600005.SH": {"prev_boards": 1, "m_days_n_boards": ""},
        "600006.SH": {"prev_boards": 1, "m_days_n_boards": ""},
    }
    quotes = [
        _raw_quote("600001.SH", "样本顶一", 18.85, 20.74, 20.74, 20.74, 20.74, 20.74, 1.20),
        _raw_quote("000002.SZ", "样本晋级", 6.06, 5.75, 6.67, 5.71, 6.67, 6.18, 6.02),
        _raw_quote("600004.SH", "样本反包", 2.35, 2.35, 2.59, 2.34, 2.59, 2.53, 8.54),
        _raw_quote("600005.SH", "样本长腿", 12.39, 12.29, 13.10, 11.57, 13.09, 12.42, 8.30),
        _raw_quote("600006.SH", "样本深水", 4.10, 3.85, 3.92, 3.80, 3.88, 3.87, 3.10),
        _raw_quote("300007.SZ", "双创微弹", 50.0, 47.0, 50.8, 46.8, 50.5, 49.5, 2.5),
        _raw_quote("688008.SH", "双创反攻", 162.0, 157.0, 185.1, 155.0, 185.0, 168.7, 34.5),
        _raw_quote("000003.SZ", "样本炸板", 2.92, 2.88, 3.14, 2.76, 2.86, 2.98, 11.2),
    ]
    parsed = [m for q in quotes if (m := LS.parse_quote(q, ladder_info)) is not None]
    buckets = LS.classify_ladder_buckets(parsed)

    assert [m.symbol for m in buckets["prev_ladder_pool"]] == [
        "600001.SH",
        "000002.SZ",
        "000003.SZ",
        "600004.SH",
        "600005.SH",
        "600006.SH",
    ]
    assert [m.symbol for m in buckets["one_words"]] == ["600001.SH"]
    assert [m.symbol for m in buckets["turnover_lu"]] == ["000002.SZ", "600004.SH"]
    assert [m.symbol for m in buckets["deep_reversals"]] == [
        "000002.SZ",
        "600005.SH",
        "600004.SH",
        "688008.SH",
    ]
    assert [m.symbol for m in buckets["traps"]] == ["000003.SZ"]


def test_tag_keyword_candidate_flags_1030_laggard_per_line(capsys):
    prefix_quotes = [
        _raw_quote("600011.SH", "华星一号", 3.61, 3.67, 3.97, 3.67, 3.97, 3.81, 22.0),
        _raw_quote("000012.SZ", "华星二号", 2.11, 2.07, 2.32, 2.06, 2.32, 2.24, 2.4),
        _raw_quote("600013.SH", "华星三号", 8.17, 7.94, 8.99, 7.94, 8.99, 8.64, 4.5),
        _raw_quote("300014.SZ", "华星四号", 12.00, 12.10, 14.40, 12.05, 14.40, 13.50, 8.2),
        _raw_quote("000015.SZ", "华星五号", 13.38, 12.99, 14.68, 12.85, 14.03, 13.84, 6.1),
        _raw_quote("000016.SZ", "华星六号", 13.74, 13.12, 13.86, 13.06, 13.76, 13.43, 3.1),
    ]
    snap_dt = LS.extract_snapshot_time(prefix_quotes)
    assert snap_dt.tzinfo is not None
    parsed = [m for q in prefix_quotes if (m := LS.parse_quote(q)) is not None]
    LS.print_sections(parsed, keyword="华星", now_hhmm="10:32")
    lines = capsys.readouterr().out.splitlines()

    assert any("600011.SH" in ln and "[🔥前排封板先锋]" in ln for ln in lines)
    assert any("000015.SZ" in ln and "[✅均线上活跃前排]" in ln for ln in lines)
    assert any("000016.SZ" in ln and "[⚠️10:30后平盘观察]" in ln for ln in lines)


def test_detect_fermenting_themes_discovers_ice_snow_and_auto_prefix():
    quotes = [
        _raw_quote("600021.SH", "冰雪一号", 3.81, 4.09, 4.19, 4.00, 4.19, 4.15, 4.7),
        _raw_quote("000022.SZ", "冰山样本", 8.70, 9.17, 9.57, 9.10, 9.57, 9.45, 8.1),
        _raw_quote("000023.SZ", "雪域样本", 14.50, 14.76, 15.30, 14.70, 15.25, 15.05, 6.2),
        _raw_quote("300024.SZ", "晶雪样本", 21.84, 22.39, 23.60, 22.30, 23.50, 23.10, 2.8),
        _raw_quote("600025.SH", "华星领航", 3.97, 4.17, 4.37, 4.16, 4.37, 4.27, 20.0),
        _raw_quote("600026.SH", "华星先锋", 8.99, 9.89, 9.89, 9.89, 9.89, 9.89, 0.5),
        _raw_quote("000027.SZ", "华星动力", 13.90, 14.29, 14.86, 14.30, 14.60, 14.55, 7.5),
    ]
    parsed = [m for q in quotes if (m := LS.parse_quote(q)) is not None]
    themes = LS.detect_fermenting_themes(parsed)
    by_name = {t["theme"]: t for t in themes}

    assert "冰雪/寒潮供暖" in by_name
    assert by_name["冰雪/寒潮供暖"]["lu_count"] == 2
    assert [m.symbol for m in by_name["冰雪/寒潮供暖"]["active_front"]] == ["300024.SZ", "000023.SZ"]
    assert "『华星』字辈" in by_name
    assert by_name["『华星』字辈"]["lu_count"] == 2
    assert [m.symbol for m in by_name["『华星』字辈"]["active_front"]] == ["000027.SZ"]


def test_reversal_risk_and_two_tier_holdings_exit(capsys):
    ladder_info = {
        "600031.SH": {"prev_boards": 2, "m_days_n_boards": "6天5板"},
        "000032.SZ": {"prev_boards": 4, "m_days_n_boards": "6天5板"},
        "600033.SH": {"prev_boards": 3, "m_days_n_boards": ""},
        "600034.SH": {"prev_boards": 2, "m_days_n_boards": ""},
    }
    quotes = [
        _raw_quote("600025.SH", "华星领航", 3.97, 4.17, 4.37, 4.16, 4.37, 4.28, 25.0),
        _raw_quote("600026.SH", "华星先锋", 8.99, 9.89, 9.89, 9.89, 9.89, 9.89, 0.5),
        _raw_quote("000027.SZ", "华星动力", 13.91, 14.30, 14.86, 14.30, 14.56, 14.55, 8.6),
        _raw_quote("600031.SH", "样本摸高", 5.40, 5.32, 5.90, 5.09, 5.74, 5.62, 14.5),
        _raw_quote("000032.SZ", "样本久盘", 11.09, 10.28, 12.19, 10.27, 11.94, 11.00, 6.2),
        # Follower (0板) below VWAP & breaking first 5m low -> unconditional exit
        _raw_quote("000035.SZ", "样本跟风", 4.00, 4.04, 4.10, 3.88, 3.91, 3.98, 3.5),
        # Multi-board (3板) below VWAP, shrunk vol (0.22 <= 0.30), mild dip (-2.5% > -6.0%) -> partial hold
        _raw_quote("600033.SH", "样本连板", 10.00, 9.95, 10.20, 9.68, 9.75, 9.92, 4.8),
        # Multi-board (2板) below VWAP with heavy volume breakdown (0.55 > 0.30) -> unconditional exit
        _raw_quote("600034.SH", "样本放量", 8.00, 7.90, 8.10, 7.60, 7.68, 7.88, 6.0),
    ]
    by_sym = {q["symbol"]: LS.parse_quote(q, ladder_info) for q in quotes}
    by_sym["000027.SZ"] = replace(
        by_sym["000027.SZ"], five_min=LS.FiveMinSummary(2.1, True, 0.20, 14.50, True, 8, 14.30, True)
    )
    by_sym["600031.SH"] = replace(
        by_sym["600031.SH"], five_min=LS.FiveMinSummary(7.0, True, 0.08, 5.82, False, 9, 5.09, True)
    )
    by_sym["000032.SZ"] = replace(
        by_sym["000032.SZ"], five_min=LS.FiveMinSummary(4.2, False, 0.15, 10.85, True, 16, 10.27, True)
    )
    by_sym["000035.SZ"] = replace(
        by_sym["000035.SZ"], five_min=LS.FiveMinSummary(1.8, True, 0.18, 4.10, False, 12, 3.96, False)
    )
    by_sym["600033.SH"] = replace(
        by_sym["600033.SH"], five_min=LS.FiveMinSummary(2.5, True, 0.22, 10.20, False, 10, 9.80, False)
    )
    by_sym["600034.SH"] = replace(
        by_sym["600034.SH"], five_min=LS.FiveMinSummary(2.0, True, 0.55, 8.10, False, 10, 7.80, False)
    )

    assert LS.reversal_risk_tag(by_sym["600031.SH"]) == "[⚠️冲板未封回落·禁半山腰追高]"
    assert LS.reversal_risk_tag(by_sym["000032.SZ"]) == "[🚨摸高久盘不封(>75m)·防尾盘做T砸盘]"
    assert LS.tag_theme_front_runner(by_sym["000027.SZ"]) == "[🎯主线低吸首选·贴均线守首K]"
    assert LS._holding_action_tag(by_sym["600033.SH"], []) == "[🛡️连板分歧缩量换手·宜分批锁利留底仓看均线承接]"
    assert LS._holding_action_tag(by_sym["600034.SH"], []) == "[🚨破VWAP且破首K低·果断去弱留强]"
    assert LS._holding_action_tag(by_sym["000035.SZ"], []) == "[🚨破VWAP且破首K低·果断去弱留强]"

    assert LS.parse_holdings_arg("000027:14.00, 600033.SH:9.00, 830799") == {
        "000027.SZ": 14.0,
        "600033.SH": 9.0,
        "830799.BJ": 0.0,
    }
    holdings = LS.parse_holdings_arg("000027:14.00,600033:9.00,000035.SZ:4.10")
    LS.print_sections(list(by_sym.values()), keyword=None, now_hhmm="14:10", holdings=holdings)
    out = capsys.readouterr().out
    assert "[🛡️主线共振(『华星』字辈(2板))+守首K低·格局防卖飞]" in out
    assert "[🛡️连板分歧缩量换手·宜分批锁利留底仓看均线承接]" in out
    assert "[🚨破VWAP且破首K低·果断去弱留强]" in out


def test_industry_theme_clustering_and_sub_35pct_holding_resonance(capsys):
    ind_map = {
        "600041.SH": "汽车配件",
        "000042.SZ": "机械基件",
        "600043.SH": "专用机械",
        "600044.SH": "互联网",
        "600045.SH": "影视音像",
        "600046.SH": "影视音像",
        "600047.SH": "家用电器",
        "600048.SH": "家用电器",
    }
    quotes = [
        _raw_quote("600041.SH", "样本动力", 10.00, 10.20, 11.00, 10.15, 11.00, 10.75, 5.2),
        _raw_quote("000042.SZ", "样本重工", 8.00, 8.10, 8.80, 8.05, 8.80, 8.55, 6.1),
        _raw_quote("600043.SH", "样本装备", 11.01, 11.10, 12.11, 11.05, 11.62, 11.60, 4.9),
        _raw_quote("600044.SH", "传媒一号", 20.00, 20.50, 22.00, 20.40, 22.00, 21.50, 19.9),
        _raw_quote("600045.SH", "影视二号", 2.59, 2.65, 2.85, 2.62, 2.85, 2.78, 9.5),
        _raw_quote("600046.SH", "文化三号", 5.09, 5.04, 5.41, 5.00, 5.24, 5.19, 1.3),
        _raw_quote("600047.SH", "家电一号", 5.32, 5.08, 5.85, 5.08, 5.85, 5.51, 8.1),
        _raw_quote("600048.SH", "家电二号", 12.00, 12.10, 13.20, 12.05, 13.20, 12.85, 3.4),
    ]
    parsed = [m for q in quotes if (m := LS.parse_quote(q, None, ind_map)) is not None]
    parsed = [
        replace(m, five_min=LS.FiveMinSummary(7.3, True, 0.12, 5.41, False, 20, 5.00, True))
        if m.symbol in {"600046.SH", "600043.SH"}
        else m
        for m in parsed
    ]
    themes = {t["theme"]: t for t in LS.detect_fermenting_themes(parsed)}
    assert themes["机器人/汽配智驾"]["lu_count"] == 2
    assert [m.symbol for m in themes["机器人/汽配智驾"]["active_front"]] == ["600043.SH"]
    assert themes["影视/短剧传媒"]["lu_count"] == 2
    assert themes["行业:家用电器"]["lu_count"] == 2

    LS.print_holdings_section(parsed, LS.parse_holdings_arg("600046.SH:5.00"))
    assert "[🛡️主线共振(影视/短剧传媒(2板))+守首K低·格局防卖飞]" in capsys.readouterr().out


def test_dragon_companion_radar_reg_risk_max_price_and_late_spike_fade(capsys):
    ladder_info = {"000051.SZ": {"prev_boards": 6, "m_days_n_boards": ""}}
    ind_map = {"000051.SZ": "建筑材料", "000052.SZ": "铝", "000055.SZ": "元器件"}
    quotes = [
        _raw_quote("000051.SZ", "铝材龙头", 5.51, 5.77, 6.06, 5.51, 6.06, 5.74, 19.22),
        _raw_quote("000052.SZ", "铝业小弟", 3.64, 3.66, 3.91, 3.61, 3.68, 3.65, 1.10),
        _raw_quote("600053.SH", "样本高价", 19.90, 19.30, 21.60, 19.15, 21.50, 20.69, 8.70),
        _raw_quote("000054.SZ", "样本低价", 5.87, 5.81, 6.25, 5.67, 6.21, 6.05, 9.21),
        _raw_quote("000055.SZ", "样本脉冲", 6.76, 6.84, 7.11, 6.80, 7.09, 6.97, 1.66),
    ]
    by_sym = {q["symbol"]: LS.parse_quote(q, ladder_info, ind_map) for q in quotes}
    assert by_sym["000051.SZ"].reg_risk_tag == "⚡高标重度异动监管"
    by_sym["000052.SZ"] = replace(
        by_sym["000052.SZ"], five_min=LS.FiveMinSummary(7.4, True, 0.09, 3.91, False, 20, 3.61, True)
    )

    spike_bars = [{"date": "2026-03-10", "open": 6.84, "high": 6.89, "low": 6.80, "close": 6.86, "amount": 300.0}]
    spike_bars += [{"date": "2026-03-10", "open": 6.86, "high": 6.88, "low": 6.84, "close": 6.87, "amount": 80.0}] * 22
    spike_bars += [
        {"date": "2026-03-10", "open": 6.87, "high": 7.05, "low": 6.87, "close": 7.03, "amount": 2040.0},
        {"date": "2026-03-10", "open": 7.03, "high": 7.11, "low": 7.01, "close": 7.06, "amount": 2110.0},
        {"date": "2026-03-10", "open": 7.06, "high": 7.10, "low": 7.02, "close": 7.09, "amount": 480.0},
    ]
    spike_5m = LS.analyze_5m_bars(pd.DataFrame(spike_bars), prev_close=6.76)
    assert spike_5m is not None and spike_5m.late_spike_fade is True
    by_sym["000055.SZ"] = replace(by_sym["000055.SZ"], five_min=spike_5m)
    assert LS.reversal_risk_tag(by_sym["000055.SZ"]) == "[⚠️午后量化脉冲衰竭·防冲高回落]"

    items = list(by_sym.values())
    pairs = LS.find_dragon_companions(items, max_price=10.0)
    assert len(pairs) == 1 and [c.symbol for c in pairs[0][1]] == ["000052.SZ"]
    assert [m.symbol for m in LS.classify_ladder_buckets(items, max_price=10.0)["deep_reversals"]] == ["000054.SZ"]
    LS.print_dragon_companions(items, max_price=10.0)
    assert "[🎯伴生铝材·首K试盘锁仓潜伏(0异动/<=10元)]" in capsys.readouterr().out


def test_realtime_concept_drilldown_and_maturity_tags(monkeypatch, capsys):
    fake_meta = {
        "光伏概念": {"order": 6, "hot_tag": "10天9次上榜", "high": "4天4板", "up": 78, "source": "同花顺热榜"},
        "AI眼镜": {"order": 8, "hot_tag": "首次上榜", "high": "2天2板", "up": 41, "source": "同花顺热榜"},
    }
    fake_stk = {
        "600061": ("家电行业", ("光伏概念", "储能")),
        "600062": ("光伏设备", ("光伏概念",)),
        "000063": ("电子元件", ("光伏概念", "机器人概念")),
        "000064": ("消费电子", ("AI眼镜",)),
        "000065": ("消费电子", ("AI眼镜",)),
    }
    fake_reasons = {"600061": "热泵+储能", "600062": "光伏玻璃+产能出清", "000064": "AI眼镜+智能声学"}
    monkeypatch.setattr(LS, "fetch_realtime_concept_bundle", lambda _syms: (fake_meta, fake_stk, fake_reasons))

    quotes = [
        _raw_quote("600061.SH", "样本光一", 10.88, 11.97, 11.97, 11.97, 11.97, 11.97, 0.62),
        _raw_quote("600062.SH", "样本光二", 6.92, 6.99, 7.61, 6.99, 7.61, 7.51, 0.41),
        _raw_quote("000063.SZ", "样本光三", 9.89, 9.90, 10.45, 9.90, 10.35, 10.25, 2.78),
        _raw_quote("000064.SZ", "样本镜一", 13.02, 14.32, 14.32, 14.32, 14.32, 14.32, 0.33),
        _raw_quote("000065.SZ", "样本镜二", 10.00, 10.10, 11.00, 10.05, 11.00, 10.70, 3.10),
    ]
    enriched = LS.enrich_with_realtime_concepts([m for q in quotes if (m := LS.parse_quote(q)) is not None])
    enriched = [
        replace(m, five_min=LS.FiveMinSummary(4.0, True, 0.14, 10.30, True, 1, 9.90, True))
        if m.symbol == "000063.SZ"
        else m
        for m in enriched
    ]
    LS.print_theme_resonance(enriched, max_price=15.0)
    out = capsys.readouterr().out
    assert "【光伏概念】 [同花顺热榜#6·10天9次上榜 | 最高标:4天4板 | 东财红盘援军:78家]" in out
    assert "【AI眼镜】 [同花顺热榜#8·首次上榜 | 最高标:2天2板 | 东财红盘援军:41家]" in out
    assert "000063.SZ" in out
    assert "[归因:热泵+储能]" in LS.format_row(next(x for x in enriched if x.symbol == "600061.SH"))
    LS._CONCEPT_META_CACHE.clear()


def test_sniper_feishu_quiet_open_low_cap_and_60d_overhead_filter():
    import sniper_feishu as SF

    trap_sent = SF.MarketSentiment(red_count=1700, green_count=3100, sealed_lu_count=35, bull_trap_count=62)
    normal_sent = SF.MarketSentiment(red_count=2900, green_count=1800, sealed_lu_count=65, bull_trap_count=20)
    assert trap_sent.is_trap_wave is True and normal_sent.is_trap_wave is False

    # Stagnant stock (broke_morning_high=False) -> rejected in both trap wave and quiet-open check
    q_stagnant = replace(
        LS.parse_quote(_raw_quote("000071.SZ", "样本横盘", 8.97, 9.05, 9.42, 9.04, 9.26, 9.25, 0.85)),
        concepts=("机器人概念",),
        five_min=LS.FiveMinSummary(1.4, True, 0.62, 9.42, False, 7, 9.04, True),
    )
    # Overhead trap stock (recent -9.9% limit-down) -> rejected by 60d daily check
    q_overhead = replace(
        LS.parse_quote(_raw_quote("600072.SH", "样本套牢", 5.54, 5.59, 5.79, 5.58, 5.78, 5.68, 2.10)),
        concepts=("机器人概念",),
        five_min=LS.FiveMinSummary(3.6, True, 0.11, 5.75, True, 0, 5.58, True),
    )
    # Clean box breakout with strong open + shrunk vol -> selected even during trap wave (>=1.5亿)
    q_clean = replace(
        LS.parse_quote(_raw_quote("000073.SZ", "样本突破", 9.89, 9.90, 10.48, 9.90, 10.45, 10.32, 3.20)),
        concepts=("机器人概念", "光伏概念"),
        five_min=LS.FiveMinSummary(4.0, True, 0.14, 10.35, True, 0, 9.90, True),
    )
    # Quiet-open small-cap (<=10元, 0.72亿 >= 0.6亿, surge 1.2% < 3.0%, min_vol_ratio 0.68 > 0.25, broke_morning_high)
    q_quiet_break = replace(
        LS.parse_quote(_raw_quote("600074.SH", "样本平起", 6.00, 6.02, 6.22, 5.99, 6.21, 6.10, 0.72)),
        concepts=("机器人概念",),
        five_min=LS.FiveMinSummary(1.2, True, 0.68, 6.08, True, 0, 5.99, True),
    )

    themes = [{"theme": "机器人概念", "lu_count": 10}, {"theme": "光伏概念", "lu_count": 6}]
    overhead = {
        "600072.SH": [{"trade_date": "20260305", "high": 6.20, "low": 4.99, "pct_chg": -9.9}],
        "000073.SZ": [{"trade_date": "20260305", "high": 10.04, "low": 9.70, "pct_chg": 0.5}],
        "600074.SH": [{"trade_date": "20260305", "high": 6.15, "low": 5.90, "pct_chg": 0.8}],
    }
    cands = [q_stagnant, q_overhead, q_clean, q_quiet_break]
    picked_trap = SF.select_sniper_targets(cands, themes, trap_sent, "10:15", overhead, 15.0, LS.reversal_risk_tag)
    assert [c.symbol for c in picked_trap] == ["000073.SZ"]
    assert picked_trap[0].daily_tag == "[🚀突破60日箱体新高·上方零套牢]"

    picked_norm = SF.select_sniper_targets(cands, themes, normal_sent, "10:15", overhead, 15.0, LS.reversal_risk_tag)
    assert [c.symbol for c in picked_norm] == ["000073.SZ", "600074.SH"]
    title, md = SF.build_sniper_card_markdown(picked_norm, normal_sent, "2026-03-10 10:15:00")
    assert "样本突破(000073)" in title and "目标价:" in title and "突破60日箱体新高" in md


def test_monster_gene_filters_dead_elephants_and_boosts_n_wave_stocks():
    import sniper_feishu as SF

    gene_map = LS.compute_monster_gene_from_rows(
        {
            "000081.SZ": [("20260301", 6.0, 6.1)],
            "000082.SZ": [("20260201", 8.0, 8.8), ("20260309", 9.0, 9.9)],
            "600083.SH": [("20260201", 7.0, 7.7)] + [(f"202602{10 + i:02d}", 7.7, 7.7) for i in range(11)],
        },
        {"000081.SZ": (310.0, 1.1), "000082.SZ": (48.0, 12.5), "600083.SH": (55.0, 6.2)},
    )
    fm = LS.FiveMinSummary(3.5, True, 0.12, 6.95, True, 0, 6.60, True)
    q_elephant = replace(
        LS.parse_quote(
            _raw_quote("000081.SZ", "样本大象", 6.50, 6.55, 6.82, 6.54, 6.80, 6.72, 4.50), gene_map=gene_map
        ),
        concepts=("算力概念",),
        five_min=fm,
    )
    q_n_wave = replace(
        LS.parse_quote(
            _raw_quote("000082.SZ", "样本妖苗", 9.90, 9.95, 10.38, 9.92, 10.35, 10.22, 1.60), gene_map=gene_map
        ),
        concepts=("算力概念",),
        five_min=fm,
    )
    q_60d_one = replace(
        LS.parse_quote(
            _raw_quote("600083.SH", "样本活股", 7.80, 7.85, 8.18, 7.84, 8.15, 8.05, 3.00), gene_map=gene_map
        ),
        concepts=("算力概念",),
        five_min=fm,
    )
    assert q_elephant.has_monster_gene is False and "缺妖气" in LS.tag_theme_front_runner(q_elephant)
    assert q_n_wave.monster_tier == 3 and "妖股苗子" in LS.tag_theme_front_runner(q_n_wave)
    assert q_60d_one.monster_tier == 1

    sent = SF.MarketSentiment(2800, 1800, 60, 20)
    themes = [{"theme": "算力概念", "lu_count": 5}]
    overhead = {"000082.SZ": [{"trade_date": "20260309", "high": 10.50, "low": 9.0, "pct_chg": 10.0}]}
    picked = SF.select_sniper_targets([q_elephant, q_60d_one, q_n_wave], themes, sent, "09:45", overhead, 15.0)
    assert [c.symbol for c in picked] == ["000082.SZ", "600083.SH"]
    assert picked[0].daily_tag == "[🔥N字首板揉搓·二波起跳区]"
