"""登录后把云端的模型与数据源配置拉到本地。

## 为什么需要这条链路

用户可能在 web 端配好了模型（Gemini / OpenAI / DeepSeek / Anthropic）和行情数据源
（TickFlow）。桌面端登录之后如果不拉下来，界面上仍然是「未配置模型」—— 用户会以为
登录没生效，或者以为要再配一遍。

**这是一条新链路，不是接线。** Python 侧从来没读过 `user_settings` 表：本地配置
（`~/.wyckoff/wyckoff.json`）和云端配置一直是两套独立的东西，CLI 也只读本地。
表结构取自 web 侧 `chat.ts` 里实际在用的 select 列。

## 合并策略：本地优先，云端只补空缺

本地已有值时**不覆盖**。理由是本地是用户在这台机器上刚改过的东西 —— 云端悄悄
盖掉它，表现就是「我明明改了，重启又变回去了」，而且完全无声。

反过来（云端优先）在多设备场景下更"一致"，但那需要一个真正的同步语义（时间戳、
冲突解决），不是登录时拉一把能做到的。所以这里只做**补空缺**：
第一次登录的新机器会拿到全部配置，已经配过的机器不受影响。

## 拉下来的值归拉取时的账号

只补空缺意味着残留的值不会被下一个账号盖掉，所以补上的值都带来源记录写入，
登出或换号时由 `local_auth` 移除。用户之后改过的值指纹对不上，会留下来。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 云端 user_settings 里的列 → 本地 config 的键。
#
# 只列真正需要的：这张表还有 chat_provider / custom_providers 等字段，
# 但本地的模型配置是一个 models 列表结构（见 _merge_models），不是平铺的键。
_DATA_SOURCE_KEYS = {
    "tickflow_api_key": "tickflow_api_key",
    "tushare_token": "tushare_token",
}

# provider 名 → 云端列前缀。本地 models 列表里的 provider_name 用左边这套。
_PROVIDERS = {
    "gemini": "gemini",
    "openai": "openai",
    "deepseek": "deepseek",
    "claude": "anthropic",  # 云端列名是 anthropic_*，本地 provider 叫 claude
}

_SELECT_COLUMNS = ", ".join(
    [
        "chat_provider",
        *(f"{prefix}_{suffix}" for prefix in _PROVIDERS.values() for suffix in ("api_key", "model", "base_url")),
        *_DATA_SOURCE_KEYS,
    ]
)


def _fetch_settings(user_id: str, access_token: str) -> dict[str, Any] | None:
    """读这个用户在 user_settings 里的一行。拿不到返回 None。"""
    if not user_id or not access_token:
        return None
    try:
        from integrations.supabase_base import create_user_client

        client = create_user_client(access_token, "")
        resp = client.table("user_settings").select(_SELECT_COLUMNS).eq("user_id", user_id).limit(1).execute()
    except Exception:
        # 拉配置失败**不能**让登录失败：用户已经通过认证了，
        # 配置拉不下来只是少了个便利，本地配置照常可用。
        logger.warning("cloud config fetch failed", exc_info=True)
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def _session_still_belongs_to(user_id: str) -> bool:
    """Merge only while disk session is still this user (IPC logout/switch race)."""
    from integrations.local_auth import load_session

    expected = str(user_id or "").strip()
    if not expected:
        return False
    disk = load_session()
    return bool(disk) and str(disk.get("user_id") or "") == expected


def _cloud_model_id(provider: str, model: str) -> str:
    return f"cloud:{provider}:{model}"


def _merge_data_sources(row: dict[str, Any], *, expected_user_id: str) -> list[str]:
    """把行情数据源的 key 补进本地 config（本地已有则跳过）。"""
    from integrations.local_auth import load_config, save_cloud_data_source

    local = load_config()
    filled: list[str] = []
    for column, local_key in _DATA_SOURCE_KEYS.items():
        value = str(row.get(column) or "").strip()
        if not value:
            continue
        if str(local.get(local_key) or "").strip():
            continue  # 本地已配，不覆盖
        if not _session_still_belongs_to(expected_user_id):
            logger.warning("refusing data-source merge: disk identity changed during pull")
            break
        if save_cloud_data_source(expected_user_id, local_key, value):
            filled.append(local_key)
    return filled


def _merge_models(row: dict[str, Any], *, expected_user_id: str) -> list[str]:
    """把云端配好的模型补进本地 models 列表。

    以 (provider, model) 判重而不是只看 provider：同一家可能配了不同模型，
    只看 provider 会把用户在本地新加的第二个模型当成重复而跳过。
    """
    from integrations.local_auth import load_model_configs, save_cloud_model_entry

    existing = load_model_configs()
    have = {(str(c.get("provider_name") or ""), str(c.get("model") or "")) for c in existing}

    added: list[str] = []
    for provider, prefix in _PROVIDERS.items():
        api_key = str(row.get(f"{prefix}_api_key") or "").strip()
        model = str(row.get(f"{prefix}_model") or "").strip()
        if not api_key or not model:
            continue
        if (provider, model) in have:
            continue
        if not _session_still_belongs_to(expected_user_id):
            logger.warning("refusing model merge: disk identity changed during pull")
            break
        entry = {
            # id 用 provider+model 而不是只用 provider：save_model_config 的默认
            # 规则是拿 provider_name 当 id，那样同一家配两个模型会互相覆盖。
            # 带上 model 名之后，重复登录也是幂等的（同一条目原地更新）。
            "id": _cloud_model_id(provider, model),
            "provider_name": provider,
            "model": model,
            "api_key": api_key,
            "base_url": str(row.get(f"{prefix}_base_url") or "").strip(),
        }
        try:
            if not save_cloud_model_entry(expected_user_id, entry):
                break
        except Exception:
            logger.warning("failed to save cloud model entry: %s", provider, exc_info=True)
            continue
        added.append(f"{provider}/{model}")

    # 本地原来一个模型都没有时，把云端的默认 provider 设成默认 ——
    # 否则界面仍显示「未配置模型」，用户得自己再去点一次默认。
    if added and not existing and _session_still_belongs_to(expected_user_id):
        _apply_default(row)
    return added


def _apply_default(row: dict[str, Any]) -> None:
    from integrations.local_auth import load_model_configs, set_default_model

    preferred = str(row.get("chat_provider") or "").strip()
    configs = load_model_configs()
    if not configs:
        return
    target = next((c for c in configs if str(c.get("provider_name") or "") == preferred), configs[0])
    try:
        set_default_model(str(target.get("id") or ""))
    except Exception:
        logger.warning("failed to set default model from cloud config", exc_info=True)


def _adopt_existing_values(row: dict[str, Any], user_id: str) -> None:
    from integrations.local_auth import adopt_cloud_values

    data_sources = {local: str(row.get(column) or "").strip() for column, local in _DATA_SOURCE_KEYS.items()}
    model_keys = {
        _cloud_model_id(provider, str(row.get(f"{prefix}_model") or "").strip()): str(
            row.get(f"{prefix}_api_key") or ""
        ).strip()
        for provider, prefix in _PROVIDERS.items()
    }
    adopt_cloud_values(user_id, data_sources, model_keys)


def pull_cloud_config(user_id: str, access_token: str) -> dict[str, Any]:
    """登录后拉一次云端配置，返回实际补上了什么。

    返回值给前端用来提示「已同步 N 项云端配置」。空结果不是错误 ——
    可能云端没配过，也可能本地已经都有了。

    网络拉完后、落盘前必须核对 ``session.json`` 仍是同一 ``user_id``：桌面 IPC
    多 worker 下，慢拉取可与 ``auth_logout`` / 换号重叠；本地优先合并只补空缺，
    迟到的 Alice 密钥一旦写入，Bob 的后续拉取不会盖掉。
    """
    row = _fetch_settings(user_id, access_token)
    if not row:
        return {"models": [], "data_sources": [], "available": False}
    if not _session_still_belongs_to(user_id):
        logger.warning("refusing cloud config merge: disk identity changed during pull")
        return {"models": [], "data_sources": [], "available": False}

    pulled = {
        "models": _merge_models(row, expected_user_id=user_id),
        "data_sources": _merge_data_sources(row, expected_user_id=user_id),
        "available": True,
    }
    if _session_still_belongs_to(user_id):
        _adopt_existing_values(row, user_id)
    return pulled
