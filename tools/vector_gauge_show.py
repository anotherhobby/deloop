# vector_gauge_show.py -- put the composed gauge on the glass and leave it up.
#
# tools/vector_gauge_spike.py proved the numbers (28,800 bytes reclaimed,
# 29.2ms -> 1.7ms per frame). This is the other half: does it LOOK right on
# the actual display, where a 1px stroke and a dim grey are things the panel
# decides, not Pillow.
#
# Runs forever so `mpremote run` doesn't exit and soft-reload the device out
# from under the screen -- that exit is what dropped the real app onto a
# churned heap last time. Ctrl-C to stop; the app comes back on reset.
#
# What to look at:
#   - tick weight and brightness vs. the arc (the biggest render-rule unknown)
#   - the pointer wedge: crisp, or ragged as it sweeps?
#   - the MENU-home frame legs at the bottom -- thin dim diagonals, the
#     hardest thing here for a polygon rasterizer
#   - the muted/busy cycle: those are a color_index rewrite on the arc
#     sectors, no repaint at all, which is what retires _scene_signature()
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/vector_gauge_show.py

import time

import board
import displayio
from adafruit_display_text import label

import dial_ui
from vector_gauge_spike import build_scene_b, _pointer_points, make_palette


SECTORS   = 12
STEP_S    = 0.03     # pointer step interval
STATE_S   = 6.0      # seconds per arc state before cycling

# (label, palette index the arc sectors get). None = the real gradient.
_STATES = (("normal", None),
           ("muted",  dial_ui._BLUE),
           ("busy",   dial_ui._GRAY))


def _add_volume_labels(group):
    """Mirror init()'s split-size volume readout so the gauge is judged in
    context rather than as a bare ring."""
    vol_y = dial_ui.CY - 8
    if dial_ui._SHOW_DECIMAL:
        anchor, pos = (1.0, 1.0), (dial_ui.CX + 20, vol_y)
    else:
        anchor, pos = (0.5, 1.0), (dial_ui.CX, vol_y)
    vol_int = label.Label(dial_ui._F_LG, text="--", color=dial_ui._C_TEXT,
                          anchor_point=anchor, anchored_position=pos)
    vol_dec = label.Label(dial_ui._F_SM, text="", color=dial_ui._C_TEXT,
                          anchor_point=(0.0, 1.0),
                          anchored_position=(dial_ui.CX + 20, vol_y))
    group.append(vol_int)
    group.append(vol_dec)
    return vol_int, vol_dec


def main():
    display = board.DISPLAY
    display.auto_refresh = False

    group, ptr, nshapes, arc_shapes = build_scene_b(SECTORS)
    vol_int, vol_dec = _add_volume_labels(group)
    display.root_group = group

    original = [s.color_index for s in arc_shapes]

    print("composed gauge: %d shapes, %d arc sectors" % (nshapes, len(arc_shapes)))
    print("sweeping; Ctrl-C to stop")

    lo, hi = dial_ui._VOL_MIN, dial_ui._VOL_MAX
    steps = 60
    i = 0
    state_i = 0
    t_state = time.monotonic()

    try:
        while True:
            f = (i % steps) / (steps - 1.0)
            if (i // steps) % 2:
                f = 1.0 - f
            vol = lo + (hi - lo) * f

            ptr.points = _pointer_points(dial_ui._vol_to_angle(vol))
            iv, dv = dial_ui._split_vol(vol)
            vol_int.text = iv
            vol_dec.text = dv

            if time.monotonic() - t_state >= STATE_S:
                state_i = (state_i + 1) % len(_STATES)
                name, ci = _STATES[state_i]
                for k, s in enumerate(arc_shapes):
                    s.color_index = original[k] if ci is None else ci
                print("  arc state: %s" % name)
                t_state = time.monotonic()

            display.refresh()
            time.sleep(STEP_S)
            i += 1
    except KeyboardInterrupt:
        print("\nstopped -- reset the device to get deloop back")


if __name__ == "__main__":
    main()
