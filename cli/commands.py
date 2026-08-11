"""命令面板 Provider — 为 Ctrl+P 提供模糊搜索命令列表。"""

from __future__ import annotations

from textual.command import Hit, Hits, Provider


def _skill_commands() -> list[tuple[str, str, str]]:
    from cli.skills import load_skills

    return [(f"/{s.name}", f"run_skill('{s.name}')", s.description) for s in load_skills().values()]


def _template_commands() -> list[tuple[str, str, str]]:
    from cli.prompt_templates import load_prompt_templates

    return [(f"/{t.name}", f"run_template('{t.name}')", t.description) for t in load_prompt_templates().values()]


def _saved_workflow_commands() -> list[tuple[str, str, str]]:
    from cli.workflows.saved import list_saved_workflows

    return [
        (f"/{row.get('name', '')}", f"run_saved_workflow('{row.get('name', '')}')", row.get("label", "saved workflow"))
        for row in list_saved_workflows()
        if row.get("name")
    ]


class WyckoffCommands(Provider):
    """Wyckoff CLI 命令面板。"""

    async def search(self, query: str) -> Hits:
        commands = (
            [
                ("切换模型", "switch_model", "选择当前使用的模型"),
                ("恢复对话", "resume_session", "恢复历史会话"),
                ("分叉会话", "fork_session", "复制当前会话，开启一条新分支"),
                ("导出会话", "export_session", "导出当前会话 transcript"),
                ("新对话", "new_chat", "清空消息开始新对话"),
                ("清屏", "clear_chat", "清空聊天记录"),
                ("模型列表", "list_models", "查看已配置的模型"),
                ("添加模型", "add_model", "配置新的 LLM 模型"),
                ("登录", "start_login", "登录 Wyckoff 账号"),
                ("退出登录", "do_logout", "退出当前账号"),
                ("Token 用量", "show_token", "查看本次会话 Token 用量"),
                ("Prompt 模板", "show_prompt_templates", "查看可复用投研 Prompt 模板"),
                ("Workflow 记录", "show_workflows", "批准/编辑/重启/保存动态 workflow"),
                ("切换主题", "switch_theme", "切换终端配色主题"),
                ("退出", "quit", "退出程序"),
            ]
            + _skill_commands()
            + _template_commands()
            + _saved_workflow_commands()
        )
        matcher = self.matcher(query)
        for name, action, help_text in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(name),
                    partial=action,
                    help=help_text,
                )

    async def discover(self) -> Hits:
        """未输入时显示全部命令。"""
        commands = (
            [
                ("切换模型", "switch_model", "/model"),
                ("恢复对话", "resume_session", "/resume"),
                ("分叉会话", "fork_session", "/fork"),
                ("导出会话", "export_session", "session export"),
                ("新对话", "new_chat", "Ctrl+N"),
                ("清屏", "clear_chat", "Ctrl+L"),
                ("模型列表", "list_models", "/model list"),
                ("添加模型", "add_model", "/model add"),
                ("切换主题", "switch_theme", "/theme"),
                ("Token 用量", "show_token", "/token"),
                ("Prompt 模板", "show_prompt_templates", "/prompt"),
                ("Workflow 记录", "show_workflows", "/workflow approve|reload|restart|pause|stop|save|run"),
                ("退出", "quit", "Ctrl+Q"),
            ]
            + _skill_commands()
            + _template_commands()
            + _saved_workflow_commands()
        )
        for name, action, help_text in commands:
            yield Hit(1.0, name, partial=action, help=help_text)
