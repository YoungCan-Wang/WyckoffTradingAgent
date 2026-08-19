"""漏斗吞吐诊断：逐层报告收缩比，直接指出卡在哪一层。

与 ``diagnose_funnel_recall.py`` 的区别：那个按**代码**回放（「这几只为什么没进池」），
本脚本按**层**统计（「今天池子里的票在哪一层被截断」）。前者查个案，后者定位瓶颈参数。

动机：2026-08 复盘发现 8/17 有 71 只 formal_l4 却 0 只进 AI、8/12 有 60 只进 0 只。
当时误以为是 ``FUNNEL_AI_TOTAL_CAP=8`` 太小，实测才发现 ``tradeable_l4`` 模式下该 cap
不参与晋级限制（workflows/funnel_ai_selection.py 把 promotion_total_cap 传 None），
真瓶颈是水温闸门（``allow_ai_review=False`` 直接提前返回）与该档 AI 配额为 0。

所以这个脚本存在的意义是：**别再靠猜调参数**。它把每层的输入/输出/收缩比摊开，
并标出当日实际生效的限制值（水温、配额、cap、每板块上限），让「卡在哪」一眼可见。

用法::

    python scripts/diagnose_funnel_throughput.py                    # 最近交易日
    python scripts/diagnose_funnel_throughput.py --date 2026-08-17
    python scripts/diagnose_funnel_throughput.py --days 10          # 近 10 个交易日趋势
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import pandas as pd

# 只读脚本：不写库、不发通知、不改配置。
FORMAL_STATUS = "formal_l4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="漏斗逐层吞吐与瓶颈定位")
    parser.add_argument("--date", default="", help="交易日 YYYY-MM-DD，默认最近有数据的一天")
    parser.add_argument("--days", type=int, default=1, help="回看交易日数，>1 时输出趋势表")
    parser.add_argument("--json-out", default="", help="结构化结果输出路径")
    return parser.parse_args()


def _load_observations() -> pd.DataFrame:
    from integrations.supabase_base import create_admin_client, is_admin_configured

    if not is_admin_configured():
        raise SystemExit("需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    client = create_admin_client()
    rows: list[dict[str, Any]] = []
    offset = 0
    while offset < 40_000:
        page = (
            client.table("signal_observations")
            .select("trade_date,code,candidate_status,selected_for_ai,selection_mode,industry,signal_type")
            .range(offset, offset + 999)
            .execute()
        )
        if not page.data:
            break
        rows += page.data
        offset += 1000
    return pd.DataFrame(rows)


def _load_regimes() -> dict[str, str]:
    from integrations.supabase_base import create_admin_client
    from workflows.step4_market import resolve_effective_market_regime

    client = create_admin_client()
    rows = (
        client.table("market_signal_daily")
        .select("trade_date,benchmark_regime,premarket_regime")
        .order("trade_date")
        .execute()
        .data
        or []
    )
    return {
        str(row["trade_date"]): resolve_effective_market_regime(
            row.get("benchmark_regime"), row.get("premarket_regime")
        )
        for row in rows
        if row.get("trade_date")
    }


def _limits_for(regime: str) -> dict[str, Any]:
    """当日实际生效的限制值。这些才是「卡在哪」的候选答案。"""
    from core.ai_candidate_allocation import resolve_ai_candidate_policy
    from core.market_trade_mode import resolve_market_trade_mode
    from workflows.ai_candidate_allocation_config import ai_candidate_allocation_config_from_env
    from workflows.step4_order_config import step4_order_config_from_env

    config = ai_candidate_allocation_config_from_env()
    policy = resolve_ai_candidate_policy(regime, config=config)
    mode = resolve_market_trade_mode(regime)
    selection_mode = os.getenv("FUNNEL_AI_SELECTION_MODE", "tradeable_l4")
    return {
        "regime": regime,
        "allow_ai_review": bool(mode.allow_ai_review),
        "trade_mode": mode.mode,
        "trend_quota": int(policy.get("trend_quota") or 0),
        "accum_quota": int(policy.get("accum_quota") or 0),
        "total_cap": int(policy.get("total_cap") or 0),
        "max_per_sector": int(config.max_per_sector),
        "selection_mode": selection_mode,
        # tradeable_l4 下 total_cap 不参与晋级限制，标注出来避免再次误判。
        "total_cap_binds_promotion": selection_mode != "tradeable_l4",
        "buy_blocked": regime in set(step4_order_config_from_env().buy_block_regimes),
    }


def _bottleneck(stages: dict[str, int], limits: dict[str, Any]) -> str:
    """按最先归零/最紧的一层给出结论，措辞必须指向可改的具体参数。"""
    if stages["observations"] == 0:
        return "无 observation：上游漏斗未产出，检查取数与 L1/L2"
    if not limits["allow_ai_review"]:
        return f"水温闸门（{limits['regime']} / {limits['trade_mode']}）：allow_ai_review=False，AI 复核被整体跳过"
    if stages["formal_l4"] == 0:
        return "无 formal_l4：候选未通过买点确认或跨日 confirmed，非配额问题"
    if stages["selected_for_ai"] == 0:
        quota = limits["trend_quota"] + limits["accum_quota"]
        if quota == 0:
            return f"该档 AI 配额为 0（FUNNEL_AI_{limits['regime']}_TREND/ACCUM）"
        return "配额非零但未选出：检查板块上限与评分门槛"
    quota = limits["trend_quota"] + limits["accum_quota"]
    if stages["selected_for_ai"] >= quota > 0:
        return f"AI 配额打满（{quota}）：如需更多，提高 FUNNEL_AI_{limits['regime']}_TREND/ACCUM"
    if limits["total_cap_binds_promotion"] and stages["selected_for_ai"] >= limits["total_cap"]:
        return f"总量 cap 打满（{limits['total_cap']}）：提高 FUNNEL_AI_TOTAL_CAP"
    return "未见硬性截断：入选数低于所有上限，瓶颈在候选质量而非配额"


def build_day_report(frame: pd.DataFrame, trade_date: str, regime: str) -> dict[str, Any]:
    day = frame[frame.trade_date == trade_date]
    formal = day[day.candidate_status == FORMAL_STATUS]
    picked = day[day.selected_for_ai]
    stages = {
        "observations": int(day.code.nunique()),
        "formal_l4": int(formal.code.nunique()),
        "selected_for_ai": int(picked.code.nunique()),
    }
    limits = _limits_for(regime)
    sector_top = {}
    if not formal.empty and "industry" in formal.columns:
        counts = formal.dropna(subset=["industry"]).groupby("industry").code.nunique().sort_values(ascending=False)
        sector_top = {str(k): int(v) for k, v in counts.head(5).items()}
    return {
        "trade_date": trade_date,
        "stages": stages,
        "shrink": {
            "observations_to_formal": _ratio(stages["formal_l4"], stages["observations"]),
            "formal_to_ai": _ratio(stages["selected_for_ai"], stages["formal_l4"]),
        },
        "limits": limits,
        "formal_by_sector_top5": sector_top,
        "bottleneck": _bottleneck(stages, limits),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else round(100.0 * numerator / denominator, 1)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value}%"


def render(reports: list[dict[str, Any]]) -> str:
    lines = [
        "| 日期 | 水温 | obs | formal_l4 | 进AI | obs→formal | formal→AI | 瓶颈 |",
        "| --- | --- | --: | --: | --: | --: | --: | --- |",
    ]
    for report in reports:
        stage = report["stages"]
        shrink = report["shrink"]
        lines.append(
            f"| {report['trade_date']} | {report['limits']['regime']} | {stage['observations']} | "
            f"{stage['formal_l4']} | {stage['selected_for_ai']} | "
            f"{_pct(shrink['observations_to_formal'])} | {_pct(shrink['formal_to_ai'])} | {report['bottleneck']} |"
        )
    latest = reports[-1]
    limits = latest["limits"]
    lines += [
        "",
        f"**{latest['trade_date']} 当日生效限制**",
        f"- 水温 `{limits['regime']}`／模式 `{limits['trade_mode']}`　allow_ai_review={limits['allow_ai_review']}",
        f"- AI 配额 trend={limits['trend_quota']} accum={limits['accum_quota']}　总量 cap={limits['total_cap']}"
        f"（{'参与' if limits['total_cap_binds_promotion'] else '**不参与**'}晋级限制，selection_mode={limits['selection_mode']}）",
        f"- 每板块上限 {limits['max_per_sector']}　买入闸门：{'禁买' if limits['buy_blocked'] else '可买'}",
    ]
    if latest["formal_by_sector_top5"]:
        top = "、".join(f"{k} {v}" for k, v in latest["formal_by_sector_top5"].items())
        lines.append(f"- formal_l4 板块分布 Top5：{top}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    frame = _load_observations()
    if frame.empty:
        print("[throughput] 无 observation 数据")
        return 1
    frame["selected_for_ai"] = frame.selected_for_ai.fillna(False).astype(bool)
    regimes = _load_regimes()
    dates = sorted(frame.trade_date.unique())
    if args.date:
        dates = [d for d in dates if d == args.date] or [args.date]
    else:
        dates = dates[-max(int(args.days), 1) :]
    reports = [build_day_report(frame, d, regimes.get(d, "UNKNOWN")) for d in dates]
    text = render(reports)
    print(text)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[throughput] written -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
