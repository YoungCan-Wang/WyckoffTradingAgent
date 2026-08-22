"""产物容器的文件层。

AI 生成的报告落在 ``~/.wyckoff/reports``，桌面端从这里列出并读取。所有路径
都必须收敛回这个根目录：报告内容是模型生成的，路径也可能来自前端，任何
``..`` 逃逸都会变成任意文件读取。
"""

from __future__ import annotations

import base64
import shutil
from dataclasses import dataclass
from pathlib import Path

from integrations import report_store as _store

# 路径与解析的**唯一** owner 是 integrations/report_store.py。
#
# 报告目录是存储，CLI / MCP / 桌面都可能读写它，不该由桌面端的产物容器定义；
# 而且 agents/ 不允许依赖 cli/（架构边界测试守着），save_report 这个工具必须能
# 从库层拿到同一份路径逻辑。
#
# 这里刻意**不**再定义 REPORTS_DIR：两处各持一份常量时，monkeypatch 只改到一份，
# 于是「测试看起来在改路径、实际读的还是真实家目录」。要覆盖路径请打
# integrations.report_store.REPORTS_DIR。

# 能在容器里渲染的类型。Word 需要额外解析库，暂不声明支持——列出但标记
# 为不可预览，比渲染出一堆乱码好。
KIND_BY_SUFFIX = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".txt": "text",
    ".log": "text",
    ".json": "text",
    ".csv": "text",
}

# 单个文件的读取上限。PDF 走 base64，膨胀约 33%，太大会把 IPC 管道撑爆。
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_BINARY_BYTES = 12 * 1024 * 1024


class ArtifactError(Exception):
    """路径非法或文件不可读。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Artifact:
    name: str
    rel_path: str
    kind: str
    size: int
    modified_at: float


def ensure_reports_dir() -> Path:
    return _store.ensure_reports_dir()


def kind_for(path: Path) -> str:
    return KIND_BY_SUFFIX.get(path.suffix.lower(), "unsupported")


def resolve_inside_reports(rel_path: str) -> Path:
    """把前端给的相对路径收敛到报告目录内。

    实现在 integrations/report_store.py —— 两处各写一份路径校验，迟早会有一处
    漏掉符号链接或 ".."。这里只把异常类型翻成本模块的 ArtifactError，让既有
    调用点（IPC 方法）不必认识存储层的异常。
    """
    try:
        return _store.resolve_inside_reports(rel_path)
    except _store.ReportPathError as exc:
        raise ArtifactError(exc.code, exc.message) from exc


def list_artifacts() -> list[Artifact]:
    """按修改时间倒序列出报告。最新生成的排最前。"""
    root = ensure_reports_dir()
    items: list[Artifact] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # 列目录期间被删掉，跳过而不是整个失败
        items.append(
            Artifact(
                name=path.name,
                rel_path=str(path.relative_to(root)),
                kind=kind_for(path),
                size=stat.st_size,
                modified_at=stat.st_mtime,
            )
        )
    items.sort(key=lambda a: a.modified_at, reverse=True)
    return items


def read_artifact(rel_path: str) -> dict[str, object]:
    """读取单个报告。文本原样返回，PDF 返回 base64。"""
    path = resolve_inside_reports(rel_path)
    if not path.is_file():
        raise ArtifactError("not_found", f"找不到文件: {rel_path}")

    kind = kind_for(path)
    size = path.stat().st_size

    if kind == "unsupported":
        raise ArtifactError("unsupported", f"暂不支持预览 {path.suffix} 文件")

    if kind == "pdf":
        if size > MAX_BINARY_BYTES:
            raise ArtifactError("too_large", f"文件过大（{size // 1024 // 1024}MB），无法预览")
        return {
            "kind": kind,
            "name": path.name,
            "rel_path": rel_path,
            "encoding": "base64",
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    if size > MAX_TEXT_BYTES:
        raise ArtifactError("too_large", f"文件过大（{size // 1024}KB），无法预览")
    # 模型生成的文本可能含非法字节；用 replace 而不是抛错，让用户至少能看。
    return {
        "kind": kind,
        "name": path.name,
        "rel_path": rel_path,
        "encoding": "utf-8",
        "content": path.read_text(encoding="utf-8", errors="replace"),
    }


def import_file(source: str) -> Artifact:
    """把外部文件复制进报告目录。

    复制而非引用：容器只信任报告目录内的文件，这样预览路径永远只有一条，
    也不会因为原文件被移动而失效。
    """
    src = Path(source).expanduser()
    if not src.is_file():
        raise ArtifactError("not_found", f"找不到文件: {source}")
    kind = kind_for(src)
    if kind == "unsupported":
        raise ArtifactError("unsupported", f"暂不支持 {src.suffix} 文件")
    size = src.stat().st_size
    limit = MAX_BINARY_BYTES if kind == "pdf" else MAX_TEXT_BYTES
    if size > limit:
        raise ArtifactError("too_large", "文件过大，无法导入预览")

    root = ensure_reports_dir()
    target = root / src.name
    # 不覆盖同名文件：拖进来的报告可能和已有的重名但内容不同。
    if target.exists():
        stem, suffix = src.stem, src.suffix
        for index in range(2, 1000):
            candidate = root / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                target = candidate
                break
        else:
            raise ArtifactError("name_conflict", "同名文件过多，请先清理报告目录")

    # 流式复制，避免先把整个文件读进常驻 IPC 进程的内存。
    shutil.copy2(src, target)
    stat = target.stat()
    return Artifact(
        name=target.name,
        rel_path=str(target.relative_to(root)),
        kind=kind_for(target),
        size=stat.st_size,
        modified_at=stat.st_mtime,
    )
