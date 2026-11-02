"""Generate the application icon without shipping binary artwork in the repo.

Run from the build script with the project's Python: ``python packaging/windows/make_icon.py``.
The mark is a rounded brand-coloured tile carrying 「聊」, which stays legible
down to 16px because the glyph fills most of the canvas.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BRAND = (0x3E, 0x51, 0x6D, 0xFF)
CANVAS = 256
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
GLYPH = "聊"
FONT_FILES = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "segoeuib.ttf", "arialbd.ttf")
TARGET = Path(__file__).with_name("we-once-talked.ico")


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in FONT_FILES:
        candidate = fonts / name
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


def build() -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=56, fill=BRAND)
    font = load_font(150)
    left, top, right, bottom = draw.textbbox((0, 0), GLYPH, font=font)
    draw.text(
        ((CANVAS - (right - left)) / 2 - left, (CANVAS - (bottom - top)) / 2 - top),
        GLYPH,
        font=font,
        fill=(255, 255, 255, 255),
    )
    return image


def main() -> int:
    build().save(TARGET, format="ICO", sizes=SIZES)
    print(f"已生成图标 {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
