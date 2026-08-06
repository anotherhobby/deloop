# render_verify.py -- confirm the composed gauge on real hardware.
#
# Post-rewrite check for the change described in docs/rendering.md: the gauge
# is built from vectorio shapes instead of painted into a 240x240 bitmap.
# Answers three questions the host-side renders cannot:
#
#   1. What does the scene actually cost, in the pool gc.mem_free() is blind
#      to? (pool_probe -- allocate 7,200-byte bitmaps until MemoryError.)
#   2. What does a real draw_main()/draw_volume() cost per frame now?
#   3. Does every screen still render without raising?
#
# Leaves the main screen up. Does not touch the network or the AVR.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/render_verify.py

import gc
import time

import displayio

import dial_ui
from state import AVRState


FRAMES = 40
_PROBE = 120 * 120 // 2      # 7,200 bytes per probe bitmap


def pool_probe(cap=120):
    """Bitmaps allocatable before MemoryError -- the only instrument that can
    see displayio allocations. See docs/rendering.md."""
    gc.collect()
    blocks = []
    try:
        while len(blocks) < cap:
            blocks.append(displayio.Bitmap(120, 120, 16))
    except MemoryError:
        pass
    n = len(blocks)
    del blocks
    gc.collect()
    return n * _PROBE


def _state():
    s = AVRState()
    s.power = "ON"
    s.power_known = True
    s.brightness = 1.0
    s.input = "SAT/CBL"
    s.preset = "2"
    s.preset_names = [("2", "Movie"), ("3", "Music"), ("4", "Night")]
    s.preset_quick_names = list(s.preset_names)
    s.preset_enabled = True
    s.volume_db = -20.5
    return s


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def main():
    print("\nrender verify -- composed gauge on hardware\n")

    before = pool_probe()
    print("pool before init(): %6d bytes" % before)

    ui = dial_ui.init()
    after = pool_probe()
    print("pool after  init(): %6d bytes" % after)
    print("scene costs         %6d bytes   (was 28,800 for the bitmap alone)"
          % (before - after))
    print("gc.mem_free():      %6d  -- blind to the above, kept for the record\n"
          % gc.mem_free())

    state = _state()
    lo, hi = dial_ui._VOL_MIN, dial_ui._VOL_MAX

    # draw_main: the FULL path. There is no fast path any more -- this is
    # what every frame costs, including the one that used to be ~350ms.
    times = []
    for i in range(FRAMES):
        state.volume_db = lo + (hi - lo) * (i % 20) / 19.0
        t0 = time.monotonic_ns()
        dial_ui.draw_main(ui, state)
        times.append((time.monotonic_ns() - t0) / 1e6)
    print("draw_main   %6.2f ms/frame  (min %.2f max %.2f)"
          % (_median(times), min(times), max(times)))

    # draw_volume: the encoder-tick path.
    times = []
    for i in range(FRAMES):
        state.volume_db = lo + (hi - lo) * (i % 20) / 19.0
        t0 = time.monotonic_ns()
        dial_ui.draw_volume(ui, state)
        times.append((time.monotonic_ns() - t0) / 1e6)
    print("draw_volume %6.2f ms/frame  (min %.2f max %.2f)"
          % (_median(times), min(times), max(times)))

    # Mute/busy are a color_index rewrite over the arc sectors -- no repaint.
    state.volume_db = -20.5
    for label, muted, busy in (("muted", True, False), ("busy", False, True)):
        t0 = time.monotonic_ns()
        dial_ui._set_arc_mode(ui, muted, busy)
        ui["display"].refresh()
        print("%-11s %6.2f ms  (arc recolour, no repaint)"
              % (label, (time.monotonic_ns() - t0) / 1e6))
    dial_ui._set_arc_mode(ui, False, False)

    # Every screen, just to prove none of them raise.
    print("\nscreens:")
    state.muted = False
    for name, call in (
        ("draw_main",     lambda: dial_ui.draw_main(ui, state)),
        ("draw_busy",     lambda: dial_ui.draw_busy(ui, state)),
        ("draw_status",   lambda: dial_ui.draw_status(ui, "connecting...")),
        ("draw_error",    lambda: dial_ui.draw_error(ui, "Reconnecting...")),
        ("draw_reconnect", lambda: dial_ui.draw_reconnecting(ui)),
        ("render_gauge_bg", lambda: dial_ui.render_gauge_bg(ui, -20.5, False)),
        ("draw_menu",     lambda: dial_ui.draw_menu(
            ui, "", ["Input", "Dirac Live", "Device"], 1, clear_bg=True)),
        ("exit_menu",     lambda: dial_ui.exit_menu(ui)),
    ):
        try:
            t0 = time.monotonic_ns()
            call()
            print("  %-16s ok  %6.2f ms" % (name, (time.monotonic_ns() - t0) / 1e6))
        except Exception as e:
            print("  %-16s RAISED %s: %s" % (name, type(e).__name__, e))
        time.sleep(0.4)

    # Standby, then back to main -- the transition that used to need a full
    # repaint in both directions.
    state.power = "STANDBY"
    dial_ui.draw_main(ui, state)
    time.sleep(1.0)
    state.power = "ON"
    dial_ui.draw_main(ui, state)
    print("\nleft on the main screen. pool now: %d bytes" % pool_probe())


if __name__ == "__main__":
    main()
