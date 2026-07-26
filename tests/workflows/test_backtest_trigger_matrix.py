"""Tests for the parameter-sweep matrix: threshold walk-forward and top_n selection lift."""

from __future__ import annotations

import json
from datetime import date

import pytest

from workflows.backtest_trigger_matrix import (
    MIN_TRIGGER_TRADES,
    build_matrix_row,
    build_selection_lift,
    build_trigger_report,
    build_trigger_walk_forward,
    focus_trigger_stats,
    load_trigger_matrix_rows,
    parse_trigger_grid,
    render_trigger_report,
    select_trigger_value,
    write_matrix_artifacts,
)


class TestParseTriggerGrid:
    def test_empty_means_disabled(self):
        assert parse_trigger_grid("") is None
        assert parse_trigger_grid("   ") is None

    def test_parses_param_and_values(self):
        grid = parse_trigger_grid("spring_vol_ratio=1.1,1.3,1.8")
        assert grid is not None
        assert grid.param == "spring_vol_ratio"
        assert grid.values == (1.1, 1.3, 1.8)

    def test_focus_trigger_is_param_prefix(self):
        assert parse_trigger_grid("spring_vol_ratio=1.3").focus_trigger == "spring"
        assert parse_trigger_grid("lps_vol_dry_ratio=0.8").focus_trigger == "lps"

    @pytest.mark.parametrize("raw", ["spring_vol_ratio", "spring_vol_ratio=", "=1.3", "a=1,b=2", "spring_vol_ratio=0"])
    def test_rejects_malformed_spec(self, raw):
        with pytest.raises(ValueError):
            parse_trigger_grid(raw)

    def test_rejects_unknown_funnel_config_field(self):
        with pytest.raises(ValueError, match="未知 FunnelConfig 字段"):
            parse_trigger_grid("spring_vol_rateo=1.3")

    def test_top_n_is_a_selection_sweep_without_focus_trigger(self):
        grid = parse_trigger_grid("top_n=0,1")
        assert grid.values == (0.0, 1.0)
        assert grid.focus_trigger == ""

    @pytest.mark.parametrize("raw", ["top_n=-1", "top_n=1.5"])
    def test_rejects_non_integer_top_n(self, raw):
        with pytest.raises(ValueError, match="top_n"):
            parse_trigger_grid(raw)


class TestFocusTriggerStats:
    def test_merges_confirmation_state_suffixes_by_trade_weight(self):
        summary = {
            "stratified": {
                "by_trigger": {
                    "spring(确认)": {"trades": 30, "avg_ret_pct": 2.0, "win_rate_pct": 60.0},
                    "spring(起跳板)": {"trades": 10, "avg_ret_pct": -2.0, "win_rate_pct": 40.0},
                    "sos": {"trades": 100, "avg_ret_pct": 9.0, "win_rate_pct": 90.0},
                }
            }
        }
        stats = focus_trigger_stats(summary, "spring")
        assert stats["trigger_trades"] == 40
        assert stats["trigger_avg_ret_pct"] == pytest.approx(1.0)
        assert stats["trigger_win_rate_pct"] == pytest.approx(55.0)

    def test_absent_trigger_yields_zero_sample(self):
        stats = focus_trigger_stats({"stratified": {"by_trigger": {"sos": {"trades": 5}}}}, "spring")
        assert stats == {"trigger_trades": 0, "trigger_avg_ret_pct": None, "trigger_win_rate_pct": None}


def _row(value: float, period: str, trades: int, avg: float | None, end: str = "2023-12-29") -> dict:
    return {
        "param": "spring_vol_ratio",
        "focus_trigger": "spring",
        "value": value,
        "period_key": period,
        "end": end,
        "trigger_trades": trades,
        "trigger_avg_ret_pct": avg,
    }


class TestSelectTriggerValue:
    def test_picks_highest_avg_return(self):
        rows = [_row(1.1, "p1", 50, 1.0), _row(1.5, "p1", 40, 3.0), _row(1.8, "p1", 30, 2.0)]
        assert select_trigger_value(rows)["value"] == 1.5

    def test_rejects_value_whose_trade_count_collapses(self):
        rows = [_row(1.1, "p1", 100, 1.0), _row(2.5, "p1", 25, 9.0)]
        assert select_trigger_value(rows)["value"] == 1.1

    def test_ignores_samples_below_min_trades(self):
        rows = [_row(1.1, "p1", MIN_TRIGGER_TRADES - 1, 99.0), _row(1.5, "p1", MIN_TRIGGER_TRADES, 1.0)]
        assert select_trigger_value(rows)["value"] == 1.5

    def test_returns_none_when_no_sample_qualifies(self):
        assert select_trigger_value([_row(1.1, "p1", 3, 5.0), _row(1.5, "p1", 40, None)]) is None


class TestWalkForward:
    def test_selects_on_train_and_scores_on_next_period(self):
        rows = [
            _row(1.1, "p1", 60, 1.0, end="2022-10-31"),
            _row(1.5, "p1", 50, 4.0, end="2022-10-31"),
            _row(1.1, "p2", 60, 1.0, end="2023-12-29"),
            _row(1.5, "p2", 55, 3.0, end="2023-12-29"),
        ]
        report = build_trigger_walk_forward(rows, param="spring_vol_ratio", focus_trigger="spring")
        assert report["window_count"] == 1
        window = report["windows"][0]
        assert (window["train_period"], window["test_period"]) == ("p1", "p2")
        assert window["selected_value"] == 1.5
        assert window["test_avg_ret_pct"] == pytest.approx(3.0)
        assert window["test_beats_baseline"] is True

    def test_marks_failure_when_train_choice_loses_out_of_sample(self):
        rows = [
            _row(1.1, "p1", 60, 1.0, end="2021-02-18"),
            _row(1.8, "p1", 45, 5.0, end="2021-02-18"),
            _row(1.1, "p2", 60, 6.0, end="2022-10-31"),
            _row(1.8, "p2", 45, 1.0, end="2022-10-31"),
            _row(1.1, "p3", 60, 6.0, end="2023-12-29"),
            _row(1.8, "p3", 45, 1.0, end="2023-12-29"),
        ]
        report = build_trigger_walk_forward(rows, param="spring_vol_ratio", focus_trigger="spring")
        assert report["status"] == "fail"
        assert report["improved_window_count"] < report["window_count"]

    def test_single_window_stays_in_review(self):
        rows = [_row(1.1, "p1", 60, 1.0, end="2022-10-31"), _row(1.1, "p2", 60, 2.0, end="2023-12-29")]
        assert build_trigger_walk_forward(rows, param="spring_vol_ratio", focus_trigger="spring")["status"] == "review"

    def test_periods_are_ordered_by_end_date_not_insertion(self):
        rows = [
            _row(1.1, "late", 60, 1.0, end="2024-12-31"),
            _row(1.5, "late", 60, 2.0, end="2024-12-31"),
            _row(1.1, "early", 60, 3.0, end="2020-12-31"),
            _row(1.5, "early", 60, 1.0, end="2020-12-31"),
        ]
        window = build_trigger_walk_forward(rows, param="spring_vol_ratio", focus_trigger="spring")["windows"][0]
        assert (window["train_period"], window["test_period"]) == ("early", "late")


def _selection_row(value: float, period: str, trades: int, avg: float | None, end: str = "2023-12-29") -> dict:
    return {
        "param": "top_n",
        "focus_trigger": "",
        "value": value,
        "period_key": period,
        "end": end,
        "total_trades": trades,
        "overall_avg_ret_pct": avg,
    }


class TestSelectionLift:
    def test_measures_each_period_against_the_unfiltered_pool(self):
        rows = [
            _selection_row(0, "p1", 900, -1.0, end="2022-10-31"),
            _selection_row(1, "p1", 120, 0.8, end="2022-10-31"),
            _selection_row(0, "p2", 800, -0.5, end="2023-12-29"),
            _selection_row(1, "p2", 110, 1.5, end="2023-12-29"),
        ]
        report = build_selection_lift(rows, param="top_n")
        assert report["status"] == "pass"
        assert report["baseline_value"] == 0
        assert [item["lift_pct"] for item in report["comparisons"]] == [pytest.approx(1.8), pytest.approx(2.0)]

    def test_any_period_without_lift_fails(self):
        rows = [
            _selection_row(0, "p1", 900, -1.0, end="2022-10-31"),
            _selection_row(1, "p1", 120, 0.8, end="2022-10-31"),
            _selection_row(0, "p2", 800, -0.5, end="2023-12-29"),
            _selection_row(1, "p2", 110, -2.0, end="2023-12-29"),
        ]
        assert build_selection_lift(rows, param="top_n")["status"] == "fail"

    def test_period_missing_its_baseline_is_skipped(self):
        rows = [
            _selection_row(0, "p1", 900, -1.0, end="2022-10-31"),
            _selection_row(1, "p1", 120, 0.8, end="2022-10-31"),
            _selection_row(1, "p2", 110, 1.5, end="2023-12-29"),
        ]
        report = build_selection_lift(rows, param="top_n")
        assert [item["period_key"] for item in report["comparisons"]] == ["p1"]
        assert report["status"] == "review"

    def test_report_routes_top_n_to_lift_and_renders_it(self):
        rows = [
            _selection_row(0, "p1", 900, -1.0, end="2022-10-31"),
            _selection_row(1, "p1", 120, 0.8, end="2022-10-31"),
            _selection_row(0, "p2", 800, -0.5, end="2023-12-29"),
            _selection_row(1, "p2", 110, 1.5, end="2023-12-29"),
        ]
        report = build_trigger_report(rows)
        assert report["status"] == "pass"
        assert "windows" not in report["params"][0]
        assert "选择层增益" in render_trigger_report(report)


class TestMatrixArtifacts:
    def test_row_carries_focus_trigger_stats_and_period(self):
        summary = {
            "stratified": {"by_trigger": {"spring(确认)": {"trades": 22, "avg_ret_pct": 1.5, "win_rate_pct": 50.0}}},
            "trades": 200,
            "cash_portfolio_total_return_pct": -1.2,
        }
        row = build_matrix_row(
            grid=parse_trigger_grid("spring_vol_ratio=1.3"),
            value=1.3,
            period_key="bear_2022",
            start_dt=date(2021, 12, 13),
            end_dt=date(2022, 10, 31),
            summary=summary,
        )
        assert row["period_key"] == "bear_2022"
        assert row["end"] == "2022-10-31"
        assert row["trigger_trades"] == 22
        assert row["total_trades"] == 200

    def test_round_trip_through_artifacts_dir(self, tmp_path):
        grid = parse_trigger_grid("spring_vol_ratio=1.1,1.5")
        period_a = tmp_path / "bear_2022"
        period_b = tmp_path / "sideways_2023"
        period_a.mkdir()
        period_b.mkdir()
        write_matrix_artifacts(period_a, grid, [_row(1.1, "bear_2022", 60, 1.0, end="2022-10-31")])
        write_matrix_artifacts(period_b, grid, [_row(1.1, "sideways_2023", 60, 2.0, end="2023-12-29")])

        rows = load_trigger_matrix_rows(tmp_path)
        assert len(rows) == 2
        assert {row["period_key"] for row in rows} == {"bear_2022", "sideways_2023"}

    def test_fanned_out_single_value_artifacts_merge_into_one_period(self, tmp_path):
        """CI 按 周期×取值 扇出，同周期的各取值写在不同目录下的同名文件里。"""
        for value in (1.1, 1.5, 1.8):
            target = tmp_path / f"backtest-trigger-bear_2022-{value:g}"
            target.mkdir()
            write_matrix_artifacts(
                target,
                parse_trigger_grid(f"spring_vol_ratio={value:g}"),
                [_row(value, "bear_2022", 60, 1.0, end="2022-10-31")],
            )
        rows = load_trigger_matrix_rows(tmp_path)
        assert sorted(row["value"] for row in rows) == [1.1, 1.5, 1.8]
        assert {row["period_key"] for row in rows} == {"bear_2022"}

    def test_duplicate_period_value_pairs_are_deduplicated(self, tmp_path):
        grid = parse_trigger_grid("spring_vol_ratio=1.1")
        for name in ("run_a", "run_b"):
            target = tmp_path / name
            target.mkdir()
            write_matrix_artifacts(target, grid, [_row(1.1, "bear_2022", 60, 1.0, end="2022-10-31")])
        assert len(load_trigger_matrix_rows(tmp_path)) == 1

    def test_markdown_and_json_are_written(self, tmp_path):
        grid = parse_trigger_grid("spring_vol_ratio=1.1")
        path = write_matrix_artifacts(tmp_path, grid, [_row(1.1, "bear_2022", 60, 1.0)])
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["param"] == "spring_vol_ratio"
        assert payload["focus_trigger"] == "spring"
        assert "spring_vol_ratio" in (tmp_path / "trigger_matrix_spring_vol_ratio.md").read_text(encoding="utf-8")


class TestTriggerReport:
    def test_report_groups_by_param(self):
        rows = [
            _row(1.1, "p1", 60, 1.0, end="2022-10-31"),
            _row(1.5, "p1", 60, 4.0, end="2022-10-31"),
            _row(1.1, "p2", 60, 1.0, end="2023-12-29"),
            _row(1.5, "p2", 60, 3.0, end="2023-12-29"),
        ]
        report = build_trigger_report(rows)
        assert [item["param"] for item in report["params"]] == ["spring_vol_ratio"]
        assert report["params"][0]["recommended_value"] == 1.5

    def test_empty_rows_stay_in_review(self):
        assert build_trigger_report([])["status"] == "review"
