from __future__ import annotations

from cli.loop_guard import resolve_turn_expectation
from cli.runtime import AgentRuntime, partition_tool_calls
from cli.screen_intent import (
    stock_screen_candidate_request_hint,
    stock_screen_style_target_hint,
    stock_screen_suggested_args,
    stock_screen_temporal_buy_hint,
    stock_screen_watch_hint,
)
from cli.workflows.router import WORKFLOWS
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry


def test_partition_tool_calls_uses_concurrency_metadata():
    calls = [
        {"id": "tc_a", "name": "fast_a", "args": {}},
        {"id": "tc_b", "name": "fast_b", "args": {}},
        {"id": "tc_c", "name": "write_file", "args": {}},
        {"id": "tc_d", "name": "fast_c", "args": {}},
    ]

    batches = partition_tool_calls(calls, {"fast_a", "fast_b", "fast_c"}.__contains__)

    assert [batch["concurrent"] for batch in batches] == [True, False, True]
    assert [[call["name"] for call in batch["calls"]] for batch in batches] == [
        ["fast_a", "fast_b"],
        ["write_file"],
        ["fast_c"],
    ]


def test_runtime_emits_tool_events_and_done():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 10, "output_tokens": 3},
            ],
            [
                {"type": "text_delta", "text": "你当前没有持仓。"},
                {"type": "usage", "input_tokens": 15, "output_tokens": 8},
            ],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [{"role": "user", "content": "我的持仓有什么"}]

    events = list(AgentRuntime(provider, tools).run_stream(messages))

    assert [e["type"] for e in events if e["type"].startswith("tool_")] == ["tool_calls", "tool_start", "tool_result"]
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "你当前没有持仓。"
    assert events[-1]["usage"] == {"input_tokens": 25, "output_tokens": 11}
    assert any(m.get("role") == "tool" and m.get("name") == "portfolio" for m in messages)


def test_runtime_passes_provider_context_window_to_compaction(monkeypatch):
    captured: dict[str, int | None] = {}

    def fake_compact_messages(messages, provider, model_name="", context_window=None, **_kwargs):
        captured["context_window"] = context_window
        return messages, False, None

    monkeypatch.setattr("cli.runtime.compact_messages", fake_compact_messages)
    provider = ScriptedProvider(rounds=[[{"type": "text_delta", "text": "ok"}]])
    provider.context_window = 123_456

    events = list(AgentRuntime(provider, StubToolRegistry()).run_stream([{"role": "user", "content": "hi"}]))

    assert captured["context_window"] == 123_456
    assert events[-1]["type"] == "done"


def test_runtime_does_not_rewrite_inline_tool_result_between_rounds():
    payload = "x" * 1200

    def second_round(messages, _tools, _system_prompt):
        tool_message = next(m for m in messages if m.get("role") == "tool" and m.get("name") == "portfolio")
        assert payload in tool_message["content"]
        return [{"type": "text_delta", "text": "已基于原始工具结果继续。"}]

    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                    "text": "",
                }
            ],
            second_round,
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": [], "payload": payload}})
    messages = [{"role": "user", "content": "看一下持仓"}]

    events = list(AgentRuntime(provider, tools).run_stream(messages))

    assert events[-1]["text"] == "已基于原始工具结果继续。"


def test_runtime_emits_retry_event_when_required_tool_is_skipped():
    provider = ScriptedProvider(
        rounds=[
            [
                {"type": "text_delta", "text": "我先说下计划。"},
                {"type": "usage", "input_tokens": 5, "output_tokens": 4},
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_diag", "name": "portfolio", "args": {"mode": "diagnose"}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 8, "output_tokens": 3},
            ],
            [
                {"type": "text_delta", "text": "体检完成。"},
                {"type": "usage", "input_tokens": 12, "output_tokens": 5},
            ],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [
        {"role": "user", "content": "我的持仓有什么"},
        {"role": "assistant", "content": "你手里现在有 4 张牌。"},
        {"role": "user", "content": "做一下体检"},
    ]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [e for e in events if e["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["required_tool"] == "portfolio"
    assert '建议参数：mode="diagnose"' in retries[0]["message"]
    assert "不要重复计划" in retries[0]["message"]
    assert events[-1]["text"] == "体检完成。"


def test_runtime_accepts_any_portfolio_mode_for_soft_expectation():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "已读取持仓。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [{"role": "user", "content": "账户里这些仓位有什么风险"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    assert not [e for e in events if e["type"] == "retry"]
    assert events[-1]["text"] == "已读取持仓。"


def test_turn_expectation_requires_portfolio_for_read_positions_wording():
    expectation = resolve_turn_expectation([{"role": "user", "content": "读取真实持仓"}])

    assert expectation is not None
    assert expectation.required_tool == "portfolio"
    assert expectation.suggested_args == {"mode": "view"}


def test_runtime_allows_question_before_natural_tool_expectation():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_ask",
                            "name": "ask_user_question",
                            "args": {"question": "你是要看真实持仓，还是只讨论方法？"},
                        }
                    ],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "已读取持仓。"}],
        ]
    )
    tools = StubToolRegistry(
        tool_results={
            "ask_user_question": {"answer": "看真实持仓"},
            "portfolio": {"positions": []},
        }
    )
    messages = [{"role": "user", "content": "你看我持仓呀"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    assert [event["type"] for event in events if event.get("name") == "ask_user_question"] == [
        "tool_start",
        "tool_result",
    ]
    assert not [event for event in events if event["type"] == "tool_error"]
    assert [call["name"] for call in tools.calls] == ["ask_user_question", "portfolio"]
    assert not [event for event in events if event["type"] == "retry"]
    assert events[-1]["text"] == "已读取持仓。"


def test_runtime_blocks_question_before_declared_required_tool():
    def _portfolio_round(messages, _tools, _system_prompt):
        ask_result = next(m for m in messages if m.get("role") == "tool" and m.get("name") == "ask_user_question")
        assert "先不要向用户提问" in ask_result["content"]
        assert "portfolio" in ask_result["content"]
        return [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                "text": "",
            }
        ]

    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_ask",
                            "name": "ask_user_question",
                            "args": {"question": "你现在有持仓吗？"},
                        }
                    ],
                    "text": "",
                }
            ],
            _portfolio_round,
            [{"type": "text_delta", "text": "已读取持仓。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [{"role": "user", "content": "你看我持仓呀"}]

    events = list(
        AgentRuntime(provider, tools, required_tools=("portfolio",), enforce_turn_expectations=True).run_stream(
            messages
        )
    )

    ask_errors = [event for event in events if event["type"] == "tool_error" and event["name"] == "ask_user_question"]
    assert len(ask_errors) == 1
    assert "先不要向用户提问" in ask_errors[0]["error"]
    assert not [event for event in events if event["type"] == "tool_start" and event["name"] == "ask_user_question"]
    assert [call["name"] for call in tools.calls] == ["portfolio"]
    assert events[-1]["text"] == "已读取持仓。"


def test_runtime_leaves_portfolio_typo_diagnosis_to_model_semantics():
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先按最可能语义处理，并说明假设。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [{"role": "user", "content": "给我做磁场诊断"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [event for event in events if event["type"] == "retry"]
    assert retries == []
    assert tools.calls == []
    assert events[-1]["text"] == "我先按最可能语义处理，并说明假设。"


def test_runtime_retries_when_stock_screening_request_skips_tool():
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先给你讲一下选股框架。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "chinext"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "创业板候选已筛出。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"screen_stocks": {"symbols_for_report": ["300750"]}})
    messages = [{"role": "user", "content": "帮我筛选创业板今天有什么好股票"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [e for e in events if e["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["required_tool"] == "screen_stocks"
    assert 'screen_stocks(board="chinext")' in retries[0]["message"]
    assert events[-1]["text"] == "创业板候选已筛出。"


def test_runtime_retries_when_chatty_watchlist_request_skips_screen_tool():
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先说一下筛选思路。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选和理由已生成。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"screen_stocks": {"symbols_for_report": ["300750"]}})
    messages = [{"role": "user", "content": "给我找几只值得复核的票，带理由"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [event for event in events if event["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["required_tool"] == "screen_stocks"
    assert '建议参数：board="all"' in retries[0]["message"]
    assert events[-1]["text"] == "候选和理由已生成。"


def test_runtime_retries_when_required_tool_args_are_missing():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_plain", "name": "screen_stocks", "args": {}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_style", "name": "screen_stocks", "args": {"style": ["trend", "pullback"]}}
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "风格候选已筛出。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"screen_stocks": {"symbols_for_report": ["300750"]}})
    messages = [{"role": "user", "content": "扫描风格候选"}]

    events = list(
        AgentRuntime(
            provider,
            tools,
            allowed_tools=("screen_stocks",),
            required_tools=("screen_stocks",),
            required_tool_args={"screen_stocks": {"style": "trend,pullback"}},
            enforce_turn_expectations=True,
        ).run_stream(messages)
    )

    retries = [event for event in events if event["type"] == "retry"]
    assert len(retries) == 1
    assert 'screen_stocks(style="trend,pullback")' in retries[0]["message"]
    assert [call["args"] for call in tools.calls] == [{}, {"style": ["trend", "pullback"]}]
    assert events[-1]["text"] == "风格候选已筛出。"


def test_runtime_retries_when_natural_screen_args_are_missing():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_plain", "name": "screen_stocks", "args": {}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "tc_screen",
                            "name": "screen_stocks",
                            "args": {
                                "board": "chinext",
                                "style": "trend,pullback",
                                "limit": 0,
                                "financial_metrics": True,
                            },
                        }
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "全量财务筛选已完成。"}],
        ]
    )
    tools = StubToolRegistry(tool_results={"screen_stocks": {"symbols_for_report": ["300750"]}})
    messages = [{"role": "user", "content": "今天全量扫描创业板强势低吸标的，要带财务过滤"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [event for event in events if event["type"] == "retry"]
    assert len(retries) == 1
    assert (
        'screen_stocks(board="chinext", style="trend,pullback", limit="0", financial_metrics="true")'
        in retries[0]["message"]
    )
    assert [call["args"] for call in tools.calls] == [
        {},
        {"board": "chinext", "style": "trend,pullback", "limit": 0, "financial_metrics": True},
    ]
    assert events[-1]["text"] == "全量财务筛选已完成。"


def test_runtime_retries_strategy_when_attack_plan_stops_after_screen():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选已出，风险边界我先口头整理。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "攻防计划已基于工具结果生成。"}],
        ]
    )
    tools = StubToolRegistry(
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"]},
            "generate_strategy_decision": {"status": "skipped_notify_unconfigured", "reviewed_codes": ["300750"]},
        }
    )
    messages = [{"role": "user", "content": "给我找几只值得复核的票，带理由和风险边界"}]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [event for event in events if event["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["required_tool"] == "generate_strategy_decision"
    assert [call["name"] for call in tools.calls] == ["screen_stocks", "generate_strategy_decision"]
    assert events[-1]["text"] == "攻防计划已基于工具结果生成。"


def test_runtime_retries_when_ai_report_followup_skips_tool():
    provider = ScriptedProvider(
        rounds=[
            [{"type": "text_delta", "text": "我先整理研报计划。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_report", "name": "generate_ai_report", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "研报完成。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "generate_ai_report", "description": "r", "parameters": {"type": "object", "properties": {}}},
        ],
        tool_results={"generate_ai_report": {"reviewed_codes": ["300750"]}},
    )
    messages = [
        {
            "role": "assistant",
            "content": (
                "筛选完成。selection_brief status=ready_for_ai_review "
                "tool_handoff=generate_ai_report symbols_for_report=300750"
            ),
        },
        {"role": "user", "content": "继续生成研报"},
    ]

    events = list(AgentRuntime(provider, tools, enforce_turn_expectations=True).run_stream(messages))

    retries = [e for e in events if e["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["required_tool"] == "generate_ai_report"
    assert events[-1]["text"] == "研报完成。"


def test_runtime_does_not_retry_stock_screening_explanation_question():
    provider = ScriptedProvider([[{"type": "text_delta", "text": "这套选股逻辑先看阶段、量价和市场门控。"}]])
    tools = StubToolRegistry(tool_results={"screen_stocks": {"symbols_for_report": ["300750"]}})
    messages = [{"role": "user", "content": "讲讲你的选股逻辑是什么"}]

    events = list(AgentRuntime(provider, tools).run_stream(messages))

    assert not [e for e in events if e["type"] == "retry"]
    assert tools.calls == []
    assert events[-1]["text"] == "这套选股逻辑先看阶段、量价和市场门控。"


def test_turn_expectation_does_not_force_tool_for_concept_questions():
    assert resolve_turn_expectation([{"role": "user", "content": "选股规则是什么"}]) is None
    assert resolve_turn_expectation([{"role": "user", "content": "持仓管理是什么"}]) is None
    assert resolve_turn_expectation([{"role": "user", "content": "机会是什么意思"}]) is None
    assert resolve_turn_expectation([{"role": "user", "content": "强势票是什么意思"}]) is None


def test_turn_expectation_requires_ai_report_after_screen_handoff():
    expectation = resolve_turn_expectation(
        [
            {
                "role": "assistant",
                "content": (
                    "筛选完成。selection_brief status=ready_for_ai_review "
                    "tool_handoff=generate_ai_report symbols_for_report=300750"
                ),
            },
            {"role": "user", "content": "继续生成研报"},
        ]
    )

    assert expectation is not None
    assert expectation.required_tool == "generate_ai_report"
    assert expectation.suggested_args == {}


def test_turn_expectation_requires_direct_ai_report_task():
    expectation = resolve_turn_expectation([{"role": "user", "content": "生成研报"}])

    assert expectation is not None
    assert expectation.required_tool == "generate_ai_report"


def test_turn_expectation_does_not_force_ai_report_for_vague_review_without_context():
    expectation = resolve_turn_expectation([{"role": "user", "content": "继续复核"}])

    assert expectation is None


def test_turn_expectation_does_not_force_ai_report_for_concept_question():
    expectation = resolve_turn_expectation(
        [
            {
                "role": "assistant",
                "content": (
                    "筛选完成。selection_brief status=ready_for_ai_review "
                    "tool_handoff=generate_ai_report symbols_for_report=300750"
                ),
            },
            {"role": "user", "content": "研报是什么"},
        ]
    )

    assert expectation is None


def test_turn_expectation_still_forces_tool_for_concrete_concept_wording():
    screen = resolve_turn_expectation([{"role": "user", "content": "今天怎么选创业板好股票"}])
    opportunity = resolve_turn_expectation([{"role": "user", "content": "今天有什么机会"}])
    portfolio = resolve_turn_expectation([{"role": "user", "content": "解释一下我的持仓风险"}])

    assert screen is not None
    assert screen.required_tool == "screen_stocks"
    assert screen.suggested_args == {"board": "chinext"}
    assert opportunity is not None
    assert opportunity.required_tool == "screen_stocks"
    assert opportunity.suggested_args == {"board": "all"}
    assert portfolio is not None
    assert portfolio.required_tool == "portfolio"
    assert portfolio.suggested_args == {"mode": "diagnose"}


def test_turn_expectation_forces_tool_for_colloquial_style_stock_selection():
    expectation = resolve_turn_expectation([{"role": "user", "content": "今天帮我找几只强势低吸标的，给下一步"}])

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {"board": "all", "style": "trend,pullback"}
    assert expectation.required_args == {"style": "trend,pullback"}


def test_turn_expectation_forces_tool_for_temporal_buy_opportunity_wording():
    for text in ("今天买啥", "现在能买啥", "尾盘能买什么"):
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all"}

    assert stock_screen_temporal_buy_hint("今天买啥") is True
    assert stock_screen_temporal_buy_hint("买啥") is False
    assert resolve_turn_expectation([{"role": "user", "content": "怎么买股票"}]) is None


def test_turn_expectation_forces_tool_for_watch_direction_wording():
    for text in ("明天看什么方向", "今天关注什么板块", "盘中看啥机会", "尾盘关注啥"):
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all"}

    assert stock_screen_watch_hint("明天看什么方向") is True
    assert stock_screen_watch_hint("盘中看啥") is True
    assert stock_screen_watch_hint("今天看啥") is False
    assert resolve_turn_expectation([{"role": "user", "content": "今天看啥电影"}]) is None


def test_turn_expectation_forces_tool_for_candidate_ticket_wording():
    for text in ("今天有什么票", "给我几只票", "今天给我几个标的", "给我几个股票", "今天有啥候选"):
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all"}

    assert stock_screen_candidate_request_hint("今天有什么票") is True
    assert stock_screen_candidate_request_hint("给我几只票") is True
    assert stock_screen_candidate_request_hint("今天有什么电影票") is False
    assert resolve_turn_expectation([{"role": "user", "content": "这几张票怎么样"}]) is None


def test_turn_expectation_infers_quality_style_from_defensive_wording():
    for text in ("今天给我几只稳一点的票", "今天低风险股票有哪些", "今天找几只波动小的票", "别太激进，给我几个标的"):
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all", "style": "quality"}
        assert expectation.required_args == {"style": "quality"}

    assert stock_screen_suggested_args("今天别太激进，给我几个标的", include_default_board=False) == {
        "style": "quality"
    }


def test_turn_expectation_infers_anti_chase_and_low_position_styles():
    cases = (
        ("今天别追高，找几只票", "pullback"),
        ("今天不要高位票，找低位机会", "pullback"),
        ("今天找几只低位刚启动的标的", "trend,pullback"),
        ("今天找几只性价比高的票", "quality"),
    )
    for text, style in cases:
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all", "style": style}
        assert expectation.required_args == {"style": style}

    assert stock_screen_suggested_args("今天别追涨，找没涨太多的标的", include_default_board=False) == {
        "style": "pullback"
    }


def test_turn_expectation_infers_liquidity_bluechip_and_elasticity_styles():
    quality_cases = ("今天找几只成交活跃的票", "今天找几只流动性好的标的", "今天找几只白马股")
    for text in quality_cases:
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all", "style": "quality"}
        assert expectation.required_args == {"style": "quality"}

    elasticity = resolve_turn_expectation([{"role": "user", "content": "今天找几只小盘弹性票"}])
    assert elasticity is not None
    assert elasticity.suggested_args == {"board": "all", "style": "trend"}
    assert elasticity.required_args == {"style": "trend"}


def test_turn_expectation_infers_quality_and_financial_metrics_from_fundamental_wording():
    for text in (
        "今天找几只基本面好的票",
        "今天给我几只财务好的股票",
        "今天业绩好的股票有哪些",
        "今天盈利能力强的标的",
        "今天ROE高的票",
        "今天找几只现金流好的票",
    ):
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {"board": "all", "style": "quality", "financial_metrics": "true"}
        assert expectation.required_args == {"style": "quality", "financial_metrics": "true"}

    assert stock_screen_style_target_hint("今天ROE高的票") is True
    assert stock_screen_suggested_args("今天财务好的股票", include_default_board=False) == {
        "style": "quality",
        "financial_metrics": "true",
    }


def test_turn_expectation_infers_quality_theme_and_financial_metrics_from_dividend_value_wording():
    cases = (
        ("今天高股息红利票有哪些", "红利低波"),
        ("今天价值蓝筹标的有哪些", "价值蓝筹"),
        ("今天找几只大盘蓝筹", "价值蓝筹"),
    )
    for text, theme in cases:
        expectation = resolve_turn_expectation([{"role": "user", "content": text}])

        assert expectation is not None
        assert expectation.required_tool == "screen_stocks"
        assert expectation.suggested_args == {
            "board": "all",
            "style": "quality",
            "financial_metrics": "true",
            "theme": theme,
        }
        assert expectation.required_args == {
            "style": "quality",
            "theme": theme,
            "financial_metrics": "true",
        }


def test_turn_expectation_infers_full_financial_stock_screen_args():
    expectation = resolve_turn_expectation(
        [{"role": "user", "content": "今天全量扫描创业板强势低吸标的，要带财务过滤"}]
    )

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {
        "board": "chinext",
        "style": "trend,pullback",
        "limit": "0",
        "financial_metrics": "true",
    }
    assert expectation.required_args == {
        "style": "trend,pullback",
        "limit": "0",
        "financial_metrics": "true",
        "board": "chinext",
    }


def test_turn_expectation_infers_quick_scan_financial_skip():
    expectation = resolve_turn_expectation([{"role": "user", "content": "今天先快扫几只强势票"}])

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {"board": "all", "style": "trend", "financial_metrics": "false"}
    assert expectation.required_args == {"style": "trend", "financial_metrics": "false"}


def test_turn_expectation_infers_combined_stock_screen_board():
    expectation = resolve_turn_expectation([{"role": "user", "content": "今天帮我筛主板和创业板强势标的"}])
    double_growth = resolve_turn_expectation([{"role": "user", "content": "今天双创低吸机会有哪些"}])
    non_bse = resolve_turn_expectation([{"role": "user", "content": "今天沪深A强势机会有哪些"}])

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {"board": "main_chinext_star", "style": "trend"}
    assert double_growth is not None
    assert double_growth.required_tool == "screen_stocks"
    assert double_growth.suggested_args == {"board": "main_chinext_star", "style": "pullback"}
    assert non_bse is not None
    assert non_bse.required_tool == "screen_stocks"
    assert non_bse.suggested_args == {"board": "main_chinext_star", "style": "trend"}


def test_stock_screen_args_infer_more_combined_a_share_board_phrases():
    assert stock_screen_suggested_args("今天帮我筛主板和科创板强势标的") == {
        "board": "main_chinext_star",
        "style": "trend",
    }
    assert stock_screen_suggested_args("双创低吸机会有哪些") == {
        "board": "main_chinext_star",
        "style": "pullback",
    }
    assert stock_screen_suggested_args("今天A股不含北交所，全量筛选") == {
        "board": "main_chinext_star",
        "limit": "0",
    }
    assert stock_screen_suggested_args("今天A股能买啥", include_default_board=False) == {"board": "all"}


def test_turn_expectation_forces_tool_for_etf_screening_wording():
    expectation = resolve_turn_expectation([{"role": "user", "content": "帮我筛ETF"}])
    concept_question = resolve_turn_expectation([{"role": "user", "content": "ETF是什么"}])

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {"board": "all"}
    assert concept_question is None


def test_turn_expectation_forces_tool_for_theme_screening_wording():
    expectation = resolve_turn_expectation([{"role": "user", "content": "今天机器人机会有哪些"}])
    concept_question = resolve_turn_expectation([{"role": "user", "content": "机器人是什么"}])

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {"board": "all", "theme": "机器人"}
    assert expectation.required_args == {"theme": "机器人"}
    assert concept_question is None


def test_stock_screen_args_infer_theme_preference():
    assert stock_screen_suggested_args("今天半导体强势机会有哪些") == {
        "board": "all",
        "style": "trend",
        "theme": "芯片半导体",
    }


def test_turn_expectation_infers_theme_strength_wording():
    expectation = resolve_turn_expectation([{"role": "user", "content": "今天机器人龙头有哪些"}])
    concept_question = resolve_turn_expectation([{"role": "user", "content": "机器人龙头是什么"}])

    assert expectation is not None
    assert expectation.required_tool == "screen_stocks"
    assert expectation.suggested_args == {"board": "all", "style": "trend", "theme": "机器人"}
    assert expectation.required_args == {"style": "trend", "theme": "机器人"}
    assert concept_question is None


def test_stock_screen_args_infer_strength_wording_as_trend_preference():
    assert stock_screen_suggested_args("今天机器人短线最强机会有哪些") == {
        "board": "all",
        "style": "trend",
        "theme": "机器人",
    }


def test_turn_expectation_does_not_screen_past_recommendation_review():
    expectation = resolve_turn_expectation([{"role": "user", "content": "过去推荐的表现怎么样"}])

    assert expectation is None


def test_turn_expectation_requires_strategy_without_rescreening_existing_candidates():
    expectation = resolve_turn_expectation([{"role": "user", "content": "基于候选 A 制定攻防计划"}])

    assert expectation is not None
    assert expectation.required_tool == "generate_strategy_decision"


def test_turn_expectation_ignores_background_system_notification():
    expectation = resolve_turn_expectation(
        [
            {"role": "user", "content": "帮我筛选创业板今天有什么好股票"},
            {
                "role": "user",
                "content": "[SYSTEM NOTIFICATION - NOT USER INPUT]\n后台筛选完成，候选 300750。",
                "_system_notification": True,
            },
        ]
    )

    assert expectation is None


def test_runtime_answers_all_tool_calls_when_doom_loop_aborts_round():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {"code": "000001"}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc2", "name": "analyze_stock", "args": {"code": "000001"}}],
                    "text": "",
                }
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc3", "name": "analyze_stock", "args": {"code": "000001"}},
                        {"id": "tc4", "name": "portfolio", "args": {"mode": "view"}},
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "已中止。"}],
        ]
    )
    tools = StubToolRegistry(
        tool_results={
            "analyze_stock": {"price": 10.5},
            "portfolio": {"positions": []},
        }
    )
    messages = [{"role": "user", "content": "反复查 000001 后再取附加数据"}]

    events = list(AgentRuntime(provider, tools).run_stream(messages))

    third_assistant = [m for m in messages if m.get("role") == "assistant" and len(m.get("tool_calls", [])) == 2][0]
    tool_call_ids = {call["id"] for call in third_assistant["tool_calls"]}
    answered_ids = {
        m["tool_call_id"] for m in messages if m.get("role") == "tool" and m.get("tool_call_id") in tool_call_ids
    }
    assert answered_ids == tool_call_ids
    assert any(e["type"] == "tool_error" and e["tool_call_id"] == "tc4" for e in events)


def test_runtime_filters_tools_for_workflow_scope():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "diagnose"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "ok"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "portfolio", "description": "p", "parameters": {"type": "object", "properties": {}}},
            {"name": "run_backtest", "description": "b", "parameters": {"type": "object", "properties": {}}},
            {
                "name": "ask_user_question",
                "description": "q",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
    )

    events = list(
        AgentRuntime(provider, tools, workflow=WORKFLOWS["portfolio_review"]).run_stream(
            [{"role": "user", "content": "我的持仓怎么样"}]
        )
    )

    exposed = {schema["name"] for schema in provider.calls[0]["tools"]}
    assert "portfolio" in exposed
    assert "ask_user_question" in exposed
    assert "run_backtest" not in exposed
    assert events[0]["type"] == "workflow_start"
    assert events[0]["workflow"] == "portfolio_review"


def test_runtime_blocks_tool_outside_workflow_scope():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_bt", "name": "run_backtest", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "已拒绝越权工具。"}],
        ]
    )
    tools = StubToolRegistry(
        schemas=[
            {"name": "portfolio", "description": "p", "parameters": {"type": "object", "properties": {}}},
            {"name": "run_backtest", "description": "b", "parameters": {"type": "object", "properties": {}}},
        ]
    )

    events = list(
        AgentRuntime(provider, tools, workflow=WORKFLOWS["portfolio_review"]).run_stream(
            [{"role": "user", "content": "帮我跑回测"}]
        )
    )

    assert tools.calls == []
    assert any(e["type"] == "tool_error" and "workflow" in e["error"] for e in events)
