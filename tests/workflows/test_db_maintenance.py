from __future__ import annotations

from dataclasses import dataclass

from workflows.db_maintenance import (
    CLEANUP_RULES,
    RECOMMENDATION_KEEP_DATES,
    cleanup_recommendation_table,
    cleanup_recommendation_tracking,
)


@dataclass
class _Response:
    data: list[dict] | None = None
    count: int | None = None


class _FakeTable:
    def __init__(self, client: _FakeClient, name: str):
        self.client = client
        self.name = name
        self.delete_mode = False
        self.filters: list[tuple[str, int]] = []
        self.limit_value: int | None = None
        self.order_desc = False
        self.want_count = False

    def select(self, _columns: str, *, count: str | None = None):
        self.want_count = count == "exact"
        return self

    def order(self, _column: str, *, desc: bool = False):
        self.order_desc = desc
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def lt(self, column: str, value: int):
        self.filters.append((column, value))
        return self

    def delete(self):
        self.delete_mode = True
        return self

    def execute(self):
        rows = self.client.tables.setdefault(self.name, [])
        for column, value in self.filters:
            rows = [row for row in rows if row[column] < value]

        if self.delete_mode:
            deleted_ids = {id(row) for row in rows}
            self.client.tables[self.name] = [row for row in self.client.tables[self.name] if id(row) not in deleted_ids]
            return _Response(data=[])

        ordered = sorted(rows, key=lambda row: row["recommend_date"], reverse=self.order_desc)
        limited = ordered[: self.limit_value] if self.limit_value is not None else ordered
        return _Response(data=limited, count=len(rows) if self.want_count else None)


class _FakeClient:
    def __init__(self, rows: list[dict] | dict[str, list[dict]]):
        self.tables = {"recommendation_tracking": rows} if isinstance(rows, list) else rows

    @property
    def rows(self) -> list[dict]:
        return self.tables["recommendation_tracking"]

    def table(self, name: str):
        return _FakeTable(self, name)


class TestCleanupRules:
    """规则表是纯声明，跑错一次要么删错数据、要么整个 workflow 退 1。"""

    def test_no_duplicate_tables(self):
        tables = [rule[0] for rule in CLEANUP_RULES]
        assert len(tables) == len(set(tables))

    def test_cutoff_kind_is_supported(self):
        for table, _date_col, _ttl, kind in CLEANUP_RULES:
            assert kind in {"iso_date", "yyyymmdd_int"}, table

    def test_ttl_is_positive(self):
        for table, _date_col, ttl, _kind in CLEANUP_RULES:
            assert ttl >= 1, table

    def test_date_columns_match_actual_schema(self):
        """列名写错时 cleanup_table 只会吞成 error 字符串，靠这里锁死。

        signal_health_daily 是 as_of_date 而非 trade_date，factor_ic_daily 是
        eval_date，signal_pending 是 signal_date——这三个最容易顺手写成 trade_date。
        """
        expected = {
            "signal_health_daily": "as_of_date",
            "factor_ic_daily": "eval_date",
            "signal_pending": "signal_date",
            "strategy_attribution_reports": "report_date",
            "signal_observations": "trade_date",
            "signal_outcomes": "trade_date",
            "review_shadow_lane_daily": "trade_date",
            "signal_policy_shadow_runs": "trade_date",
            "theme_radar_snapshot": "trade_date",
            "concept_heat_history": "trade_date",
        }
        actual = {rule[0]: rule[1] for rule in CLEANUP_RULES}
        for table, date_col in expected.items():
            assert actual.get(table) == date_col, table

    def test_raw_evidence_tables_keep_at_least_one_year(self):
        """observations/outcomes 不可重算，且 Actions artifact 只活 90 天（上限也是 90），
        它们是唯一能跨年比较的证据。留存短于 365 天等于把证据删在结论之前。
        """
        actual = {rule[0]: rule[2] for rule in CLEANUP_RULES}
        assert actual["signal_observations"] >= 365
        assert actual["signal_outcomes"] >= 365

    def test_attribution_retention_holds_two_report_windows(self):
        """报告自身是 30 天滚动窗（strategy_attribution_report.py 的 days=30）。
        留 30 天日历日即握着两段不重叠的窗（-30..0 与 -60..-30），够做本期 vs 上期。
        它是全库最大的字节源（单行约 870KB、每交易日一行），所以也不该留得更长。
        """
        actual = {rule[0]: rule[2] for rule in CLEANUP_RULES}
        assert actual["strategy_attribution_reports"] >= 30
        assert actual["strategy_attribution_reports"] <= 60

    def test_shadow_runs_retention_covers_read_window(self):
        """读取口径写死 30 天（load_policy_shadow_runs 的 days=30）。"""
        actual = {rule[0]: rule[2] for rule in CLEANUP_RULES}
        assert actual["signal_policy_shadow_runs"] >= 30


def test_cleanup_recommendation_tracking_keeps_latest_distinct_dates():
    dates = [20260505, 20260503, 20260430, 20260425, 20260420]
    rows = [{"recommend_date": date, "code": code} for date in dates for code in range(2)]
    client = _FakeClient(rows)

    status, count = cleanup_recommendation_tracking(client, keep_dates=3, page_size=2)

    remaining_dates = {row["recommend_date"] for row in client.rows}
    assert status == "ok, keep_dates=3, cutoff=20260430"
    assert count is None
    assert remaining_dates == {20260505, 20260503, 20260430}


def test_recommendation_tracking_default_retention_is_latest_30_dates():
    assert RECOMMENDATION_KEEP_DATES == 30


def test_cleanup_recommendation_tracking_dry_run_counts_rows_before_cutoff():
    dates = [20260505, 20260503, 20260430, 20260425]
    rows = [{"recommend_date": date, "code": code} for date in dates for code in range(2)]
    client = _FakeClient(rows)

    status, count = cleanup_recommendation_tracking(client, keep_dates=3, page_size=3, dry_run=True)

    assert status == "dry_run, keep_dates=3, cutoff=20260430"
    assert count == 2
    assert len(client.rows) == 8


def test_cleanup_recommendation_table_uses_requested_table():
    client = _FakeClient(
        {
            "recommendation_tracking": [{"recommend_date": 20260505, "code": 1}],
            "recommendation_tracking_us": [
                {"recommend_date": 20260505, "code": "A.US"},
                {"recommend_date": 20260504, "code": "B.US"},
                {"recommend_date": 20260503, "code": "C.US"},
            ],
        }
    )

    status, count = cleanup_recommendation_table(
        client,
        "recommendation_tracking_us",
        keep_dates=2,
        page_size=10,
    )

    assert status == "ok, keep_dates=2, cutoff=20260504"
    assert count is None
    assert [row["recommend_date"] for row in client.tables["recommendation_tracking_us"]] == [20260505, 20260504]
    assert client.rows == [{"recommend_date": 20260505, "code": 1}]
