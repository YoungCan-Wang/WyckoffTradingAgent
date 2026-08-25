"""产物容器的文件层。

重点是路径收敛：报告内容与路径都可能来自模型，任何逃逸都等于任意文件读取。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cli.ipc import artifacts as art
from integrations import report_store as _store


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    # 路径的 owner 是存储层（agents 不能依赖 cli，所以两边共用它）
    monkeypatch.setattr(_store, "REPORTS_DIR", root)
    # 返回**分区**目录：产物现在按账号分子目录，这些测试不传 user_id
    # 所以落在 __anon__ 下。逃逸校验的基准也是分区目录 ——
    # 那正是「Bob 传 Alice 分区下的相对路径」被挡住的原因。
    scoped = root / _store.ANON_PARTITION
    scoped.mkdir(parents=True, exist_ok=True)
    return scoped


class TestPathContainment:
    @pytest.mark.parametrize(
        "evil",
        [
            "../../../etc/passwd",
            "../secrets.json",
            "sub/../../outside.md",
            "./../../../.ssh/id_rsa",
        ],
    )
    def test_rejects_traversal(self, reports: Path, evil: str) -> None:
        with pytest.raises(art.ArtifactError) as excinfo:
            art.resolve_inside_reports(evil)
        assert excinfo.value.code == "outside_root"

    def test_rejects_absolute_path(self, reports: Path) -> None:
        with pytest.raises(art.ArtifactError) as excinfo:
            art.resolve_inside_reports("/etc/passwd")
        assert excinfo.value.code == "invalid_path"

    def test_rejects_empty(self, reports: Path) -> None:
        with pytest.raises(art.ArtifactError) as excinfo:
            art.resolve_inside_reports("  ")
        assert excinfo.value.code == "invalid_path"

    def test_rejects_symlink_escape(self, reports: Path, tmp_path: Path) -> None:
        """只查字符串里的 '..' 挡不住符号链接，必须比对 resolve() 后的真实路径。"""
        secret = tmp_path / "outside.md"
        secret.write_text("secret", encoding="utf-8")
        (reports / "link.md").symlink_to(secret)
        with pytest.raises(art.ArtifactError) as excinfo:
            art.resolve_inside_reports("link.md")
        assert excinfo.value.code == "outside_root"

    def test_allows_nested_relative(self, reports: Path) -> None:
        (reports / "sub").mkdir()
        target = reports / "sub" / "a.md"
        target.write_text("ok", encoding="utf-8")
        assert art.resolve_inside_reports("sub/a.md") == target.resolve()


class TestListing:
    def test_sorted_newest_first(self, reports: Path) -> None:
        import os
        import time

        for index, name in enumerate(["old.md", "mid.md", "new.md"]):
            path = reports / name
            path.write_text("x", encoding="utf-8")
            os.utime(path, (time.time() + index, time.time() + index))
        assert [a.name for a in art.list_artifacts()] == ["new.md", "mid.md", "old.md"]

    def test_skips_hidden_and_dirs(self, reports: Path) -> None:
        (reports / ".hidden.md").write_text("x", encoding="utf-8")
        (reports / "adir").mkdir()
        (reports / "real.md").write_text("x", encoding="utf-8")
        assert [a.name for a in art.list_artifacts()] == ["real.md"]

    def test_labels_kinds(self, reports: Path) -> None:
        for name in ["a.md", "b.html", "c.pdf", "d.txt", "e.docx"]:
            (reports / name).write_bytes(b"x")
        kinds = {a.name: a.kind for a in art.list_artifacts()}
        assert kinds == {
            "a.md": "markdown",
            "b.html": "html",
            "c.pdf": "pdf",
            "d.txt": "text",
            "e.docx": "unsupported",
        }


class TestReading:
    def test_reads_markdown_as_text(self, reports: Path) -> None:
        (reports / "r.md").write_text("# 标题", encoding="utf-8")
        out = art.read_artifact("r.md")
        assert out["kind"] == "markdown"
        assert out["encoding"] == "utf-8"
        assert out["content"] == "# 标题"

    def test_reads_pdf_as_base64(self, reports: Path) -> None:
        (reports / "r.pdf").write_bytes(b"%PDF-1.4 fake")
        out = art.read_artifact("r.pdf")
        assert out["encoding"] == "base64"
        assert base64.b64decode(str(out["content"])) == b"%PDF-1.4 fake"

    def test_rejects_unsupported_kind(self, reports: Path) -> None:
        (reports / "r.docx").write_bytes(b"x")
        with pytest.raises(art.ArtifactError) as excinfo:
            art.read_artifact("r.docx")
        assert excinfo.value.code == "unsupported"

    def test_rejects_oversized_text(self, reports: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(art, "MAX_TEXT_BYTES", 10)
        (reports / "big.md").write_text("x" * 50, encoding="utf-8")
        with pytest.raises(art.ArtifactError) as excinfo:
            art.read_artifact("big.md")
        assert excinfo.value.code == "too_large"

    def test_survives_invalid_utf8(self, reports: Path) -> None:
        """模型生成的文本可能含非法字节，应该照样能看而不是整个失败。"""
        (reports / "bad.md").write_bytes(b"ok \xff\xfe tail")
        assert "ok" in str(art.read_artifact("bad.md")["content"])

    def test_missing_file(self, reports: Path) -> None:
        with pytest.raises(art.ArtifactError) as excinfo:
            art.read_artifact("nope.md")
        assert excinfo.value.code == "not_found"


class TestImport:
    def test_copies_into_reports(self, reports: Path, tmp_path: Path) -> None:
        src = tmp_path / "outside.md"
        src.write_text("hello", encoding="utf-8")
        result = art.import_file(str(src))
        assert (reports / result.rel_path).read_text(encoding="utf-8") == "hello"
        # 原文件保留：这是复制，不是移动。
        assert src.exists()

    def test_does_not_overwrite_same_name(self, reports: Path, tmp_path: Path) -> None:
        (reports / "a.md").write_text("original", encoding="utf-8")
        src = tmp_path / "a.md"
        src.write_text("incoming", encoding="utf-8")
        result = art.import_file(str(src))
        assert result.rel_path == "a-2.md"
        assert (reports / "a.md").read_text(encoding="utf-8") == "original"

    def test_rejects_unsupported(self, reports: Path, tmp_path: Path) -> None:
        src = tmp_path / "thing.exe"
        src.write_bytes(b"MZ")
        with pytest.raises(art.ArtifactError) as excinfo:
            art.import_file(str(src))
        assert excinfo.value.code == "unsupported"

    def test_rejects_oversized_import(self, reports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(art, "MAX_TEXT_BYTES", 10)
        src = tmp_path / "large.md"
        src.write_bytes(b"x" * 11)

        with pytest.raises(art.ArtifactError) as excinfo:
            art.import_file(str(src))

        assert excinfo.value.code == "too_large"
        assert not (reports / "large.md").exists()

    def test_rejects_missing_source(self, reports: Path) -> None:
        with pytest.raises(art.ArtifactError) as excinfo:
            art.import_file("/nonexistent/x.md")
        assert excinfo.value.code == "not_found"


class TestLegacyRootFiles:
    """加账号隔离之前落在根目录的报告，必须「列得出来也读得开」。

    这是实测踩到的 bug：`list_artifacts` 同时扫账号分区和根目录，`rel_path` 是
    相对**根目录**算的；而 `resolve_inside_reports` 收敛到**账号分区**。两个基准
    不一致，于是根目录那些历史文件在报告页能看见，点开只有一句「无法读取该文件」。
    """

    def test_lists_and_reads_legacy_root_file(self, reports: Path) -> None:
        root = _store.reports_dir()
        (root / "legacy.md").write_text("历史报告", encoding="utf-8")

        listed = art.list_artifacts()
        rels = [a.rel_path for a in listed]
        assert "legacy.md" in rels, f"列表里没有历史文件: {rels}"

        # 关键：**用列表给出的 rel_path** 去读。分别测两个函数会漏掉这个
        # 不一致，因为它只在「列表的基准」和「读取的基准」之间才暴露。
        payload = art.read_artifact("legacy.md")
        assert payload["content"] == "历史报告"

    def test_scoped_file_still_wins(self, reports: Path) -> None:
        """同名时优先账号分区 —— 自己的文件不该被根目录的历史文件盖掉。"""
        (_store.reports_dir() / "dup.md").write_text("根目录的", encoding="utf-8")
        (reports / "dup.md").write_text("我自己的", encoding="utf-8")
        assert art.read_artifact("dup.md")["content"] == "我自己的"

    def test_legacy_fallback_is_not_a_cross_account_hole(self, reports: Path) -> None:
        """兜底只放行根目录下的直接文件，别人的分区仍然读不到。

        这条是这个修复的护栏：如果兜底写成「找不到就去根目录拼一下」，
        `<别人的uid>/x.md` 就会被解析成真实路径 —— 那等于把当初分区要解决的
        越权读回原样。
        """
        other = _store.reports_dir() / "other-account"
        other.mkdir()
        (other / "secret.md").write_text("别人的私有报告", encoding="utf-8")

        with pytest.raises(art.ArtifactError) as excinfo:
            art.read_artifact("other-account/secret.md")
        assert excinfo.value.code == "not_found"

    @pytest.mark.parametrize(
        "evil",
        ["../other/secret.md", "sub/../../etc/passwd", "../../outside.md"],
    )
    def test_read_still_rejects_traversal(self, reports: Path, evil: str) -> None:
        """兜底不能顺带放开 `..` —— 读取路径同样来自前端。"""
        with pytest.raises(art.ArtifactError):
            art.read_artifact(evil)
