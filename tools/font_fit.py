#!/usr/bin/env python3
"""
font_fit.py -- measure whether a display string fits deloop's round screen,
using the real .pcf bitmap fonts the device loads.

Why this exists: the M5 Dial's screen is a 240px *circle*, so the width
available to a label depends on how far its row sits from the centre, and
the gauge arc (inner radius 90) eats into that further. A name that looks
fine in a menu can draw straight under the arc band on the main screen and
then get clipped by the display circle -- which is exactly what three of
wiim.py's _MODE_NAME entries did (found 2026-08-06: "TIDAL Connect" 144px,
"Spotify Connect" 156px, "External Storage" 158px, against a 122px budget).

It measures the actual src/fonts/*.pcf glyph advances rather than the
Inter TTF those were generated from, so the numbers match the device. This
is a pixel budget, not a character count -- Inter's advances run from 5px
("i") to 20px ("W"), so "10 characters" is anywhere from 50px to 200px.

Usage:
    tools/font_fit.py "TIDAL Connect" "Spotify"      # default: input row
    tools/font_fit.py --row preset "Movie Night"
    tools/font_fit.py --list                          # show every row's budget
    tools/font_fit.py --table src/wiim.py:_MODE_NAME  # check a whole dict

Run it before adding or renaming any backend's friendly_input()/preset name.
"""
import argparse
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "src" / "fonts"

from adafruit_bitmap_font import bitmap_font  # noqa: E402

# Geometry is READ from dial_ui.py, never restated here. An earlier version of
# this file hardcoded the row Y values from memory and got two of them wrong
# (preset 158 vs the real 138, player 182 vs 162), quietly producing confident,
# wrong budgets -- the exact failure mode this tool exists to catch.
#
# Fetched via a subprocess rather than a plain import: dial_sim installs
# CircuitPython shims that replace displayio.Bitmap, which breaks the real
# adafruit_bitmap_font glyph loader this module needs. The two can't share a
# process, so the shimmed one runs in its own and reports numbers back.
def _geometry():
    import json
    import subprocess
    src = (
        "import sys, json; sys.path.insert(0, %r); import dial_sim as d;"
        "u = d.dial_ui;"
        "print(json.dumps({'CX': u.CX, 'CY': u.CY, 'R_IN': u._R_IN,"
        " 'R_OUT': u._R_OUT, 'PRESET_Y': u.PRESET_NAME_Y,"
        " 'PLAYER_Y': u._PLAYER_NAME_Y}))" % str(ROOT / "tools")
    )
    out = subprocess.run([sys.executable, "-c", src], capture_output=True,
                         text=True, cwd=str(ROOT))
    if out.returncode != 0:
        sys.exit("could not read dial_ui geometry:\n" + out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


_G = _geometry()

SCREEN_R = 120
CX, CY = _G["CX"], _G["CY"]
ARC_R_IN = _G["R_IN"]     # clearing this keeps text off the arc band
ARC_R_OUT = _G["R_OUT"]   # past this the display circle clips

# name -> (y, font file, approximate half-height of the glyph box). The y
# values come from dial_ui's own constants; the half-heights are the one
# genuine approximation here (a 20px font's glyph box is ~16px tall).
ROWS = {
    "input":  (62,               "Inter_Medium_20.pcf", 8),   # input_lbl
    "preset": (_G["PRESET_Y"],   "Inter_Medium_20.pcf", 8),   # preset_lbl
    "player": (_G["PLAYER_Y"],   "Inter_Medium_20.pcf", 8),   # player_lbl
    "status": (CY,               "Inter_Medium_24.pcf", 10),  # status/menu rows
}
# ha_ui.py deliberately SWAPS these two: the device name goes in ui["preset"]
# (upper row) and the preset name in ui["player"]. So an HA device name is
# measured against "preset", not "player".

_fonts = {}


def _font(name):
    if name not in _fonts:
        _fonts[name] = bitmap_font.load_font(str(FONT_DIR / name))
    return _fonts[name]


def text_width(s, font_name):
    """Advance width in pixels, matching how label.Label lays out one line."""
    font = _font(font_name)
    total = 0
    for ch in s:
        glyph = font.get_glyph(ord(ch))
        if glyph is None:
            continue
        total += glyph.shift_x
    return total


def _half_chord(r, dy):
    if abs(dy) >= r:
        return 0.0
    return math.sqrt(r * r - dy * dy)


def budgets(row):
    """(clear-of-arc, clipped-by-screen) width budgets for a label row.

    The label is vertically centred on its y, so its glyphs span y +/- half.
    The binding constraint is whichever of those rows sits furthest from the
    screen centre, since that is where the circle is narrowest.
    """
    y, _font_name, half = ROWS[row]
    worst_dy = max(abs(CY - (y - half)), abs(CY - (y + half)))
    return (2 * _half_chord(ARC_R_IN, worst_dy),
            2 * _half_chord(ARC_R_OUT, worst_dy))


def check(s, row):
    """Return (width, verdict) for a string on a given row."""
    _y, font_name, _half = ROWS[row]
    w = text_width(s, font_name)
    clear, clipped = budgets(row)
    if w <= clear:
        return w, "ok"
    if w <= clipped:
        return w, "OVERLAPS ARC"
    return w, "CLIPPED"


def _report(strings, row):
    clear, clipped = budgets(row)
    _y, font_name, _half = ROWS[row]
    print("row {!r}  font {}  clear budget {:.0f}px  (arc-overlap to {:.0f}px)"
          .format(row, font_name, clear, clipped))
    print()
    print("{:<24} {:>5} {:>7}  {}".format("text", "chars", "px", "verdict"))
    print("-" * 52)
    worst = 0
    for s in strings:
        w, verdict = check(s, row)
        worst = max(worst, 0 if verdict == "ok" else 1)
        print("{:<24} {:>5} {:>7}  {}".format(s, len(s), w, verdict))
    return worst


def _load_table(spec):
    """Parse 'path/to/file.py:DICT_NAME' into its string values, without
    importing the module (backend modules pull in CircuitPython-only deps)."""
    path, _, name = spec.partition(":")
    if not name:
        sys.exit("--table wants path.py:DICT_NAME, got {!r}".format(spec))
    src = (ROOT / path).read_text() if not Path(path).is_absolute() else Path(path).read_text()
    try:
        start = src.index(name + " = {")
    except ValueError:
        sys.exit("no {!r} in {}".format(name, path))
    body = src[start:src.index("}", start)]
    return [v for _k, v in re.findall(r'"([^"]*)":\s*"([^"]*)"', body)]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("strings", nargs="*", help="strings to measure")
    p.add_argument("--row", default="input", choices=sorted(ROWS),
                   help="which label row to measure against (default: input)")
    p.add_argument("--table", help="measure every value of a dict, e.g. src/wiim.py:_MODE_NAME")
    p.add_argument("--list", action="store_true", help="show every row's budget and exit")
    args = p.parse_args()

    if args.list:
        print("{:<9} {:>4} {:<22} {:>8} {:>8}".format(
            "row", "y", "font", "clear", "clipped"))
        print("-" * 56)
        for row in sorted(ROWS):
            y, font_name, _half = ROWS[row]
            clear, clipped = budgets(row)
            print("{:<9} {:>4} {:<22} {:>7.0f}px {:>7.0f}px".format(
                row, y, font_name, clear, clipped))
        return 0

    strings = list(args.strings)
    if args.table:
        strings += _load_table(args.table)
    if not strings:
        p.error("give some strings, or --table, or --list")

    return _report(strings, args.row)


if __name__ == "__main__":
    sys.exit(main())
