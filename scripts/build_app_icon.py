#!/usr/bin/env python3
"""从主图标抠出图形本体，生成各平台的应用图标资产。

## 为什么需要这一步

`desktop/build/icon.png` 是一张**自带白色圆角方块**的成品图：图形画在一个白色
squircle 上，四角透明。这在 macOS 25 及以前没问题，但 macOS 26 会给所有应用图标
**再套一层自己的 squircle** —— 于是自带的那层白底缩在系统外形里面，观感就是
「大灰底 + 小图标」（实测截图确认）。

Apple 论坛也写明：macOS 26 强制 squircle，没有合规 AppIcon 资产的第三方应用会
回退到灰色背景。所以「把图形放大」只是缓解，根因是**图形不该自带外形**。

## 这个脚本做什么

1. 从原图里识别图形本体（深色折线 + 橙色笔画 + 深色柱条），忽略白色底板
2. 裁到图形的真实边界
3. 按目标占比重新居中放到透明画布上

产出两份：

- `icon-macos.png` —— 无底板、图形占 ~78%，交给 macOS 26 画外形。
  78% 而不是 94%：系统 squircle 会裁掉四角，留白是**必需的安全区**，
  塞满会让图形边缘被切。
- `icon.png`（Windows / Linux / 旧 macOS）—— 保留一个自绘 squircle 底板，
  图形占 ~62%。那些平台不套外形，没有底板会让图标浮在桌面上。

## 为什么不用 Icon Composer 直接生成

Icon Composer 是 GUI 工具，没有可脚本化的 CLI，CI 上跑不了。而且它导出的 PNG
**不带边距**（Apple 自己的文档也提到这点），仍然需要这一步的重新居中。
所以这里生成合规的位图，`.icon` 资产留给需要 Liquid Glass 多层效果时再手工做。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - 只在没装 Pillow 的环境
    print("需要 Pillow：uv pip install pillow", file=sys.stderr)
    raise SystemExit(1) from None

# macOS 26 会在图形外面画 squircle，所以图形只能占安全区。
# 78% 是权衡：再大四角会被系统外形裁到，再小就还是「小图标」。
MACOS_GLYPH_RATIO = 0.78

# 其他平台不套外形，我们自己画底板；图形在底板内的占比。
PLATE_GLYPH_RATIO = 0.62

# 底板颜色。取自原图的白底采样（253, 252, 248）。
PLATE_COLOR = (253, 252, 248, 255)


def _is_glyph(pixel: tuple[int, int, int, int]) -> bool:
    """判断一个像素属于图形本体而不是白色底板。

    只认两类：深色（W 折线与柱状条）和品牌橙。底板的阴影边缘是浅灰，会被排除 ——
    用「非白」当条件会把底板的渐变边也算进来，裁出来的框跟整块底板一样大。
    """
    r, g, b, a = pixel
    if a < 128:
        return False
    dark = r < 110 and g < 110 and b < 110
    orange = r > 190 and 70 < g < 150 and b < 110
    return dark or orange


def extract_glyph(source: Image.Image) -> Image.Image:
    """裁出图形本体，去掉白色底板。"""
    rgba = source.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()

    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        for x in range(width):
            if _is_glyph(pixels[x, y]):
                xs.append(x)
                ys.append(y)
    if not xs:
        raise SystemExit("在源图里找不到图形本体（深色/橙色像素）")

    box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    glyph = rgba.crop(box)

    # 白底连着图形一起被裁进来了，把它抹成透明：图形自身是深色/橙色，
    # 底板是接近白的浅色，按亮度分开即可。
    out = Image.new("RGBA", glyph.size, (0, 0, 0, 0))
    src = glyph.load()
    dst = out.load()
    for y in range(glyph.height):
        for x in range(glyph.width):
            pixel = src[x, y]
            if _is_glyph(pixel):
                dst[x, y] = pixel
            else:
                # 抗锯齿边缘：保留半透明的深色部分，纯白底完全去掉。
                r, g, b, a = pixel
                if a > 0 and (r + g + b) / 3 < 200:
                    dst[x, y] = (r, g, b, a)
    return out


def _fit(glyph: Image.Image, canvas: int, ratio: float) -> tuple[Image.Image, tuple[int, int]]:
    """把图形等比缩放到目标占比，返回缩放后的图和居中坐标。"""
    target = int(canvas * ratio)
    scale = min(target / glyph.width, target / glyph.height)
    size = (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale)))
    resized = glyph.resize(size, Image.LANCZOS)
    offset = ((canvas - size[0]) // 2, (canvas - size[1]) // 2)
    return resized, offset


def render_macos(glyph: Image.Image, canvas: int) -> Image.Image:
    """无底板：macOS 26 自己画 squircle，我们只提供居中的图形。"""
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    resized, offset = _fit(glyph, canvas, MACOS_GLYPH_RATIO)
    out.paste(resized, offset, resized)
    return out


def render_plate(glyph: Image.Image, canvas: int) -> Image.Image:
    """带底板：Windows / Linux / 旧 macOS 不套外形，图标需要自己的形状。"""
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    # squircle 用大圆角矩形近似。真正的 Apple squircle 是超椭圆，
    # 但这份资产是给不套外形的平台用的，圆角矩形已经够。
    margin = round(canvas * 0.055)
    radius = round(canvas * 0.235)
    plate = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        (margin, margin, canvas - margin, canvas - margin),
        radius=radius,
        fill=PLATE_COLOR,
    )
    out.alpha_composite(plate)
    resized, offset = _fit(glyph, canvas, PLATE_GLYPH_RATIO)
    out.paste(resized, offset, resized)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="desktop/build/icon-source.png")
    parser.add_argument("--out-dir", default="desktop/build")
    parser.add_argument("--canvas", type=int, default=1024)
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"找不到源图: {source_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    glyph = extract_glyph(Image.open(source_path))
    print(f"图形本体: {glyph.width}×{glyph.height}")

    mac = render_macos(glyph, args.canvas)
    mac_path = out_dir / "icon-macos.png"
    mac.save(mac_path)
    box = mac.split()[-1].getbbox()
    print(f"{mac_path.name}: 图形占 {(box[2] - box[0]) / args.canvas * 100:.0f}%（无底板）")

    plate = render_plate(glyph, args.canvas)
    plate_path = out_dir / "icon.png"
    plate.save(plate_path)
    print(f"{plate_path.name}: 带底板，图形占 {PLATE_GLYPH_RATIO * 100:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
