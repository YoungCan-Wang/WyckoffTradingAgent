"""盘后影子账本：兑现昨日计划、写下夜计划，并推送独立飞书卡。"""

from __future__ import annotations

from datetime import date, datetime

from core.concept_filters import is_user_facing_etf
from core.market_trade_mode import resolve_market_trade_mode
from core.shadow_ledger import SHADOW_ACCOUNT_ID, ShadowSession, book_nav, run_shadow_session
from utils.env import env_bool
from utils.feishu import send_feishu_notification
from workflows.daily_job_common import log_line, stage_summary
from workflows.daily_job_runtime import DailyJobConfig
from workflows.shadow_ledger_card import render_shadow_card
from workflows.step4_pipeline import TZ, latest_trade_date_str, step4_candidate_meta


def shadow_ledger_enabled() -> bool:
    """影子账本总开关，默认关。

    它依赖 supabase/migrations/20260825_shadow_ledger.sql 建的五张表；迁移未 apply 时
    每次运行都会走到 except 分支记一行 failed_soft。默认关掉，让"建表"成为显式的启用
    动作，而不是让漏斗 summary 每天多一条噪音。
    """
    return env_bool("SHADOW_LEDGER_ENABLED", False)


def run_shadow_ledger_stage(
    *,
    cfg: DailyJobConfig,
    step2_details: dict,
    symbols_info: list[dict],
    step3_report_text: str,
    benchmark_context: dict,
) -> dict:
    t0 = datetime.now(TZ)
    if not shadow_ledger_enabled():
        log_line("影子账本: 跳过（SHADOW_LEDGER_ENABLED=0）", cfg.logs_path)
        return stage_summary("影子账本", "skipped (disabled)")
    if cfg.preview_only or cfg.historical_replay:
        reason = "preview" if cfg.preview_only else "replay"
        log_line(f"影子账本: 跳过（{reason}）", cfg.logs_path)
        return stage_summary("影子账本", f"skipped ({reason})")
    try:
        session = _run_shadow_session(step2_details, symbols_info, step3_report_text, benchmark_context)
        _persist_session(session)
        title, content = render_shadow_card(session, _as_of())
        sent = send_feishu_notification(cfg.webhook, title, content) if cfg.webhook else True
        elapsed = (datetime.now(TZ) - t0).total_seconds()
        log_line(
            f"影子账本: fills={len(session.fills)} plans={len(session.new_plans)} "
            f"equity={session.nav.get('equity')} feishu={sent}",
            cfg.logs_path,
        )
        return {
            "step": "影子账本",
            "ok": True,
            "err": None if sent else "飞书推送失败",
            "elapsed_s": round(elapsed, 1),
            "output": f"fills={len(session.fills)} plans={len(session.new_plans)}",
        }
    except Exception as exc:
        log_line(f"影子账本失败（不阻断漏斗）: {exc}", cfg.logs_path)
        return {
            "step": "影子账本",
            "ok": True,
            "err": str(exc),
            "elapsed_s": round((datetime.now(TZ) - t0).total_seconds(), 1),
            "output": "failed_soft",
        }


def _run_shadow_session(
    step2_details: dict,
    symbols_info: list[dict],
    step3_report_text: str,
    benchmark_context: dict,
) -> ShadowSession:
    from integrations.supabase_shadow import load_planned, load_shadow_book

    as_of = _as_of()
    book = load_shadow_book(SHADOW_ACCOUNT_ID)
    mode = resolve_market_trade_mode((benchmark_context or {}).get("regime"))
    return run_shadow_session(
        book,
        load_planned(SHADOW_ACCOUNT_ID),
        _buy_candidates(symbols_info, step3_report_text),
        step2_details.get("all_df_map") or {},
        as_of,
        # 跟 allow_ai_review 而非 allow_recommendation_write：影子账本是研究对照，
        # 「禁正式推荐」的水温（RISK_ON/NEUTRAL 等 execution_blocked 档）正是最需要攒
        # 对照样本的时候，模式名里的「shadow 对照」就是这个意思。只有硬防守档
        # （RISK_OFF/CRASH/BLACK_SWAN，allow_ai_review=False）才真正停止建新仓。
        allow_new_buys=bool(mode.allow_ai_review),
        prev_equity=float(book_nav(book)["equity"]),
    )


def _persist_session(session: ShadowSession) -> None:
    from integrations.supabase_shadow import persist_shadow_session

    persist_shadow_session(
        account_id=SHADOW_ACCOUNT_ID,
        as_of=_as_of(),
        book=session.book,
        fills=session.fills,
        new_plans=session.new_plans,
        nav=session.nav,
    )


def _buy_candidates(symbols_info: list[dict], step3_report_text: str) -> list[dict]:
    selected, _blocked = step4_candidate_meta(symbols_info, [], step3_report_text)
    return [
        item for item in selected if not is_user_facing_etf(str(item.get("code") or ""), str(item.get("name") or ""))
    ]


def _as_of() -> date:
    return date.fromisoformat(latest_trade_date_str())
