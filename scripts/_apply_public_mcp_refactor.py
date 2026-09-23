"""One-shot integration patch, removed before submitting the resulting PR."""

from __future__ import annotations

import re
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ValueError(f"Expected exactly one integration anchor in {path}: {old!r}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "cli/__main__.py",
    'def _cmd_mcp(_args):\n    print("启动 Wyckoff MCP Server ...")\n    print("按 Ctrl+C 停止\\n")',
    'def _cmd_mcp(_args):\n    import sys\n\n    print("启动 Wyckoff MCP Server ...", file=sys.stderr)\n    print("按 Ctrl+C 停止\\n", file=sys.stderr)',
)
replace_once(".github/workflows/ci.yml", 'pip install -e ".[dev]" coverage', 'pip install -e ".[dev,mcp]" coverage')

path = Path("tests/test_mcp_write_guard.py")
text = path.read_text(encoding="utf-8")
assert text.count("class TestMcpEntrypoints:") == 1
text = text.split("class TestMcpEntrypoints:")[0] + '''class TestMcpEntrypoints:
    def test_update_portfolio_refuses_without_touching_data(self):
        from integrations.public_mcp.runtime import Runtime

        runtime = Runtime()
        result = runtime.call("update_portfolio", {"action": "remove", "code": "605007"})
        assert result.is_error and result.data["code"] == "WRITE_DENIED"
        assert runtime._backend is None

    def test_record_trade_fill_refuses_without_touching_data(self):
        from integrations.public_mcp.runtime import Runtime

        runtime = Runtime()
        result = runtime.call("record_trade_fill", {"code": "605007", "side": "sell", "shares": 100, "price": 13.0})
        assert result.is_error and result.data["code"] == "WRITE_DENIED"
        assert runtime._backend is None

    def test_update_portfolio_omitted_free_cash_stays_none(self, monkeypatch):
        from integrations.public_mcp.runtime import Runtime

        captured = {}

        def capture(spec, args):
            captured.update(args)
            return {"error": "set_cash 必须显式传入 free_cash，省略会被当成清零"}

        monkeypatch.setenv("WYCKOFF_MCP_ALLOW_WRITES", "1")
        result = Runtime(capture).call("update_portfolio", {"action": "set_cash"})
        assert "free_cash" in captured and captured["free_cash"] is None
        assert result.is_error and "清零" in result.data["error"]
'''
path.write_text(text, encoding="utf-8")

path = Path("README.md")
text = path.read_text(encoding="utf-8")
text, count = re.subn(r"(?m)^- \*\*MCP Server\*\*.*$", "- **MCP Server** — 19 个公开工具，契约、权限与业务执行分层；[安装与安全边界](docs/PUBLIC_MCP.md)", text)
assert count == 1
anchor = "### 接入外部 MCP server"
assert text.count(anchor) == 1
section = '''### 向外部客户端提供 MCP 工具

```bash
uvx --from 'youngcan-wyckoff-analysis[mcp]' wyckoff-mcp
```

`mcp_server.py` 保留兼容入口；19 个公开工具由 `integrations/public_mcp/` 管理。
握手与工具发现不加载业务模块、不读登录态、不初始化数据库。具体业务仍需要相应依赖和凭证，
持仓写入默认拒绝；不是匿名多租户服务。当前重构不缩减主包安装依赖，合并后还需发布 PyPI 才能通过上述命令获得新版。
完整契约、MCPVault 配置与验证方式见 [PUBLIC_MCP.md](docs/PUBLIC_MCP.md)。

'''
path.write_text(text.replace(anchor, section + anchor, 1), encoding="utf-8")

path = Path("docs/ARCHITECTURE.md")
text = path.read_text(encoding="utf-8")
match = re.search(r"(?m)^(#{1,6}) [^\n]*MCP Server[^\n]*$", text)
assert match is not None, "MCP Server section not found"
level = len(match.group(1))
next_heading = re.search(r"(?m)^#{1," + str(level) + r"} ", text[match.end():])
end = match.end() + next_heading.start() if next_heading else len(text)
replacement = '''

`mcp_server.py` 是兼容入口，对外实现位于 `integrations/public_mcp/`。
工具契约独立于业务导入，`initialize`/`tools/list` 不初始化用户状态或数据库；
调用先校验 JSON Schema 和写权限，再延迟加载生产 `ToolSurface` 与用户上下文。
所有 19 个工具均走同一边界，业务错误用 MCP `isError` 返回，标准输出仅承载协议消息。

这是本地单用户 stdio 接口，不是带 OAuth 的公共 HTTP 服务，也未实现 MCP Tasks。
当前主包依赖图未缩减；延迟导入不能解决安装阶段的磁盘不足。
安装、参数默认值、权限、响应格式、长任务与验证契约的唯一维护位置是
[PUBLIC_MCP.md](PUBLIC_MCP.md)。作为客户端接入第三方 MCP 的实现不在本次重构范围。

'''
path.write_text(text[:match.end()] + replacement + text[end:], encoding="utf-8")

path = Path("GLOSSARY.md")
text = path.read_text(encoding="utf-8")
text = text.replace('`mcp_server.py` 是本项目**作为 server**', '`mcp_server.py`（兼容入口）与 `integrations/public_mcp/`（实现）是本项目**作为 server**')
text += "\n## 对外 MCP 契约\n\n工具发现与业务执行分离；可握手不代表所有工具免认证。写入默认拒绝，业务错误映射为 `isError`。详见 [PUBLIC_MCP.md](docs/PUBLIC_MCP.md)。\n"
path.write_text(text, encoding="utf-8")

path = Path("docs/README_EN.md")
if path.exists():
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^.*MCP.*18.*$", "Public MCP exposes 19 tools; see [the authoritative contract](PUBLIC_MCP.md).", text)
    text += "\n## Public MCP server\n\nSee [PUBLIC_MCP.md](PUBLIC_MCP.md) for installation, the 19-tool contract, lazy discovery, default-deny writes and stdio compatibility. This is separate from the external MCP client. Base package dependencies are not reduced by this refactor.\n"
    path.write_text(text, encoding="utf-8")
