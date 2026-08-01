"""Controlled classic and A-share empirical strategy ablation definitions."""

from __future__ import annotations

from dataclasses import replace

from core.a_share_entry_research import AShareEntryResearchPolicy

VARIANT_LABELS = {
    "live": "当前生产配置",
    "A": "基线",
    "B": "基线 + Upthrust/UTAD",
    "C": "基线 + regime 触发阈值分层",
    "D": "基线 + Creek/LPS + 跨信号时序加分",
    "E": "B+C+D 全部开启",
    "F": "A股实证：剔除 EVR 确认信号",
    "G": "A股实证：剔除 EVR 与 SOS 确认信号",
    "H": "A股实证：NEUTRAL 入场需广度确认",
    "I": "A股实证：按历史命中先验重排确认信号",
    "M": "A股实证：弱水温信号缩仓",
    "P": "A股实证：M + NEUTRAL Spring 缩仓至 25%",
    "Q": "P + 禁止 NEUTRAL Spring（不补位）",
    "R": "P + 禁止 LPS（不补位）",
    "S": "P + 禁止 NEUTRAL EVR（不补位）",
    "T": "P + 禁止 CAUTION SOS（不补位）",
}

DEFAULT_COMPARISON_VARIANTS = ("P", "Q", "R", "S", "T")

_ALL_SWITCHES = {
    "dist_upthrust_enabled": False,
    "regime_trigger_profiles_enabled": False,
    "lps_creek_confirmation_enabled": False,
    "signal_sequence_bonus_enabled": False,
}

_VARIANT_SWITCHES = {
    "A": {},
    "B": {"dist_upthrust_enabled": True},
    "C": {"regime_trigger_profiles_enabled": True},
    "D": {"lps_creek_confirmation_enabled": True, "signal_sequence_bonus_enabled": True},
    "E": {
        "dist_upthrust_enabled": True,
        "regime_trigger_profiles_enabled": True,
        "lps_creek_confirmation_enabled": True,
        "signal_sequence_bonus_enabled": True,
    },
    "F": {},
    "G": {},
    "H": {},
    "I": {},
    "M": {},
    "P": {},
    "Q": {},
    "R": {},
    "S": {},
    "T": {},
}

_WEAK_REGIME_WEIGHTS = (
    ("NEUTRAL", "spring", 0.5),
    ("NEUTRAL", "evr", 0.5),
    ("PANIC_REPAIR_CONFIRMED", "spring", 0.25),
    ("PANIC_REPAIR_CONFIRMED", "sos", 0.25),
    ("PANIC_REPAIR_INTRADAY", "spring", 0.25),
    ("PANIC_REPAIR_INTRADAY", "sos", 0.25),
)

_LOWER_NEUTRAL_SPRING_WEIGHTS = (
    ("NEUTRAL", "spring", 0.25),
    ("NEUTRAL", "evr", 0.5),
    ("PANIC_REPAIR_CONFIRMED", "spring", 0.25),
    ("PANIC_REPAIR_CONFIRMED", "sos", 0.25),
    ("PANIC_REPAIR_INTRADAY", "spring", 0.25),
    ("PANIC_REPAIR_INTRADAY", "sos", 0.25),
)

_ENTRY_POLICIES = {
    "F": AShareEntryResearchPolicy(blocked_confirmed_signals=("evr",)),
    "G": AShareEntryResearchPolicy(blocked_confirmed_signals=("evr", "sos")),
    "H": AShareEntryResearchPolicy(require_neutral_breadth_confirmation=True),
    "I": AShareEntryResearchPolicy(calibrate_confirmed_score=True),
    "M": AShareEntryResearchPolicy(entry_weight_multipliers=_WEAK_REGIME_WEIGHTS),
    "P": AShareEntryResearchPolicy(entry_weight_multipliers=_LOWER_NEUTRAL_SPRING_WEIGHTS),
    "Q": AShareEntryResearchPolicy(
        blocked_confirmed_regime_signals=(("NEUTRAL", "spring"),),
        entry_weight_multipliers=_LOWER_NEUTRAL_SPRING_WEIGHTS,
    ),
    "R": AShareEntryResearchPolicy(
        blocked_confirmed_signals=("lps",),
        entry_weight_multipliers=_LOWER_NEUTRAL_SPRING_WEIGHTS,
    ),
    "S": AShareEntryResearchPolicy(
        blocked_confirmed_regime_signals=(("NEUTRAL", "evr"),),
        entry_weight_multipliers=_LOWER_NEUTRAL_SPRING_WEIGHTS,
    ),
    "T": AShareEntryResearchPolicy(
        blocked_confirmed_regime_signals=(("CAUTION", "sos"),),
        entry_weight_multipliers=_LOWER_NEUTRAL_SPRING_WEIGHTS,
    ),
}


def normalize_strategy_variant(raw: str) -> str:
    value = str(raw or "live").strip()
    normalized = value.upper() if value.lower() != "live" else "live"
    if normalized not in VARIANT_LABELS:
        raise ValueError("strategy_variant 必须是 live 或已定义的 A-T 研究组")
    return normalized


def strategy_variant_overrides(raw: str) -> dict[str, object]:
    variant = normalize_strategy_variant(raw)
    if variant == "live":
        return {}
    return {**_ALL_SWITCHES, **_VARIANT_SWITCHES[variant]}


def strategy_variant_label(raw: str) -> str:
    return VARIANT_LABELS[normalize_strategy_variant(raw)]


def strategy_variant_entry_policy(raw: str) -> AShareEntryResearchPolicy:
    return _ENTRY_POLICIES.get(normalize_strategy_variant(raw), AShareEntryResearchPolicy())


def strategy_variants_share_signal_ledger(raw_variants: list[str]) -> bool:
    variants = [normalize_strategy_variant(raw) for raw in raw_variants]
    if not variants:
        return False
    first_overrides = strategy_variant_overrides(variants[0])
    first_policy = _signal_build_policy(strategy_variant_entry_policy(variants[0]))
    return all(
        strategy_variant_overrides(variant) == first_overrides
        and _signal_build_policy(strategy_variant_entry_policy(variant)) == first_policy
        for variant in variants[1:]
    )


def _signal_build_policy(policy: AShareEntryResearchPolicy) -> AShareEntryResearchPolicy:
    return replace(
        policy,
        blocked_confirmed_signals=(),
        blocked_confirmed_regime_signals=(),
        entry_weight_multipliers=(),
    )
