from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from workflows import benchmark_funnel_fetch as bench


def test_build_universe_includes_main_chinext_star_and_bse(monkeypatch) -> None:
    boards = {
        "main": [{"code": "000001"}],
        "chinext": [{"code": "300001"}],
        "star": [{"code": "688001"}],
        "bse": [{"code": "830000"}],
    }
    monkeypatch.setattr(bench, "get_stocks_by_board", lambda board: boards[board])

    assert bench.build_universe(sample=0) == ["000001", "300001", "688001", "830000"]


def test_summarize_fetch_rows_counts_success_alignment_and_sources() -> None:
    summary = bench.summarize_fetch_rows(
        "single",
        ["000001", "000002", "000003"],
        [
            {"ok": True, "latest": "2026-06-22", "source": "tickflow"},
            {"ok": True, "latest": "2026-06-21", "source": ""},
            {"ok": False, "error": "TimeoutError"},
        ],
        elapsed=2.0,
        target_date="2026-06-22",
    )

    assert summary["ok"] == 2
    assert summary["success_pct"] == 66.67
    assert summary["aligned"] == 1
    assert summary["qps"] == 1.0
    assert summary["sources"] == {"tickflow": 1, "unknown": 1}
    assert summary["errors"] == {"TimeoutError": 1}


def test_run_benchmark_compare_writes_json_and_forces_runners(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []
    window = SimpleNamespace(start_trade_date=date(2026, 1, 1), end_trade_date=date(2026, 1, 5))
    output = tmp_path / "bench" / "summary.json"

    monkeypatch.setattr(bench, "resolve_end_calendar_day", lambda: date(2026, 1, 5))
    monkeypatch.setattr(bench, "resolve_trading_window", lambda *_args: window)

    def fake_run_path(label, symbols, _window, _config, _log_fn, *, runner_override=""):
        calls.append((label, runner_override))
        return {"path": label, "symbols": len(symbols), "fetch_stats": {}}

    monkeypatch.setattr(bench, "_run_path", fake_run_path)

    result = bench.run_benchmark_funnel_fetch(
        bench.BenchmarkFetchConfig(symbols=("000001", "300001"), path="compare", output=output),
        log_fn=lambda _msg: None,
    )

    assert calls == [("batch", "batch"), ("single", "single")]
    assert result == [
        {"path": "batch", "symbols": 2, "fetch_stats": {}},
        {"path": "single", "symbols": 2, "fetch_stats": {}},
    ]
    assert '"path": "batch"' in output.read_text(encoding="utf-8")


def test_run_benchmark_single_path_uses_matching_runner(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    window = SimpleNamespace(start_trade_date=date(2026, 1, 1), end_trade_date=date(2026, 1, 5))

    monkeypatch.setattr(bench, "resolve_end_calendar_day", lambda: date(2026, 1, 5))
    monkeypatch.setattr(bench, "resolve_trading_window", lambda *_args: window)
    monkeypatch.setattr(
        bench,
        "_run_path",
        lambda label, *_args, runner_override="": calls.append((label, runner_override)) or {"path": label},
    )

    result = bench.run_benchmark_funnel_fetch(
        bench.BenchmarkFetchConfig(symbols=("000001",), path="single", runner="batch"),
        log_fn=lambda _msg: None,
    )

    assert calls == [("single", "single")]
    assert result == [{"path": "single"}]
