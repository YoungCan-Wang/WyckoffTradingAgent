"""应用图标资产的形状约束。

实机 DMG 验证表明，传统 ICNS 只提供透明前景时会显示成灰色兼容底板。打包资产
必须直接包含亮色 squircle，同时让品牌图形保持足够大且居中。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

BUILD = Path(__file__).resolve().parents[1] / "desktop" / "build"
CANVAS_TOLERANCE = 0.04


def _is_glyph(pixel: tuple[int, int, int, int]) -> bool:
    r, g, b, a = pixel
    dark = a >= 128 and r < 110 and g < 110 and b < 110
    orange = a >= 128 and r > 190 and 70 < g < 150 and b < 110
    return dark or orange


def _glyph_box(path: Path) -> tuple[Image.Image, tuple[int, int, int, int]]:
    image = Image.open(path).convert("RGBA")
    points = [(x, y) for y in range(image.height) for x in range(image.width) if _is_glyph(image.getpixel((x, y)))]
    assert points, f"{path.name} 里没有品牌图形"
    xs, ys = zip(*points, strict=True)
    return image, (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def test_source_icon_is_kept_for_reproducibility():
    """生成脚本的输入要在仓库里，否则图标改动无法复现。"""
    assert (BUILD / "icon-source.png").is_file()


def test_macos_icon_has_bright_squircle_plate():
    """核心约束：亮底要存在，四角要透明，不能退化成灰底或方块。"""
    image = Image.open(BUILD / "icon-macos.png").convert("RGBA")
    width, height = image.size
    pixels = image.load()

    for x, y in [(2, 2), (width - 3, 2), (2, height - 3), (width - 3, height - 3)]:
        assert pixels[x, y][3] == 0, f"({x},{y}) 不透明 —— 图标会显示成方块"

    for x, y in [(width // 2, int(height * 0.11)), (int(width * 0.11), height // 2)]:
        r, g, b, a = pixels[x, y]
        assert a >= 250, f"({x},{y}) 缺少亮色底板"
        assert min(r, g, b) >= 245, f"({x},{y}) 不够亮"


def test_macos_glyph_fills_enough_but_stays_in_safe_area():
    """太小=还是小图标；太大=四角被系统外形裁掉。"""
    image, box = _glyph_box(BUILD / "icon-macos.png")
    w_ratio = (box[2] - box[0]) / image.width
    h_ratio = (box[3] - box[1]) / image.height
    assert 0.58 <= w_ratio <= 0.68, f"横向占比 {w_ratio:.0%} 不在目标区间"
    assert h_ratio <= 0.68, f"纵向占比 {h_ratio:.0%} 过大，四角可能被裁"


def test_macos_glyph_is_centred():
    """偏心在 Dock 里很明显，而且系统外形是居中的。"""
    image, (left, top, right, bottom) = _glyph_box(BUILD / "icon-macos.png")
    width, height = image.size
    assert abs(left - (width - right)) <= width * CANVAS_TOLERANCE, "水平不居中"
    assert abs(top - (height - bottom)) <= height * CANVAS_TOLERANCE, "垂直不居中"


def test_other_platforms_keep_a_plate():
    """Windows / Linux / 旧 macOS 不套外形 —— 没有底板图标会浮在桌面上。

    这条和上面那条方向相反，是刻意的：同一张图不能同时满足两边。
    """
    image = Image.open(BUILD / "icon.png").convert("RGBA")
    width, height = image.size
    pixels = image.load()
    # 图形之外应当有不透明的底板
    assert pixels[width // 2, int(height * 0.11)][3] > 200, "顶部缺少底板"
    assert pixels[int(width * 0.11), height // 2][3] > 200, "左侧缺少底板"
    # 但四角仍要透明（圆角，不是整块方形）
    assert pixels[2, 2][3] == 0, "四角不透明 —— 那是方形而不是圆角"


def test_packager_uses_verified_macos_icon():
    """防止打包配置重新指向只有透明前景、会显示灰底的旧资产。"""
    package = json.loads((BUILD.parent / "package.json").read_text())
    assert package["build"]["mac"]["icon"] == "build/icon-macos.png"


def test_generator_is_deterministic(tmp_path):
    """同一输入两次生成结果一致 —— 否则每次构建都会产生无意义的 diff。"""
    import subprocess
    import sys

    root = BUILD.parents[1]
    for _ in range(2):
        subprocess.run(
            [sys.executable, "scripts/build_app_icon.py", "--out-dir", str(tmp_path)],
            cwd=root,
            check=True,
            capture_output=True,
        )
    first = (tmp_path / "icon-macos.png").read_bytes()
    subprocess.run(
        [sys.executable, "scripts/build_app_icon.py", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    assert (tmp_path / "icon-macos.png").read_bytes() == first
