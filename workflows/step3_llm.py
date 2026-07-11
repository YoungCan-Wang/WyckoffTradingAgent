"""Step3 LLM routing and report repair helpers."""

from __future__ import annotations

import re

import pandas as pd

from integrations.llm_client import call_llm, get_provider_credentials
from workflows.step3_inputs import TRACK_LABELS
from workflows.step3_models import Step3LlmResult, Step3RunOptions, Step3TrackInputs
from workflows.step3_runtime_config import Step3RuntimeConfig


def route_label(provider: str, model: str) -> str:
    labels = {
        "gemini": "Gemini",
        "efficiency": "Efficiency",
    }
    return f"{labels.get(provider, provider)}:{model}"


def build_step3_llm_routes(
    provider: str,
    model: str,
    api_key: str,
    llm_base_url: str,
    runtime_config: Step3RuntimeConfig | None = None,
) -> list[dict[str, str]]:
    cfg = runtime_config or Step3RuntimeConfig()
    routes: list[dict[str, str]] = []
    provider = str(provider or "gemini").strip().lower() or "gemini"
    _append_llm_route(routes, provider=provider, model=model, api_key=api_key, base_url=llm_base_url)
    gemini_fallback = cfg.gemini_model_fallback
    if provider == "gemini" and gemini_fallback and model != gemini_fallback:
        _append_llm_route(routes, provider=provider, model=gemini_fallback, api_key=api_key, base_url=llm_base_url)
    fallback_default = "efficiency" if provider == "gemini" else "gemini"
    fallback_providers = cfg.llm_fallback_providers or tuple(fallback_default.split(","))
    for fallback_provider in fallback_providers:
        fallback_provider = fallback_provider.strip().lower()
        eff_key, eff_model, eff_base_url = get_provider_credentials(fallback_provider)
        _append_llm_route(routes, provider=fallback_provider, model=eff_model, api_key=eff_key, base_url=eff_base_url)
    return routes


def call_track_report(
    *,
    track: str,
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
    selected_df: pd.DataFrame,
    provider: str = "gemini",
    llm_base_url: str = "",
    runtime_config: Step3RuntimeConfig | None = None,
) -> tuple[bool, str, str]:
    cfg = runtime_config or Step3RuntimeConfig()
    routes = build_step3_llm_routes(provider, model, api_key, llm_base_url, cfg)
    if not routes:
        print(f"[step3] {track} 轨没有可用模型路由，请检查 Gemini 或 Efficiency 配置")
        return (False, "", "")

    report, used_model, used_route = _try_track_llm_routes(
        track=track,
        routes=routes,
        system_prompt=system_prompt,
        user_message=user_message,
        max_output_tokens=cfg.max_output_tokens,
    )
    if not used_route:
        return (False, "", "")
    report = _repair_track_report_if_needed(
        track=track,
        report=report,
        route=used_route,
        selected_codes=selected_codes,
        selected_df=selected_df,
        runtime_config=cfg,
    )
    report = _append_leak_warning(track, report, selected_codes)
    return (True, report, used_model or route_label(provider, model))


def call_step3_track_reports(
    track_requests: list[dict],
    track_inputs: Step3TrackInputs,
    selected_df: pd.DataFrame,
    options: Step3RunOptions,
    system_prompt: str,
    report_progress,
) -> Step3LlmResult:
    track_reports: list[tuple[str, str]] = []
    used_models: dict[str, str] = {}
    for req_idx, request in enumerate(track_requests):
        track = str(request.get("track", "Trend"))
        report_progress("LLM生成", f"{track}轨调用中", 0.3 + 0.5 * req_idx / max(len(track_requests), 1))
        ok, track_report, used_model = call_track_report(
            track=track,
            system_prompt=system_prompt,
            user_message=str(request.get("user_message", "")),
            model=options.model,
            api_key=options.api_key,
            selected_codes=track_inputs.selected_codes_by_track.get(track, []),
            selected_df=track_inputs.df_by_track.get(track, selected_df.iloc[0:0].copy()),
            provider=options.provider,
            llm_base_url=options.llm_base_url,
            runtime_config=options.runtime_config,
        )
        if not ok:
            return Step3LlmResult(ok=False, status="llm_failed", report="", used_models={})
        used_models[track] = used_model
        track_title = TRACK_LABELS.get(track, track)
        track_reports.append((track, f"## {track_title}\n\n{_strip_report_title(track_report)}".strip()))
    report = "\n\n---\n\n".join(section for _, section in track_reports).strip()
    return Step3LlmResult(ok=True, status="ok", report=report, used_models=used_models)


def _append_llm_route(
    routes: list[dict[str, str]],
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str = "",
) -> None:
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip()
    api_key = str(api_key or "").strip()
    base_url = str(base_url or "").strip()
    if not provider or not model or not api_key:
        return
    route_key = (provider, model, base_url)
    if any((r["provider"], r["model"], r["base_url"]) == route_key for r in routes):
        return
    routes.append({"provider": provider, "model": model, "api_key": api_key, "base_url": base_url})


def _try_track_llm_routes(
    *,
    track: str,
    routes: list[dict[str, str]],
    system_prompt: str,
    user_message: str,
    max_output_tokens: int,
) -> tuple[str, str, dict[str, str] | None]:
    for idx, route in enumerate(routes):
        current_route_label = route_label(route["provider"], route["model"])
        try:
            report = call_llm(
                provider=route["provider"],
                model=route["model"],
                api_key=route["api_key"],
                system_prompt=system_prompt,
                user_message=user_message,
                base_url=route["base_url"] or None,
                timeout=300,
                max_output_tokens=max_output_tokens,
            )
            return report, current_route_label, route
        except Exception as e:
            print(f"[step3] {track} 轨模型 {current_route_label} 失败: {e}")
            if idx == len(routes) - 1:
                return "", "", None
    return "", "", None


def _repair_track_report_if_needed(
    *,
    track: str,
    report: str,
    route: dict[str, str],
    selected_codes: list[str],
    selected_df: pd.DataFrame,
    runtime_config: Step3RuntimeConfig,
) -> str:
    if not _has_required_sections(report):
        print(f"[step3] {track} 轨首版研报缺少可识别分层章节，执行一次结构修复")
        report = _repair_report_structure(
            report=report,
            model=route["model"],
            api_key=route["api_key"],
            selected_codes=selected_codes,
            provider=route["provider"],
            llm_base_url=route["base_url"],
            max_output_tokens=runtime_config.max_output_tokens,
        )
    if not _has_required_sections(report):
        print(f"[step3] {track} 轨结构修复后仍缺少关键章节，追加系统兜底分层")
        report = report.rstrip() + "\n\n" + _build_fallback_sections(selected_df)
    return report


def _repair_report_structure(
    report: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
    *,
    provider: str = "gemini",
    llm_base_url: str = "",
    max_output_tokens: int = 32768,
) -> str:
    if not report.strip():
        return report

    repair_system = (
        "你是格式修复器。请将输入研报重排为标准 Markdown，"
        "必须包含三个章节：1) 逻辑破产 2) 储备营地 3) 处于起跳板。"
        "如果输入原本是旧口径的继续观察/立刻建仓，也要将其重排到上述三阵营中。"
        "明显假突破、派发、放量失守归入逻辑破产；其余未到起跳点的非操作标的归入储备营地。"
        "若原文包含 candidate_theme/candidate_phase/candidate_role 或对应的主线/阶段/角色，必须原样保留，"
        "不得重新判断、改名或编造；confirmed 和处于起跳板仍不等于 BUY。"
        "不可新增未在输入中出现的股票代码。"
    )
    repair_user = "允许使用的股票代码：" + ", ".join(selected_codes) + "\n\n以下是待修复文本：\n\n" + report
    try:
        fixed = call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=repair_system,
            user_message=repair_user,
            base_url=llm_base_url or None,
            timeout=180,
            max_output_tokens=max_output_tokens,
        )
        return fixed or report
    except Exception as e:
        print(f"[step3] 结构修复失败: {e}")
        return report


def _has_required_sections(report: str) -> bool:
    text = (report or "").replace(" ", "")
    has_invalidated = "逻辑破产" in text
    has_building = "储备营地" in text
    has_springboard = "处于起跳板" in text
    return has_invalidated and has_building and has_springboard


def _build_fallback_sections(selected_df: pd.DataFrame) -> str:
    if selected_df is None or selected_df.empty:
        return (
            "## 💀 逻辑破产（系统兜底）\n"
            "- 无（本轮无明确失效标的可判定）。\n\n"
            "## ⏳ 储备营地（系统兜底）\n"
            "- 无（本轮无可用候选）。\n\n"
            "## 🏹 处于起跳板（系统兜底）\n"
            "- 无（本轮无可操作标的）。"
        )

    lines = ["## 💀 逻辑破产（系统兜底）", "- 无（系统未判定明确逻辑破产标的）。", ""]
    lines.append("## ⏳ 储备营地（系统兜底）")
    for _, row in selected_df.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", code))
        tag = str(row.get("tag", ""))
        score = row.get("wyckoff_score")
        score_text = f"{float(score):.3f}" if pd.notna(score) else "-"
        lines.append(
            f"- `{code} {name}` | 标签: {tag or '-'} | 量化分: {score_text} | 仍需条件: 回踩结构战区时需缩量确认。"
        )

    lines.append("")
    lines.append("## 🏹 处于起跳板（系统兜底）")
    lines.append("- 无（模型未输出可操作标的，保持耐心观察）")
    return "\n".join(lines)


def _strip_report_title(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _append_leak_warning(track: str, report: str, selected_codes: list[str]) -> str:
    input_set = {str(c).strip() for c in selected_codes}
    leaked = set(re.findall(r"\b(\d{6})\b", report)) - input_set
    if leaked:
        print(f"[step3] ⚠ {track} 轨报告中出现非本轨标的: {','.join(sorted(leaked))}")
        report += f"\n\n> ⚠ 以下代码不在{track}轨输入集中，可能为模型幻觉: {', '.join(sorted(leaked))}"
    return report
