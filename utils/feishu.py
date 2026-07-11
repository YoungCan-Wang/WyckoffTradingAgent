"""Feishu webhook and file delivery helpers."""

from __future__ import annotations

import json
import os
import time

import requests

import utils.feishu_backtest_card
import utils.feishu_report_card
import utils.feishu_tail_buy_card
import utils.feishu_text
from integrations.tickflow_notice import append_tickflow_limit_hint


def _post_card(webhook_url: str, title: str, chunk: str) -> tuple[bool, str]:
    elements = utils.feishu_report_card.build_report_card_elements(chunk)
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": utils.feishu_report_card.report_card_template(title, chunk),
            },
            "elements": elements,
        },
    }
    return _post_interactive_payload(webhook_url, payload, timeout=10)


def _post_legacy_card(webhook_url: str, title: str, chunk: str) -> tuple[bool, str]:
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": chunk}}],
        },
    }
    return _post_interactive_payload(webhook_url, payload, timeout=10)


def _post_rich_card(
    webhook_url: str,
    title: str,
    elements: list,
    template: str = "blue",
) -> tuple[bool, str]:
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        },
    }
    return _post_interactive_payload(webhook_url, payload, timeout=15)


def _post_interactive_payload(webhook_url: str, payload: dict, *, timeout: int) -> tuple[bool, str]:
    resp = requests.post(
        webhook_url.strip(),
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        return False, f"http_{resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return True, "ok_non_json"
    code = int(data.get("code", -1))
    if code == 0:
        return True, "ok"
    return False, f"feishu_code_{code}: {data.get('msg', '')}"


def send_backtest_card(webhook_url: str, summary_path: str) -> bool:
    if not webhook_url or not webhook_url.strip():
        return False
    try:
        with open(summary_path, encoding="utf-8") as file:
            content = file.read()
        elements, template = utils.feishu_backtest_card.build_backtest_card_elements(content)
        return _send_rich_card_with_retry(webhook_url, "📊 Backtest 回测报告", elements, template, "backtest")
    except Exception as exc:
        print(f"[feishu] backtest card error: {exc}")
        return False


def send_tail_buy_card(webhook_url: str, title: str, content: str) -> bool:
    if not webhook_url or not webhook_url.strip():
        return False
    try:
        elements = utils.feishu_tail_buy_card.build_tail_buy_card_elements(content)
        return _send_rich_card_with_retry(webhook_url, title, elements, "blue", "tail_buy")
    except Exception as exc:
        print(f"[feishu] tail_buy rich card error: {exc}")
        return False


def _send_rich_card_with_retry(
    webhook_url: str,
    title: str,
    elements: list,
    template: str,
    label: str,
) -> bool:
    last_err = "unknown"
    for attempt in range(1, 4):
        ok, err = _post_rich_card(webhook_url, title, elements, template)
        if ok:
            print(f"[feishu] {label} rich card sent, attempt={attempt}")
            return True
        last_err = err
        time.sleep(0.6 * attempt)
    print(f"[feishu] {label} rich card failed: {last_err}")
    return False


def send_feishu_notification(webhook_url: str, title: str, content: str) -> bool:
    if not webhook_url or not webhook_url.strip():
        return False

    annotated = utils.feishu_text.annotate_financial_terms(append_tickflow_limit_hint(content))
    normalized = utils.feishu_text.normalize_lark_md(annotated)
    chunks = utils.feishu_text.split_lark_md(normalized, max_len=int(os.getenv("FEISHU_LARK_MAX_LEN", "2800")))
    try:
        for idx, chunk in enumerate(chunks, start=1):
            if not _send_card_chunk(webhook_url, title, chunk, idx, len(chunks)):
                return False
            if idx < len(chunks):
                time.sleep(0.15)
        return True
    except Exception as exc:
        print(f"Feishu notification failed: {exc}")
        return False


def _send_card_chunk(webhook_url: str, title: str, chunk: str, idx: int, total: int) -> bool:
    part_title = title if total == 1 else f"{title} ({idx}/{total})"
    last_err = "unknown"
    for attempt in range(1, 4):
        ok, err = _post_card(webhook_url, part_title, chunk)
        if ok:
            print(f"[feishu] sent part {idx}/{total}, len={len(chunk)}, attempt={attempt}")
            return True
        last_err = err
        sleep_s = 0.6 * attempt
        print(
            f"[feishu] failed part {idx}/{total}, len={len(chunk)}, "
            f"attempt={attempt}, err={err}, retry_in={sleep_s:.1f}s"
        )
        time.sleep(sleep_s)
    fallback_ok, fallback_err = _post_legacy_card(webhook_url, part_title, chunk)
    if fallback_ok:
        print(f"[feishu] rich layout rejected; sent legacy part {idx}/{total}")
        return True
    last_err = f"{last_err}; fallback={fallback_err}"
    print(f"Feishu notification failed on part {idx}/{total}: {last_err}")
    return False


def _feishu_api_base_url() -> str:
    return os.getenv("FEISHU_OPENAPI_BASE_URL", "https://open.feishu.cn/open-apis").strip().rstrip("/")


def _feishu_file_credentials(chat_id: str, app_id: str, app_secret: str) -> tuple[str, str, str]:
    app_id = (app_id or os.getenv("FEISHU_APP_ID", "")).strip()
    app_secret = (app_secret or os.getenv("FEISHU_APP_SECRET", "")).strip()
    chat_id = (
        chat_id
        or os.getenv("FEISHU_PREVIEW_CHAT_ID", "")
        or os.getenv("FEISHU_CHAT_ID", "")
        or os.getenv("LARK_PREVIEW_CHAT_ID", "")
        or os.getenv("LARK_CHAT_ID", "")
    ).strip()
    return app_id, app_secret, chat_id


def _tenant_access_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        f"{_feishu_api_base_url()}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    if int(payload.get("code", -1)) != 0:
        raise RuntimeError(str(payload.get("msg") or payload))
    token = str(payload.get("tenant_access_token", "") or "").strip()
    if not token:
        raise RuntimeError("tenant_access_token missing")
    return token


def _upload_feishu_file(file_path: str, token: str) -> str:
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as file:
        resp = requests.post(
            f"{_feishu_api_base_url()}/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            data={"file_type": "stream", "file_name": filename},
            files={"file": (filename, file, "text/markdown")},
            timeout=30,
        )
    resp.raise_for_status()
    payload = resp.json()
    if int(payload.get("code", -1)) != 0:
        raise RuntimeError(str(payload.get("msg") or payload))
    file_key = str((payload.get("data") or {}).get("file_key", "") or "").strip()
    if not file_key:
        raise RuntimeError("file_key missing")
    return file_key


def _send_feishu_file_message(file_key: str, token: str, chat_id: str, receive_id_type: str) -> None:
    resp = requests.post(
        f"{_feishu_api_base_url()}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={
            "receive_id": chat_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if int(payload.get("code", -1)) != 0:
        raise RuntimeError(str(payload.get("msg") or payload))


def send_feishu_file(
    file_path: str,
    *,
    chat_id: str = "",
    app_id: str = "",
    app_secret: str = "",
    receive_id_type: str = "chat_id",
) -> bool:
    if not file_path or not os.path.isfile(file_path):
        print(f"[feishu] file send skipped: file not found: {file_path}")
        return False
    app_id, app_secret, chat_id = _feishu_file_credentials(chat_id, app_id, app_secret)
    if not app_id or not app_secret or not chat_id:
        print("[feishu] file send skipped: missing FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_PREVIEW_CHAT_ID")
        return False
    try:
        token = _tenant_access_token(app_id, app_secret)
        file_key = _upload_feishu_file(file_path, token)
        _send_feishu_file_message(file_key, token, chat_id, receive_id_type)
        print(f"[feishu] file sent: {os.path.basename(file_path)}")
        return True
    except Exception as exc:
        print(f"[feishu] file send failed: {type(exc).__name__}: {exc}")
        return False
