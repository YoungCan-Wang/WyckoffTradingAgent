"""Step4 deterministic OMS order engine."""

from __future__ import annotations

import math

from core.market_trade_mode import PROBE_ONLY_REGIMES, normalize_regime
from workflows.step4_models import DecisionItem, ExecutionTicket, OrderContext, PositionItem, Step4OrderConfig
from workflows.step4_text import clean_text, contains_keyword, normalize_stage, normalize_track

DEFAULT_STEP4_ORDER_CONFIG = Step4OrderConfig()

_SENTINEL = object()
_T_PLUS_ONE_REJECT = "T+1限制: 当日买入当日不可卖出"


def _format_wyckoff_context(
    track: str,
    stage: str,
    tag: str,
    *,
    funnel_score: float | None = None,
    capital_migration_bonus: float | None = None,
    source_type: str = "",
) -> str:
    parts = [x for x in [clean_text(track), clean_text(stage), clean_text(tag)] if x]
    if funnel_score is not None:
        parts.append(f"score={float(funnel_score):.2f}")
    if capital_migration_bonus is not None and abs(float(capital_migration_bonus)) > 1e-9:
        parts.append(f"资金迁移={float(capital_migration_bonus):+.2f}")
    source = clean_text(source_type)
    if source:
        parts.append(f"source={source}")
    return " | ".join(parts)


def _decision_wyckoff_context(dec: DecisionItem) -> str:
    return _format_wyckoff_context(
        dec.wyckoff_track,
        dec.wyckoff_stage,
        dec.wyckoff_tag,
        funnel_score=dec.funnel_score,
        capital_migration_bonus=dec.capital_migration_bonus,
        source_type=dec.source_type,
    )


def _resolve_chase_limits(
    dec: DecisionItem,
    market_regime: str,
    config: Step4OrderConfig,
) -> tuple[float, float, str, str]:
    regime = normalize_regime(clean_text(market_regime))
    track = normalize_track(dec.wyckoff_track) or normalize_track(dec.wyckoff_tag)
    stage = normalize_stage(dec.wyckoff_stage) or normalize_stage(dec.wyckoff_tag)
    tag = clean_text(dec.wyckoff_tag)

    pct_limit = float(max(config.max_gap_up_pct, 0.0))
    atr_limit = float(max(config.max_gap_up_atr_mult, 0.0))
    profile_parts = [regime]

    regime_mult = {
        "RISK_ON": 1.10,
        "NEUTRAL": 1.00,
        "CAUTION": 0.92,
        "BEAR_REBOUND": 0.92,
        "PANIC_REPAIR": 0.95,
        "PANIC_REPAIR_CONFIRMED": 0.88,
        "RISK_OFF": 0.85,
        "CRASH": 0.70,
        "BLACK_SWAN": 0.60,
    }.get(regime, 1.00)
    pct_limit *= regime_mult
    atr_limit *= regime_mult

    if track == "Trend":
        pct_limit *= 1.12
        atr_limit *= 1.12
        profile_parts.append("Trend")
    elif track == "Accum":
        pct_limit *= 0.82
        atr_limit *= 0.82
        profile_parts.append("Accum")
    else:
        profile_parts.append("Unclassified")

    if stage == "Markup" or contains_keyword(tag, ("sos", "点火", "突破", "主升")):
        pct_limit *= 1.10
        atr_limit *= 1.15
        profile_parts.append("Momentum")
    elif stage == "Accum_C" or contains_keyword(tag, ("spring", "lps", "终极震仓", "缩量回踩")):
        pct_limit *= 0.90
        atr_limit *= 0.90
        profile_parts.append("Trigger")
    elif stage in {"Accum_A", "Accum_B"}:
        pct_limit *= 0.82
        atr_limit *= 0.85
        profile_parts.append(stage)
    elif stage:
        profile_parts.append(stage)

    if dec.is_add_on:
        pct_limit *= 0.95
        atr_limit *= 0.95
        profile_parts.append("AddOn")

    pct_limit = min(max(pct_limit, config.chase_gap_pct_min), config.chase_gap_pct_max)
    atr_limit = min(max(atr_limit, config.chase_atr_mult_min), config.chase_atr_mult_max)
    context = _format_wyckoff_context(
        track,
        stage,
        tag,
        funnel_score=dec.funnel_score,
        capital_migration_bonus=dec.capital_migration_bonus,
        source_type=dec.source_type,
    )
    return (pct_limit, atr_limit, "/".join(profile_parts), context)


class WyckoffOrderEngine:
    """Deterministic order execution engine."""

    SLIPPAGE_BPS = 0.005
    RISK_LIMITS = {
        "PROBE": 0.008,
        "ATTACK": 0.012,
    }
    PRIORITY_MAP = {
        "EXIT": 1,
        "TRIM": 2,
        "HOLD": 3,
        "PROBE": 4,
        "ATTACK": 5,
    }

    def __init__(
        self,
        total_equity: float,
        free_cash: float,
        position_map: dict[str, PositionItem],
        latest_price_map: dict[str, float],
        atr_map: dict[str, float] | None = None,
        market_regime: str | None = None,
        config: Step4OrderConfig | None = None,
        trade_date: str = "",
        stale_exit_codes: frozenset[str] = frozenset(),
    ) -> None:
        self.total_equity = float(max(total_equity, 0.0))
        self.free_cash = float(max(free_cash, 0.0))
        self.position_map = position_map
        self.latest_price_map = latest_price_map
        self.atr_map = atr_map or {}
        self.trade_date = trade_date
        self.stale_exit_codes = stale_exit_codes
        self.market_regime = normalize_regime(market_regime)
        self.config = config or DEFAULT_STEP4_ORDER_CONFIG
        probe_limit = self.config.probe_budget_limit
        if self.market_regime in {"PANIC_REPAIR_CONFIRMED", "PANIC_REPAIR_INTRADAY"}:
            probe_limit = self.config.repair_probe_budget_limit
        self.budget_limits = {
            "PROBE": probe_limit,
            "ATTACK": self.config.attack_budget_limit,
        }

    def process(self, decisions: list[DecisionItem]) -> tuple[list[ExecutionTicket], float]:
        ordered = sorted(decisions, key=lambda d: self.PRIORITY_MAP.get(d.action, 99))
        tickets: list[ExecutionTicket] = []
        for dec in ordered:
            tickets.append(self._process_one(dec))
        return (tickets, self.free_cash)

    def _approved_hold(
        self,
        dec: DecisionItem,
        name: str,
        current_price: float,
        effective_stop_loss: float | None,
        atr14: float | None,
        original_stop_loss: float | None,
        audit_parts: list[str],
        reason: str | None = None,
    ) -> ExecutionTicket:
        return ExecutionTicket(
            code=dec.code,
            name=name,
            action="HOLD",
            status="APPROVED",
            shares=0,
            price_hint=current_price,
            amount=0.0,
            stop_loss=effective_stop_loss,
            max_loss=0.0,
            drawdown_ratio=0.0,
            reason=(reason or dec.reason or "").strip(),
            tape_condition=dec.tape_condition,
            invalidate_condition=dec.invalidate_condition,
            is_holding=(dec.code in self.position_map and self.position_map[dec.code].shares >= 100),
            atr14=atr14,
            original_stop_loss=original_stop_loss,
            effective_stop_loss=effective_stop_loss,
            slippage_bps=self.SLIPPAGE_BPS,
            audit="; ".join(audit_parts + ["hold"]),
            wyckoff_context=_decision_wyckoff_context(dec),
        )

    def _build_order_context(self, dec: DecisionItem) -> OrderContext | ExecutionTicket:
        name = dec.name or dec.code
        current_price = self.latest_price_map.get(dec.code)
        if current_price is None or current_price <= 0:
            return self._no_trade(dec, name, "缺少最新价格")
        pos = self.position_map.get(dec.code)
        return OrderContext(
            dec=dec,
            name=name,
            action=dec.action,
            current_price=current_price,
            pos=pos,
            held_shares=int(pos.shares) if pos else 0,
            sellable_shares=pos.sellable_shares(self.trade_date) if pos else 0,
            atr14=self.atr_map.get(dec.code),
            original_stop_loss=dec.stop_loss,
            effective_stop_loss=dec.stop_loss,
            audit_parts=[],
        )

    def _raise_stop_from_position(self, ctx: OrderContext) -> None:
        pos_stop = ctx.pos.stop_loss if ctx.pos else None
        if pos_stop is None or pos_stop <= 0:
            return
        if ctx.effective_stop_loss is None:
            ctx.effective_stop_loss = pos_stop
            ctx.audit_parts.append(f"inherit_pos_stop({pos_stop:.2f})")
            return
        merged = max(ctx.effective_stop_loss, pos_stop)
        if merged > ctx.effective_stop_loss:
            ctx.audit_parts.append(f"tighter_by_pos_stop({ctx.effective_stop_loss:.2f}->{merged:.2f})")
        ctx.effective_stop_loss = merged

    def _raise_stop_from_atr(self, ctx: OrderContext) -> None:
        if ctx.atr14 is None or ctx.atr14 <= 0:
            return
        trailing_stop = ctx.current_price - self.config.atr_multiplier * ctx.atr14
        if ctx.action in {"HOLD", "TRIM", "EXIT"}:
            if ctx.effective_stop_loss is None or trailing_stop > ctx.effective_stop_loss:
                ctx.effective_stop_loss = trailing_stop
                original = ctx.original_stop_loss if ctx.original_stop_loss is not None else float("nan")
                ctx.audit_parts.append(f"atr_trailing_raise({original:.2f}->{ctx.effective_stop_loss:.2f})")
        elif ctx.action in {"PROBE", "ATTACK"}:
            self._raise_entry_stop_from_atr(ctx, trailing_stop)

    def _raise_entry_stop_from_atr(self, ctx: OrderContext, trailing_stop: float) -> None:
        if ctx.effective_stop_loss is None:
            ctx.effective_stop_loss = trailing_stop
            ctx.audit_parts.append(f"atr_entry_guard({ctx.effective_stop_loss:.2f})")
            return
        merged = max(ctx.effective_stop_loss, trailing_stop)
        if merged > ctx.effective_stop_loss:
            ctx.audit_parts.append(f"atr_entry_tighten({ctx.effective_stop_loss:.2f}->{merged:.2f})")
        ctx.effective_stop_loss = merged

    def _raise_buy_stop_floor(self, ctx: OrderContext) -> None:
        if ctx.action not in {"PROBE", "ATTACK"} or not self.config.buy_hard_stop_enabled:
            return
        if self.config.buy_hard_stop_pct <= 0:
            return
        hard_stop = ctx.current_price * (1.0 - self.config.buy_hard_stop_pct / 100.0)
        if hard_stop <= 0:
            return
        if self.config.buy_stop_mode == "fixed":
            self._apply_fixed_hard_stop(ctx, hard_stop)
            return
        if ctx.effective_stop_loss is None:
            ctx.effective_stop_loss = hard_stop
            ctx.audit_parts.append(f"hard_stop_floor_init({ctx.effective_stop_loss:.2f})")
        elif ctx.effective_stop_loss < hard_stop:
            ctx.audit_parts.append(f"hard_stop_floor_raise({ctx.effective_stop_loss:.2f}->{hard_stop:.2f})")
            ctx.effective_stop_loss = hard_stop

    def _apply_fixed_hard_stop(self, ctx: OrderContext, hard_stop: float) -> None:
        prev_stop = ctx.effective_stop_loss
        if prev_stop is None:
            ctx.effective_stop_loss = hard_stop
            ctx.audit_parts.append(f"hard_stop_fixed_init({ctx.effective_stop_loss:.2f})")
        elif prev_stop < hard_stop:
            ctx.effective_stop_loss = hard_stop
            ctx.audit_parts.append(f"hard_stop_fixed_raise({prev_stop:.2f}->{hard_stop:.2f})")
        else:
            ctx.audit_parts.append(f"hard_stop_fixed_keep_tighter({prev_stop:.2f})")

    def _prepare_order_context(self, dec: DecisionItem) -> OrderContext | ExecutionTicket:
        ctx = self._build_order_context(dec)
        if isinstance(ctx, ExecutionTicket):
            return ctx
        self._raise_stop_from_position(ctx)
        self._raise_stop_from_atr(ctx)
        self._raise_buy_stop_floor(ctx)
        return ctx

    def _hold_from_context(
        self,
        ctx: OrderContext,
        audit_parts: list[str] | None = None,
        reason: str | None = None,
        effective_stop_loss: float | None | object = _SENTINEL,
    ) -> ExecutionTicket:
        return self._approved_hold(
            ctx.dec,
            ctx.name,
            ctx.current_price,
            ctx.effective_stop_loss if effective_stop_loss is _SENTINEL else effective_stop_loss,
            ctx.atr14,
            ctx.original_stop_loss,
            audit_parts if audit_parts is not None else ctx.audit_parts,
            reason=reason,
        )

    def _sell_ticket(self, ctx: OrderContext, sell_shares: int, audit_parts: list[str]) -> ExecutionTicket:
        fill_price = ctx.current_price * (1.0 - self.SLIPPAGE_BPS)
        proceeds = sell_shares * fill_price
        self.free_cash += proceeds
        return ExecutionTicket(
            code=ctx.dec.code,
            name=ctx.name,
            action=ctx.action,
            status="APPROVED",
            shares=sell_shares,
            price_hint=fill_price,
            amount=proceeds,
            stop_loss=ctx.effective_stop_loss,
            max_loss=0.0,
            drawdown_ratio=0.0,
            reason=ctx.dec.reason,
            tape_condition=ctx.dec.tape_condition,
            invalidate_condition=ctx.dec.invalidate_condition,
            is_holding=ctx.held_shares >= 100,
            atr14=ctx.atr14,
            original_stop_loss=ctx.original_stop_loss,
            effective_stop_loss=ctx.effective_stop_loss,
            slippage_bps=self.SLIPPAGE_BPS,
            audit="; ".join(audit_parts),
            wyckoff_context=_decision_wyckoff_context(ctx.dec),
        )

    def _process_exit(self, ctx: OrderContext) -> ExecutionTicket:
        if ctx.held_shares <= 0:
            return self._no_trade(ctx.dec, ctx.name, "无可卖持仓")
        if ctx.sellable_shares <= 0:
            return self._no_trade(ctx.dec, ctx.name, _T_PLUS_ONE_REJECT)
        return self._sell_ticket(ctx, ctx.sellable_shares, ctx.audit_parts + ["sell_with_slippage"])

    def _process_trim(self, ctx: OrderContext) -> ExecutionTicket:
        if ctx.held_shares > 0 and ctx.sellable_shares <= 0:
            return self._no_trade(ctx.dec, ctx.name, _T_PLUS_ONE_REJECT)
        ratio = ctx.dec.trim_ratio if ctx.dec.trim_ratio is not None else 0.5
        ratio = min(max(ratio, 0.1), 1.0)
        sell_shares = int(math.floor(ctx.sellable_shares * ratio / 100.0) * 100)
        if sell_shares < 100:
            return self._no_trade(ctx.dec, ctx.name, "减仓股数不足100股")
        return self._sell_ticket(ctx, sell_shares, ctx.audit_parts + [f"trim_ratio={ratio:.2f}", "sell_with_slippage"])

    def _validate_buy_stop(self, ctx: OrderContext) -> ExecutionTicket | None:
        if ctx.effective_stop_loss is None:
            if ctx.held_shares >= 100:
                return self._hold_from_context(
                    ctx,
                    ctx.audit_parts + ["invalid_stop_loss->hold"],
                    reason=f"非法指令: 缺少 stop_loss，降级为 HOLD；原建议: {ctx.dec.reason}",
                )
            return self._no_trade(ctx.dec, ctx.name, "缺少 stop_loss")
        if ctx.effective_stop_loss <= 0:
            if ctx.held_shares >= 100:
                return self._hold_from_context(
                    ctx,
                    ctx.audit_parts + ["stop_loss<=0->hold"],
                    reason=f"非法指令: stop_loss<=0，降级为 HOLD；原建议: {ctx.dec.reason}",
                    effective_stop_loss=None,
                )
            return self._no_trade(ctx.dec, ctx.name, "非法 stop_loss<=0")
        if ctx.effective_stop_loss >= ctx.current_price:
            return self._no_trade(ctx.dec, ctx.name, "止损倒挂(stop_loss >= current_price)")
        return None

    def _validate_add_on(self, ctx: OrderContext) -> ExecutionTicket | None:
        is_add_on_action = ctx.action in {"PROBE", "ATTACK"} and ctx.held_shares >= 100
        if not (ctx.dec.is_add_on or is_add_on_action):
            return None
        if not ctx.pos or ctx.held_shares < 100:
            return self._no_trade(ctx.dec, ctx.name, "is_add_on=true 但无可加仓持仓")
        if ctx.pos.cost > 0 and ctx.current_price <= ctx.pos.cost:
            return self._hold_from_context(
                ctx,
                ctx.audit_parts + ["add_on_without_profit->hold"],
                reason=f"加仓条件不满足（当前未浮盈），降级为 HOLD；原建议: {ctx.dec.reason}",
            )
        if is_add_on_action and not ctx.dec.is_add_on:
            ctx.audit_parts.append("implicit_add_on_for_existing_position")
        return None

    def _resolve_entry_limits(self, ctx: OrderContext) -> tuple[float, str, str]:
        gap_pct_limit, atr_mult_limit, chase_profile, wyckoff_context = _resolve_chase_limits(
            ctx.dec,
            self.market_regime,
            self.config,
        )
        limit_by_pct = ctx.current_price * (1.0 + gap_pct_limit / 100.0)
        limit_by_atr = (
            ctx.current_price + (atr_mult_limit * ctx.atr14)
            if ctx.atr14 is not None and ctx.atr14 > 0
            else float("inf")
        )
        limit_by_ai = ctx.dec.entry_zone_max if ctx.dec.entry_zone_max is not None else float("inf")
        max_entry_price = min(limit_by_pct, limit_by_atr, limit_by_ai)
        ctx.audit_parts.extend(
            [
                f"chase_profile={chase_profile}",
                f"gap_limit_pct={gap_pct_limit:.2f}",
                f"atr_limit_mult={atr_mult_limit:.2f}",
                f"T+1_max_entry_price={max_entry_price:.2f}",
            ]
        )
        return max_entry_price, chase_profile, wyckoff_context

    def _invalid_entry_zone_ticket(self, ctx: OrderContext, reason: str, audit: str) -> ExecutionTicket:
        if ctx.held_shares >= 100:
            return self._hold_from_context(
                ctx,
                ctx.audit_parts + [f"{audit}->hold"],
                reason=f"{reason}，降级为 HOLD；原建议: {ctx.dec.reason}",
            )
        return self._no_trade(ctx.dec, ctx.name, reason)

    def _resolve_entry_price_for_calc(
        self, ctx: OrderContext, max_entry_price: float
    ) -> tuple[float, float | None, float, ExecutionTicket | None]:
        price_for_calc = ctx.current_price
        if ctx.dec.entry_zone_min is None or ctx.dec.entry_zone_max is None:
            ticket = self._invalid_entry_zone_ticket(ctx, "缺少买入区间", "missing_entry_zone")
            return price_for_calc, None, max_entry_price, ticket
        if ctx.dec.entry_zone_min <= 0 or ctx.dec.entry_zone_max <= 0:
            ticket = self._invalid_entry_zone_ticket(ctx, "非法 entry_zone<=0", "entry_zone<=0")
            return price_for_calc, None, max_entry_price, ticket
        if ctx.dec.entry_zone_min > ctx.dec.entry_zone_max:
            ticket = self._invalid_entry_zone_ticket(ctx, "非法 entry_zone_min>entry_zone_max", "entry_zone_invert")
            return price_for_calc, None, max_entry_price, ticket
        effective_max = min(ctx.dec.entry_zone_max, max_entry_price)
        if effective_max < ctx.dec.entry_zone_min:
            return price_for_calc, None, effective_max, self._no_trade(ctx.dec, ctx.name, "买入区间与防追高上限无交集")
        price_for_calc = (ctx.dec.entry_zone_min + effective_max) / 2.0
        return price_for_calc, ctx.dec.entry_zone_min, effective_max, None

    def _buy_risk_inputs(self, ctx: OrderContext) -> tuple[float, float, float, float, float, float]:
        base_slippage = ctx.current_price * self.SLIPPAGE_BPS
        atr_slippage = (
            max(float(ctx.atr14), 0.0) * max(self.config.atr_slippage_factor, 0.0) if ctx.atr14 is not None else 0.0
        )
        slippage_abs = max(base_slippage, atr_slippage)
        fill_price = ctx.current_price + slippage_abs
        expected_exit_price = max(float(ctx.effective_stop_loss or 0.0) - slippage_abs, 0.0)
        risk_per_share = fill_price - expected_exit_price
        return base_slippage, atr_slippage, slippage_abs, fill_price, expected_exit_price, risk_per_share

    def _build_buy_ticket(
        self,
        ctx: OrderContext,
        price_for_calc: float,
        entry_zone_min: float | None,
        entry_zone_max: float,
        chase_profile: str,
        wyckoff_context: str,
    ) -> ExecutionTicket:
        base_slippage, atr_slippage, slippage_abs, fill_price, expected_exit_price, risk_per_share = (
            self._buy_risk_inputs(ctx)
        )
        if risk_per_share <= 0:
            return self._no_trade(ctx.dec, ctx.name, "风险参数异常(risk_per_share<=0)")
        max_loss_allowed = self.total_equity * self.RISK_LIMITS[ctx.action]
        max_shares_by_risk = max_loss_allowed / risk_per_share
        budget = min(self.total_equity * self.budget_limits[ctx.action], self.free_cash)
        max_shares_by_cash = budget / fill_price
        actual_shares = math.floor(min(max_shares_by_risk, max_shares_by_cash) / 100.0) * 100
        if actual_shares < 100:
            return self._no_trade(ctx.dec, ctx.name, "计算股数不足100股(触及风控或资金限制)")
        actual_shares = int(actual_shares)
        amount = actual_shares * fill_price
        max_loss = actual_shares * risk_per_share
        self.free_cash -= amount
        return self._approved_buy_ticket(
            ctx,
            price_for_calc,
            entry_zone_min,
            entry_zone_max,
            chase_profile,
            wyckoff_context,
            slippage_abs,
            base_slippage,
            atr_slippage,
            fill_price,
            expected_exit_price,
            risk_per_share,
            budget,
            max_shares_by_risk,
            max_shares_by_cash,
            actual_shares,
            amount,
            max_loss,
        )

    def _approved_buy_ticket(
        self,
        ctx: OrderContext,
        price_for_calc: float,
        entry_zone_min: float | None,
        entry_zone_max: float,
        chase_profile: str,
        wyckoff_context: str,
        slippage_abs: float,
        base_slippage: float,
        atr_slippage: float,
        fill_price: float,
        expected_exit_price: float,
        risk_per_share: float,
        budget: float,
        max_shares_by_risk: float,
        max_shares_by_cash: float,
        actual_shares: int,
        amount: float,
        max_loss: float,
    ) -> ExecutionTicket:
        audit = ctx.audit_parts + [
            f"risk_per_share={risk_per_share:.4f}",
            f"expected_exit_price={expected_exit_price:.4f}",
            f"base_slippage={base_slippage:.4f}",
            f"atr_slippage={atr_slippage:.4f}",
            f"budget={budget:.2f}",
            f"shares_by_risk={max_shares_by_risk:.2f}",
            f"shares_by_cash={max_shares_by_cash:.2f}",
            "buy_with_slippage",
        ]
        return ExecutionTicket(
            code=ctx.dec.code,
            name=ctx.name,
            action=ctx.action,
            status="APPROVED",
            shares=actual_shares,
            price_hint=price_for_calc if price_for_calc > 0 else fill_price,
            amount=amount,
            stop_loss=ctx.effective_stop_loss,
            max_loss=max_loss,
            drawdown_ratio=(max_loss / self.total_equity) if self.total_equity > 0 else 0.0,
            reason=ctx.dec.reason,
            tape_condition=ctx.dec.tape_condition,
            invalidate_condition=ctx.dec.invalidate_condition,
            is_holding=ctx.held_shares >= 100,
            atr14=ctx.atr14,
            original_stop_loss=ctx.original_stop_loss,
            effective_stop_loss=ctx.effective_stop_loss,
            slippage_bps=slippage_abs / ctx.current_price if ctx.current_price > 0 else self.SLIPPAGE_BPS,
            audit="; ".join(audit),
            entry_zone_min=entry_zone_min,
            entry_zone_max=entry_zone_max,
            chase_profile=chase_profile,
            wyckoff_context=wyckoff_context,
        )

    def _process_buy(self, ctx: OrderContext) -> ExecutionTicket:
        if self.stale_exit_codes and self.config.block_buy_on_stale_exit:
            # 止损只有成交才算数。旧仓位的离场建议还没落地就开新仓，等于一边放任亏损扩大一边加仓。
            pending = "、".join(sorted(self.stale_exit_codes))
            return self._no_trade(ctx.dec, ctx.name, f"存在未执行的离场工单（{pending}），清干净之前禁止开新仓")
        if ctx.action in {"PROBE", "ATTACK"} and self.market_regime in self.config.buy_block_regimes:
            return self._no_trade(ctx.dec, ctx.name, f"系统性风控拦截: regime={self.market_regime} 禁止买入")
        if ctx.action == "ATTACK" and self.market_regime in PROBE_ONLY_REGIMES:
            return self._no_trade(ctx.dec, ctx.name, "防守试探阶段只允许小额 PROBE，禁止 ATTACK")
        for validator in (self._validate_buy_stop, self._validate_add_on):
            ticket = validator(ctx)
            if ticket is not None:
                return ticket
        max_entry_price, chase_profile, wyckoff_context = self._resolve_entry_limits(ctx)
        price_for_calc, entry_zone_min, entry_zone_max, ticket = self._resolve_entry_price_for_calc(
            ctx, max_entry_price
        )
        if ticket is not None:
            return ticket
        return self._build_buy_ticket(
            ctx, price_for_calc, entry_zone_min, entry_zone_max, chase_profile, wyckoff_context
        )

    def _process_one(self, dec: DecisionItem) -> ExecutionTicket:
        if dec.system_reject_reason:
            return self._no_trade(dec, dec.name or dec.code, dec.system_reject_reason)
        ctx = self._prepare_order_context(dec)
        if isinstance(ctx, ExecutionTicket):
            return ctx
        if ctx.action == "EXIT":
            return self._process_exit(ctx)
        if ctx.action == "TRIM":
            return self._process_trim(ctx)
        if ctx.action == "HOLD":
            return self._process_hold(ctx)
        return self._process_buy(ctx)

    def _process_hold(self, ctx: OrderContext) -> ExecutionTicket:
        """HOLD 前做结构止损兜底：若现价已跌破系统止损线，模型误判也要强制离场。

        止损价的自动上移（ATR 跟踪止损/持仓止损继承）只在这里之前完成计算，
        若只是记录而不强制执行，一旦模型误判给出 HOLD，系统就没有最后一道
        防线。这里在 HOLD 分支收口，将其降级为强制卖出。
        """
        breached = ctx.effective_stop_loss and ctx.current_price <= ctx.effective_stop_loss
        if not (ctx.held_shares >= 100 and breached):
            return self._hold_from_context(ctx)
        if ctx.sellable_shares < 100:
            return self._hold_from_context(
                ctx,
                ctx.audit_parts + ["stop_breach_blocked_by_t1"],
                reason=(
                    f"已跌破止损线{ctx.effective_stop_loss:.2f}，但当日买入受 T+1 限制无法卖出，"
                    f"顺延至下一交易日执行；原建议: {ctx.dec.reason}"
                ),
            )
        ctx.dec.reason = (
            f"系统强制止损: 现价{ctx.current_price:.2f}已跌破止损线{ctx.effective_stop_loss:.2f}，"
            f"覆盖模型HOLD建议；原建议: {ctx.dec.reason}"
        )
        ctx.action = "EXIT"
        ctx.audit_parts.append(f"system_stop_breach_override(price={ctx.current_price:.2f})")
        return self._sell_ticket(ctx, ctx.sellable_shares, ctx.audit_parts + ["forced_exit_stop_breach"])

    def _no_trade(self, dec: DecisionItem, name: str, reason: str) -> ExecutionTicket:
        return ExecutionTicket(
            code=dec.code,
            name=name,
            action=dec.action,
            status="NO_TRADE",
            shares=0,
            price_hint=None,
            amount=0.0,
            stop_loss=dec.stop_loss,
            max_loss=0.0,
            drawdown_ratio=0.0,
            reason=f"{reason} | {dec.reason}".strip(" |"),
            tape_condition=dec.tape_condition,
            invalidate_condition=dec.invalidate_condition,
            is_holding=(dec.code in self.position_map and self.position_map[dec.code].shares >= 100),
            atr14=self.atr_map.get(dec.code),
            original_stop_loss=dec.stop_loss,
            effective_stop_loss=dec.stop_loss,
            slippage_bps=self.SLIPPAGE_BPS,
            audit=f"reject:{reason}",
            wyckoff_context=_decision_wyckoff_context(dec),
        )
