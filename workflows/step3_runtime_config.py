"""Runtime configuration loader for Step3 report workflow."""

from __future__ import annotations

import os
from dataclasses import dataclass

from utils.env import env_bool as _env_bool
from utils.env import env_float as _env_float
from utils.env import env_int as _env_int

_LEGACY_REPORT_STYLES = {
    "legacy",
    "legacy_dual_pool",
    "dual_pool",
    "classic",
    "v1",
}


@dataclass(frozen=True)
class Step3RuntimeConfig:
    trading_days: int = 320
    report_style: str = "v3_three_camp"
    max_output_tokens: int = 6000
    gemini_model_fallback: str = ""
    max_ai_input: int = 5
    default_context_cap: int = 5
    max_per_industry: int = 0
    empty_compression_fallback_cap: int = 8
    max_upstream_fill: int = 0
    entry_quality_tie_bucket: float = 1.0
    enable_compression: bool = True
    enable_rag_veto: bool = True
    skip_llm: bool = False
    respect_upstream_priority: bool = True
    send_compliance_brief: bool = True
    require_confirmed_operation: bool = True
    enforce_target_trade_date: bool = False
    llm_fallback_providers: tuple[str, ...] = ()
    history_max_workers: int = 6


def step3_runtime_config_from_env() -> Step3RuntimeConfig:
    report_style = os.getenv("STEP3_REPORT_STYLE", "v3_three_camp").strip().lower()
    if report_style in _LEGACY_REPORT_STYLES:
        raise RuntimeError(
            "STEP3_REPORT_STYLE legacy 口径已禁用。请改为 v3_three_camp（或任意非 legacy 样式）以启用三阵营输出。"
        )
    return Step3RuntimeConfig(
        trading_days=max(_env_int("STEP3_TRADING_DAYS", 320), 1),
        report_style=report_style or "v3_three_camp",
        max_output_tokens=max(_env_int("STEP3_MAX_OUTPUT_TOKENS", 6000), 1),
        gemini_model_fallback=os.getenv("STEP3_GEMINI_MODEL_FALLBACK", "").strip(),
        max_ai_input=max(_env_int("STEP3_MAX_AI_INPUT", 5), 0),
        default_context_cap=max(_env_int("STEP3_DEFAULT_CONTEXT_CAP", 5), 0),
        max_per_industry=max(_env_int("STEP3_MAX_PER_INDUSTRY", 0), 0),
        empty_compression_fallback_cap=max(_env_int("STEP3_EMPTY_COMPRESSION_FALLBACK_CAP", 8), 0),
        max_upstream_fill=max(_env_int("STEP3_MAX_UPSTREAM_FILL", 0), 0),
        entry_quality_tie_bucket=max(_env_float("STEP3_ENTRY_QUALITY_TIE_BUCKET", 1.0), 0.0),
        enable_compression=_env_bool("STEP3_ENABLE_COMPRESSION", True),
        enable_rag_veto=_env_bool("STEP3_ENABLE_RAG_VETO", True),
        skip_llm=_env_bool("STEP3_SKIP_LLM", False),
        respect_upstream_priority=_env_bool("STEP3_RESPECT_UPSTREAM_PRIORITY", True),
        send_compliance_brief=_env_bool("STEP3_SEND_COMPLIANCE_BRIEF", True),
        require_confirmed_operation=_env_bool("STEP3_REQUIRE_CONFIRMED_OPERATION", True),
        enforce_target_trade_date=_env_bool("STEP3_ENFORCE_TARGET_TRADE_DATE", False),
        llm_fallback_providers=_env_csv("STEP3_LLM_FALLBACK_PROVIDERS"),
        history_max_workers=max(_env_int("STEP3_HISTORY_MAX_WORKERS", 6), 1),
    )


def _env_csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.getenv(name, "").split(",") if item.strip())
