from __future__ import annotations

from core.candidate_report_semantics import candidate_semantic_parts
from core.execution_playbook import tail_buy_playbook_lines
from core.strategy_policy_display import format_policy_meta_text, format_policy_weight_text
from core.tail_buy.decision_semantics import HIGH_RISK_MOMENTUM_SIGNALS, is_limit_up_candidate
from core.tail_buy.models import DECISION_BUY, DECISION_SKIP, DECISION_WATCH, TailBuyCandidate


def summarize_decision_counts(candidates: list[TailBuyCandidate]) -> dict[str, int]:
    out = {DECISION_BUY: 0, DECISION_WATCH: 0, DECISION_SKIP: 0}
    for item in candidates or []:
        decision = str(item.final_decision or "").strip().upper()
        if decision in out:
            out[decision] += 1
    return out


def _clean_extra_sections(extra_sections: list[str] | None) -> list[str]:
    return [text for section in extra_sections or [] if (text := str(section or "").strip())]


def _header_lines(
    *,
    now_text: str,
    source_text: str,
    candidates: list[TailBuyCandidate],
    counts: dict[str, int],
    llm_total: int,
    llm_success: int,
    elapsed_seconds: float,
    buy_only: bool,
    data_fetched_at: str,
    market_reminder: str,
    report_mode: str,
    policy_weights: dict[str, float] | None,
    policy_weight_meta: dict[str, object] | None,
) -> list[str]:
    layer_text = f"- 分层结果: BUY={counts[DECISION_BUY]}"
    risky_buy_count = _high_risk_buy_count(candidates, report_mode)
    if risky_buy_count:
        layer_text += f"（可执行买入{counts[DECISION_BUY] - risky_buy_count} / 观察买入{risky_buy_count}）"
    if not buy_only:
        layer_text += f" / WATCH={counts[DECISION_WATCH]} / SKIP={counts[DECISION_SKIP]}"
    title, action_line, guard_line = _report_mode_text(report_mode)
    lines = [
        f"{title} {now_text}",
        "",
        action_line,
        _execution_scope_line(report_mode),
        f"- 候选来源: {source_text}",
        f"- 扫描数量: {len(candidates)}",
        layer_text,
        f"- AI 二判: {llm_success}/{llm_total}",
        _policy_weight_line(policy_weights, policy_weight_meta),
        f"- 分时数据获取: {data_fetched_at}" if data_fetched_at else "- 分时数据获取: -",
        f"- 总耗时: {elapsed_seconds:.1f}s",
        "",
        f"⚠️ 风险提醒: {market_reminder} | {guard_line}",
        "",
    ]
    lines.extend(tail_buy_playbook_lines(report_mode=report_mode))
    return lines


def _item_line(item: TailBuyCandidate) -> str:
    reasons = _item_reason_text(item)
    llm_tag = f" | AI:{item.llm_decision}" if item.llm_decision else ""
    llm_reason = f" | {item.llm_reason}" if item.llm_reason else ""
    add_tag = "[加仓] " if item.signal_type == "holding" else ""
    semantic = candidate_semantic_parts(
        candidate_reasons=item.candidate_reasons,
        candidate_status=item.candidate_status,
        stock_role_score=item.stock_role_score,
        candidate_lane=item.candidate_lane,
        explicit_theme=item.candidate_theme,
        explicit_phase=item.candidate_phase,
        explicit_role=item.candidate_role,
    )
    semantic_text = f" | {' / '.join(semantic)}" if semantic else ""
    return (
        f"- {add_tag}{item.code} {item.name} | priority={item.priority_score:.1f} | "
        f"rule={item.rule_decision}({item.rule_score:.1f}){llm_tag}{semantic_text} | {reasons}{llm_reason}"
    )


def _item_reason_text(item: TailBuyCandidate) -> str:
    reasons = list(item.rule_reasons[:2]) if item.rule_reasons else []
    policy_reason = _item_policy_weight_reason(item)
    if policy_reason and all(policy_reason not in reason for reason in reasons):
        reasons.append(policy_reason)
    if _is_limit_up_buy(item):
        reasons.append("当日已涨停，无法按现价挂单，仅观察买入")
    elif _is_high_risk_momentum_buy(item):
        reasons.append("高位动能票，仅观察买入，默认不买")
    trap_reason = str(item.features.get("daily_trap_reason") or "").strip()
    if trap_reason and all(trap_reason not in reason for reason in reasons):
        reasons.append(trap_reason)
    return "；".join(reasons) if reasons else "规则信号一般"


def _item_policy_weight_reason(item: TailBuyCandidate) -> str:
    features = item.features or {}
    multiplier = features.get("policy_weight_multiplier")
    if multiplier is None:
        return ""
    signal = str(features.get("policy_weight_signal") or item.signal_type or "").strip().lower() or "unknown"
    old_score = _float_feature(features.get("policy_weight_old_score"))
    new_score = _float_feature(features.get("policy_weight_new_score"))
    return f"归因治理调权({signal}) x{_float_feature(multiplier):.2f}: {old_score:.1f}->{new_score:.1f}"


def _float_feature(raw: object, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _is_high_risk_momentum_buy(item: TailBuyCandidate) -> bool:
    return item.final_decision == DECISION_BUY and str(item.signal_type or "").strip() in HIGH_RISK_MOMENTUM_SIGNALS


def _is_limit_up_buy(item: TailBuyCandidate) -> bool:
    return item.final_decision == DECISION_BUY and is_limit_up_candidate(item.features)


def _is_watch_only_buy(item: TailBuyCandidate) -> bool:
    return _is_high_risk_momentum_buy(item) or _is_limit_up_buy(item)


def _high_risk_buy_count(candidates: list[TailBuyCandidate], report_mode: str) -> int:
    if report_mode == "post_close_review":
        return 0
    return sum(1 for item in candidates if _is_watch_only_buy(item))


def _decision_block(
    candidates: list[TailBuyCandidate],
    *,
    title: str,
    decision: str,
    max_error_items_per_block: int,
    exclude_high_risk_momentum: bool = False,
    only_high_risk_momentum: bool = False,
) -> list[str]:
    block = [x for x in candidates if x.final_decision == decision]
    if exclude_high_risk_momentum:
        block = [x for x in block if not _is_watch_only_buy(x)]
    if only_high_risk_momentum:
        block = [x for x in block if _is_watch_only_buy(x)]
    lines = [f"## {title}"]
    if not block:
        return lines + ["- 无", ""]
    max_errors = max(int(max_error_items_per_block), 1)
    error_items = [x for x in block if str(x.fetch_error or "").strip()]
    normal_items = [x for x in block if not str(x.fetch_error or "").strip()]
    lines.extend(_item_line(item) for item in normal_items + error_items[:max_errors])
    omitted_errors = max(len(error_items) - max_errors, 0)
    if omitted_errors > 0:
        lines.append(f"- ... 其余 {omitted_errors} 只报错标的已省略（详见日志 artifacts）")
    return lines + [""]


def _append_extra_sections(lines: list[str], extra_sections: list[str]) -> None:
    for text in extra_sections:
        lines.append(text)
        lines.append("")


def _source_text(target_signal_date: str, candidate_source: str | None) -> str:
    return str(candidate_source or "").strip() or (
        f"signal_pending（signal_date={target_signal_date}, status in pending/confirmed）"
    )


def _append_decision_sections(
    lines: list[str],
    candidates: list[TailBuyCandidate],
    *,
    max_error_items_per_block: int,
    buy_only: bool,
    report_mode: str,
) -> None:
    sections = _decision_sections(report_mode)
    if not buy_only:
        sections.extend(_watch_skip_section_titles(report_mode))
    for title, decision, options in sections:
        lines.extend(
            _decision_block(
                candidates,
                title=title,
                decision=decision,
                max_error_items_per_block=max_error_items_per_block,
                **options,
            )
        )


def build_tail_buy_markdown(
    *,
    now_text: str,
    target_signal_date: str,
    market_reminder: str,
    candidates: list[TailBuyCandidate],
    llm_total: int,
    llm_success: int,
    elapsed_seconds: float,
    extra_sections: list[str] | None = None,
    extra_sections_first: bool = False,
    max_error_items_per_block: int = 5,
    candidate_source: str | None = None,
    buy_only: bool = False,
    data_fetched_at: str = "",
    report_mode: str = "intraday",
    policy_weights: dict[str, float] | None = None,
    policy_weight_meta: dict[str, object] | None = None,
) -> str:
    lines = _header_lines(
        now_text=now_text,
        source_text=_source_text(target_signal_date, candidate_source),
        candidates=candidates,
        counts=summarize_decision_counts(candidates),
        llm_total=llm_total,
        llm_success=llm_success,
        elapsed_seconds=elapsed_seconds,
        buy_only=buy_only,
        data_fetched_at=data_fetched_at,
        market_reminder=market_reminder,
        report_mode=report_mode,
        policy_weights=policy_weights,
        policy_weight_meta=policy_weight_meta,
    )
    cleaned_sections = _clean_extra_sections(extra_sections)
    if extra_sections_first:
        _append_extra_sections(lines, cleaned_sections)
    _append_decision_sections(
        lines,
        candidates,
        max_error_items_per_block=max_error_items_per_block,
        buy_only=buy_only,
        report_mode=report_mode,
    )
    if not extra_sections_first:
        _append_extra_sections(lines, cleaned_sections)
    lines.append(_footer_text(report_mode))
    return "\n".join(lines).strip() + "\n"


def _report_mode_text(report_mode: str) -> tuple[str, str, str]:
    if report_mode == "post_close_review":
        return (
            "📋 盘后尾盘复核",
            "- 任务定位: 使用今日完整日线二次确认 + 今日分钟线，生成明日入场计划。",
            "安全闸: 明日仍需开盘/尾盘二次确认；跌支撑/冲高回落/无承接不执行",
        )
    return (
        "⏰ Tail Buy",
        "- 任务定位: 使用上一交易日候选 + 今日分钟线，确认今日尾盘是否可执行。",
        "安全闸: 缺支撑/防守水温单EVR/跌支撑/冲高回落不进BUY",
    )


def _policy_weight_line(weights: dict[str, float] | None, meta: dict[str, object] | None = None) -> str:
    selection_summary = _policy_selection_summary(meta)
    if not weights:
        suffix = f"；{selection_summary}" if selection_summary else ""
        return "- 归因调权: 无" + suffix
    text = format_policy_weight_text(weights, limit=12, delimiter="；")
    suffix = f"；{selection_summary}" if selection_summary else ""
    return "- 归因调权: " + (text if text else "无") + format_policy_meta_text(meta) + suffix


def _policy_selection_summary(meta: dict[str, object] | None) -> str:
    if not isinstance(meta, dict):
        return ""
    summary = str(meta.get("selection_action_summary") or "").strip()
    return summary if summary and summary != "候选源治理=无" else ""


def _execution_scope_line(report_mode: str) -> str:
    if report_mode == "post_close_review":
        return "- 执行口径: BUY=明日观察买入；WATCH=继续观察；SKIP=明日放弃"
    return "- 执行口径: BUY=可执行买入；WATCH=观察买入；SKIP=禁止新仓/暂不买"


def _decision_sections(report_mode: str) -> list[tuple[str, str, dict]]:
    if report_mode == "post_close_review":
        return [("BUY（明日观察买入）", DECISION_BUY, {})]
    return [
        ("BUY（可执行买入）", DECISION_BUY, {"exclude_high_risk_momentum": True}),
        ("BUY（观察买入：高位动能默认不买）", DECISION_BUY, {"only_high_risk_momentum": True}),
    ]


def _watch_skip_section_titles(report_mode: str) -> list[tuple[str, str, dict]]:
    if report_mode == "post_close_review":
        return [("WATCH（明日观察）", DECISION_WATCH, {}), ("SKIP（明日放弃）", DECISION_SKIP, {})]
    return [("WATCH（观察买入）", DECISION_WATCH, {}), ("SKIP（禁止新仓/暂不买）", DECISION_SKIP, {})]


def _footer_text(report_mode: str) -> str:
    if report_mode == "post_close_review":
        return "说明：BUY=明日观察买入；WATCH=继续观察；SKIP=明日放弃。本任务不生成订单，不写入交易表。"
    return "说明：BUY=可执行买入；WATCH=观察买入；SKIP=禁止新仓/暂不买。本任务不生成订单，不写入交易表。"
