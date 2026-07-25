"""Step4 OMS data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES
from integrations.fetch_a_share_csv import TradingWindow

_TRADE_DAY_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d")


def parse_trade_day(raw: str) -> date | None:
    text = str(raw or "").strip()[:10]
    for fmt in _TRADE_DAY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class PositionItem:
    code: str
    name: str
    cost: float
    buy_dt: str
    shares: int
    stop_loss: float | None = None

    def sellable_shares(self, trade_date: str) -> int:
        """A 股 T+1：当日买入的股份当日不可卖出。

        持仓表按代码聚合成一行、没有分笔明细，买入日期等于当前交易日时只能把整个
        仓位视为冻结。买入日期缺失或无法解析时按可卖处理，否则历史脏数据会让持仓
        永远卖不掉。
        """
        held = max(int(self.shares), 0)
        buy_day = parse_trade_day(self.buy_dt)
        today = parse_trade_day(trade_date)
        if buy_day is None or today is None:
            return held
        return 0 if buy_day >= today else held


@dataclass
class PortfolioState:
    free_cash: float
    total_equity: float | None
    positions: list[PositionItem]


@dataclass
class DecisionItem:
    code: str
    name: str
    action: str
    entry_zone_min: float | None
    entry_zone_max: float | None
    stop_loss: float | None
    trim_ratio: float | None
    tape_condition: str
    invalidate_condition: str
    is_add_on: bool
    reason: str
    confidence: float | None
    funnel_score: float | None = None
    wyckoff_track: str = ""
    wyckoff_stage: str = ""
    wyckoff_tag: str = ""
    source_type: str = ""
    capital_migration_bonus: float | None = None
    system_reject_reason: str = ""


@dataclass
class ExecutionTicket:
    code: str
    name: str
    action: str
    status: str
    shares: int
    price_hint: float | None
    amount: float
    stop_loss: float | None
    max_loss: float
    drawdown_ratio: float
    reason: str
    tape_condition: str
    invalidate_condition: str
    is_holding: bool
    atr14: float | None
    original_stop_loss: float | None
    effective_stop_loss: float | None
    slippage_bps: float
    audit: str
    entry_zone_min: float | None = None
    entry_zone_max: float | None = None
    chase_profile: str = ""
    wyckoff_context: str = ""


@dataclass
class OrderContext:
    dec: DecisionItem
    name: str
    action: str
    current_price: float
    pos: PositionItem | None
    held_shares: int
    sellable_shares: int
    atr14: float | None
    original_stop_loss: float | None
    effective_stop_loss: float | None
    audit_parts: list[str]


@dataclass(frozen=True)
class NewBuyLimits:
    caution: int = 1
    neutral: int = 1


@dataclass(frozen=True)
class Step4OrderConfig:
    atr_multiplier: float = 2.0
    buy_hard_stop_enabled: bool = True
    buy_hard_stop_pct: float = 8.0
    buy_stop_mode: str = "floor"
    atr_slippage_factor: float = 0.25
    probe_budget_limit: float = 0.10
    repair_probe_budget_limit: float = 0.05
    attack_budget_limit: float = 0.20
    buy_block_regimes: frozenset[str] = EXECUTE_BLOCK_NEW_BUY_REGIMES
    chase_gap_pct_min: float = 1.2
    chase_gap_pct_max: float = 5.5
    chase_atr_mult_min: float = 0.8
    chase_atr_mult_max: float = 2.4
    max_gap_up_pct: float = 3.0
    max_gap_up_atr_mult: float = 1.5


@dataclass(frozen=True)
class Step4RuntimeConfig:
    trading_days: int = 320
    enforce_target_trade_date: bool = False
    max_output_tokens: int = 8192
    atr_period: int = 14
    max_workers: int = 8
    max_external_report_candidates: int = 12
    ai_candidate_policy: str = "veto_only"
    new_buy_limits: NewBuyLimits = field(default_factory=NewBuyLimits)


@dataclass(frozen=True)
class CandidateMeta:
    code: str
    name: str
    tag: str = ""
    track: str = ""
    stage: str = ""
    industry: str = ""
    sector_state: str = ""
    sector_state_code: str = ""
    sector_note: str = ""
    funnel_score: float | None = None
    capital_migration_bonus: float | None = None
    exit_signal: str = ""
    exit_price: float | None = None
    exit_reason: str = ""
    source_type: str = ""
    action_status: str = ""
    trade_readiness: str = ""
    new_buy_allowed: bool | None = None
    label_ready: bool | None = None
    risk_factors: tuple[str, ...] = ()
    next_step: str = ""


@dataclass(frozen=True)
class Step4RunOptions:
    provider: str
    model: str
    api_key: str
    llm_base_url: str
    portfolio_id: str
    tg_bot_token: str
    tg_chat_id: str
    runtime_config: Step4RuntimeConfig
    order_config: Step4OrderConfig


@dataclass
class Step4InputContext:
    portfolio: PortfolioState
    state_signature: str
    window: TradingWindow
    trade_date: str
    total_equity: float
    latest_price_map: dict[str, float]
    atr_map: dict[str, float]
    allowed_codes: set[str]
    candidate_meta_map: dict[str, CandidateMeta]
    name_map: dict[str, str]
    market_regime: str
    system_market_view: str
    user_message: str


@dataclass(frozen=True)
class Step4DecisionResult:
    market_view: str
    decisions: list[DecisionItem]


@dataclass(frozen=True)
class Step4PayloadContext:
    total_equity: float
    positions_payload: str
    position_failures: list[str]
    candidate_codes: list[str]
    allowed_codes: set[str]
    candidate_payload: str
    candidate_failures: list[str]
    latest_price_map: dict[str, float]
    atr_map: dict[str, float]
    candidate_meta_map: dict[str, CandidateMeta]
    name_map: dict[str, str]
