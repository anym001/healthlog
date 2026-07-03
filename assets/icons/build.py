#!/usr/bin/env python3
"""Regenerate the HealthLog PNG/ICO icons from the source SVGs.

The SVGs in this directory are the single source of truth; the raster files are
generated. Run this after editing `icon.svg` / `icon-source-maskable.svg`.

    pip install cairosvg pillow
    python assets/icons/build.py

Letterforms in the SVGs are drawn as vector rectangles (not <text>), so the
output does not depend on any installed font.
"""
import io
import os

import cairosvg
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))


def render(svg_name, size):
    png = cairosvg.svg2png(url=os.path.join(HERE, svg_name), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main():
    # "any"-purpose set (rounded tile)
    for size in (16, 32, 192, 512):
        render("icon.svg", size).save(os.path.join(HERE, f"icon-{size}.png"))
    render("icon.svg", 180).save(os.path.join(HERE, "apple-touch-icon.png"))

    # maskable set (full-bleed background, monogram inside the safe zone)
    for size in (192, 512):
        render("icon-source-maskable.svg", size).save(os.path.join(HERE, f"icon-maskable-{size}.png"))

    # multi-resolution favicon
    render("icon.svg", 256).save(
        os.path.join(HERE, "favicon.ico"),
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    print("Icons regenerated.")


if __name__ == "__main__":
    main()
