"""Step3 final report assembly and notification."""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.candidate_report_semantics import candidate_semantic_parts
from core.compliance_report import generate_compliance_brief
from core.x_social_summary import generate_x_social_summary
from integrations.llm_client import call_llm
from workflows.compliance_report_config import compliance_llm_config_from_env
from workflows.step3_delivery import notify_step3_channels, send_step3_input_preview
from workflows.step3_inputs import build_step3_preview_report
from workflows.step3_llm import route_label
from workflows.step3_models import Step3LlmResult, Step3RunOptions, Step3TrackInputs
from workflows.step3_operation_gate import (
    build_signal_confirmed_preview,
    build_unconfirmed_ops_block,
    resolve_step3_operation_codes,
)

SPRINGBOARD_ABC_LEGEND = (
    "## 🧾 起跳板硬门槛释义（A/B/C）\n"
    "- A：近5日出现缩量测试/拒绝下跌（量比 < 0.8x 且收位 > 60%）\n"
    "- B：突破日量比 >= 1.5x 且收盘站稳突破位（收位 > 70%）\n"
    "- C：支撑位至少经过 2 次测试且未被有效击穿\n"
    "- 进入“处于起跳板”候选区通常需至少满足 2 条；若弱市（RISK_OFF/CRASH）执行更严格。\n"
    "- 起跳板只是送 OMS 复核的候选，不等于买入订单；只有 OMS BUY-APPROVED 才可执行。\n\n"
    "---\n"
)


def send_empty_step3_report(
    options: Step3RunOptions,
    items: list[dict],
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    rag_veto_preview: str,
    rag_veto_lines: list[str],
) -> tuple[bool, str, str]:
    report = _empty_step3_report(rag_veto_preview, rag_veto_lines)
    if not options.notify:
        return (True, "ok", report)
    if not notify_step3_channels(options, _step3_title(), report):
        return (False, "feishu_failed", report)
    _maybe_send_compliance_brief(
        options=options,
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=[],
        code_name=_items_name_map(items),
    )
    _maybe_send_x_summary(
        options=options,
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=[],
        code_name=_items_name_map(items),
        report_text=report,
    )
    return (True, "ok", report)


def maybe_return_step3_preview(
    options: Step3RunOptions,
    track_requests: list[dict],
    system_prompt: str,
) -> tuple[bool, str, str] | None:
    if not options.runtime_config.skip_llm:
        return None
    if not options.notify:
        return (True, "ok_preview", build_step3_preview_report(track_requests))
    ok, preview_report = send_step3_input_preview(
        webhook_url=options.webhook_url,
        model=route_label(options.provider, options.model),
        system_prompt=system_prompt,
        previews=track_requests,
    )
    if not ok:
        return (False, "feishu_failed", preview_report)
    return (True, "ok_preview", preview_report)


def send_step3_final_report(
    *,
    options: Step3RunOptions,
    active_tracks: list[str],
    track_inputs: Step3TrackInputs,
    selected_df: pd.DataFrame,
    selected_codes: list[str],
    benchmark_context: dict,
    rag_veto_preview: str,
    rag_veto_lines: list[str],
    failed: list[tuple[str, str]],
    llm_result: Step3LlmResult,
    report_progress,
) -> tuple[bool, str, str]:
    code_name, ops_codes, blocked_unconfirmed = resolve_step3_operation_codes(
        llm_result.report,
        selected_codes,
        selected_df,
        options.runtime_config,
    )
    content = _build_final_content(
        report=llm_result.report,
        selected_df=selected_df,
        code_name=code_name,
        ops_codes=ops_codes,
        blocked_unconfirmed=blocked_unconfirmed,
        rag_veto_preview=rag_veto_preview,
        rag_veto_lines=rag_veto_lines,
        failed=failed,
    )
    _log_step3_report_stats(content, llm_result, active_tracks, track_inputs, failed, options.model, options.notify)
    if options.notify and not notify_step3_channels(options, _step3_title(), content):
        print("[step3] 飞书推送失败")
        return (False, "feishu_failed", llm_result.report)
    _maybe_send_compliance_brief(
        options=options,
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        code_name=code_name,
    )
    _maybe_send_x_summary(
        options=options,
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        code_name=code_name,
        report_text=content,
    )
    report_progress("研报完成", "", 1.0)
    return (True, "ok", llm_result.report)


def _empty_step3_report(rag_veto_preview: str, rag_veto_lines: list[str]) -> str:
    report = (
        "# 🏛️ Alpha 投委会机密电报：威科夫盘面审判\n\n"
        "## 💀 逻辑破产\n"
        "- 无（本轮无明确失效标的可判定）\n\n"
        "## ⏳ 储备营地\n"
        "- 无（候选均被 RAG 防雷 veto 或数据不足）\n\n"
        "## 🏹 处于起跳板\n"
        "- 无（风险过高，今日保持观望）"
    )
    if rag_veto_lines:
        report = rag_veto_preview + report + "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
    return report


def _build_final_content(
    *,
    report: str,
    selected_df: pd.DataFrame,
    code_name: dict[str, str],
    ops_codes: list[str],
    blocked_unconfirmed: list[str],
    rag_veto_preview: str,
    rag_veto_lines: list[str],
    failed: list[tuple[str, str]],
) -> str:
    content = (
        f"{rag_veto_preview}{build_signal_confirmed_preview(selected_df)}"
        f"{_mainline_preview(selected_df)}"
        f"{_ops_preview(ops_codes, code_name)}"
        f"{build_unconfirmed_ops_block(blocked_unconfirmed, code_name)}"
        f"{SPRINGBOARD_ABC_LEGEND}\n{report}"
    )
    if rag_veto_lines:
        content += "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"
    return content


def _mainline_preview(selected_df: pd.DataFrame) -> str:
    if selected_df.empty:
        return ""
    rows: list[str] = []
    for _, item in selected_df.iterrows():
        parts = candidate_semantic_parts(
            candidate_reasons=item.get("candidate_reasons"),
            candidate_status=item.get("candidate_status"),
            stock_role_score=item.get("stock_role_score"),
            candidate_lane=item.get("candidate_lane"),
            explicit_theme=item.get("candidate_theme"),
            explicit_phase=item.get("candidate_phase"),
            explicit_role=item.get("candidate_role"),
        )
        if not parts:
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or code).strip()
        rows.append(f"- {code} {name} | {' / '.join(parts)}")
    if not rows:
        return ""
    return "## 🎯 主线定位（确定性字段）\n" + "\n".join(rows) + "\n\n---\n"


def _ops_preview(ops_codes: list[str], code_name: dict[str, str]) -> str:
    if not ops_codes:
        return "## 🏹 处于起跳板速览（前置）\n今日无可执行买入候选，保持观望。\n\n---\n"
    ops_lines = [f"- {c} {code_name.get(c, c)}" for c in ops_codes]
    return (
        "## 🏹 处于起跳板速览（前置）\n候选需经过 OMS 风控复核；只有 BUY-APPROVED 才是可执行买入。\n"
        + "\n".join(ops_lines)
        + "\n\n---\n"
    )


def _maybe_send_compliance_brief(
    *,
    options: Step3RunOptions,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str],
    code_name: dict[str, str],
) -> None:
    if not options.notify or not options.runtime_config.send_compliance_brief:
        return
    content = _build_compliance_brief(benchmark_context, selected_df, ops_codes, code_name)
    if not notify_step3_channels(options, _compliance_title(), content):
        print("[step3] 合规简报推送失败（主报告已发送）")


def _build_compliance_brief(
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str],
    code_name: dict[str, str],
) -> str:
    return generate_compliance_brief(
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        code_name=code_name,
        rag_veto_count=_rag_veto_count(selected_df),
        llm_config=compliance_llm_config_from_env(),
        llm_caller=call_llm,
    )


def _maybe_send_x_summary(
    *,
    options: Step3RunOptions,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str],
    code_name: dict[str, str],
    report_text: str,
) -> None:
    if not options.notify or not options.runtime_config.send_x_summary:
        return
    try:
        content = _build_x_summary(benchmark_context, selected_df, ops_codes, code_name, report_text)
    except Exception as exc:
        print(f"[step3] X直白版总结生成失败（主报告已发送）: {exc}")
        return
    if not notify_step3_channels(options, _x_summary_title(), content):
        print("[step3] X直白版总结推送失败（主报告已发送）")


def _build_x_summary(
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str],
    code_name: dict[str, str],
    report_text: str,
) -> str:
    return generate_x_social_summary(
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        code_name=code_name,
        report_text=report_text,
        llm_config=compliance_llm_config_from_env(),
        llm_caller=call_llm,
    )


def _rag_veto_count(selected_df: pd.DataFrame) -> int:
    if not isinstance(selected_df, pd.DataFrame) or "rag_veto_count" not in selected_df.columns:
        return 0
    try:
        return int(pd.to_numeric(selected_df["rag_veto_count"], errors="coerce").fillna(0).max())
    except Exception:
        return 0


def _items_name_map(items: list[dict]) -> dict[str, str]:
    return {
        str(x.get("code", "")).strip(): str(x.get("name", x.get("code", ""))).strip()
        for x in items
        if isinstance(x, dict) and str(x.get("code", "")).strip()
    }


def _log_step3_report_stats(
    content: str,
    llm_result: Step3LlmResult,
    active_tracks: list[str],
    track_inputs: Step3TrackInputs,
    failed: list[tuple[str, str]],
    fallback_model: str,
    notify: bool,
) -> None:
    if notify:
        print(f"[step3] 飞书发送原文长度={len(content)}（不压缩，交由飞书分片）")
    else:
        print(f"[step3] 研报原文长度={len(content)}（仅生成，不推送外部渠道）")
    models = " | ".join(f"{track}:{llm_result.used_models.get(track, fallback_model)}" for track in active_tracks)
    print(f"[step3] 研报实际使用模型={models}")
    stock_count = sum(len(track_inputs.payloads_by_track.get(t, [])) for t in active_tracks)
    action = "发送成功" if notify else "生成完成"
    print(f"[step3] 研报{action}，股票数={stock_count}，拉取失败数={len(failed)}")


def _step3_title() -> str:
    return f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"


def _compliance_title() -> str:
    return f"📄 市场观察简报（合规版） {date.today().strftime('%Y-%m-%d')}"


def _x_summary_title() -> str:
    return f"🧵 X直白版总结 {date.today().strftime('%Y-%m-%d')}"
