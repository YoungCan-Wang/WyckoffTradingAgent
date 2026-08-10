"""Step3 final report assembly and notification."""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.compliance_report import generate_compliance_brief
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
    "> A=缩量高收测试，B=放量高收突破，C=支撑多次测试；"
    "SURVIVED 仅表示未失效，VALIDATED 才表示需求确认，最终仍需 OMS_APPROVED。\n\n"
)


def send_empty_step3_report(
    options: Step3RunOptions,
    items: list[dict],
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    rag_veto_preview: str,
    rag_veto_lines: list[str],
) -> tuple[bool, str, str]:
    report = _empty_step3_report(rag_veto_preview, rag_veto_lines, input_count=len(items))
    if options.runtime_config.skip_llm:
        return (True, "ok_preview", report)
    if not options.notify:
        return (True, "ok", report)
    if not notify_step3_channels(options, _step3_title(benchmark_context), report):
        return (False, "feishu_failed", report)
    _maybe_send_compliance_brief(
        options=options,
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=[],
        code_name=_items_name_map(items),
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
        model_footer=build_model_footer(llm_result, active_tracks, options.model),
    )
    _log_step3_report_stats(content, llm_result, active_tracks, track_inputs, failed, options.model, options.notify)
    if options.notify and not notify_step3_channels(options, _step3_title(benchmark_context), content):
        print("[step3] 飞书推送失败")
        return (False, "feishu_failed", llm_result.report)
    _maybe_send_compliance_brief(
        options=options,
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        code_name=code_name,
    )
    report_progress("研报完成", "", 1.0)
    return (True, "ok", llm_result.report)


def _empty_step3_report(rag_veto_preview: str, rag_veto_lines: list[str], *, input_count: int) -> str:
    if input_count <= 0:
        reason = "上游实际送入 Step3 的候选为 0，只发送状态说明，未调用三阵营模型"
    elif rag_veto_lines:
        reason = f"Step3 收到 {input_count} 只候选，RAG 防雷后无剩余候选，未调用三阵营模型"
    else:
        reason = f"Step3 收到 {input_count} 只候选，但数据或质量门槛后无可用模型输入"
    report = (
        "# 🏛️ Alpha 投委会机密电报：威科夫盘面审判\n\n"
        f"> ⚪ **本轮未执行三阵营模型审判**：{reason}。\n\n"
        "## 💀 逻辑破产\n"
        "- 无（未执行模型审判，不代表候选逻辑有效或失效）\n\n"
        "## ⏳ 储备营地\n"
        "- 无（未执行模型审判）\n\n"
        "## 🏹 处于起跳板\n"
        "- 无（未执行模型审判，不表示任何风险结论）"
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
    model_footer: str = "",
) -> str:
    content = (
        f"{_compact_rag_preview(rag_veto_preview)}{build_signal_confirmed_preview(selected_df)}"
        f"{_ops_preview(ops_codes, code_name)}"
        f"{build_unconfirmed_ops_block(blocked_unconfirmed, code_name)}"
        f"{SPRINGBOARD_ABC_LEGEND}\n{report}"
    )
    if rag_veto_lines:
        content += "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"
    if model_footer:
        content += f"\n\n---\n{model_footer}"
    return content


def build_model_footer(
    llm_result: Step3LlmResult,
    active_tracks: list[str],
    fallback_model: str,
) -> str:
    """把实际使用的模型写进研报尾部。

    此前该信息只 print 到 stdout（`[step3] 研报实际使用模型=...`），要翻 CI 日志
    才能看到，于是"这份研报是哪个模型生成的、有没有走 fallback"在阅读时不可见。
    模型选型直接决定 API 成本与输出风格，成本排查时缺了它只能靠猜。

    显示每条轨实际命中的路由（provider:model）。若某轨回退到了非首选模型，
    这里会与其他轨不同——那正是需要被看见的信号。
    """
    if not active_tracks:
        return ""
    used = [f"{track}={llm_result.used_models.get(track) or fallback_model}" for track in active_tracks]
    distinct = {llm_result.used_models.get(track) or fallback_model for track in active_tracks}
    line = f"🤖 生成模型: {' | '.join(used)}"
    if len(distinct) > 1:
        line += "\n⚠️ 各轨模型不一致，说明有轨道回退到了备用路由"
    return line


def _compact_rag_preview(preview: str) -> str:
    if not preview:
        return ""
    keep = ("扫描股票", "新闻拉取成功", "veto 剔除", "拉取异常")
    rows = [line for line in preview.splitlines() if line.startswith("## ") or any(key in line for key in keep)]
    return "\n".join(rows).strip() + "\n\n---\n" if rows else ""


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
    if not notify_step3_channels(options, _compliance_title(benchmark_context), content):
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


def _report_trade_date(benchmark_context: dict | None) -> str:
    raw = str((benchmark_context or {}).get("trade_date") or (benchmark_context or {}).get("end_trade_date") or "")
    clean = raw.strip()
    if len(clean) == 8 and clean.isdigit():
        clean = f"{clean[:4]}-{clean[4:6]}-{clean[6:]}"
    try:
        return date.fromisoformat(clean[:10]).isoformat()
    except ValueError:
        return date.today().isoformat()


def _step3_title(benchmark_context: dict | None = None) -> str:
    return f"📄 批量研报 {_report_trade_date(benchmark_context)}"


def _compliance_title(benchmark_context: dict | None = None) -> str:
    return f"📄 市场观察简报（合规版） {_report_trade_date(benchmark_context)}"
