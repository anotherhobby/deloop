#!/usr/bin/env python3
"""
render_ui_grid.py -- compose the individual per-backend ui-<backend>.png
renders (produced by render_ui_screenshots.py / `make ui-renders`) into one
combined grid image, ui/ui-devices-grid.png: 3 dials wide, however many
rows tall, each with its backend name below it. README embeds this single
image instead of an HTML <table> of separately-sized <img> tags.

Why this exists: GitHub's README renderer does not reliably honor a bare
width=/height= attribute on <img> tags inside an HTML table -- confirmed
2026-07-30 live on github.com, where the "Device UI Examples" row rendered
with visibly different box sizes per image despite every source PNG being
pixel-identical 260x260 and the served HTML specifying the same width and
height on every <img> tag (raw.githubusercontent.com was also confirmed
serving identical bytes for all five at the time -- this was GitHub's
table/image rendering itself, not stale/mismatched source files). A single
pre-composed image sidesteps the whole question. Empty grid slots (row not
fully filled) render as plain black cells rather than being omitted, so the
grid shape stays a clean rectangle regardless of backend count.

Run after render_ui_screenshots.py has produced every ui-<backend>.png --
`make ui-renders` does this in the right order. This script only reads
already-rendered PNGs off disk, so unlike render_ui_screenshots.py it does
NOT import config.py/driver.py and is not DEVICE_DRIVER-import-order-
sensitive.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import dial_sim  # noqa: E402 -- only used for TTF_PATH; no CircuitPython shim needed here

UI_DIR   = ROOT / "ui"
OUT_PATH = UI_DIR / "ui-devices-grid.png"

# Backend render + label, in display order. Add a new backend here (and to
# render_ui_screenshots.py's _FIXTURES / the Makefile's ui-renders target)
# when a sixth backend ships -- same one-entry-per-backend pattern as
# everywhere else in this project's "adding a backend" recipe.
_ENTRIES = [
    ("ui-denon.png", "Denon"),
    ("ui-minidsp.png", "MiniDSP"),
    ("ui-wiim.png", "WiiM"),
    ("ui-camilladsp.png", "CamillaDSP"),
    ("ui-homeassistant.png", "Home Assistant"),
]

COLS      = 3
CELL      = 260   # matches render_ui_screenshots.py's saved dial PNG size
LABEL_H   = 40     # strip below each dial for the backend name
GRID      = 2      # grid-line / outer-frame thickness, in px
GRAY      = (0x60, 0x60, 0x60)  # dial_ui.py's _C_DIM -- same gray as the
                                 # on-screen input label, per the user's ask
BG        = (0, 0, 0)
FONT_SIZE = 26


def main():
    rows   = -(-len(_ENTRIES) // COLS)  # ceil division
    cell_h = CELL + LABEL_H
    w = GRID + COLS * (CELL + GRID)
    h = GRID + rows * (cell_h + GRID)

    # Filling the whole canvas gray first, then pasting opaque cells on top,
    # is what actually draws the grid lines/outer frame -- the gray that's
    # left showing through the gaps between cells *is* the grid.
    grid = Image.new("RGB", (w, h), GRAY)
    font = ImageFont.truetype(str(dial_sim.TTF_PATH), FONT_SIZE)

    for i in range(rows * COLS):
        r, c = divmod(i, COLS)
        x0 = GRID + c * (CELL + GRID)
        y0 = GRID + r * (cell_h + GRID)
        cell = Image.new("RGB", (CELL, cell_h), BG)
        if i < len(_ENTRIES):
            fname, label = _ENTRIES[i]
            dial = Image.open(UI_DIR / fname).convert("RGB")
            cell.paste(dial, (0, 0))
            cdraw = ImageDraw.Draw(cell)
            bbox = cdraw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tx = (CELL - tw) // 2
            ty = CELL + (LABEL_H - th) // 2 - bbox[1]
            cdraw.text((tx, ty), label, font=font, fill=(255, 255, 255))
        grid.paste(cell, (x0, y0))

    grid.save(OUT_PATH)
    print(f"  {OUT_PATH.relative_to(ROOT)}  ({w}x{h}, {COLS}x{rows} grid)")


if __name__ == "__main__":
    main()
