"""``candidate_lane``/``entry_type`` 这两列只放车道标识,不放市场状态。

``_tracking_source`` 会在市场闸门关闭时给 ``selection_source`` 追加
``:market_blocked``,标的是「这一行写入时闸门是关的」。而 candidate_lane 缺失时
会退到 selection_source,后缀就跟着进了车道列:生产库实测 ``signal_confirmed``
开市侧 14 行、拦截侧 94 行,跨 20260805~20260828 共 13 个交易日,按车道汇总的归因
会把同一条车道读成两条,且占多数的那半是带后缀的那个标签。
"""

from __future__ import annotations

from core.recommendation_payload import build_recommendation_payload


def _row(**over: object) -> dict[str, object]:
    base = {"code": "600000", "name": "测试", "price": 10.0, "tag": "AI复核候选"}
    base.update(over)
    return base


def _build(item: dict[str, object]) -> dict[str, object]:
    return build_recommendation_payload(20260805, [item], {}, {})[0]


def test_market_block_suffix_does_not_enter_lane_columns() -> None:
    row = _build(_row(selection_source="signal_confirmed:market_blocked"))

    assert row["candidate_lane"] == "signal_confirmed"
    assert row["entry_type"] == "signal_confirmed"
    assert row["signal_key"] == "signal_confirmed"


def test_open_market_and_blocked_market_share_one_lane_label() -> None:
    """同一条车道在两种市场状态下必须落成同一个标签,否则归因分母被劈开。"""
    open_row = _build(_row(selection_source="signal_confirmed"))
    blocked_row = _build(_row(selection_source="signal_confirmed:market_blocked"))

    assert open_row["candidate_lane"] == blocked_row["candidate_lane"]


def test_explicit_candidate_lane_still_wins_over_selection_source() -> None:
    """显式车道优先级不变:摘后缀只作用在退到 selection_source 的那条路上。"""
    row = _build(_row(candidate_lane="sos", selection_source="signal_confirmed:market_blocked"))

    assert row["candidate_lane"] == "sos"


def test_lane_stays_empty_when_no_source_is_available() -> None:
    """没有任何来源时不能把空串写成车道,否则出现一个 '' 车道桶。"""
    row = _build(_row())

    assert not row.get("candidate_lane")
    assert not row.get("entry_type")
