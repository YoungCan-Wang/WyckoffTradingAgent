#!/usr/bin/env python3
"""从主图标抠出图形本体，生成各平台的应用图标资产。

## 为什么需要这一步

`desktop/build/icon-source.png` 是带阴影和圆角底板的品牌母版。打包资产需要先提取
品牌图形，再把它按统一比例放回干净的亮色底板，避免不同平台缩放结果不一致。

实机 DMG 验证还发现：传统 ICNS 只有透明前景时，Finder 会套上灰色兼容底板；
满画布不透明位图又会留下方形内框。这里因此输出四角透明的亮色 squircle，直接
得到用户选中的亮白版本。

## 这个脚本做什么

1. 从原图里识别图形本体（深色折线 + 橙色笔画 + 深色柱条），忽略白色底板
2. 裁到图形的真实边界
3. 按目标占比重新居中放到各平台需要的底板上

产出两份：

- `icon-macos.png` —— 亮色 squircle、图形占 ~62%，供 electron-builder 转 ICNS。
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

# DMG 中选中的亮色版本使用清晰底板和约 62% 的品牌图形。
MACOS_GLYPH_RATIO = 0.62

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
    """亮色 squircle：避免 Finder 为透明前景套灰色兼容底板。"""
    return render_plate(glyph, canvas, MACOS_GLYPH_RATIO)


def render_plate(glyph: Image.Image, canvas: int, ratio: float = PLATE_GLYPH_RATIO) -> Image.Image:
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
    resized, offset = _fit(glyph, canvas, ratio)
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
    print(f"{mac_path.name}: 亮色底板，图形占 {MACOS_GLYPH_RATIO * 100:.0f}%")

    plate = render_plate(glyph, args.canvas)
    plate_path = out_dir / "icon.png"
    plate.save(plate_path)
    print(f"{plate_path.name}: 带底板，图形占 {PLATE_GLYPH_RATIO * 100:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
