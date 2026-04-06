#!/usr/local/bin/python3
"""Generate app icon assets for Homebrew Update Manager.

Outputs:
- build/icon.png  (1024x1024 master)
- build/icon.icns (macOS app icon bundle)
"""

from __future__ import annotations

import math
import struct
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
MASTER_PNG = BUILD / "icon.png"
ICONSET = BUILD / "icon.iconset"
ICNS = BUILD / "icon.icns"

W = 1024
H = 1024


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA buffer size does not match dimensions")

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # PNG filter type None
        start = y * stride
        raw.extend(rgba[start : start + stride])

    png = bytearray()
    png.extend(b"\x89PNG\r\n\x1a\n")
    png.extend(
        _chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
        )
    )
    png.extend(_chunk(b"IDAT", zlib.compress(bytes(raw), level=9)))
    png.extend(_chunk(b"IEND", b""))
    path.write_bytes(bytes(png))


def blend_pixel(buf: bytearray, x: int, y: int, color: tuple[int, int, int, int]) -> None:
    if x < 0 or y < 0 or x >= W or y >= H:
        return

    idx = (y * W + x) * 4
    sr, sg, sb, sa = color
    if sa <= 0:
        return
    if sa >= 255:
        buf[idx : idx + 4] = bytes((sr, sg, sb, 255))
        return

    dr, dg, db, da = buf[idx], buf[idx + 1], buf[idx + 2], buf[idx + 3]
    a = sa / 255.0
    ia = 1.0 - a

    rr = int(sr * a + dr * ia)
    gg = int(sg * a + dg * ia)
    bb = int(sb * a + db * ia)
    aa = int(sa + da * ia)

    buf[idx : idx + 4] = bytes((rr, gg, bb, aa))


def fill_background(buf: bytearray) -> None:
    cx = W / 2
    cy = H / 2
    rmax = math.hypot(cx, cy)

    for y in range(H):
        t = y / (H - 1)
        base_r = int(18 + 30 * t)
        base_g = int(95 + 70 * t)
        base_b = int(72 + 65 * t)
        for x in range(W):
            dx = x - cx
            dy = y - cy
            radial = max(0.0, 1.0 - (math.hypot(dx, dy) / rmax))
            glow = radial**1.7
            r = min(255, base_r + int(30 * glow))
            g = min(255, base_g + int(34 * glow))
            b = min(255, base_b + int(38 * glow))
            i = (y * W + x) * 4
            buf[i : i + 4] = bytes((r, g, b, 255))


def draw_rounded_rect(
    buf: bytearray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)

    min_x = max(0, int(left - radius - 2))
    max_x = min(W - 1, int(right + radius + 2))
    min_y = max(0, int(top - radius - 2))
    max_y = min(H - 1, int(bottom + radius + 2))

    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    hx = (right - left) / 2.0 - radius
    hy = (bottom - top) / 2.0 - radius

    for y in range(min_y, max_y + 1):
        py = abs(y - cy) - hy
        for x in range(min_x, max_x + 1):
            px = abs(x - cx) - hx
            qx = max(px, 0.0)
            qy = max(py, 0.0)
            d = math.hypot(qx, qy) + min(max(px, py), 0.0) - radius

            if d <= -0.5:
                coverage = 1.0
            elif d >= 0.5:
                coverage = 0.0
            else:
                coverage = 0.5 - d

            if coverage <= 0:
                continue

            r, g, b, a = color
            blend_pixel(buf, x, y, (r, g, b, int(a * coverage)))


def draw_circle(
    buf: bytearray,
    cx: float,
    cy: float,
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    min_x = max(0, int(cx - radius - 2))
    max_x = min(W - 1, int(cx + radius + 2))
    min_y = max(0, int(cy - radius - 2))
    max_y = min(H - 1, int(cy + radius + 2))

    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            d = math.hypot(x - cx, y - cy) - radius
            if d <= -0.5:
                coverage = 1.0
            elif d >= 0.5:
                coverage = 0.0
            else:
                coverage = 0.5 - d

            if coverage <= 0:
                continue

            r, g, b, a = color
            blend_pixel(buf, x, y, (r, g, b, int(a * coverage)))


def draw_icon(buf: bytearray) -> None:
    # soft glass card
    draw_rounded_rect(buf, 120, 120, 904, 904, 180, (255, 255, 255, 32))

    # mug body
    draw_rounded_rect(buf, 280, 360, 720, 760, 85, (252, 252, 252, 255))
    # mug top rim
    draw_rounded_rect(buf, 260, 330, 740, 410, 40, (243, 245, 248, 255))
    draw_rounded_rect(buf, 300, 350, 700, 395, 28, (66, 130, 110, 180))

    # mug handle (ring)
    draw_circle(buf, 760, 550, 110, (252, 252, 252, 255))
    draw_circle(buf, 760, 550, 62, (44, 131, 108, 255))

    # upward arrow (update signal)
    draw_rounded_rect(buf, 476, 500, 524, 690, 24, (32, 148, 116, 255))
    draw_rounded_rect(buf, 420, 500, 580, 548, 24, (32, 148, 116, 255))
    draw_rounded_rect(buf, 430, 458, 570, 518, 20, (32, 148, 116, 255))

    # steam lines
    draw_rounded_rect(buf, 380, 210, 430, 340, 24, (236, 245, 245, 215))
    draw_rounded_rect(buf, 495, 180, 545, 340, 24, (236, 245, 245, 215))
    draw_rounded_rect(buf, 610, 210, 660, 340, 24, (236, 245, 245, 215))


def generate_icns(master_png: Path) -> None:
    ICONSET.mkdir(parents=True, exist_ok=True)

    sizes = [16, 32, 128, 256, 512]
    for s in sizes:
        out1 = ICONSET / f"icon_{s}x{s}.png"
        out2 = ICONSET / f"icon_{s}x{s}@2x.png"

        subprocess.run(
            ["sips", "-z", str(s), str(s), str(master_png), "--out", str(out1)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["sips", "-z", str(s * 2), str(s * 2), str(master_png), "--out", str(out2)],
            check=True,
            capture_output=True,
            text=True,
        )

    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)],
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)

    rgba = bytearray(W * H * 4)
    fill_background(rgba)
    draw_icon(rgba)

    write_png(MASTER_PNG, W, H, bytes(rgba))
    generate_icns(MASTER_PNG)

    print(f"Generated: {MASTER_PNG}")
    print(f"Generated: {ICNS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
