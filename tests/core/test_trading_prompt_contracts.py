from cli.sub_agent_prompts import ANALYSIS_AGENT_PROMPT, TRADING_AGENT_PROMPT
from core.prompts import (
    CHAT_AGENT_SYSTEM_PROMPT,
    PRIVATE_PM_DECISION_JSON_PROMPT,
    PRIVATE_PM_SYSTEM_PROMPT,
    WYCKOFF_FUNNEL_SYSTEM_PROMPT,
)
from workflows.holding_diagnosis_llm import SYSTEM_PROMPT as HOLDING_SYSTEM_PROMPT


def test_candidate_prompts_preserve_deterministic_mainline_semantics() -> None:
    prompts = (
        WYCKOFF_FUNNEL_SYSTEM_PROMPT,
        PRIVATE_PM_DECISION_JSON_PROMPT,
        CHAT_AGENT_SYSTEM_PROMPT,
        ANALYSIS_AGENT_PROMPT,
        TRADING_AGENT_PROMPT,
    )

    for prompt in prompts:
        assert "candidate_theme" in prompt
        assert "candidate_phase" in prompt
        assert "candidate_role" in prompt
        assert "不得" in prompt


def test_execution_prompts_keep_research_and_order_boundaries_separate() -> None:
    assert "confirmed 仍不等于 BUY" in WYCKOFF_FUNNEL_SYSTEM_PROMPT
    assert "confirmed 都只是研究状态" in PRIVATE_PM_DECISION_JSON_PROMPT
    assert "具体仓位由 OMS 决定" in PRIVATE_PM_DECISION_JSON_PROMPT
    assert "不计算仓位比例" in PRIVATE_PM_SYSTEM_PROMPT
    assert "不计算金额、仓位比例和股数" in TRADING_AGENT_PROMPT
    assert "不能覆盖硬止损" in HOLDING_SYSTEM_PROMPT
