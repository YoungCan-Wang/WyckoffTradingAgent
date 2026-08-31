"""跑 regime 前瞻检验并推送飞书卡片（含「接下来做什么」的行动建议）。

用法::

    python scripts/evaluate_regime_forward.py --horizon 5 --index 000001
    python scripts/evaluate_regime_forward.py --no-notify      # 只出报告不推送
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.regime_forward_eval import MIN_REGIME_DAYS, RegimeReport, evaluate_regimes

# docs/evidence 而非 artifacts/——后者在 .gitignore 里，CI 的 upload-artifact
# 按路径匹配不到文件，证据会静默丢失。
EVIDENCE_DIR = Path("docs/evidence")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="市场水温 regime 的前瞻检验")
    parser.add_argument("--horizon", type=int, default=5, help="前瞻交易日数")
    parser.add_argument("--index", default="000001", help="主指数代码")
    parser.add_argument("--start", default="", help="起始日期，默认取 market_signal_daily 最早一天前 10 天")
    parser.add_argument("--no-notify", action="store_true", help="不推送飞书")
    parser.add_argument("--out", default=str(EVIDENCE_DIR), help="报告输出目录")
    return parser.parse_args()


def load_regimes() -> dict[str, str]:
    from integrations.supabase_base import create_admin_client, is_admin_configured

    if not is_admin_configured():
        raise SystemExit("需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    client = create_admin_client()
    rows = client.table("market_signal_daily").select("trade_date,benchmark_regime").order("trade_date").execute().data
    return {
        str(row["trade_date"]): str(row.get("benchmark_regime") or "")
        for row in rows or []
        if row.get("trade_date") and row.get("benchmark_regime")
    }


def load_index_series(code: str, start: str, end: str) -> tuple[list[str], list[float]]:
    from integrations.index_data_source import fetch_index_hist

    frame = fetch_index_hist(code, start, end)
    if frame is None or frame.empty:
        raise SystemExit(f"指数 {code} 取数为空")
    date_col = "date" if "date" in frame.columns else frame.columns[0]
    work = frame.copy()
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["close"])
    work["ds"] = pd.to_datetime(work[date_col]).dt.strftime("%Y-%m-%d")
    work = work.sort_values("ds").drop_duplicates("ds")
    return work.ds.tolist(), work.close.astype(float).tolist()


def build_action_lines(report: RegimeReport) -> list[str]:
    """把统计结论翻译成可执行动作。措辞必须与证据强度匹配。"""
    lines: list[str] = []
    crash = next((s for s in report.stats if s.regime == "CRASH"), None)
    control = report.drawdown_control or {}

    if crash is None or crash.days < MIN_REGIME_DAYS:
        lines.append("① CRASH 样本不足，本期不据此调整任何开关；继续积累。")
    elif crash.excess is not None and crash.excess > 0 and (crash.p_value or 1.0) <= 0.05:
        lines.append(
            f"① CRASH 后市场偏涨（超基准 {crash.excess:+.2f}pct，p={crash.p_value}）。"
            "**不要**执行以 CRASH 为由的清仓建议——它标的是底部，不是危险。"
        )
        if str(control.get("verdict", "")).startswith("CRASH 含独立"):
            lines.append(
                f"② 该效应独立于「跌多了」（相对纯跌幅对照 {control.get('increment_over_drawdown'):+.2f}pct，"
                f"重叠 {control.get('overlap_days')}/{control.get('crash_days')}），不是单纯均值回复。"
            )
        else:
            lines.append("② 但与纯跌幅规则接近，可能只是短期均值回复，暂不视为独立信号。")
    elif crash.excess is not None and crash.excess < 0 and (crash.p_value or 1.0) <= 0.05:
        lines.append(f"① CRASH 后市场确实偏跌（{crash.excess:+.2f}pct，p={crash.p_value}），现有清仓逻辑方向正确。")
    else:
        lines.append(f"① CRASH 方向未达显著（超基准 {crash.excess:+.2f}pct，p={crash.p_value}），维持现状不动开关。")

    negative = [s for s in report.stats if s.verdict.startswith("负向")]
    if negative:
        names = "、".join(f"{s.regime}({s.excess:+.2f})" for s in negative)
        lines.append(f"③ 后市偏跌且超出随机的状态：{names} —— 这些才是真正该降低敞口的时点。")

    lines.append(
        "④ 暂不改 `STEP4_BUY_BLOCK_REGIMES`：选股信号本身仍是负超额，"
        "「几乎不买」当前是保护而非缺陷；先等选股 alpha 转正。"
    )
    lines.append("⑤ 下一步只做一件事：继续积累样本，下月重跑本检验，看结论是否跨周期稳定。")
    return lines


def render_markdown(report: RegimeReport) -> str:
    head = (
        f"**窗口** {report.window[0]} ~ {report.window[1]}　"
        f"**指数** {report.index_code}　**前瞻** T+{report.horizon}\n"
        f"**基准**（任意一天买入）{report.baseline:+.2f}%　样本 {report.baseline_days} 天\n\n"
    )
    rows = ["| 水温 | 天数 | 前瞻 | 超基准 | 上涨占比 | p | 判定 |", "| --- | --: | --: | --: | --: | --: | --- |"]
    for stat in report.stats:
        if stat.forward is None:
            rows.append(f"| {stat.regime} | {stat.days} | — | — | — | — | {stat.note or stat.verdict} |")
            continue
        p_text = "—" if stat.p_value is None else f"{stat.p_value:.3f}"
        rows.append(
            f"| {stat.regime} | {stat.days} | {stat.forward:+.2f}% | {stat.excess:+.2f} | "
            f"{stat.positive_pct:.0f}% | {p_text} | {stat.verdict} |"
        )
    control = report.drawdown_control or {}
    control_text = ""
    if control.get("crash_forward") is not None:
        control_text = (
            f"\n**纯跌幅对照**　CRASH {control['crash_forward']:+.2f}% vs "
            f"近3日跌最多 {control['pure_drawdown_forward']:+.2f}%　"
            f"增量 {control['increment_over_drawdown']:+.2f}pct　"
            f"重叠 {control['overlap_days']}/{control['crash_days']}\n"
            f"{control['verdict']}\n"
        )
    actions = "\n".join(f"- {line}" for line in build_action_lines(report))
    return f"{head}{chr(10).join(rows)}\n{control_text}\n**接下来做什么**\n{actions}\n"


def main() -> int:
    args = parse_args()
    regimes = load_regimes()
    if not regimes:
        print("[regime] market_signal_daily 无 regime 记录")
        return 1
    start = args.start or (pd.to_datetime(min(regimes)) - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    end = (pd.to_datetime(max(regimes)) + pd.Timedelta(days=args.horizon * 3 + 10)).strftime("%Y-%m-%d")
    dates, closes = load_index_series(args.index, start, end)
    report = evaluate_regimes(regimes, dates, closes, horizon=args.horizon, index_code=args.index)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    payload["action_lines"] = build_action_lines(report)
    (out_dir / f"regime_forward_h{args.horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(report)
    print(markdown)

    if not args.no_notify:
        _notify(markdown, report)
    return 0


def _notify(markdown: str, report: RegimeReport) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[regime] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"水温前瞻检验 T+{report.horizon}｜{report.window[1]}"
    print("[regime] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[regime] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
