#!/usr/bin/env python
"""Generate wine-db favicon / app icons from the emoji used as the app logo.

The brand mark in the UI is U+1F377 WINE GLASS (static/index.html `.brand-mark`,
rendered by the visitor's emoji font). These icons rasterize that same glyph with
Noto Color Emoji so every browser gets the identical artwork instead of whatever
emoji font it happens to ship.

Outputs (all in static/assets/, served at /assets/):
  favicon.ico           16/32/48, transparent - browser tabs
  apple-touch-icon.png  180, brand tile        - iOS home screen (must be opaque)
  icon-192.png          192, brand tile        - manifest / Android
  icon-512.png          512, brand tile        - manifest / Android install

Run from the repo root:  .venv/bin/python scripts/make_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

GLYPH = "\U0001F377"  # 🍷 wine glass - the app logo
FONT_PATH = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
# Noto Color Emoji is a CBDT/CBLC bitmap font with a single embedded strike;
# Pillow only renders it at that native size (larger sizes come out blank).
STRIKE = 109
# Home-screen tile background. Must NOT be the deep wine brand colour: the
# emoji's own liquid is a dark red (~#7a1f2b) that would vanish into #7b2d3e,
# leaving an outline-only glass. Use the light surface the logo sits on in the
# UI instead (--bg #f6f3ef, also the manifest background_color) - the emoji is
# drawn for light backgrounds, so this renders exactly as the user sees it.
TILE_BG = (246, 243, 239, 255)  # --bg  #f6f3ef
OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "assets"


def render_glyph() -> Image.Image:
    """Rasterize the wine-glass glyph and crop to its ink."""
    font = ImageFont.truetype(FONT_PATH, STRIKE)
    canvas = Image.new("RGBA", (STRIKE * 2, STRIKE * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((STRIKE // 2, STRIKE // 2), GLYPH, font=font,
                                embedded_color=True)
    bbox = canvas.getbbox()
    if bbox is None:
        raise SystemExit("FAILED: the glyph rendered nothing (font/tofu problem)")
    glyph = canvas.crop(bbox)

    # Sanity-check that we got real artwork rather than a blank/outline box:
    # the wine glass is mostly coloured (deep red liquid + grey bowl).
    raw = glyph.tobytes()  # flat RGBA
    total = len(raw) // 4
    opaque = 0
    reddish = 0
    for i in range(0, len(raw), 4):
        a = raw[i + 3]
        if a > 40:
            opaque += 1
            r, g, b = raw[i], raw[i + 1], raw[i + 2]
            if r > g + 25 and r > b + 15:
                reddish += 1
    if opaque < 0.15 * total:
        raise SystemExit(f"FAILED: only {opaque}/{total} inked pixels")
    if reddish < 0.03 * opaque:
        raise SystemExit("FAILED: no wine-red pixels - wrong glyph?")
    print(f"glyph {glyph.size} inked={opaque}/{total} "
          f"reddish={reddish} ({100 * reddish / opaque:.1f}% of ink)")
    return glyph


def on_tile(glyph: Image.Image, size: int, *, bg: tuple | None,
            glyph_ratio: float) -> Image.Image:
    """Centre the glyph on a square canvas, optionally over a solid brand tile."""
    canvas = Image.new("RGBA", (size, size), bg if bg else (0, 0, 0, 0))
    target = max(1, int(size * glyph_ratio))
    scale = min(target / glyph.width, target / glyph.height)
    art = glyph.resize(
        (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas.alpha_composite(art, ((size - art.width) // 2, (size - art.height) // 2))
    return canvas


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    glyph = render_glyph()

    # Tabs: bare glyph on transparency, nearly edge-to-edge (legible at 16px).
    # Pillow's ICO writer re-resizes the source per size, so give it the largest
    # frame and let it downscale.
    ico = on_tile(glyph, 48, bg=None, glyph_ratio=1.0)
    ico.save(OUT_DIR / "favicon.ico", format="ICO",
             sizes=[(16, 16), (32, 32), (48, 48)])

    # Home screen / install: opaque tile (transparent icons look broken on iOS),
    # glyph inset into the safe zone so a circular maskable crop is fine.
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192),
                       ("icon-512.png", 512)):
        on_tile(glyph, size, bg=TILE_BG, glyph_ratio=0.72).save(OUT_DIR / name,
                                                                format="PNG")

    for p in sorted(OUT_DIR.glob("*.ico")) + sorted(OUT_DIR.glob("icon-*.png")) + \
            [OUT_DIR / "apple-touch-icon.png"]:
        with Image.open(p) as im:
            frames = getattr(im, "n_frames", 1)
            print(f"  {p.name:22} {im.format:5} {im.size} frames={frames} "
                  f"{p.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
