from __future__ import annotations


def _write_grid_cell(tmp_path, period, start, end, hold, stop, cash_return):
    artifact = tmp_path / f"backtest-grid-{period}-h{hold}-sl-{stop}-tp0-tr0-37"
    artifact.mkdir()
    (artifact / f"summary_{period}_h{hold}.md").write_text(
        "\n".join(
            [
                f"- 区间: {start} ~ {end}",
                "- 每日候选上限: Top 4",
                "- 股票池: main_chinext (sample=0)",
                "- 绩效引擎: legacy",
                "- 策略治理调权: lps[regime=RISK_ON]×0.50↓（远端, 报告=2026-07-04, 周期=h5, 策略=shadow 对照(shadow), 范围=尾盘+漏斗shadow）",
                "- 成交样本: 10",
                "- 胜率: 40.0%",
                "- 平均收益: 1.0%",
                "- 中位收益: 0.5%",
                "- 夏普比 (Sharpe Ratio): 0.3",
                "- 最大回撤: -10.0%",
                "- 初始现金: 100000.00",
                f"- 最终现金: {100000 * (1 + cash_return / 100):.2f}",
                f"- 总收益: {cash_return}%",
                "- 成交笔数: 4",
            ]
        ),
        encoding="utf-8",
    )


def test_market_report_includes_cash_account_metrics(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    artifact = tmp_path / "backtest-grid-h5-sl-6-tp0-tr0-37"
    artifact.mkdir()
    (artifact / "summary_20211213_20221031_h5_n4.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "",
                "- 区间: 2021-12-13 ~ 2022-10-31",
                "- 持有周期: 5 交易日",
                "- 每日候选上限: Top 4",
                "- 策略治理调权: lps[regime=RISK_ON]×0.50↓（远端, 报告=2026-07-04, 周期=h5, 策略=正式调权(on), 范围=尾盘+正式漏斗）",
                "- 股票池: main_chinext (sample=0)",
                "- 绩效引擎: auto（wbt 可用）",
                "- 成交样本: 249",
                "",
                "## 收益统计",
                "- 胜率: 29.32%",
                "- 平均收益: -1.520%",
                "- 中位收益: -5.984%",
                "",
                "## 组合风险指标（单利口径 · 基于每日净值曲线）",
                "- 夏普比 (Sharpe Ratio): -1.040",
                "- 卡玛比 (Calmar Ratio): -0.563",
                "- 最大回撤: -66.98%",
                "- 组合总收益: -32.16%",
                "",
                "## 真实现金账户模拟",
                "- 初始现金: 100000.00",
                "- 最终现金: 53785.51",
                "- 总收益: -46.21%",
                "- 现金最大回撤: -12.34%",
                "- 成交笔数: 151",
                "- 佣金合计: 1011.52",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact / "trades_20211213_20221031_h5_n4.csv").write_text(
        "signal_date,ret_pct,regime,trigger\n2022-01-01,-1.2,NEUTRAL,lps\n",
        encoding="utf-8",
    )

    cells = load_grid_cells(tmp_path)
    assert cells[0].cash_initial == 100000.0
    assert cells[0].cash_final == 53785.51
    assert cells[0].strategy_policy.startswith("lps[regime=RISK_ON]×0.50↓")

    report = build_report(cells)

    assert "代表现金账户: 初始 **100000.00**；最终 **53785.51**；盈亏 **-46214.49**" in report
    assert (
        "- 策略治理调权: lps[regime=RISK_ON]×0.50↓（远端, 报告=2026-07-04, 周期=h5, 策略=正式调权(on), 范围=尾盘+正式漏斗）"
        in report
    )
    assert "| 排名 | 参数组合 | 夏普 | 胜率 | 均收 | 现金回撤 | 最终现金 | 现金收益 | 样本 |" in report


def test_market_report_groups_multi_period_grid(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    for period, start, end, cash_return, sharpe in [
        ("recent_6m", "2025-12-01", "2026-05-31", 2.5, 0.3),
        ("bull_2020", "2020-07-01", "2021-02-18", 16.9, 0.8),
        ("bear_2022", "2021-12-13", "2022-10-31", 13.6, -1.3),
    ]:
        artifact = tmp_path / f"backtest-grid-{period}-h10-sl-6-tp0-tr0-37"
        artifact.mkdir()
        (artifact / f"summary_{start.replace('-', '')}_{end.replace('-', '')}_h10_n4.md").write_text(
            "\n".join(
                [
                    "# Wyckoff Funnel Daily Backtest",
                    "",
                    f"- 区间: {start} ~ {end}",
                    "- 每日候选上限: Top 4",
                    "- 股票池: main_chinext (sample=0)",
                    "- 绩效引擎: legacy",
                    "- 成交样本: 10",
                    "- 胜率: 40.0%",
                    "- 平均收益: 1.0%",
                    "- 中位收益: 0.5%",
                    "- 夏普比 (Sharpe Ratio): " + str(sharpe),
                    "- 卡玛比 (Calmar Ratio): 0.1",
                    "- 最大回撤: -10.0%",
                    "- 组合总收益: 1.0%",
                    "- 初始现金: 100000.00",
                    "- 最终现金: 110000.00",
                    f"- 总收益: {cash_return}%",
                    "- 成交笔数: 4",
                    "- 佣金合计: 20.00",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    cells = load_grid_cells(tmp_path)
    assert {cell.period_key for cell in cells} == {"recent_6m", "bull_2020", "bear_2022"}

    report = build_report(cells)

    assert "## 各周期最佳" in report
    assert "最近6个月: 2025-12-01 ~ 2026-05-31 (1组)" in report
    assert "牛市 2020-07~2021-02" in report
    assert "熊市 2021-12~2022-10" in report


def test_market_report_prefers_cross_period_robust_params(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    rows = [
        ("recent_6m", "h10", "sl6", 30.0),
        ("bull_2020", "h10", "sl6", -25.0),
        ("bear_2022", "h10", "sl6", -20.0),
        ("recent_6m", "h15", "sl8", 6.0),
        ("bull_2020", "h15", "sl8", 4.0),
        ("bear_2022", "h15", "sl8", 3.0),
    ]
    ranges = {
        "recent_6m": ("2025-12-01", "2026-05-31"),
        "bull_2020": ("2020-07-01", "2021-02-18"),
        "bear_2022": ("2021-12-13", "2022-10-31"),
    }
    for period, hold, stop, cash_return in rows:
        start, end = ranges[period]
        artifact = tmp_path / f"backtest-grid-{period}-{hold}-{stop}-tp0-tr0-37"
        artifact.mkdir()
        (artifact / f"summary_{period}_{hold}.md").write_text(
            "\n".join(
                [
                    f"- 区间: {start} ~ {end}",
                    "- 每日候选上限: Top 4",
                    "- 股票池: main_chinext (sample=0)",
                    "- 绩效引擎: legacy",
                    "- 成交样本: 10",
                    "- 胜率: 40.0%",
                    "- 平均收益: 1.0%",
                    "- 中位收益: 0.5%",
                    "- 夏普比 (Sharpe Ratio): 0.3",
                    "- 最大回撤: -10.0%",
                    "- 初始现金: 100000.00",
                    f"- 最终现金: {100000 * (1 + cash_return / 100):.2f}",
                    f"- 总收益: {cash_return}%",
                    "- 成交笔数: 4",
                ]
            ),
            encoding="utf-8",
        )

    report = build_report(load_grid_cells(tmp_path))

    assert "交易手册（按市场状态）" in report
    assert "稳健参数（跨周期全正）: **等额四仓 / 15天 / SL-8% / 无TP / 无Trail**" in report
    assert "跨周期参数稳健性" in report


def test_backtest_confirmation_passes_only_cross_period_positive(tmp_path):
    from scripts.update_backtest_market_report import build_confirmation, load_grid_cells

    for period, start, end, cash_return in [
        ("recent_6m", "2025-12-01", "2026-05-31", 6.0),
        ("bull_2020", "2020-07-01", "2021-02-18", 4.0),
        ("bear_2022", "2021-12-13", "2022-10-31", 3.0),
    ]:
        _write_grid_cell(tmp_path, period, start, end, 15, 8, cash_return)

    confirmation = build_confirmation(
        load_grid_cells(tmp_path),
        run_url="https://github.com/example/actions/runs/1",
        generated_at="2026-07-04 00:00:00 Asia/Shanghai",
    )

    assert confirmation["status"] == "pass"
    assert confirmation["report_date"] == "2026-07-04"
    assert confirmation["positive_periods"] == 3
    assert confirmation["min_cash_return"] == 3.0
    assert confirmation["best_param"]["label"] == "等额四仓 / 15天 / SL-8% / 无TP / 无Trail"
    assert confirmation["strategy_policy_ready"] is True
    assert confirmation["strategy_policy"].startswith("lps[regime=RISK_ON]×0.50↓")


def test_backtest_confirmation_requires_strategy_policy_evidence(tmp_path):
    from scripts.update_backtest_market_report import build_confirmation, load_grid_cells

    for period, start, end in [
        ("recent_6m", "2025-12-01", "2026-05-31"),
        ("bull_2020", "2020-07-01", "2021-02-18"),
        ("bear_2022", "2021-12-13", "2022-10-31"),
    ]:
        artifact = tmp_path / f"backtest-grid-{period}-h15-sl-8-tp0-tr0-37"
        artifact.mkdir()
        (artifact / f"summary_{period}_h15.md").write_text(
            "\n".join(
                [
                    f"- 区间: {start} ~ {end}",
                    "- 每日候选上限: Top 4",
                    "- 股票池: main_chinext (sample=0)",
                    "- 绩效引擎: legacy",
                    "- 成交样本: 10",
                    "- 胜率: 40.0%",
                    "- 夏普比 (Sharpe Ratio): 0.3",
                    "- 最大回撤: -10.0%",
                    "- 初始现金: 100000.00",
                    "- 最终现金: 103000.00",
                    "- 总收益: 3.0%",
                    "- 成交笔数: 4",
                ]
            ),
            encoding="utf-8",
        )

    confirmation = build_confirmation(load_grid_cells(tmp_path))

    assert confirmation["status"] == "review"
    assert confirmation["strategy_policy_ready"] is False
    assert confirmation["strategy_policy_reason"] == "缺少策略治理调权记录"
    assert "缺少策略治理口径证据" in confirmation["summary"] or "缺少策略治理调权记录" in confirmation["summary"]


def test_backtest_confirmation_fails_when_any_period_has_no_positive_combo(tmp_path):
    from scripts.update_backtest_market_report import build_confirmation, load_grid_cells

    _write_grid_cell(tmp_path, "recent_6m", "2025-12-01", "2026-05-31", 10, 7, 12.0)
    _write_grid_cell(tmp_path, "bear_2022", "2021-12-13", "2022-10-31", 10, 7, -6.5)

    confirmation = build_confirmation(load_grid_cells(tmp_path))

    assert confirmation["status"] == "fail"
    assert confirmation["weak_periods"] == [
        {"period_key": "bear_2022", "period_label": "熊市 2021-12~2022-10", "best_cash_return": -6.5}
    ]
    assert "不能作为 dynamic=on 晋级依据" in confirmation["summary"]


def test_update_market_report_writes_confirmation_json(tmp_path, monkeypatch):
    import json

    from scripts.update_backtest_market_report import main

    _write_grid_cell(tmp_path, "recent_6m", "2025-12-01", "2026-05-31", 10, 7, 2.0)
    report_path = tmp_path / "report.md"
    confirmation_path = tmp_path / "confirmation.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "update_backtest_market_report.py",
            "--artifacts-dir",
            str(tmp_path),
            "--output",
            str(report_path),
            "--confirmation-output",
            str(confirmation_path),
            "--generated-at",
            "2026-07-04 00:00:00 Asia/Shanghai",
        ],
    )

    assert main() == 0
    confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    assert report_path.exists()
    assert confirmation["source"] == "backtest_grid"
    assert confirmation["status"] == "review"


def test_market_report_flags_period_with_no_positive_cash_combo(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    for period, start, end, cash_return in [
        ("recent_6m", "2025-12-01", "2026-05-31", 12.0),
        ("bear_2022", "2021-12-13", "2022-10-31", -6.5),
    ]:
        artifact = tmp_path / f"backtest-grid-{period}-h10-sl7-tp18-tr0-37"
        artifact.mkdir()
        (artifact / f"summary_{period}_h10.md").write_text(
            "\n".join(
                [
                    f"- 区间: {start} ~ {end}",
                    "- 每日候选上限: Top 4",
                    "- 股票池: main_chinext (sample=0)",
                    "- 绩效引擎: legacy",
                    "- 成交样本: 10",
                    "- 胜率: 40.0%",
                    "- 平均收益: 1.0%",
                    "- 中位收益: 0.5%",
                    "- 夏普比 (Sharpe Ratio): 0.3",
                    "- 最大回撤: -10.0%",
                    "- 初始现金: 100000.00",
                    f"- 最终现金: {100000 * (1 + cash_return / 100):.2f}",
                    f"- 总收益: {cash_return}%",
                    "- 成交笔数: 4",
                ]
            ),
            encoding="utf-8",
        )

    report = build_report(load_grid_cells(tmp_path))

    assert "周期风控: **熊市 2021-12~2022-10** 全部组合非正" in report


def test_market_report_expands_cash_portfolio_styles(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    artifact = tmp_path / "backtest-grid-recent_6m-h10-sl-6-tp0-tr0-37"
    artifact.mkdir()
    (artifact / "summary_20251201_20260531_h10_n4.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "",
                "- 区间: 2025-12-01 ~ 2026-05-31",
                "- 每日候选上限: Top 4",
                "- 股票池: main_chinext (sample=0)",
                "- 绩效引擎: legacy",
                "- 成交样本: 10",
                "- 胜率: 40.0%",
                "- 平均收益: 1.0%",
                "- 中位收益: 0.5%",
                "- 夏普比 (Sharpe Ratio): 0.3",
                "- 卡玛比 (Calmar Ratio): 0.1",
                "- 最大回撤: -10.0%",
                "- 组合总收益: 1.0%",
                "- 初始现金: 100000.00",
                "- 最终现金: 101000.00",
                "- 总收益: 1.0%",
                "- 成交笔数: 4",
                "- 佣金合计: 20.00",
                "",
                "## 交易风格对比",
                "",
                "| 风格ID | 风格 | 最终现金 | 总收益 | 现金回撤 | 成交 | 胜率 | 平均盈利 | 平均亏损 | 加仓 | 换股 | 观察未确认 | 跳过 |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                "| slot_equal_4 | 等额四仓 | 101000.00 | +1.00% | -2.00% | 4 | 50.00% | 3.0% | -1.0% | 0 | 0 | 0 | 1 |",
                "| probe_add | 观察仓补仓 | 112000.00 | +12.00% | -1.00% | 6 | 66.67% | 4.0% | -0.5% | 2 | 0 | 0 | 2 |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact / "trades_20251201_20260531_h10_n4.csv").write_text(
        "signal_date,code,ret_pct,regime,trigger\n2026-01-01,000001,-9.0,RISK_OFF,sos\n",
        encoding="utf-8",
    )
    (artifact / "cash_trades_probe_add_20251201_20260531_h10_n4.csv").write_text(
        "\n".join(
            [
                "signal_date,code,ret_pct,regime,trigger",
                "2026-01-02,000002,5.0,RISK_ON,lps",
                "2026-01-03,000003,7.0,RISK_ON,evr",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cells = load_grid_cells(tmp_path)
    assert [(cell.portfolio_style, cell.cash_total_return) for cell in cells] == [
        ("slot_equal_4", 1.0),
        ("probe_add", 12.0),
    ]
    assert cells[0].trades_path and cells[0].trades_path.name.startswith("trades_")
    assert cells[1].trades_path and cells[1].trades_path.name.startswith("cash_trades_probe_add_")

    report = build_report(cells)

    assert "## 各交易风格最佳" in report
    assert "观察仓补仓 / 10天 / SL-6% / 无TP / 无Trail" in report
    assert "- 交易笔数: 2；盈利 2；亏损 0" in report
    assert "会提交到仓库" not in report
    assert "上传为 artifact" in report


def test_market_report_loads_merged_tp_artifact_layout(tmp_path):
    from scripts.update_backtest_market_report import load_grid_cells

    artifact = tmp_path / "backtest-grid-recent_6m-h10-sl-8-tr0-37"
    for tp, cash_return in [(0, 1.0), (18, 3.0)]:
        cell_dir = artifact / f"backtest-grid-recent_6m-h10-sl8-tp{tp}-tr0"
        cell_dir.mkdir(parents=True)
        (cell_dir / f"summary_20251201_20260531_h10_tp{tp}.md").write_text(
            "\n".join(
                [
                    "# Wyckoff Funnel Daily Backtest",
                    "",
                    "- 区间: 2025-12-01 ~ 2026-05-31",
                    "- 每日候选上限: Top 4",
                    "- 股票池: main_chinext (sample=0)",
                    "- 绩效引擎: legacy",
                    "- 成交样本: 10",
                    "- 胜率: 40.0%",
                    "- 平均收益: 1.0%",
                    "- 中位收益: 0.5%",
                    "- 夏普比 (Sharpe Ratio): 0.3",
                    "- 卡玛比 (Calmar Ratio): 0.1",
                    "- 最大回撤: -10.0%",
                    "- 组合总收益: 1.0%",
                    "- 初始现金: 100000.00",
                    "- 最终现金: 101000.00",
                    f"- 总收益: {cash_return}%",
                    "- 成交笔数: 4",
                    "- 佣金合计: 20.00",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    cells = load_grid_cells(tmp_path)

    assert [(cell.take_profit, cell.cash_total_return) for cell in cells] == [(0, 1.0), (18, 3.0)]
    assert {cell.period_key for cell in cells} == {"recent_6m"}


def test_market_report_labels_zero_stop_loss_as_disabled(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    artifact = tmp_path / "backtest-grid-recent_6m-h5-sl0-tp0-tr0-37"
    artifact.mkdir()
    (artifact / "summary_20251201_20260531_h5_n4.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "",
                "- 区间: 2025-12-01 ~ 2026-05-31",
                "- 每日候选上限: Top 4",
                "- 股票池: all (sample=0)",
                "- 绩效引擎: legacy",
                "- 成交样本: 10",
                "- 胜率: 40.0%",
                "- 平均收益: 1.0%",
                "- 中位收益: 0.5%",
                "- 夏普比 (Sharpe Ratio): 0.3",
                "- 卡玛比 (Calmar Ratio): 0.1",
                "- 最大回撤: -10.0%",
                "- 组合总收益: 1.0%",
                "- 初始现金: 100000.00",
                "- 最终现金: 101000.00",
                "- 总收益: 1.0%",
                "- 成交笔数: 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cells = load_grid_cells(tmp_path)
    report = build_report(cells)

    assert cells[0].stop_loss == 0
    assert "等额四仓 / 5天 / 无SL / 无TP / 无Trail" in report
    assert "SL-0%" not in report


def test_market_report_recognizes_recent_2m_fast_grid(tmp_path):
    from scripts.update_backtest_market_report import build_report, load_grid_cells

    artifact = tmp_path / "backtest-grid-recent_2m-h20-sl8-tp0-tr5"
    artifact.mkdir()
    (artifact / "summary_20260427_20260627_h20_n4.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "",
                "- 区间: 2026-04-27 ~ 2026-06-27",
                "- 每日候选上限: Top 4",
                "- 股票池: all (sample=0)",
                "- 绩效引擎: legacy",
                "- 成交样本: 10",
                "- 胜率: 40.0%",
                "- 平均收益: 1.0%",
                "- 中位收益: 0.5%",
                "- 夏普比 (Sharpe Ratio): 0.3",
                "- 卡玛比 (Calmar Ratio): 0.1",
                "- 最大回撤: -10.0%",
                "- 组合总收益: 1.0%",
                "- 初始现金: 100000.00",
                "- 最终现金: 101000.00",
                "- 总收益: 1.0%",
                "- 成交笔数: 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact / "trades_20260427_20260627_h20_n4.csv").write_text(
        "signal_date,code,ret_pct,regime,trigger\n2026-06-01,000001,3.0,,sos\n",
        encoding="utf-8",
    )
    weaker_artifact = tmp_path / "backtest-grid-recent_2m-h5-sl0-tp0-tr0"
    weaker_artifact.mkdir()
    (weaker_artifact / "summary_20260427_20260627_h5_n4.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "",
                "- 区间: 2026-04-27 ~ 2026-06-27",
                "- 每日候选上限: Top 4",
                "- 股票池: all (sample=0)",
                "- 绩效引擎: legacy",
                "- 成交样本: 10",
                "- 胜率: 40.0%",
                "- 平均收益: 1.0%",
                "- 中位收益: 0.5%",
                "- 夏普比 (Sharpe Ratio): 0.2",
                "- 卡玛比 (Calmar Ratio): 0.1",
                "- 最大回撤: -10.0%",
                "- 组合总收益: -1.0%",
                "- 初始现金: 100000.00",
                "- 最终现金: 99000.00",
                "- 总收益: -1.0%",
                "- 成交笔数: 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cells = load_grid_cells(tmp_path)
    report = build_report(cells)

    assert {cell.period_key for cell in cells} == {"recent_2m"}
    assert "最近2个月" in report
    assert "等额四仓 / 20天 / SL-8% / 无TP / Trail-5%" in report
    assert "| 1 | 等额四仓 / 20天 / SL-8% / 无TP / Trail-5% 🏆 | 1/1 | +1.00%" in report
    assert "- 市场周期: 市场周期未标注" in report
    assert "| 未标注 | 回测样本未写入周期标签 | 1 |" in report
