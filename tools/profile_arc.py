#!/usr/bin/env python3
"""
profile_arc.py -- Mac-side correctness/speed regression check for
dial_ui.py's _arc_solid() gauge-arc fill.

Origin: _arc_solid() used to test every pixel's angle with atan2()+
degrees() (~296ms on hardware, confirmed live -- see docs/architecture.md's
2026-08-03/04 tap-drop investigation, the actual reason for a fast arc
fill). This script built and validated the 2026-08-04 replacement (a
`dx = -dy * tan(edge_angle)` per-row-boundary approach, ~16x fewer trig
calls) before it ever touched hardware, by running the real dial_ui.py
(via tools/dial_sim.py's CircuitPython shims) and comparing it against a
frozen copy of the original per-pixel algorithm kept below as
`_arc_solid_reference_original`. Keep this script working as a regression
check: anything that touches _arc_solid() again should be re-verified
against that frozen reference, not just eyeballed.

Two kinds of evidence, not one:
  - Trig-call count (atan2/degrees calls) -- hardware-independent, this is
    what actually costs time on the ESP32-S3's interpreter.
  - Rendered PNGs, reference vs current, for a few representative
    scenarios -- for a human to actually look at. A raw pixel-diff is
    reported too, but only as a fast smoke test (catches gross bugs
    immediately); it is NOT the correctness bar -- _arc_solid_reference_
    original has no anti-aliasing itself, but two acceptable rasterizations
    of the same arc can still legitimately differ at edge pixels, so a
    nonzero diff count is a prompt to look at the images, not an automatic
    fail. (Confirmed 2026-08-04: ~16 px / 57600 differed, all at color-band
    or sector-edge boundaries, visually confirmed negligible before this
    replaced the original implementation in dial_ui.py.)

Wall-clock timing on the Mac is also printed, but is NOT directly
comparable to ESP32-S3 CircuitPython timing (different CPU, different
interpreter, and dial_sim's shims deliberately don't register
bitmaptools -- see its docstring -- so this always exercises dial_ui.py's
pure-Python fallback fill, not the on-device C-accelerated one). Useful
for comparing implementations against each other on identical hardware,
not for predicting on-device milliseconds.

Usage:
    python tools/profile_arc.py
"""
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dial_sim  # noqa: E402  (installs CircuitPython shims, imports dial_ui)

dial_ui = dial_sim.dial_ui

CX, CY = dial_ui.CX, dial_ui.CY
_W, _H = dial_ui._W, dial_ui._H
_R_IN, _R_OUT = dial_ui._R_IN, dial_ui._R_OUT
_ARC_START, _ARC_SWEEP = dial_ui._ARC_START, dial_ui._ARC_SWEEP
_arc_color = dial_ui._arc_color
_BLUE, _GRAY = dial_ui._BLUE, dial_ui._GRAY


class _CallCounter:
    def __init__(self, fn):
        self._fn = fn
        self.count = 0

    def __call__(self, *a, **kw):
        self.count += 1
        return self._fn(*a, **kw)


def _make_bitmap():
    return dial_sim._FakeBitmap(_W, _H, 16)


def _full_sweep_args(color):
    return (_ARC_START, _ARC_START + _ARC_SWEEP, _R_IN, _R_OUT, color)


# ---------------------------------------------------------------------------
# Frozen copy of the pre-2026-08-04 original implementation -- ground truth
# for this regression check. Do NOT "fix" this to match dial_ui.py; if it
# ever needs to change, it's because the intentional design (not just the
# fast path) changed, and this file's whole job is catching the opposite.
# ---------------------------------------------------------------------------

def _arc_solid_reference_original(bmp, a_start, a_end, r_in, r_out, color):
    r_out2  = r_out * r_out
    r_in2   = r_in  * r_in
    wraps   = a_end > 360.0
    a_end2  = a_end - 360.0 if wraps else a_end
    is_grad = callable(color)

    for y in range(max(0, CY - r_out), min(_H, CY + r_out + 1)):
        dy  = y - CY
        dy2 = dy * dy
        if dy2 >= r_out2:
            continue
        dx_out = int(math.sqrt(float(r_out2 - dy2)))
        dx_in  = int(math.sqrt(float(max(0.0, r_in2 - dy2)))) if dy2 < r_in2 else 0

        for xA, xB in ((CX - dx_out, CX - dx_in), (CX + dx_in, CX + dx_out)):
            ci_run = -1
            x_run  = xA
            for x in range(max(0, xA), min(_W, xB + 1)):
                dx = x - CX
                a  = math.degrees(math.atan2(dx, -dy))
                if a < 0.0:
                    a += 360.0
                ok = (a >= a_start or a <= a_end2) if wraps else (a_start <= a <= a_end2)
                arc_a = a if a >= a_start else a + 360.0
                ci = (color(arc_a) if is_grad else color) if ok else -1
                if ci != ci_run:
                    if ci_run >= 0:
                        for xx in range(x_run, x):
                            bmp[xx, y] = ci_run
                    ci_run = ci
                    x_run  = x
            if ci_run >= 0:
                xe = min(_W, xB + 1)
                for xx in range(x_run, xe):
                    bmp[xx, y] = ci_run


# ---------------------------------------------------------------------------
# Correctness + speed harness
# ---------------------------------------------------------------------------

SCENARIOS = [
    ("gradient", lambda: _arc_color),
    ("muted_flat", lambda: _BLUE),
    ("busy_flat", lambda: _GRAY),
]


def _diff_count(bmp_a, bmp_b):
    return sum(1 for i in range(len(bmp_a._data)) if bmp_a._data[i] != bmp_b._data[i])


def _run_scenario(color, fn, label):
    bmp = _make_bitmap()
    atan2_ct = _CallCounter(math.atan2)
    deg_ct   = _CallCounter(math.degrees)
    real_atan2, real_degrees = math.atan2, math.degrees
    math.atan2, math.degrees = atan2_ct, deg_ct
    t0 = time.perf_counter()
    try:
        fn(bmp, *_full_sweep_args(color))
    finally:
        math.atan2, math.degrees = real_atan2, real_degrees
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"    {label:12s} atan2={atan2_ct.count:5d}  degrees={deg_ct.count:5d}  wall={elapsed_ms:7.2f}ms")
    return bmp


def main():
    print("Regression check: frozen original _arc_solid vs. current dial_ui._arc_solid\n")
    for name, color_fn in SCENARIOS:
        color = color_fn()
        print(f"  === {name} ===")
        bmp_orig = _run_scenario(color, _arc_solid_reference_original, "reference")
        bmp_cur  = _run_scenario(color, dial_ui._arc_solid, "current")
        diff = _diff_count(bmp_orig, bmp_cur)
        pct = 100.0 * diff / len(bmp_orig._data)
        print(f"    pixel diff: {diff} / {len(bmp_orig._data)} ({pct:.3f}%) -- smoke test only, inspect PNGs before judging")

        out_dir = dial_sim.OUT_DIR / "arc_profile"
        out_dir.mkdir(parents=True, exist_ok=True)
        palette = dial_ui._PALETTE
        fake_palette = dial_sim._FakePalette(len(palette))
        for i, c in enumerate(palette):
            fake_palette[i] = c
        bmp_orig.to_image(fake_palette).save(out_dir / f"{name}_reference.png")
        bmp_cur.to_image(fake_palette).save(out_dir / f"{name}_current.png")
        print(f"    saved {out_dir.relative_to(dial_sim.ROOT)}/{name}_{{reference,current}}.png\n")

    print("Done. Trig-call counts are the hardware-transferable number; wall-clock is Mac-only.")


if __name__ == "__main__":
    main()
