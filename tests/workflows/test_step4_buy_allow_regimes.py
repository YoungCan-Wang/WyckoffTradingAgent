"""Tests for the STEP4_BUY_ALLOW_REGIMES exemption switch.

背景：``EXECUTE_BLOCK_NEW_BUY_REGIMES`` 被无条件并入禁买集合，所以单改
``STEP4_BUY_BLOCK_REGIMES`` 无法放开其中任何一档。而那个常量同时被 AI 复核、
推荐写入与横幅文案消费（直接改它会连带影响 30 个用例），因此放开只在下单闸门这层做。
"""

from __future__ import annotations

from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES
from workflows.step4_order_config import step4_order_config_from_env

ALL_REGIMES = frozenset(
    {
        "RISK_ON",
        "NEUTRAL",
        "CAUTION",
        "BEAR_REBOUND",
        "PANIC_REPAIR",
        "RISK_OFF",
        "CRASH",
        "BLACK_SWAN",
        "UNKNOWN",
    }
)


def _blocked(monkeypatch, *, allow: str | None = None, block: str | None = None) -> frozenset[str]:
    monkeypatch.delenv("STEP4_BUY_ALLOW_REGIMES", raising=False)
    monkeypatch.delenv("STEP4_BUY_BLOCK_REGIMES", raising=False)
    if allow is not None:
        monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", allow)
    if block is not None:
        monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", block)
    return step4_order_config_from_env().buy_block_regimes


def test_default_keeps_every_hard_block(monkeypatch):
    """不设开关时行为不变：硬编码禁买集合仍全部生效。"""
    blocked = _blocked(monkeypatch)
    assert EXECUTE_BLOCK_NEW_BUY_REGIMES <= blocked
    assert "NEUTRAL" not in blocked


def test_allow_switch_exempts_named_regimes(monkeypatch):
    blocked = _blocked(monkeypatch, allow="BEAR_REBOUND,RISK_ON")
    assert "BEAR_REBOUND" not in blocked
    assert "RISK_ON" not in blocked


def test_allow_switch_does_not_touch_other_regimes(monkeypatch):
    """豁免必须是精确的：没点名的档一个都不能被放开。"""
    blocked = _blocked(monkeypatch, allow="BEAR_REBOUND")
    for regime in ("CRASH", "BLACK_SWAN", "RISK_OFF", "UNKNOWN", "PANIC_REPAIR"):
        assert regime in blocked
    assert "RISK_ON" in blocked


def test_neutral_can_be_blocked_via_env(monkeypatch):
    """NEUTRAL 证据最强（正式推荐口径绝对收益 H=5/10/20 为 -2.43/-6.19/-5.72%），需能被禁掉。"""
    blocked = _blocked(monkeypatch, block="UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN")
    assert "NEUTRAL" in blocked


def test_production_combination(monkeypatch):
    """生产目标组合：禁 NEUTRAL、放 BEAR_REBOUND/RISK_ON。"""
    blocked = _blocked(
        monkeypatch,
        allow="BEAR_REBOUND,RISK_ON",
        block="UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN",
    )
    assert sorted(ALL_REGIMES - blocked) == ["BEAR_REBOUND", "CAUTION", "RISK_ON"]


def test_allow_switch_tolerates_whitespace_and_case(monkeypatch):
    blocked = _blocked(monkeypatch, allow=" bear_rebound , RISK_ON ")
    assert "BEAR_REBOUND" not in blocked
    assert "RISK_ON" not in blocked


def test_empty_allow_is_noop(monkeypatch):
    assert _blocked(monkeypatch, allow="") == _blocked(monkeypatch)
    assert _blocked(monkeypatch, allow="  ,  ") == _blocked(monkeypatch)


def test_unknown_token_in_allow_is_ignored(monkeypatch):
    """写错档位名不该意外放开任何东西。"""
    blocked = _blocked(monkeypatch, allow="TYPO_REGIME")
    assert EXECUTE_BLOCK_NEW_BUY_REGIMES <= blocked
