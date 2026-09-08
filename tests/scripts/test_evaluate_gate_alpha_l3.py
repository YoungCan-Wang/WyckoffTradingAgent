"""L3 硬过滤那一节的取数与配对。

这一节量的是**生产那道闸**：两侧成员直接读 trace 的 ``l3_eligible``，不像题材层那样用
「行业动量 topN」当代理。所以它的正确性全押在两件事上——同日多份 trace 谁说话（
``latest_trace_per_date``），以及两侧成员是不是站在同一把尺子上（流动性池 ∩ 有动量）。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pandas as pd

from core.funnel_effect_panels import build_panels, normalize_market_frame
from core.gate_alpha_eval import GateReport
from scripts.evaluate_gate_alpha import attach_l3_gate, build_l3_days, load_trace_payloads


def _write_trace(root: Path, ds: str, l2: list[str], l3: list[str], *, generated_at: str = "") -> None:
    payload = {
        "trade_date": ds,
        "generated_at": generated_at or f"{ds}T09:00:00",
        "market_context": {"regime": "RISK_OFF"},
        "symbols": {code: {"l2_eligible": True, "l3_eligible": code in set(l3)} for code in sorted(set(l2))},
    }
    root.mkdir(parents=True, exist_ok=True)
    stamp = (generated_at or ds).replace(":", "").replace("-", "")
    with gzip.open(root / f"review_trace_{ds}_{stamp}.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _market(codes: list[str], days: int = 40, *, amount: float = 5e8) -> pd.DataFrame:
    """造一段够长的行情：``build_panels`` 的 20 日动量与 20 日均额都要预热。"""
    rows = []
    for code in codes:
        for i in range(days):
            rows.append(
                {
                    "ts_code": f"{code}.SZ",
                    "trade_date": (pd.Timestamp("2026-07-01") + pd.Timedelta(days=i)).strftime("%Y%m%d"),
                    "open": 10.0 + i * 0.1,
                    "close": 10.0 + i * 0.1,
                    "amount": amount,
                }
            )
    return pd.DataFrame(rows)


def _panels(codes: list[str], days: int = 40, *, amount: float = 5e8):
    return build_panels(normalize_market_frame(_market(codes, days, amount=amount)))


class TestLoadTracePayloads:
    def test_reads_recursively(self, tmp_path: Path):
        """工作流按天各解一个子目录,平铺会因同名覆盖丢掉重跑那份。"""
        _write_trace(tmp_path / "day-a", "2026-08-11", ["000001", "000002"], ["000001"])
        _write_trace(tmp_path / "nested" / "day-b", "2026-08-12", ["000001", "000002"], ["000001"])
        assert len(load_trace_payloads(str(tmp_path))) == 2

    def test_keeps_both_copies_of_same_date(self, tmp_path: Path):
        """同日两份都要读进来,由 latest_trace_per_date 定夺——这里不能提前挑。"""
        codes = ["000001", "000002"]
        _write_trace(tmp_path / "a", "2026-08-24", codes, ["000001"], generated_at="2026-08-24T12:44:00")
        _write_trace(tmp_path / "b", "2026-08-24", codes, ["000002"], generated_at="2026-08-24T23:24:00")
        assert len(load_trace_payloads(str(tmp_path))) == 2

    def test_missing_dir_returns_empty_not_raise(self, tmp_path: Path):
        assert load_trace_payloads(str(tmp_path / "nope")) == []

    def test_corrupt_file_is_skipped(self, tmp_path: Path):
        _write_trace(tmp_path, "2026-08-11", ["000001", "000002"], ["000001"])
        (tmp_path / "review_trace_bad.json.gz").write_bytes(b"not gzip")
        assert len(load_trace_payloads(str(tmp_path))) == 1

    def test_payload_without_symbols_is_rejected(self, tmp_path: Path):
        with gzip.open(tmp_path / "review_trace_x.json.gz", "wt", encoding="utf-8") as handle:
            json.dump({"trade_date": "2026-08-11"}, handle)
        assert load_trace_payloads(str(tmp_path)) == []


class TestAttachL3Gate:
    """降级路径要能被读者看见。空池/缺列会让每天都被跳过,最终判定读成「尚未可知」——
    与「数据确实不够」同形,这是已咬过的那类静默早返回。"""

    def _traces(self, root: Path, codes: list[str], kept: list[str]) -> str:
        for i in range(6):
            ds = (pd.Timestamp("2026-07-20") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
            _write_trace(root, ds, codes, kept)
        return str(root)

    def test_missing_amount_column_says_so(self, tmp_path: Path):
        codes = [f"{i:06d}" for i in range(1, 11)]
        report = GateReport()
        attach_l3_gate(report, _market(codes).drop(columns=["amount"]), self._traces(tmp_path, codes, codes[:5]), (5,))
        assert "缺 amount" in report.l3_gate_note
        assert report.l3_gate == []

    def test_empty_liquidity_pool_says_so(self, tmp_path: Path):
        codes = [f"{i:06d}" for i in range(1, 11)]
        report = GateReport()
        attach_l3_gate(report, _market(codes, amount=1e4), self._traces(tmp_path, codes, codes[:5]), (5,))
        assert "流动性池为空" in report.l3_gate_note
        assert report.l3_gate == []

    def test_no_trace_is_not_a_failed_control(self, tmp_path: Path):
        report = GateReport()
        attach_l3_gate(report, _market(["000001"]), str(tmp_path / "empty"), (5,))
        assert "不是没通过" in report.l3_gate_note

    def test_normal_path_emits_one_row_per_horizon(self, tmp_path: Path):
        codes = [f"{i:06d}" for i in range(1, 11)]
        report = GateReport()
        attach_l3_gate(report, _market(codes), self._traces(tmp_path, codes, codes[:5]), (1, 3, 5))
        assert [stat.horizon for stat in report.l3_gate] == [1, 3, 5]
        assert "硬过滤日 6 天" in report.l3_gate_note

    def test_同日重跑取最新那份而不是先读到的那份(self, tmp_path: Path):
        """同一交易日多份 trace,配对数必须来自 generated_at 最新的那份。

        这不是假想:2026-08-24 真有三份(12:44 / 14:51 / 23:24),L3 分别 732/732/449,
        config_digest 还是同一个。按平铺 `cp -n` 留下 14:51 那份(L3=732),T+5 胜率差读
        +3.844;留下 23:24 那份(L3=449)读 +4.764。同一批证据两个数,差别全在谁说话。
        所以工作流必须按 run 各解一个子目录,由这里按时间定夺。
        """
        codes = [f"{i:06d}" for i in range(1, 21)]
        for i in range(6):
            ds = (pd.Timestamp("2026-07-20") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
            # 先写的那份留 4 只,后写的留 12 只;若取错,配对数会被 4 只那侧卡住。
            _write_trace(tmp_path / "early", ds, codes, codes[:4], generated_at=f"{ds}T12:44:00")
            _write_trace(tmp_path / "late", ds, codes, codes[:12], generated_at=f"{ds}T23:24:00")
        report = GateReport()
        attach_l3_gate(report, _market(codes), str(tmp_path), (1,))
        # 配对数上限 = min(留下, 被拒) = min(12, 8) = 8;取错那份会是 min(4, 16) = 4。
        assert report.l3_gate[0].avg_pairs == 8.0


class TestBuildL3Days:
    def _hard(self, ds: str, l2: list[str], l3: list[str]) -> dict:
        return {ds: {"regime": "RISK_OFF", "l2": sorted(l2), "l3": sorted(l3)}}

    def test_tested_side_is_the_rejected_basket(self):
        """待测=被 L3 拒的票。方向错了整节的符号就反了,而反了不会报错。"""
        codes = [f"{i:06d}" for i in range(1, 11)]
        kept, rejected = codes[:5], codes[5:]
        days = build_l3_days(self._hard("2026-07-25", codes, kept), _panels(codes), 5)
        assert len(days) == 1
        assert {pair[0] for pair in days[0].pairs} <= set(rejected)
        assert {pair[1] for pair in days[0].pairs} <= set(kept)

    def test_skips_day_when_either_side_too_small(self):
        codes = [f"{i:06d}" for i in range(1, 11)]
        assert build_l3_days(self._hard("2026-07-25", codes, codes[:2]), _panels(codes), 5) == []

    def test_skips_day_without_forward_window(self):
        """前瞻窗口落在行情尾部之外的日子必须丢掉,否则收益读的是别的天。"""
        codes = [f"{i:06d}" for i in range(1, 11)]
        panels = _panels(codes)
        last = panels.dates[-1]
        assert build_l3_days(self._hard(last, codes, codes[:5]), panels, 5) == []

    def test_illiquid_codes_are_excluded_from_both_sides(self):
        """两侧都要过流动性池——生产漏斗那把尺子。只筛一侧就是两套标准。"""
        codes = [f"{i:06d}" for i in range(1, 11)]
        # tushare 的 amount 是千元,normalize 除以 10 得万元：1e4 千元 = 1000 万元 < 8000 万门槛。
        panels = _panels(codes, amount=1e4)
        assert build_l3_days(self._hard("2026-07-25", codes, codes[:5]), panels, 5) == []

    def test_pairs_are_capped_by_smaller_basket(self):
        """1:1 无放回,配对数上限是小的那一侧。"""
        codes = [f"{i:06d}" for i in range(1, 21)]
        kept = codes[:4]
        days = build_l3_days(self._hard("2026-07-25", codes, kept), _panels(codes), 5)
        assert len(days[0].pairs) <= len(kept)

    def test_buy_and_sell_dates_span_the_horizon(self):
        """T+1 开盘买 / T+1+H 收盘卖,与 funnel_effect_eval 同口径。"""
        codes = [f"{i:06d}" for i in range(1, 11)]
        panels = _panels(codes)
        days = build_l3_days(self._hard("2026-07-25", codes, codes[:5]), panels, 3)
        buy_index = panels.dates.index(days[0].buy_ds)
        assert panels.dates.index(days[0].sell_ds) - buy_index == 3
        assert buy_index == panels.dates.index("2026-07-25") + 1
