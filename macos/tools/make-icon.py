#!/usr/bin/env python3
"""生成 LocPilot 应用图标（1024×1024 PNG）。

优先用 Pillow（画质更好）；没有 Pillow 时退到纯标准库实现（zlib + struct 手写 PNG），
这样在只装了 Command Line Tools 的机器上 build.sh 依然能产出 AppIcon.icns。
"""
from __future__ import annotations

import sys
from pathlib import Path

SIZE = 1024
BG = (13, 17, 23, 255)
EDGE = (38, 49, 64, 255)
ACCENT = (47, 129, 247, 255)
ACCENT_SOFT = (47, 129, 247, 70)
ROUTE = (63, 185, 80, 255)


def build_pillow(path: Path) -> None:
    from PIL import Image, ImageDraw

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=224, fill=BG)
    draw.rounded_rectangle([6, 6, SIZE - 7, SIZE - 7], radius=218, outline=EDGE, width=6)

    # 路线：一段折线 + 起点/终点
    pts = [(230, 720), (400, 560), (560, 640), (770, 360)]
    draw.line(pts, fill=ROUTE, width=26, joint="curve")
    for point in (pts[0], pts[-1]):
        draw.ellipse([point[0] - 26, point[1] - 26, point[0] + 26, point[1] + 26], fill=BG, outline=ROUTE, width=18)

    # 定位大头针
    cx, cy, r = 620, 372, 150
    draw.ellipse([cx - r - 40, cy - r - 40, cx + r + 40, cy + r + 40], fill=ACCENT_SOFT)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    draw.ellipse([cx - 54, cy - 54, cx + 54, cy + 54], fill=BG)
    pin = [(cx - 108, cy + 118), (cx + 108, cy + 118), (cx, cy + 372)]
    draw.polygon(pin, fill=ACCENT)

    canvas.putalpha(rounded_mask_pillow(SIZE, 224))
    canvas.save(path)


def rounded_mask_pillow(size: int, radius: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


# ---------------------------------------------------------------------------
# 纯标准库回退：没有 Pillow 也能生成图标
# ---------------------------------------------------------------------------
def build_stdlib(path: Path) -> None:
    import math
    import struct
    import zlib

    size = SIZE
    radius = 224
    pts = [(230, 720), (400, 560), (560, 640), (770, 360)]
    widths = (13, 13)                      # 折线半宽（Pillow 版 width=26）
    cx, cy, r = 620, 372, 150
    halo = r + 40
    inner_r = 54
    pin = ((cx - 108, cy + 118), (cx + 108, cy + 118), (cx, cy + 372))

    def in_round_rect(x: float, y: float, rad: int, inset: int = 0) -> bool:
        lo, hi = inset + rad, size - 1 - inset - rad
        dx = lo - x if x < lo else (x - hi if x > hi else 0.0)
        dy = lo - y if y < lo else (y - hi if y > hi else 0.0)
        return dx * dx + dy * dy <= rad * rad

    def in_triangle(x: float, y: float) -> bool:
        (x1, y1), (x2, y2), (x3, y3) = pin
        d1 = (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
        d2 = (x - x3) * (y2 - y3) - (x2 - x3) * (y - y3)
        d3 = (x - x1) * (y3 - y1) - (x3 - x1) * (y - y1)
        return (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0)

    def blend(base, top, alpha: float):
        return tuple(int(base[i] * (1 - alpha) + top[i] * alpha + 0.5) for i in range(3))

    segments = list(zip(pts, pts[1:]))
    soft = blend(BG[:3], ACCENT[:3], 70 / 255.0)
    pixels = bytearray(size * size * 4)

    for y in range(size):
        row = y * size * 4
        for x in range(size):
            i = row + x * 4
            if not in_round_rect(x, y, radius):
                continue                     # 圆角外保持全透明
            color = EDGE[:3] if not in_round_rect(x, y, radius - 6, inset=6) else BG[:3]
            # 路线折线
            if color == BG[:3]:
                for (ax, ay), (bx, by) in segments:
                    dx, dy = bx - ax, by - ay
                    length2 = dx * dx + dy * dy
                    t = 0.0 if not length2 else ((x - ax) * dx + (y - ay) * dy) / length2
                    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                    if math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= widths[0]:
                        color = ROUTE[:3]
                        break
            # 起点 / 终点圆环
            for px_, py_ in (pts[0], pts[-1]):
                d = math.hypot(x - px_, y - py_)
                if d <= 26:
                    color = ROUTE[:3] if d > 13 else BG[:3]
            # 定位大头针（光晕 → 主体 → 内孔 → 针尖）
            d = math.hypot(x - cx, y - cy)
            if d <= halo:
                color = soft
            if d <= r:
                color = ACCENT[:3]
            if d <= inner_r:
                color = BG[:3]
            if in_triangle(x, y):
                color = ACCENT[:3]
            pixels[i] = color[0]
            pixels[i + 1] = color[1]
            pixels[i + 2] = color[2]
            pixels[i + 3] = 255

    raw = bytearray()
    for y in range(size):
        raw.append(0)                        # PNG filter type 0
        raw += pixels[y * size * 4:(y + 1) * size * 4]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def build(path: Path) -> str:
    try:
        build_pillow(path)
        return "pillow"
    except ImportError:
        build_stdlib(path)
        return "stdlib"


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "AppIcon-1024.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    print("icon(" + build(target) + "):", target)
