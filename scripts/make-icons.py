#!/usr/bin/env python3
"""Draw Kith's icons: the wordmark's paper and ochre, at every size a browser asks for.

Generated rather than committed as binaries nobody can edit — change the palette, re-run, and
the whole set follows. Nothing here runs at request time; the PNGs it writes are served like
any other static file, so the app keeps its no-build-step promise where it matters.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PAPER = (247, 246, 243)          # --bg, light
OCHRE = (140, 90, 18)            # --accent, the dot in "Kith."
SANS = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
OUT = Path(__file__).resolve().parents[1] / "static" / "icon"
SQUARE = {"kith-192.png": 192, "kith-512.png": 512, "apple-touch-icon.png": 180}


def draw(size: int, *, bleed: bool) -> Image.Image:
    """One icon: "K." on paper. `bleed` fills the square for maskable icons, whose corners
    the platform is allowed to cut off."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ctx = ImageDraw.Draw(image)
    ctx.rounded_rectangle([0, 0, size - 1, size - 1], 0 if bleed else int(size * 0.22), fill=PAPER)
    font = ImageFont.truetype(SANS, int(size * (0.46 if bleed else 0.58)))
    left, top, right, bottom = ctx.textbbox((0, 0), "K.", font=font)
    ctx.text(
        ((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top),
        "K.",
        font=font,
        fill=OCHRE,
    )
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in SQUARE.items():
        draw(size, bleed=False).save(OUT / name)
    draw(512, bleed=True).save(OUT / "kith-maskable-512.png")
    draw(64, bleed=False).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    for path in sorted(OUT.iterdir()):
        print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
