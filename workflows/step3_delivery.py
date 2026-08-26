"""Step3 report delivery helpers."""

from __future__ import annotations

import os
from datetime import date

from utils.env import env_flag
from utils.feishu import send_feishu_file, send_feishu_notification
from utils.markdown_webhooks import send_dingtalk_notification, send_wecom_notification
from workflows.step3_models import Step3RunOptions


def send_step3_input_preview(
    webhook_url: str,
    model: str,
    system_prompt: str,
    previews: list[dict],
    *,
    wecom_webhook: str = "",
    dingtalk_webhook: str = "",
) -> tuple[bool, str]:
    total_selected = sum(int(x.get("selected_count", 0) or 0) for x in previews)
    report = _build_input_preview_report(total_selected, system_prompt, previews)
    title = f"🧪 模型输入预演 {date.today().strftime('%Y-%m-%d')}"
    artifact_path = _write_input_preview_artifact(report)
    file_enabled = _preview_file_enabled()
    file_sent = send_feishu_file(artifact_path) if file_enabled else False
    notification = _build_input_preview_notice(total_selected, previews, artifact_path) if file_sent else report
    sent = send_feishu_notification(webhook_url, title, notification) if webhook_url else file_sent or not file_enabled
    if wecom_webhook:
        send_wecom_notification(wecom_webhook, title, notification)
    if dingtalk_webhook:
        send_dingtalk_notification(dingtalk_webhook, title, notification)
    if not sent:
        print("[step3] 预演报告飞书推送失败")
        return (False, report)
    print(f"[step3] 预演报告发送成功，股票数={total_selected}, file_sent={file_sent}, path={artifact_path}")
    return (True, report)


def write_step3_evidence_sidecar(
    *,
    codes: list[str],
    veto_lines: list[str],
    prose: str,
    selected_count: int,
) -> str:
    from pathlib import Path

    from core.evidence_snapshot import freeze_evidence, merge_llm_prose, write_evidence_snapshot

    evidence = freeze_evidence(
        {
            "selected_count": selected_count,
            "veto_count": len(veto_lines),
            "codes": codes,
            "veto_lines": veto_lines,
        }
    )
    merged = merge_llm_prose(evidence, prose)
    raw = os.getenv("STEP3_EVIDENCE_PATH") or os.path.join(
        os.getenv("LOGS_DIR", "logs"), "step3_evidence_snapshot.json"
    )
    return str(write_evidence_snapshot(merged, Path(raw)))


def notify_step3_channels(options: Step3RunOptions, title: str, content: str) -> bool:
    sent = send_feishu_notification(options.webhook_url, title, content) if options.webhook_url else True
    if options.wecom_webhook:
        send_wecom_notification(options.wecom_webhook, title, content)
    if options.dingtalk_webhook:
        send_dingtalk_notification(options.dingtalk_webhook, title, content)
    return bool(sent)


def _build_input_preview_report(total_selected: int, system_prompt: str, previews: list[dict]) -> str:
    blocks: list[str] = [
        "# 🧪 Step3 模型输入预演（未调用大模型）",
        "",
        f"- 输入股票数: `{total_selected}`",
        "- 模式: `STEP3_SKIP_LLM=1`",
        "",
        "## SYSTEM PROMPT",
        "",
        "```text",
        system_prompt,
        "```",
        "",
    ]
    for idx, item in enumerate(previews, start=1):
        blocks += [
            f"## USER MESSAGE {idx} / {len(previews)}",
            "",
            f"- 轨道: `{item.get('track', '')}`",
            f"- 股票数: `{item.get('selected_count', 0)}`",
            "",
            "```text",
            str(item.get("user_message", "") or ""),
            "```",
            "",
        ]
    return "\n".join(blocks).rstrip() + "\n"


def _preview_file_enabled() -> bool:
    return env_flag("FEISHU_INPUT_PREVIEW_AS_FILE")


def _write_input_preview_artifact(report: str) -> str:
    path = os.getenv("STEP3_INPUT_PREVIEW_PATH", "").strip()
    if not path:
        path = os.path.join(os.getenv("LOGS_DIR", "logs"), "step3_llm_input_preview.md")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return path


def _github_run_url() -> str:
    server_url = os.getenv("GITHUB_SERVER_URL", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    if not server_url or not repository or not run_id:
        return ""
    return f"{server_url}/{repository}/actions/runs/{run_id}"


def _build_input_preview_notice(
    total_selected: int,
    previews: list[dict],
    artifact_path: str,
) -> str:
    run_number = os.getenv("GITHUB_RUN_NUMBER", "").strip()
    artifact_name = f"input-preview-logs-{run_number}" if run_number else "input-preview-logs-*"
    track_parts = [
        f"{str(item.get('track', '') or 'Unknown')} {int(item.get('selected_count', 0) or 0)}" for item in previews
    ]
    lines = [
        "完整 LLM input 已作为飞书文件发送，卡片不再展开长文本。",
        "",
        f"- 输入股票数: `{total_selected}`",
        f"- 分轨: `{', '.join(track_parts) if track_parts else '-'}`",
        f"- 文件名: `{os.path.basename(artifact_path)}`",
        f"- Actions 备份 artifact: `{artifact_name}`",
    ]
    run_url = _github_run_url()
    if run_url:
        lines.append(f"- Run: {run_url}")
    lines.extend(
        [
            "",
            "任务结束后，在本次 Actions 页面底部 Artifacts 下载该文件。",
        ]
    )
    return "\n".join(lines)
