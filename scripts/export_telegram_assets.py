"""Create the Telegram branding assets with only the Python standard library.

The renderer is intentionally small and deterministic: it emits SVG source
and a PNG export without a font, browser, network, or AI raster dependency.
The PNG uses a compact built-in bitmap alphabet for the few words shown on the
welcome visual; all user-facing copy still lives in the Telegram caption.
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path
from typing import Final

Color = tuple[int, int, int]

BACKGROUND: Final[Color] = (15, 22, 30)
PANEL: Final[Color] = (27, 38, 49)
PANEL_DARK: Final[Color] = (21, 30, 40)
GRID: Final[Color] = (36, 51, 63)
WHITE: Final[Color] = (242, 247, 244)
MUTED: Final[Color] = (151, 171, 176)
LIME: Final[Color] = (190, 244, 73)
MINT: Final[Color] = (91, 218, 184)
GOLD: Final[Color] = (255, 194, 91)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR: Final[Path] = ROOT / "assets" / "telegram"


# Five-by-seven glyphs keep the export self-contained and make its pixels
# reproducible on Linux, Windows, and CI without depending on installed fonts.
_GLYPHS: Final[dict[str, tuple[str, ...]]] = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "G": ("01111", "10000", "10111", "10001", "10001", "10001", "01111"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("00110", "01010", "10010", "10001", "10001", "10001", "10001"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "В": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Г": ("11111", "10000", "10000", "10000", "10000", "10000", "10000"),
    "Е": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "К": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "Л": ("00110", "01010", "10010", "10001", "10001", "10001", "10001"),
    "О": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "П": ("11111", "10001", "10001", "10001", "10001", "10001", "10001"),
    "Р": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "С": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "Т": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "Ы": ("10001", "10001", "10001", "11101", "10011", "10001", "10001"),
}


class Canvas:
    def __init__(self, width: int, height: int, *, scale: int = 2) -> None:
        self.scale = scale
        self.width = width * scale
        self.height = height * scale
        self._pixels = bytearray(self.width * self.height * 3)

    def _coordinate(self, value: float) -> int:
        return round(value * self.scale)

    def fill(self, color: Color) -> None:
        self._pixels[:] = bytes(color) * (self.width * self.height)

    def _row(self, y: int, start: int, end: int, color: Color) -> None:
        if y < 0 or y >= self.height or start >= end:
            return
        start = max(0, start)
        end = min(self.width, end)
        if start >= end:
            return
        offset = (y * self.width + start) * 3
        self._pixels[offset : offset + (end - start) * 3] = bytes(color) * (end - start)

    def rectangle(self, x0: float, y0: float, x1: float, y1: float, color: Color) -> None:
        left = self._coordinate(min(x0, x1))
        right = self._coordinate(max(x0, x1))
        top = self._coordinate(min(y0, y1))
        bottom = self._coordinate(max(y0, y1))
        for y in range(max(0, top), min(self.height, bottom)):
            self._row(y, left, right, color)

    def rounded_rectangle(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        color: Color,
    ) -> None:
        left = self._coordinate(min(x0, x1))
        right = self._coordinate(max(x0, x1))
        top = self._coordinate(min(y0, y1))
        bottom = self._coordinate(max(y0, y1))
        corner = min(self._coordinate(radius), (right - left) // 2, (bottom - top) // 2)
        for y in range(max(0, top), min(self.height, bottom)):
            if y < top + corner:
                dy = top + corner - y
                dx = math.isqrt(max(0, corner * corner - dy * dy))
                row_left, row_right = left + corner - dx, right - corner + dx
            elif y >= bottom - corner:
                dy = y - (bottom - corner - 1)
                dx = math.isqrt(max(0, corner * corner - dy * dy))
                row_left, row_right = left + corner - dx, right - corner + dx
            else:
                row_left, row_right = left, right
            self._row(y, row_left, row_right, color)

    def circle(self, cx: float, cy: float, radius: float, color: Color) -> None:
        center_x = self._coordinate(cx)
        center_y = self._coordinate(cy)
        radius_px = max(0, self._coordinate(radius))
        radius_sq = radius_px * radius_px
        for y in range(center_y - radius_px, center_y + radius_px + 1):
            dy = y - center_y
            remaining = radius_sq - dy * dy
            if remaining < 0:
                continue
            half_width = math.isqrt(remaining)
            self._row(y, center_x - half_width, center_x + half_width + 1, color)

    def line(self, x0: float, y0: float, x1: float, y1: float, color: Color, width: float) -> None:
        start_x, start_y = self._coordinate(x0), self._coordinate(y0)
        end_x, end_y = self._coordinate(x1), self._coordinate(y1)
        steps = max(abs(end_x - start_x), abs(end_y - start_y), 1)
        radius = width / 2
        for index in range(steps + 1):
            fraction = index / steps
            px = start_x + (end_x - start_x) * fraction
            py = start_y + (end_y - start_y) * fraction
            self.circle(px / self.scale, py / self.scale, radius, color)

    def text(
        self, value: str, x: float, y: float, unit: int, color: Color, *, tracking: int = 1
    ) -> None:
        cursor = x
        for character in value.upper():
            glyph = _GLYPHS.get(character, _GLYPHS[" "])
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        self.rectangle(
                            cursor + column * unit,
                            y + row * unit,
                            cursor + (column + 1) * unit,
                            y + (row + 1) * unit,
                            color,
                        )
            cursor += (5 + tracking) * unit

    def png_bytes(self) -> bytes:
        width = self.width // self.scale
        height = self.height // self.scale
        pixels = bytearray(width * height * 3)
        samples = self.scale * self.scale
        for y in range(height):
            for x in range(width):
                red = green = blue = 0
                for sample_y in range(self.scale):
                    for sample_x in range(self.scale):
                        source = (
                            (y * self.scale + sample_y) * self.width + x * self.scale + sample_x
                        ) * 3
                        red += self._pixels[source]
                        green += self._pixels[source + 1]
                        blue += self._pixels[source + 2]
                target = (y * width + x) * 3
                pixels[target : target + 3] = bytes(
                    (red // samples, green // samples, blue // samples)
                )

        scanlines = b"".join(
            b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3] for row in range(height)
        )

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines, level=9))
            + chunk(b"IEND", b"")
        )


def _draw_check(canvas: Canvas, cx: float, cy: float, size: float, color: Color) -> None:
    canvas.line(cx - size * 0.45, cy, cx - size * 0.08, cy + size * 0.34, color, size * 0.13)
    canvas.line(
        cx - size * 0.08, cy + size * 0.34, cx + size * 0.52, cy - size * 0.38, color, size * 0.13
    )


def _draw_clock(canvas: Canvas, cx: float, cy: float, radius: float, color: Color) -> None:
    canvas.circle(cx, cy, radius, color)
    canvas.circle(cx, cy, radius - 10, PANEL_DARK)
    canvas.line(cx, cy, cx, cy - radius * 0.48, WHITE, 10)
    canvas.line(cx, cy, cx + radius * 0.36, cy + radius * 0.08, WHITE, 10)
    canvas.circle(cx, cy, 9, color)


def _draw_logo(canvas: Canvas, cx: float, cy: float, radius: float) -> None:
    canvas.circle(cx, cy, radius, PANEL)
    canvas.circle(cx, cy, radius - 9, LIME)
    canvas.circle(cx, cy, radius - 19, PANEL_DARK)
    _draw_clock(canvas, cx, cy - 2, radius * 0.57, LIME)
    _draw_check(canvas, cx + radius * 0.20, cy + radius * 0.28, radius * 0.55, WHITE)


def _draw_chip_icon(canvas: Canvas, kind: str, cx: float, cy: float, color: Color) -> None:
    if kind == "text":
        canvas.rounded_rectangle(cx - 14, cy - 11, cx + 15, cy + 10, 6, color)
        canvas.line(cx - 8, cy - 3, cx + 8, cy - 3, PANEL_DARK, 3)
        canvas.line(cx - 8, cy + 4, cx + 4, cy + 4, PANEL_DARK, 3)
    elif kind == "voice":
        for offset, height in ((-12, 12), (-4, 23), (4, 30), (12, 17)):
            canvas.line(cx + offset, cy - height / 2, cx + offset, cy + height / 2, PANEL_DARK, 5)
    else:
        canvas.line(cx - 14, cy - 2, cx - 5, cy - 11, PANEL_DARK, 5)
        canvas.line(cx - 5, cy - 11, cx + 10, cy - 11, PANEL_DARK, 5)
        canvas.line(cx + 10, cy - 11, cx + 14, cy - 3, PANEL_DARK, 5)
        canvas.line(cx + 14, cy + 3, cx + 5, cy + 12, PANEL_DARK, 5)
        canvas.line(cx + 5, cy + 12, cx - 10, cy + 12, PANEL_DARK, 5)
        canvas.line(cx - 10, cy + 12, cx - 14, cy + 4, PANEL_DARK, 5)


def render_welcome() -> Canvas:
    canvas = Canvas(1600, 900, scale=2)
    canvas.fill(BACKGROUND)
    canvas.circle(1510, 15, 325, PANEL_DARK)
    canvas.line(720, 250, 720, 660, GRID, 2)
    canvas.line(70, 660, 1490, 660, GRID, 2)

    _draw_logo(canvas, 135, 145, 74)
    canvas.text("REMINDER BOT", 275, 112, 7, WHITE, tracking=1)
    canvas.rounded_rectangle(278, 202, 535, 211, 4, LIME)

    canvas.rounded_rectangle(840, 70, 1480, 640, 38, PANEL)
    canvas.rounded_rectangle(895, 125, 1425, 585, 28, PANEL_DARK)
    _draw_clock(canvas, 1155, 295, 128, LIME)
    _draw_check(canvas, 1325, 260, 78, MINT)
    canvas.rounded_rectangle(1005, 480, 1300, 500, 10, GRID)
    canvas.rounded_rectangle(1005, 525, 1380, 545, 10, GRID)
    canvas.rounded_rectangle(1005, 570, 1205, 590, 10, GOLD)
    canvas.circle(1340, 490, 14, MINT)
    canvas.circle(1370, 535, 14, LIME)
    canvas.circle(1225, 580, 14, GOLD)

    chips = (("ТЕКСТ", "text", LIME), ("ГОЛОС", "voice", MINT), ("ПОВТОРЫ", "repeat", GOLD))
    for index, (label, kind, color) in enumerate(chips):
        left = 70 + index * 500
        canvas.rounded_rectangle(left, 710, left + 450, 842, 28, PANEL)
        canvas.circle(left + 55, 776, 29, color)
        _draw_chip_icon(canvas, kind, left + 55, 776, color)
        canvas.text(label, left + 105, 752, 6, WHITE, tracking=1)
    return canvas


def render_avatar() -> Canvas:
    canvas = Canvas(1024, 1024, scale=3)
    canvas.fill(BACKGROUND)
    canvas.circle(512, 512, 470, PANEL_DARK)
    canvas.circle(512, 512, 350, LIME)
    canvas.circle(512, 512, 326, BACKGROUND)
    _draw_clock(canvas, 512, 490, 205, LIME)
    _draw_check(canvas, 512, 585, 180, WHITE)
    canvas.circle(512, 490, 13, LIME)
    return canvas


def welcome_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900" role="img" aria-labelledby="title desc">
  <title id="title">Reminder Bot — Текст, голос, повторы</title>
  <desc id="desc">Clean productivity visual with a clock, a completed check mark and three reminder capabilities.</desc>
  <rect width="1600" height="900" fill="#0f161e"/>
  <circle cx="1510" cy="15" r="325" fill="#151e28"/>
  <path d="M720 250V660M70 660H1490" stroke="#24333f" stroke-width="2"/>
  <circle cx="135" cy="145" r="74" fill="#1b2631"/>
  <circle cx="135" cy="145" r="65" fill="#bef449"/>
  <circle cx="135" cy="143" r="55" fill="#151e28"/>
  <circle cx="135" cy="143" r="42" fill="#bef449"/>
  <circle cx="135" cy="143" r="32" fill="#151e28"/>
  <path d="M135 143V120M135 143L150 146" stroke="#f2f7f4" stroke-width="8" stroke-linecap="round"/>
  <path d="M149 162l13 12 27-35" fill="none" stroke="#f2f7f4" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="275" y="160" fill="#f2f7f4" font-family="Arial, sans-serif" font-size="56" font-weight="700" letter-spacing="5">REMINDER BOT</text>
  <rect x="278" y="202" width="257" height="9" rx="4" fill="#bef449"/>
  <rect x="840" y="70" width="640" height="570" rx="38" fill="#1b2631"/>
  <rect x="895" y="125" width="530" height="460" rx="28" fill="#151e28"/>
  <circle cx="1155" cy="295" r="128" fill="#bef449"/>
  <circle cx="1155" cy="295" r="118" fill="#151e28"/>
  <path d="M1155 295V233M1155 295l46 10" stroke="#f2f7f4" stroke-width="10" stroke-linecap="round"/>
  <circle cx="1155" cy="295" r="9" fill="#bef449"/>
  <path d="M1289 260l17 16 36-45" fill="none" stroke="#5bdab8" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="1005" y="480" width="295" height="20" rx="10" fill="#24333f"/>
  <rect x="1005" y="525" width="375" height="20" rx="10" fill="#24333f"/>
  <rect x="1005" y="570" width="200" height="20" rx="10" fill="#ffc25b"/>
  <circle cx="1340" cy="490" r="14" fill="#5bdab8"/>
  <circle cx="1370" cy="535" r="14" fill="#bef449"/>
  <circle cx="1225" cy="580" r="14" fill="#ffc25b"/>
  <g font-family="Arial, sans-serif" font-size="48" font-weight="700" fill="#f2f7f4">
    <rect x="70" y="710" width="450" height="132" rx="28" fill="#1b2631"/>
    <circle cx="125" cy="776" r="29" fill="#bef449"/>
    <text x="175" y="793">ТЕКСТ</text>
    <rect x="570" y="710" width="450" height="132" rx="28" fill="#1b2631"/>
    <circle cx="625" cy="776" r="29" fill="#5bdab8"/>
    <text x="675" y="793">ГОЛОС</text>
    <rect x="1070" y="710" width="450" height="132" rx="28" fill="#1b2631"/>
    <circle cx="1125" cy="776" r="29" fill="#ffc25b"/>
    <text x="1175" y="793">ПОВТОРЫ</text>
  </g>
</svg>
"""


def avatar_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" role="img" aria-labelledby="title desc">
  <title id="title">Reminder Bot icon</title>
  <desc id="desc">A clock with a completed check mark, centered for a circular Telegram avatar crop.</desc>
  <rect width="1024" height="1024" fill="#0f161e"/>
  <circle cx="512" cy="512" r="470" fill="#151e28"/>
  <circle cx="512" cy="512" r="350" fill="#bef449"/>
  <circle cx="512" cy="512" r="326" fill="#0f161e"/>
  <circle cx="512" cy="490" r="205" fill="#bef449"/>
  <circle cx="512" cy="490" r="195" fill="#151e28"/>
  <path d="M512 490V391M512 490l70 15" stroke="#f2f7f4" stroke-width="16" stroke-linecap="round"/>
  <path d="M422 585l35 34 145-174" fill="none" stroke="#f2f7f4" stroke-width="23" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="512" cy="490" r="13" fill="#bef449"/>
</svg>
"""


def export_assets(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "welcome.svg").write_text(welcome_svg(), encoding="utf-8")
    (output_dir / "avatar.svg").write_text(avatar_svg(), encoding="utf-8")
    (output_dir / "welcome.png").write_bytes(render_welcome().png_bytes())
    (output_dir / "avatar.png").write_bytes(render_avatar().png_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    export_assets(args.output_dir)


if __name__ == "__main__":
    main()
