"""报告库的存储层：`~/.wyckoff/reports` 下的 markdown 文件。

## 为什么在 integrations 而不是 cli

路径与读写本来在 `cli/ipc/artifacts.py`（桌面端的产物容器）里。但 `save_report`
是一个 agent 工具，而 `agents/` **不允许依赖 `cli/`** —— 那条边界由
`tests/test_architecture_boundaries.py` 守着，理由是 `mcp_server.py` 和库层要能
在没有 CLI 的环境里独立工作。

我第一版直接从工具里 `from cli.ipc.artifacts import ...`，被那条测试挡下来了。
边界是对的：报告目录是**存储**，不是桌面端的概念 —— CLI、MCP、桌面都可能读写它。
`integrations/chart_annotations.py` 是同一个形状的先例（也是 `~/.wyckoff` 下的
一份存储，被 `annotate_chart` 使用）。

`cli/ipc/artifacts.py` 现在从这里取路径，两边只有一份定义。
"""

from __future__ import annotations

import re
from pathlib import Path

REPORTS_DIR = Path.home() / ".wyckoff" / "reports"


class ReportPathError(Exception):
    """路径不合法或逃出了报告目录。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def reports_dir() -> Path:
    """当前的报告目录。

    刻意每次读模块级变量而不是缓存：测试要能 monkeypatch 掉它，而缓存或
    「导入时复制一份常量」都会让替换失效（我第一版在 cli 侧重导出常量，
    十几个既有测试因此全红 —— 它们打的是重导出的那份副本）。
    """
    return REPORTS_DIR


# 未登录时的分区名。与 desktop 侧 portfolioCache 的约定一致。
ANON_PARTITION = "__anon__"

# 账号目录名允许的字符。user_id 是 UUID，但它来自磁盘上的会话文件 ——
# 不校验就等于让一个被改过的 session 文件决定写到哪里。
_SAFE_PARTITION = re.compile(r"[^0-9A-Za-z._-]+")


def reports_dir_for(user_id: str) -> Path:
    """某个账号的报告目录。

    按账号分子目录：所有产物原来落在同一个根目录，`tool_context` 身份完全
    没用 —— Alice 存的持仓分析，Bob 能直接列出并读出全文（实测复现过）。
    这和之前修过的「持仓缓存按账号分区」是同一类问题。

    未登录用 `__anon__`：给它一个明确的分区，而不是让它落在根目录和历史文件
    混在一起。
    """
    raw = str(user_id or "").strip()
    name = _SAFE_PARTITION.sub("", raw) or ANON_PARTITION
    return reports_dir() / name


def ensure_reports_dir(user_id: str = "") -> Path:
    target = reports_dir_for(user_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_inside_reports(rel_path: str, user_id: str = "") -> Path:
    """把相对路径收敛到**该账号的**报告目录内。

    用 resolve() 后再比对前缀，可同时挡掉 ``..`` 与符号链接逃逸 —— 只检查
    字符串里有没有 ".." 是不够的。报告内容和路径都可能来自模型，所以这层
    不是防御性编程，是必需的。

    收敛到账号分区而不是根目录：否则 Bob 传一个 Alice 分区下的相对路径就能
    读到她的报告 —— 列表隔离挡不住按路径直读。
    """
    raw = (rel_path or "").strip()
    if not raw:
        raise ReportPathError("invalid_path", "缺少文件路径")
    if Path(raw).is_absolute():
        raise ReportPathError("invalid_path", "只接受报告目录内的相对路径")

    root = ensure_reports_dir(user_id).resolve()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ReportPathError("outside_root", "路径超出报告目录")
    return target
