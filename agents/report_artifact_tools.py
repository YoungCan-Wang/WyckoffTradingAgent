"""把分析结论存成一份报告文件，并在桌面端打开。

命名注意：`report_tools.py` 是**生成 AI 三阵营研报**的那一套（跑模型、算候选）。
这里只做「把已经写好的 markdown 落盘 + 打开」—— 不产生分析，只是产物容器的
写入端。两者放一起会让「report tools」在代码里同时指两件事。

## 为什么需要它

桌面端原来靠 `looksLikeReport()` 猜：正文超过 400 字、且含标题或表格，就当成
报告送去右侧面板。误判漏判都有 —— 一段长的普通回答会被整段收走（对话里只剩
一句提示），而一份短报告会留在对话里。

根因是「这是不是一份报告」该由**产出它的人**声明，而不是由读者事后猜。

## 顺带解决持久化

落盘到 `~/.wyckoff/reports`（报告库同一目录），所以关掉页签能找回来、刷新窗口
和重启应用都还在，且与手动导入的报告走同一套读取渲染路径。

这是「产物跨会话持久化」的一半 —— 报告本来就该是文件。K 线不落盘：它由代码
加标注重建，存一份渲染结果没有意义。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# 单份报告的上限。模型偶尔会把整段行情数据贴进正文，那既没人读也会把报告库
# 撑大。256 KiB 够放一份很长的深度分析。
MAX_REPORT_BYTES = 256 * 1024

# 文件名允许的字符。中文保留 —— 报告库是给人看的。路径逃逸最终由
# resolve_inside_reports 兜底，这里先挡掉明显不合法的，错误信息更清楚。
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z一-鿿 _-]+")


def _slug(title: str) -> str:
    cleaned = _UNSAFE_NAME.sub("", title).strip().strip(".")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:60] or "report"


def save_report(
    title: str,
    markdown: str,
    tool_context: Any = None,
) -> dict[str, Any]:
    """把一份 markdown 报告存进报告库，并在桌面端右侧打开。

    返回相对路径而不是全文：工具结果会进模型上下文，把刚写的报告回灌一遍
    纯属浪费 token，还可能挤掉真实对话历史。前端从产物事件拿正文。
    """
    # 从 integrations 取而不是 cli.ipc —— agents 不允许依赖 cli
    # （见 integrations/report_store.py 顶部说明）。
    from integrations.report_store import ReportPathError, ensure_reports_dir, resolve_inside_reports

    name = str(title or "").strip()
    if not name:
        return {"error": "需要 title"}

    body = str(markdown or "")
    if not body.strip():
        return {"error": "需要 markdown 内容"}

    size = len(body.encode("utf-8"))
    if size > MAX_REPORT_BYTES:
        return {
            "error": (
                f"报告太大（{size // 1024} KiB，上限 {MAX_REPORT_BYTES // 1024} KiB）。"
                "不要把原始行情数据整段贴进正文，只写结论和关键证据。"
            )
        }

    # 文件名带时间戳：同一个标题写两次不该互相覆盖 —— 报告是某个时点的判断，
    # 覆盖掉旧的等于把判断历史抹掉。
    #
    # 秒级精度不够：同一轮里连着存两份（模型分开写多只票的报告）会落在同一秒，
    # 后一份直接盖掉前一份。所以撞名时再补一个序号 —— 用 exists() 循环而不是
    # 毫秒时间戳，因为那只是把碰撞概率变小，没有消除它。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _slug(name)

    try:
        ensure_reports_dir()
        rel = f"{stamp}-{slug}.md"
        target = resolve_inside_reports(rel)
        seq = 2
        while target.exists():
            rel = f"{stamp}-{slug}-{seq}.md"
            target = resolve_inside_reports(rel)
            seq += 1
        target.write_text(body, encoding="utf-8")
    except ReportPathError as exc:
        return {"error": f"报告写入失败: {exc.message}"}
    except OSError as exc:
        logger.warning("save_report write failed", exc_info=True)
        return {"error": f"报告写入失败: {exc}"}

    return {
        "saved": True,
        "title": name,
        "path": rel,
        "bytes": size,
        "note": "报告已存入报告库，并在桌面端右侧打开。关掉页签后可从报告库重新打开。",
    }
