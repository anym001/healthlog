# HealthLog icons

Brand/app icon for HealthLog, styled after the sibling
[PocketLog](https://github.com/anym001/pocketlog) icon: a rounded "superellipse"
tile with a flat diagonal gradient on an Apple-Health-inspired pink→red palette
(`#FF6383` → `#E11D48`). The primary mark is an **analytics** motif — rising bars
with a trend arrow — reflecting HealthLog's core job of computing statistical
findings and trends.

## Source of truth

The SVGs are authoritative; the PNG/ICO files are generated from them. Every
glyph is drawn as vector shapes (not `<text>`), so rendering does not depend on
any installed font.

| File | Purpose |
| --- | --- |
| `icon.svg` | Primary source — rounded tile, analytics bars + trend arrow |
| `icon-source-maskable.svg` | Full-bleed background, motif scaled into the inner-80% safe zone |

## Generated files

`icon-16/32/192/512.png`, `apple-touch-icon.png` (180), `icon-maskable-192/512.png`,
`favicon.ico` (16/32/48). Regenerate after editing an SVG:

```bash
pip install cairosvg pillow
python assets/icons/build.py
```

## Alternative design proposals

`proposals/` holds three additional concepts (SVG + a 512px preview), each
reflecting Apple Health data preparation / HealthLog's function. Swap one in by
copying its SVG over `icon.svg` (adjusting the maskable variant) and re-running
`build.py`.

| Proposal | Motif |
| --- | --- |
| `proposal-monogram.svg` | Bold "HL" initials — closest to the PocketLog icon |
| `proposal-heart.svg` | White heart with an ECG/heartbeat pulse line — vitals/health data |
| `proposal-rings.svg` | Apple-style concentric activity rings on a near-black tile |
