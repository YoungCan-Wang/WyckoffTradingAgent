"""Step3 report parsing helpers for operation-pool extraction."""

from __future__ import annotations

import json
import re

from utils.json_text import extract_json_block

WATCH_POOL_KEYS = (
    "\u903b\u8f91\u7834\u4ea7",
    "\u50a8\u5907\u8425\u5730",
    "invalidated",
    "building_cause",
    "building_camp",
)
OPERATION_POOL_KEYS = (
    "operation_pool",
    "\u5904\u4e8e\u8d77\u8df3\u677f",
    "on_the_springboard",
    "springboard_pool",
)
INVALIDATED_POOL_KEYS = ("\u903b\u8f91\u7834\u4ea7", "invalidated")
BUILDING_POOL_KEYS = ("\u50a8\u5907\u8425\u5730", "building cause")
SPRINGBOARD_POOL_KEYS = ("\u8d77\u8df3\u677f", "springboard")

# LLM \u4e09\u5206\u7c7b\u5224\u5b9a\uff0c\u7528\u4e8e\u4e8b\u540e\u8bc4\u4f30 LLM \u662f\u5426\u771f\u7684\u6709\u533a\u5206\u5ea6\u3002
# \u53ea\u6709\u628a\u300c\u88ab\u5426\u51b3\u7684\u300d\u4e5f\u8bb0\u5f55\u4e0b\u6765\uff0c\u624d\u80fd\u56de\u7b54"\u62e6\u5bf9\u4e86\u5417"\u2014\u2014\u6b64\u524d\u4ec5\u6807\u8bb0\u653e\u884c\u7684
# springboard \u7801\uff0c\u88ab LLM \u5426\u51b3\u7684\u5019\u9009\u4e0d\u8fdb\u8ddf\u8e2a\u8868\uff0c\u5bfc\u81f4 LLM \u4ef7\u503c\u65e0\u6cd5\u8bc4\u4f30\u3002
STEP3_VERDICT_INVALIDATED = "invalidated"
STEP3_VERDICT_BUILDING = "building"
STEP3_VERDICT_SPRINGBOARD = "springboard"


def extract_invalidated_codes(
    report: str,
    allowed_codes: list[str] | set[str] | tuple[str, ...],
) -> list[str]:
    allowed_set = {str(code).strip() for code in allowed_codes if re.fullmatch(r"\d{6}", str(code).strip())}
    if not allowed_set:
        return []
    codes = _extract_markdown_section_codes(
        report,
        allowed_set,
        start_tokens=("逻辑破产", "Invalidated"),
    )
    for code in _extract_structured_section_codes(report, allowed_set, INVALIDATED_POOL_KEYS):
        if code not in codes:
            codes.append(code)
    return codes


def extract_step3_verdicts(
    report: str,
    allowed_codes: list[str] | set[str] | tuple[str, ...],
) -> dict[str, str]:
    """把研报的三个分组解析成 code -> 判定，用于事后评估 LLM 的区分度。

    优先级：逻辑破产 > 起跳板 > 储备营地。同一代码若出现在多个分组（模型偶发
    自相矛盾），按最保守的判定归类，避免把被否决的标的记成放行。

    未在任何分组出现的候选不会出现在返回值里——那既可能是模型漏写，也可能是
    研报生成失败，两者都不应被当作"通过"。
    """
    allowed_set = {str(code).strip() for code in allowed_codes if re.fullmatch(r"\d{6}", str(code).strip())}
    if not allowed_set:
        return {}
    buckets = (
        (STEP3_VERDICT_BUILDING, ("储备营地", "Building Cause"), BUILDING_POOL_KEYS),
        (STEP3_VERDICT_SPRINGBOARD, ("起跳板", "Springboard"), SPRINGBOARD_POOL_KEYS),
        (STEP3_VERDICT_INVALIDATED, ("逻辑破产", "Invalidated"), INVALIDATED_POOL_KEYS),
    )
    verdicts: dict[str, str] = {}
    for verdict, md_tokens, struct_keys in buckets:
        codes = _extract_markdown_section_codes(report, allowed_set, start_tokens=md_tokens)
        for code in _extract_structured_section_codes(report, allowed_set, struct_keys):
            if code not in codes:
                codes.append(code)
        for code in codes:
            verdicts[code] = verdict
    return verdicts


def _extract_markdown_section_codes(
    report: str,
    allowed_codes: set[str],
    *,
    start_tokens: tuple[str, ...],
) -> list[str]:
    in_section = False
    section_level = 0
    codes: list[str] = []
    for raw_line in str(report or "").splitlines():
        line = str(raw_line or "").strip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if any(token.lower() in line.lower() for token in start_tokens):
                in_section = True
                section_level = level
                continue
            # 更深层级的子标题（如储备营地下的「近就绪 / 早期」）不结束当前分组，
            # 否则子标题里的代码会全部漏掉。只有同级或更浅的标题才切出分组。
            if in_section and level <= section_level:
                in_section = False
            continue
        if not in_section:
            continue
        for code in re.findall(r"\b\d{6}\b", line):
            if code in allowed_codes and code not in codes:
                codes.append(code)
    return codes


def _extract_structured_section_codes(report: str, allowed_codes: set[str], keys: tuple[str, ...]) -> list[str]:
    raw = str(report or "").strip()
    for candidate in (raw, extract_json_block(raw)):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        return [
            code
            for item in _collect_structured_items(payload, keys)
            if (code := str(item.get("code") or "").strip()) in allowed_codes
        ]
    return []


def _collect_structured_items(payload: dict, keys: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            out.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            out.append(value)
    return out


def _normalized_structured_item(
    item: dict,
    allowed_codes: set[str],
    code_name: dict[str, str],
    field_map: dict[str, str],
) -> dict[str, str] | None:
    code = str(item.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code) or code not in allowed_codes:
        return None
    normalized = {"code": code, "name": str(item.get("name", "")).strip() or code_name.get(code, code)}
    for source, target in field_map.items():
        normalized[target] = str(item.get(source, "")).strip()
    return normalized


def _normalize_pool_items(
    raw_items: list[dict],
    allowed_codes: set[str],
    code_name: dict[str, str],
    field_map: dict[str, str],
) -> list[dict[str, str]]:
    normalized_items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = _normalized_structured_item(item, allowed_codes, code_name, field_map)
        if normalized is None:
            continue
        code = normalized["code"]
        if code in seen:
            continue
        seen.add(code)
        normalized_items.append(normalized)
    return normalized_items


def _normalize_structured_pool(
    payload: dict,
    allowed_codes: set[str],
    code_name: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    return {
        "watch_pool": _normalize_pool_items(
            _collect_structured_items(payload, WATCH_POOL_KEYS),
            allowed_codes,
            code_name,
            {"reason": "reason", "condition": "condition"},
        ),
        "operation_pool": _normalize_pool_items(
            _collect_structured_items(payload, OPERATION_POOL_KEYS),
            allowed_codes,
            code_name,
            {"action": "action", "reason": "reason", "entry_condition": "entry_condition"},
        ),
    }


def try_parse_structured_report(
    report: str,
    allowed_codes: set[str],
    code_name: dict[str, str],
) -> dict[str, list[dict[str, str]]] | None:
    raw = (report or "").strip()
    if not raw:
        return None
    for candidate in [raw, extract_json_block(raw)]:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        normalized = _normalize_structured_pool(payload, allowed_codes, code_name)
        if normalized["watch_pool"] or normalized["operation_pool"]:
            return normalized
    return None


def extract_ops_codes_from_markdown(report: str, allowed_codes: set[str]) -> list[str]:
    lines = str(report or "").splitlines()
    in_ops_section = False
    ops_codes: list[str] = []
    stop_tokens = ("\u903b\u8f91\u7834\u4ea7", "\u50a8\u5907\u8425\u5730")
    start_tokens = ("\u5904\u4e8e\u8d77\u8df3\u677f",)

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            if any(token in line for token in start_tokens):
                in_ops_section = True
            elif any(token in line for token in stop_tokens):
                in_ops_section = False
        if not in_ops_section:
            continue
        for code in re.findall(r"\b\d{6}\b", line):
            if code in allowed_codes and code not in ops_codes:
                ops_codes.append(code)
    return ops_codes


def _springboard_gate_fields(raw_text: str) -> dict[str, object] | None:
    text = str(raw_text or "").strip()
    gates = {gate for gate in re.findall(r"(?<![A-Za-z])[ABC](?![A-Za-z])", text.upper())}
    ordered = [gate for gate in ("A", "B", "C") if gate in gates]
    if not ordered:
        return None
    combo = "+".join(ordered)
    return {
        "springboard_a": "A" in gates,
        "springboard_b": "B" in gates,
        "springboard_c": "C" in gates,
        "springboard_combo": combo,
        "springboard_grade": combo,
        "springboard_met_count": len(ordered),
        "springboard_evidence": {
            "source": "step3_report",
            "llm_hard_gates": text,
        },
        "springboard_scored": True,
    }


def extract_operation_pool_springboards(
    report: str,
    allowed_codes: list[str] | set[str] | tuple[str, ...],
) -> dict[str, dict[str, object]]:
    allowed_set = {str(code).strip() for code in allowed_codes if re.fullmatch(r"\d{6}", str(code).strip())}
    if not allowed_set:
        return {}
    lines = str(report or "").splitlines()
    in_ops_section = False
    current_code = ""
    out: dict[str, dict[str, object]] = {}
    stop_tokens = ("\u903b\u8f91\u7834\u4ea7", "\u50a8\u5907\u8425\u5730")
    start_tokens = ("\u5904\u4e8e\u8d77\u8df3\u677f",)

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            if any(token in line for token in start_tokens):
                in_ops_section = True
            elif any(token in line for token in stop_tokens):
                in_ops_section = False
        if not in_ops_section:
            continue
        for code in re.findall(r"\b\d{6}\b", line):
            if code in allowed_set:
                current_code = code
        match = re.search(r"满足的硬门槛\s*[:：]\s*(.+)", line)
        if match and current_code in allowed_set:
            fields = _springboard_gate_fields(match.group(1))
            if fields:
                out[current_code] = fields
    return out


def extract_operation_pool_codes(
    report: str,
    allowed_codes: list[str] | set[str] | tuple[str, ...],
) -> list[str]:
    ordered_allowed = [str(code).strip() for code in allowed_codes if re.fullmatch(r"\d{6}", str(code).strip())]
    allowed_set = set(ordered_allowed)
    if not allowed_set:
        return []

    ops_codes = extract_ops_codes_from_markdown(report, allowed_set)
    if not ops_codes:
        code_name = {code: code for code in allowed_set}
        structured = try_parse_structured_report(report=report, allowed_codes=allowed_set, code_name=code_name)
        if structured and structured.get("operation_pool"):
            ops_codes.extend(_structured_operation_codes(structured["operation_pool"], allowed_set, ops_codes))
    return _dedupe_allowed_codes(ops_codes, allowed_set)


def _structured_operation_codes(
    operation_pool: list[dict], allowed_set: set[str], existing_codes: list[str]
) -> list[str]:
    out: list[str] = []
    seen = set(existing_codes)
    for item in operation_pool:
        code = str(item.get("code", "")).strip()
        if code in allowed_set and code not in seen:
            out.append(code)
            seen.add(code)
    return out


def _dedupe_allowed_codes(codes: list[str], allowed_set: set[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if code in allowed_set and code not in seen:
            seen.add(code)
            deduped.append(code)
    return deduped
