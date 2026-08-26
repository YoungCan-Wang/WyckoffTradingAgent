"""应用图标资产的形状约束。

两份资产的区别只在**边距**，圆角两份都要：

- `icon-macos.png` —— 圆角铺满画布，**边距为零**。边距是灰边的来源：macOS 26
  在图标下垫一层它自己的（灰色）squircle，比自绘的白底板大，灰就从边距里
  透出来。实测 NSWorkspace 合成结果（Dock 真正画的东西）：留 56px 边距时
  中线灰边 206px，边距归零后 131px。
- `icon.png` —— 圆角 + 5.5% 边距。Windows / Linux 什么都不垫，边距是图标
  自己的呼吸空间。

**别把「去边距」做成「去圆角」。** 实测过：系统不会把 legacy ICNS 位图裁成
自己的外形，去掉圆角会得到一个压在灰 squircle 上的白色方块，比灰边更难看。
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


def test_macos_icon_fills_canvas_without_margin():
    """macOS 那份：圆角铺满画布，**边距为零**。

    边距是灰边的来源 —— 系统的 squircle 比自绘的白底板大，灰从边距里透出来。
    所以四条边的中点必须不透明（贴到画布边缘）。
    """
    image = Image.open(BUILD / "icon-macos.png").convert("RGBA")
    width, height = image.size
    pixels = image.load()

    edge_midpoints = [
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    ]
    for x, y in edge_midpoints:
        r, g, b, a = pixels[x, y]
        assert a == 255, f"({x},{y}) 透明 —— 有边距，系统的灰会从这里透出来"
        assert min(r, g, b) >= 245, f"({x},{y}) 不是亮底"


def test_macos_icon_still_has_rounded_corners():
    """圆角要留住。

    盯的是一个我实测踩过的坑：把边距去掉时顺手把圆角也去了，结果系统**不会**
    把 legacy ICNS 位图裁成自己的外形 —— 白色方块直接压在灰 squircle 上，
    四个硬角比原来的灰边更难看。灰边来自边距，不来自圆角，两件事要分开。
    """
    image = Image.open(BUILD / "icon-macos.png").convert("RGBA")
    width, height = image.size
    pixels = image.load()
    for x, y in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]:
        assert pixels[x, y][3] == 0, f"({x},{y}) 不透明 —— 圆角丢了，会显示成方块"


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

    这条和 macOS 那条方向相反，是刻意的：macOS 要满画布让系统裁，这边要自绘
    圆角。同一张图不能同时满足两边，所以脚本出两份。
    """
    image = Image.open(BUILD / "icon.png").convert("RGBA")
    width, height = image.size
    pixels = image.load()
    # 图形之外应当有不透明的底板
    assert pixels[width // 2, int(height * 0.11)][3] > 200, "顶部缺少底板"
    assert pixels[int(width * 0.11), height // 2][3] > 200, "左侧缺少底板"
    # 但四角仍要透明（圆角，不是整块方形）
    assert pixels[2, 2][3] == 0, "四角不透明 —— 那是方形而不是圆角"


def test_two_assets_are_not_the_same_file():
    """两份资产必须真的不同。

    盯的是一个真实发生过的退化：某次「修复」之后两份图变成了逐字节相同
    （md5 都是 d26d4d12），于是 macOS 拿到的是带透明边距的那版，灰边照旧。
    约束方向相反的两份图长得一样，只能说明有一边被覆盖了。
    """
    mac = (BUILD / "icon-macos.png").read_bytes()
    other = (BUILD / "icon.png").read_bytes()
    assert mac != other, "两份资产完全相同 —— 有一边被覆盖了，灰边会回来"


def test_glyph_size_matches_across_assets():
    """图形的绝对尺寸两份要一致，否则同一个应用在不同平台大小不一。"""
    mac_img, mac_box = _glyph_box(BUILD / "icon-macos.png")
    other_img, other_box = _glyph_box(BUILD / "icon.png")
    assert mac_img.size == other_img.size
    mac_w = mac_box[2] - mac_box[0]
    other_w = other_box[2] - other_box[0]
    # 允许 1px 的缩放取整差异。
    assert abs(mac_w - other_w) <= 1, f"图形宽度不一致: {mac_w} vs {other_w}"


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
