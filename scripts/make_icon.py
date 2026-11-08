# -*- coding: utf-8 -*-
"""生成 icon.png（512x512）：音频波形 + 字幕条。

纯几何绘制，不依赖字体/外部素材。用法：python scripts/make_icon.py
"""
import os

from PIL import Image, ImageDraw

SIZE = 512
TOP = (79, 70, 229)
BOTTOM = (30, 27, 120)
WHITE = (255, 255, 255, 255)
SOFT = (255, 255, 255, 210)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rounded_gradient(size=SIZE, radius=112):
    base = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(base)
    for y in range(size):
        t = y / float(size - 1)
        draw.line([(0, y), (size, y)],
                  fill=tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255)
    canvas.paste(base, (0, 0), mask)
    return canvas


def draw_waveform(draw):
    heights = [46, 78, 118, 168, 212, 168, 118, 78, 46]
    center_y = 196
    start_x = 112
    gap = 36
    width = 20
    for index, height in enumerate(heights):
        x = start_x + index * gap
        draw.rounded_rectangle(
            [x, center_y - height // 2, x + width, center_y + height // 2],
            radius=width // 2, fill=WHITE)


def draw_subtitles(draw):
    box = [92, 322, 420, 430]
    draw.rounded_rectangle(box, radius=30, fill=(255, 255, 255, 36),
                           outline=(255, 255, 255, 110), width=4)
    draw.rounded_rectangle([124, 350, 356, 366], radius=8, fill=SOFT)
    draw.rounded_rectangle([124, 386, 388, 402], radius=8, fill=SOFT)


def build(out_path):
    canvas = rounded_gradient()
    draw = ImageDraw.Draw(canvas)
    draw_waveform(draw)
    draw_subtitles(draw)
    canvas.save(out_path, "PNG")
    return out_path


def main():
    icon = os.path.join(REPO_ROOT, "icon.png")
    build(icon)
    source = os.path.join(REPO_ROOT, "assets", "icon_source.png")
    os.makedirs(os.path.dirname(source), exist_ok=True)
    build(source)
    print("wrote %s (%d bytes)" % (icon, os.path.getsize(icon)))


if __name__ == "__main__":
    main()
