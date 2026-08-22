"""应用图标资产的形状约束。

macOS 26 会给所有应用图标**再套一层自己的 squircle**。原图自带白色圆角底板，
于是那层底板缩在系统外形里面 —— 观感是「大灰底 + 小图标」（实测截图确认）。
Apple 论坛也写明：没有合规 AppIcon 资产的第三方应用会回退到灰色背景。

所以 macOS 那份资产**不能自带外形**，而其他平台（不套外形）反而需要底板。
这两条约束方向相反，很容易在某次「统一图标」的改动里被弄反，因此固定下来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

BUILD = Path(__file__).resolve().parents[1] / "desktop" / "build"
CANVAS_TOLERANCE = 0.04


def _bbox_ratio(path: Path) -> tuple[float, float]:
    image = Image.open(path).convert("RGBA")
    width, height = image.size
    box = image.split()[-1].getbbox()
    assert box, f"{path.name} 整张透明"
    return (box[2] - box[0]) / width, (box[3] - box[1]) / height


def test_source_icon_is_kept_for_reproducibility():
    """生成脚本的输入要在仓库里，否则图标改动无法复现。"""
    assert (BUILD / "icon-source.png").is_file()


def test_macos_icon_has_no_self_drawn_plate():
    """核心约束：macOS 资产只给图形，外形由系统画。

    判据是四角透明**且**中心区域没有大面积浅色底 —— 只看四角不够，
    自带底板的那张图四角也是透明的（它画的是圆角方块）。
    """
    image = Image.open(BUILD / "icon-macos.png").convert("RGBA")
    width, height = image.size
    pixels = image.load()

    # 图形与画布边缘之间必须是完全透明的：有底板的话这里是浅色不透明。
    inset = int(width * 0.08)
    for x, y in [(inset, height // 2), (width - inset, height // 2), (width // 2, inset)]:
        assert pixels[x, y][3] == 0, f"({x},{y}) 不透明 —— macOS 资产不该自带底板"


def test_macos_glyph_fills_enough_but_stays_in_safe_area():
    """太小=还是小图标；太大=四角被系统外形裁掉。"""
    w_ratio, h_ratio = _bbox_ratio(BUILD / "icon-macos.png")
    assert 0.70 <= w_ratio <= 0.86, f"横向占比 {w_ratio:.0%} 不在安全区间"
    assert h_ratio <= 0.86, f"纵向占比 {h_ratio:.0%} 过大，四角可能被裁"


def test_macos_glyph_is_centred():
    """偏心在 Dock 里很明显，而且系统外形是居中的。"""
    image = Image.open(BUILD / "icon-macos.png").convert("RGBA")
    width, height = image.size
    left, top, right, bottom = image.split()[-1].getbbox()
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
