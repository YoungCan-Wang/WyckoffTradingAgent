from __future__ import annotations

from core.prompts import append_beijing_time_context, beijing_time_context_line, with_current_time


def test_with_current_time_keeps_system_prompt_byte_stable() -> None:
    base = "你是静态 system。"
    assert with_current_time(base) == base
    assert "当前北京时间" not in with_current_time(base)


def test_append_beijing_time_context_is_idempotent() -> None:
    once = append_beijing_time_context("帮我看看持仓")
    assert "帮我看看持仓" in once
    assert "当前北京时间：" in once
    twice = append_beijing_time_context(once)
    assert twice == once
    assert once.count("当前北京时间：") == 1


def test_beijing_time_context_line_format() -> None:
    line = beijing_time_context_line()
    assert line.startswith("当前北京时间：")
    assert "UTC+8" in line
