from __future__ import annotations

import textwrap
from pathlib import Path

from cli.prompt_templates import (
    BUILTIN_PROMPT_TEMPLATES,
    _parse_prompt_md,
    load_prompt_templates,
    render_prompt_template,
)


class TestBuiltinPromptTemplates:
    def test_builtins_cover_daily_workflows(self):
        assert {"daily", "review-l4", "holding-risk", "step3-audit", "feishu-summary"}.issubset(
            BUILTIN_PROMPT_TEMPLATES
        )

    def test_builtins_have_descriptions_and_prompts(self):
        for name, template in BUILTIN_PROMPT_TEMPLATES.items():
            assert template.description, f"{name} missing description"
            assert template.prompt, f"{name} missing prompt"

    def test_render_replaces_user_input(self):
        rendered = render_prompt_template(BUILTIN_PROMPT_TEMPLATES["daily"], "重点看半导体")
        assert "重点看半导体" in rendered
        assert "{user_input}" not in rendered

    def test_daily_template_requests_attribution_execution_state(self):
        prompt = BUILTIN_PROMPT_TEMPLATES["daily"].prompt

        assert "latest_source" in prompt
        assert "remote_error" in prompt
        assert "latest_operator_summary" in prompt
        assert "latest_policy_display" in prompt
        assert "latest_execution_summary" in prompt
        assert "raw next_action/promotion_status" in prompt
        assert "promotion_checklist" in prompt
        assert "latest_operations" in prompt
        assert "归因数据来源" in prompt
        assert "归因运营摘要" in prompt
        assert "shadow 最新新增/移除样本" in prompt
        assert "归因调权当前影响范围" in prompt
        assert "dynamic 是否只适合继续 shadow" in prompt


class TestParsePromptMd:
    def test_with_frontmatter(self, tmp_path: Path):
        md = tmp_path / "morning.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            name: morning
            description: 早盘复盘模板
            argument_hint: "[关注行业]"
            ---

            先看大盘，再看 {user_input}
        """),
            encoding="utf-8",
        )

        template = _parse_prompt_md(md)

        assert template is not None
        assert template.name == "morning"
        assert template.description == "早盘复盘模板"
        assert template.argument_hint == "[关注行业]"
        assert "先看大盘" in template.prompt

    def test_without_frontmatter(self, tmp_path: Path):
        md = tmp_path / "quick.md"
        md.write_text("快速复盘 {user_input}", encoding="utf-8")

        template = _parse_prompt_md(md)

        assert template is not None
        assert template.name == "quick"
        assert template.prompt == "快速复盘 {user_input}"

    def test_invalid_name_rejected(self, tmp_path: Path):
        md = tmp_path / "bad'name.md"
        md.write_text("prompt", encoding="utf-8")

        assert _parse_prompt_md(md) is None


class TestLoadPromptTemplates:
    def test_builtin_only(self, monkeypatch):
        monkeypatch.setattr("cli.prompt_templates.PROMPTS_DIR", Path("/nonexistent"))

        templates = load_prompt_templates()

        assert "daily" in templates
        assert len(templates) >= 5

    def test_user_overrides_builtin(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("cli.prompt_templates.PROMPTS_DIR", tmp_path)
        (tmp_path / "daily.md").write_text(
            textwrap.dedent("""\
            ---
            name: daily
            description: 自定义每日模板
            ---
            自定义每日流程 {user_input}
        """),
            encoding="utf-8",
        )

        templates = load_prompt_templates()

        assert templates["daily"].description == "自定义每日模板"
        assert "自定义每日流程" in templates["daily"].prompt
