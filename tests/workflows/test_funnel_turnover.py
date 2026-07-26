from __future__ import annotations

import pandas as pd

from workflows import funnel_data


def _frame(volume: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": [10.0] * len(volume), "volume": volume, "turnover": [pd.NA] * len(volume)})


def test_attach_turnover_derives_pct_from_float_share(monkeypatch) -> None:
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda: {"000001": 1_000_000.0})
    df_map = {"000001": _frame([10_000.0, 25_000.0])}

    coverage = funnel_data._attach_turnover(df_map)

    assert coverage == 1.0
    assert list(df_map["000001"]["turnover"]) == [1.0, 2.5]


def test_attach_turnover_leaves_column_missing_when_float_share_unknown(monkeypatch) -> None:
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda: {})
    df_map = {"000001": _frame([10_000.0])}

    coverage = funnel_data._attach_turnover(df_map)

    assert coverage == 0.0
    assert df_map["000001"]["turnover"].isna().all()


def test_attach_turnover_survives_float_share_source_failure(monkeypatch) -> None:
    def _boom() -> dict[str, float]:
        raise RuntimeError("tushare down")

    monkeypatch.setattr(funnel_data, "fetch_float_share_map", _boom)
    df_map = {"000001": _frame([10_000.0])}

    assert funnel_data._attach_turnover(df_map) == 0.0
