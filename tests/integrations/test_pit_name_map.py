"""PIT 名称还原：按 as-of 取当时证券名，避免今日 ST 名误滤历史可交易标的。"""

from __future__ import annotations

from integrations.pit_universe import build_name_spans, name_on


def _row(ts_code: str, name: str, start: str, end: str | None = None) -> dict:
    return {"ts_code": ts_code, "name": name, "start_date": start, "end_date": end}


# 生产实测的真实改名史（600393 粤泰股份）：2020 年名称干净，今日为 ST粤泰(退)
YUETAI = [
    _row("600393.SH", "东华实业", "20010319", "20051106"),
    _row("600393.SH", "粤泰股份", "20160512", "20210505"),
    _row("600393.SH", "ST粤泰", "20210506", "20220614"),
    _row("600393.SH", "粤泰股份", "20220615", "20230504"),
    _row("600393.SH", "ST粤泰", "20230505", None),
]


def test_name_on_returns_the_name_in_force_at_as_of() -> None:
    """回归：600393 在 bull_2020 窗口名为「粤泰股份」，今日名 ST粤泰 会让它被误滤。"""
    spans = build_name_spans(YUETAI)

    assert name_on(spans, "600393", "20200101") == "粤泰股份"
    assert "ST" not in name_on(spans, "600393", "20200101").upper()


def test_name_on_tracks_later_st_periods() -> None:
    spans = build_name_spans(YUETAI)

    assert name_on(spans, "600393", "20210601") == "ST粤泰"
    assert name_on(spans, "600393", "20220701") == "粤泰股份"
    assert name_on(spans, "600393", "20260101") == "ST粤泰"


def test_open_ended_span_covers_future_dates() -> None:
    spans = build_name_spans([_row("000001.SZ", "平安银行", "20120802", None)])

    assert name_on(spans, "000001", "20200101") == "平安银行"
    assert name_on(spans, "000001", "20991231") == "平安银行"


def test_date_before_any_span_falls_back() -> None:
    spans = build_name_spans(YUETAI)

    assert name_on(spans, "600393", "19990101", fallback="旧名") == "旧名"


def test_unknown_code_falls_back() -> None:
    spans = build_name_spans(YUETAI)

    assert name_on(spans, "999999", "20200101", fallback="兜底") == "兜底"


def test_same_day_multiple_changes_takes_the_last() -> None:
    """namechange 偶有同日多条（生产实例：000918 在 20090429 当天连改两次）。"""
    spans = build_name_spans(
        [
            _row("000918.SZ", "SST亚华", "20080310", "20090428"),
            _row("000918.SZ", "S*ST亚华", "20090429", "20090429"),
            _row("000918.SZ", "*ST亚华", "20090430", "20091019"),
        ]
    )

    assert name_on(spans, "000918", "20090429") == "S*ST亚华"


def test_blank_rows_are_dropped() -> None:
    spans = build_name_spans([_row("600393.SH", "", "20200101"), {"ts_code": "", "name": "X"}])

    assert spans == {}


def test_ts_code_and_bare_code_both_resolve() -> None:
    spans = build_name_spans(YUETAI)

    assert name_on(spans, "600393.SH", "20200101") == "粤泰股份"
    assert name_on(spans, "600393", "20200101") == "粤泰股份"
