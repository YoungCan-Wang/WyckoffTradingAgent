"""港美持仓进入 OMS，且总权益按汇率折算成人民币。"""

from __future__ import annotations

import pytest

from workflows import step4_payload
from workflows.step4_decision_parser import parse_decisions
from workflows.step4_models import DecisionItem, PositionItem
from workflows.step4_order_engine import WyckoffOrderEngine
from workflows.step4_portfolio import _build_position_item
from workflows.step4_results import _should_persist_stop


def _item(code: str) -> dict:
    return {"code": code, "name": "某股", "shares": 100, "cost": 10.0, "buy_dt": "2026-08-01"}


def test_hk_position_enters_oms() -> None:
    """回归：06881.HK 此前被 6 位数字校验丢弃，既不出工单也不受任何 OMS 风控。"""
    assert _build_position_item(0, _item("06881.HK")).code == "06881.HK"


def test_us_position_enters_oms() -> None:
    assert _build_position_item(0, _item("AAPL.US")).code == "AAPL.US"


def test_a_share_position_unchanged() -> None:
    assert _build_position_item(0, _item("002648")).code == "002648"


def test_bare_short_digits_still_rejected() -> None:
    """裸 4 位数字可能是残缺 A 股码，不能猜成港股。"""
    assert _build_position_item(0, _item("6881")) is None


def test_hk_code_is_zero_padded() -> None:
    assert _build_position_item(0, _item("6881.HK")).code == "06881.HK"


@pytest.mark.parametrize("dirty", ["bad", "not-a-code", "平安银行", "TSLA"])
def test_bare_ticker_is_not_coerced_into_us_position(dirty: str) -> None:
    """持仓来自库/环境变量，脏数据不能被补成 .US 变出一笔幽灵美股仓。

    写入侧 record_fill 接受裸 ticker 是因为人工即时输入能看到回显；读取侧没有纠错机会。
    """
    assert _build_position_item(0, _item(dirty)) is None


def test_hk_decision_is_parsed_and_normalized() -> None:
    """LLM 可能回 700.HK；须收成 00700.HK 才能命中 allowed_codes。"""
    raw = '{"market_view":"v","decisions":[{"code":"700.HK","action":"HOLD","reason":"r"}]}'

    _view, decisions, err = parse_decisions(raw, {"00700.HK"}, {})

    assert err is None
    assert [(d.code, d.action) for d in decisions] == [("00700.HK", "HOLD")]


def test_decision_outside_allowed_codes_still_rejected() -> None:
    """规范化不能放宽白名单——LLM 臆造的代码仍须拦掉。"""
    raw = '{"market_view":"v","decisions":[{"code":"ZZZZ.US","action":"EXIT","reason":"r"}]}'

    _view, decisions, err = parse_decisions(raw, {"002648"}, {})

    assert err is None
    assert decisions == []


def test_cny_total_converts_foreign_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：混币加总会让 total_equity 虚高，仓位上限与预算约束随之偏松。"""
    monkeypatch.setattr(
        "integrations.portfolio_market_value.load_cny_rates",
        lambda _c: {"CNY": 1.0, "HKD": 0.92},
    )
    failures: list[str] = []

    total = step4_payload._to_cny_total({"002648": 10000.0, "06881.HK": 7500.0}, failures)

    assert total == pytest.approx(10000.0 + 7500.0 * 0.92)
    assert failures == []


def test_cny_only_portfolio_skips_fx_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_c):
        raise AssertionError("纯人民币组合不该查汇率")

    monkeypatch.setattr("integrations.portfolio_market_value.load_cny_rates", _boom)

    assert step4_payload._to_cny_total({"002648": 100.0}, []) == pytest.approx(100.0)


def test_fx_failure_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """汇率取不到时按 1:1 计入，但必须写进 failures——静默用错汇率会直接影响下单规模。"""

    def _boom(_c):
        raise RuntimeError("ecb down")

    monkeypatch.setattr("integrations.portfolio_market_value.load_cny_rates", _boom)
    failures: list[str] = []

    total = step4_payload._to_cny_total({"06881.HK": 7500.0}, failures)

    assert total == pytest.approx(7500.0)
    assert any("汇率" in f for f in failures)


def test_missing_single_rate_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("integrations.portfolio_market_value.load_cny_rates", lambda _c: {"CNY": 1.0})
    failures: list[str] = []

    step4_payload._to_cny_total({"06881.HK": 7500.0}, failures)

    assert any("06881.HK" in f for f in failures)


def _us_hold_decision(*, stop_loss: float) -> DecisionItem:
    return DecisionItem(
        code="AAPL.US",
        name="Apple",
        action="HOLD",
        entry_zone_min=None,
        entry_zone_max=None,
        stop_loss=stop_loss,
        trim_ratio=None,
        tape_condition="",
        invalidate_condition="",
        is_add_on=False,
        reason="继续持有",
        confidence=0.6,
    )


def test_us_odd_lot_stop_breach_forces_exit() -> None:
    """回归：港美进 OMS 后若仍要求 >=100 股，10 股美股跌破止损会静默 HOLD。"""
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "AAPL.US": PositionItem(
                code="AAPL.US",
                name="Apple",
                cost=200.0,
                buy_dt="2026-08-01",
                shares=10,
                stop_loss=180.0,
            )
        },
        latest_price_map={"AAPL.US": 170.0},
        market_regime="NEUTRAL",
        trade_date="2026-08-12",
        cny_rates={"CNY": 1.0, "USD": 7.2},
    )

    tickets, cash = engine.process([_us_hold_decision(stop_loss=180.0)])

    ticket = tickets[0]
    assert ticket.action == "EXIT"
    assert ticket.status == "APPROVED"
    assert ticket.shares == 10
    assert ticket.is_holding is True
    assert "forced_exit_stop_breach" in ticket.audit
    expected = 10 * 170.0 * (1.0 - WyckoffOrderEngine.SLIPPAGE_BPS) * 7.2
    assert ticket.amount == pytest.approx(expected)
    assert cash == pytest.approx(50000 + expected)


def test_us_odd_lot_hold_stop_is_persistable() -> None:
    """小仓位 HOLD 必须 is_holding=True，否则 ATR 止损上移写不进库。"""
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=50000,
        position_map={
            "AAPL.US": PositionItem(
                code="AAPL.US",
                name="Apple",
                cost=200.0,
                buy_dt="2026-08-01",
                shares=10,
                stop_loss=180.0,
            )
        },
        latest_price_map={"AAPL.US": 190.0},
        atr_map={"AAPL.US": 5.0},
        market_regime="NEUTRAL",
        trade_date="2026-08-12",
        cny_rates={"CNY": 1.0, "USD": 7.2},
    )

    tickets, _cash = engine.process([_us_hold_decision(stop_loss=180.0)])
    ticket = tickets[0]

    assert ticket.action == "HOLD"
    assert ticket.status == "APPROVED"
    assert ticket.is_holding is True
    assert ticket.effective_stop_loss is not None
    assert _should_persist_stop(ticket) is True


def test_us_buy_sizes_against_cny_budget() -> None:
    """人民币预算 / 美元报价若不乘汇率，会把 $200 当成 ¥200，严重超买。"""
    engine = WyckoffOrderEngine(
        total_equity=100000,
        free_cash=20000,
        position_map={},
        latest_price_map={"AAPL.US": 200.0},
        atr_map={"AAPL.US": 4.0},
        market_regime="NEUTRAL",
        trade_date="2026-08-12",
        cny_rates={"CNY": 1.0, "USD": 7.0},
    )
    decision = DecisionItem(
        code="AAPL.US",
        name="Apple",
        action="PROBE",
        entry_zone_min=195.0,
        entry_zone_max=205.0,
        stop_loss=180.0,
        trim_ratio=None,
        tape_condition="",
        invalidate_condition="",
        is_add_on=False,
        reason="试探",
        confidence=0.7,
    )

    tickets, cash = engine.process([decision])
    ticket = tickets[0]

    assert ticket.status == "APPROVED"
    assert ticket.shares >= 1
    # 若未乘汇率，预算 20000 可买约 100 股；乘 7.0 后最多约 14 股量级。
    assert ticket.shares < 50
    assert ticket.amount == pytest.approx(ticket.shares * ticket.price_hint * 7.0, rel=0.05)
    assert cash < 20000
