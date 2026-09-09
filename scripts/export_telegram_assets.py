"""Export SVG/PNG branding from checked-in vector outlines without dependencies."""

from __future__ import annotations

import argparse
import json
import math
import struct
import zlib
from pathlib import Path

Color = tuple[int, int, int]
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "assets" / "telegram"
INK = (22, 40, 65)
BLUE = (32, 114, 239)
WHITE = (255, 255, 255)
PAPER = (241, 246, 252)
MUTED = (92, 111, 137)
PALE = (225, 237, 254)


def hex_color(color: Color) -> str:
    return "#" + "".join(f"{channel:02x}" for channel in color)


class Canvas:
    def __init__(self, width: int, height: int, *, scale: int = 2) -> None:
        self.scale = scale
        self.width = width * scale
        self.height = height * scale
        self._pixels = bytearray(self.width * self.height * 3)
        self.shapes: list[str] = []
        self._record = True

    def _coordinate(self, value: float) -> int:
        return round(value * self.scale)

    def fill(self, color: Color) -> None:
        self._pixels[:] = bytes(color) * (self.width * self.height)
        self.shapes.append(
            f'<rect width="{self.width // self.scale}" height="{self.height // self.scale}" fill="{hex_color(color)}"/>'
        )

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
        self.shapes.append(
            f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{radius}" fill="{hex_color(color)}"/>'
        )
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
        if self._record:
            self.shapes.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="{hex_color(color)}"/>'
            )
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
        self.shapes.append(
            f'<path d="M{x0} {y0}L{x1} {y1}" fill="none" stroke="{hex_color(color)}" stroke-width="{width}" stroke-linecap="round"/>'
        )
        self._record = False
        start_x, start_y = self._coordinate(x0), self._coordinate(y0)
        end_x, end_y = self._coordinate(x1), self._coordinate(y1)
        steps = max(abs(end_x - start_x), abs(end_y - start_y), 1)
        radius = width / 2
        for index in range(steps + 1):
            fraction = index / steps
            px = start_x + (end_x - start_x) * fraction
            py = start_y + (end_y - start_y) * fraction
            self.circle(px / self.scale, py / self.scale, radius, color)

        self._record = True

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

    def text(self, key: str, x: float, y: float, color: Color) -> None:
        outlines = json.loads(
            (DEFAULT_OUTPUT_DIR / "lettering.json").read_text(encoding="utf-8-sig")
        )
        paths, edges = [], []
        for polygon in outlines[key]:
            points = [(x + p[0], y + p[1]) for p in polygon]
            paths.append("M" + "L".join(f"{px} {py}" for px, py in points) + "Z")
            scaled = [(self._coordinate(px), self._coordinate(py)) for px, py in points]
            edges.extend(zip(scaled, scaled[1:] + scaled[:1], strict=True))
        self.shapes.append(
            f'<path d="{"".join(paths)}" fill="{hex_color(color)}" fill-rule="evenodd"/>'
        )
        for row in range(
            max(0, min(a[1] for a, _ in edges)), min(self.height, max(a[1] for a, _ in edges) + 1)
        ):
            hits = sorted(
                a[0] + (row + 0.5 - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                for a, b in edges
                if min(a[1], b[1]) <= row + 0.5 < max(a[1], b[1])
            )
            for i in range(0, len(hits) - 1, 2):
                self._row(row, math.ceil(hits[i] - 0.5), math.ceil(hits[i + 1] - 0.5), color)

    def svg(self) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width // self.scale}" height="{self.height // self.scale}" role="img"><title>Reminder Bot</title>'
            + "".join(self.shapes)
            + "</svg>\n"
        )


def logo(c: Canvas, x: float, y: float, size: float, color: Color) -> None:
    r = size * 0.32
    for degree in range(0, 360, 2):
        a, b = math.radians(degree), math.radians(degree + 2)
        c.line(
            x + r * math.cos(a),
            y + r * math.sin(a),
            x + r * math.cos(b),
            y + r * math.sin(b),
            color,
            size * 0.062,
        )
    c.line(x - size * 0.12, y - size * 0.12, x, y, color, size * 0.062)
    c.line(x, y, x + size * 0.16, y - size * 0.16, color, size * 0.062)
    c.circle(x, y - size * 0.235, size * 0.026, (164, 224, 255))


def render_welcome() -> Canvas:
    c = Canvas(1600, 900, scale=2)
    c.fill(PAPER)
    c.rounded_rectangle(72, 64, 152, 144, 24, BLUE)
    logo(c, 112, 104, 80, WHITE)
    c.text("brand", 174, 84, INK)
    c.text("headline1", 72, 214, INK)
    c.text("headline2", 72, 322, BLUE)
    c.text("subtitle1", 76, 478, MUTED)
    c.text("subtitle2", 76, 526, MUTED)
    c.rounded_rectangle(76, 638, 365, 702, 32, PALE)
    c.text("voice", 106, 651, BLUE)
    c.rounded_rectangle(835, 199, 1528, 382, 38, BLUE)
    c.text("request1", 875, 232, WHITE)
    c.text("request2", 875, 287, WHITE)
    c.rounded_rectangle(800, 420, 1492, 713, 36, (221, 230, 243))
    c.rounded_rectangle(800, 410, 1492, 703, 36, WHITE)
    c.circle(858, 468, 20, PALE)
    c.line(858, 456, 858, 468, BLUE, 4)
    c.line(858, 468, 867, 473, BLUE, 4)
    c.text("time", 900, 443, BLUE)
    c.text("task", 844, 510, INK)
    c.rounded_rectangle(840, 590, 1452, 663, 22, BLUE)
    c.line(1060, 625, 1070, 635, WHITE, 4)
    c.line(1070, 635, 1088, 615, WHITE, 4)
    c.text("confirm", 1110, 607, WHITE)
    c.text("footer", 76, 797, MUTED)
    return c


def render_avatar() -> Canvas:
    c = Canvas(1024, 1024, scale=2)
    c.fill(BLUE)
    logo(c, 512, 512, 900, WHITE)
    return c


def export_assets(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, canvas in (("welcome", render_welcome()), ("avatar", render_avatar())):
        (output_dir / f"{name}.svg").write_text(canvas.svg(), encoding="utf-8", newline="\n")
        (output_dir / f"{name}.png").write_bytes(canvas.png_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    export_assets(args.output_dir)


if __name__ == "__main__":
    main()
