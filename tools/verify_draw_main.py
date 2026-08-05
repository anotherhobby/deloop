#!/usr/bin/env python3
"""
verify_draw_main.py -- Mac-side correctness check for draw_main()'s
incremental fast path.

Origin: draw_main() used to do a full _render_gauge() (clear + gradient arc +
ticks + pointer) on every call. Measured on real hardware 2026-08-04
(tools/render_timing_check.py), that cost ~330-380ms -- which collapsed the
cooperative main loop to ~3 iterations/sec, stretched each async status poll
to ~1000ms, and dropped touch events. The fix: skip the static repaint
whenever dial_ui._scene_signature() is unchanged, moving only the pointer via
the already-proven _restore_region()/_move_pointer() path.

That fix is only safe if the incremental result is pixel-identical to a full
render. This script proves it, by running the real src/dial_ui.py (via
tools/dial_sim.py's CircuitPython shims) two ways for the same final state --
once reaching it incrementally through a sequence of draw_main() calls, once
via a fresh full render -- and diffing the resulting bitmaps.

Unlike tools/profile_arc.py, an exact-zero diff IS the bar here: both sides
run the identical drawing code, so any nonzero difference is real staleness
(a scene change that failed to invalidate the cache), not a rasterization
variance. The scenarios below deliberately include the cases most likely to
leak: long volume sweeps (cumulative drift -- the same methodology that
caught _restore_region's arc-overshoot clamp bug), the min/max extremes it
occurred at, and every state field that changes static bitmap content.

Usage:
    python tools/verify_draw_main.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dial_sim  # noqa: E402  (installs CircuitPython shims, imports dial_ui)

dial_ui = dial_sim.dial_ui
_base_state = dial_sim._base_state


def _diff(bmp_a, bmp_b):
    return sum(1 for i in range(len(bmp_a._data)) if bmp_a._data[i] != bmp_b._data[i])


def _fresh_full(state):
    """A clean full render of `state` into a brand-new ui/bitmap."""
    ui = dial_ui.init()
    dial_ui.draw_main(ui, state)
    return ui


def _apply(state, **fields):
    for k, v in fields.items():
        setattr(state, k, v)
    return state


def _case(name, steps, results):
    """steps: list of dicts of state fields, applied in order via draw_main.

    The final step's state is then rendered fresh and the two compared.
    """
    state = _base_state()
    ui = dial_ui.init()
    for step in steps:
        _apply(state, **step)
        dial_ui.draw_main(ui, state)

    ref = _fresh_full(state)
    d = _diff(ui["bmp"], ref["bmp"])
    total = len(ui["bmp"]._data)
    status = "PASS" if d == 0 else "FAIL"
    print(f"  [{status}] {name:38s} diff={d}/{total}")
    results.append(d == 0)
    return ui


def main():
    print("Verifying draw_main() incremental fast path vs. full render\n")
    results = []

    # Plain volume moves -- the core fast-path case.
    _case("volume step (2 draws)",
          [{"volume_db": -20.5}, {"volume_db": -12.0}], results)

    # Long sweep: cumulative incremental drift is what the _restore_region
    # clamp bug looked like, so walk a lot of positions before comparing.
    _case("volume sweep (40 draws)",
          [{"volume_db": -60.0 + i * 1.5} for i in range(40)], results)

    # The extremes _restore_region's clamp exists for.
    _case("sweep to max volume",
          [{"volume_db": v} for v in (-30.0, -10.0, 0.0, 10.0, 18.0)], results)
    _case("sweep to min volume",
          [{"volume_db": v} for v in (-10.0, -40.0, -70.0, -78.0, -80.0)], results)

    # Scene changes that MUST invalidate and force a full repaint.
    _case("mute on after volume moves",
          [{"volume_db": -30.0}, {"volume_db": -15.0}, {"muted": True}], results)
    _case("mute off again",
          [{"volume_db": -30.0}, {"muted": True}, {"muted": False}], results)
    _case("preset change",
          [{"volume_db": -25.0}, {"preset": "3"}], results)
    _case("preset disabled",
          [{"volume_db": -25.0}, {"preset_enabled": False}], results)
    _case("power off then on",
          [{"volume_db": -25.0}, {"power": "STANDBY"}, {"power": "ON"}], results)
    _case("media_state change",
          [{"volume_db": -25.0}, {"media_state": "playing"}], results)

    # Interleaved scene change + volume moves: the fast path must resume
    # cleanly from the repaint, not from the pre-change bitmap.
    _case("mute then more volume moves",
          [{"volume_db": -30.0}, {"muted": True},
           {"volume_db": -22.0}, {"volume_db": -18.0}], results)

    # Other public draws overwrite the bitmap; draw_main must not resume
    # incrementally from any of them.
    print()
    for name, fn in (
        ("after draw_menu", lambda ui, st: dial_ui.draw_menu(
            ui, "", ["Input", "Dirac Live", "Device"], 1, clear_bg=True)),
        ("after draw_busy", lambda ui, st: dial_ui.draw_busy(ui, st)),
        ("after draw_status", lambda ui, st: dial_ui.draw_status(ui, "connecting...")),
        ("after draw_error", lambda ui, st: dial_ui.draw_error(ui, "Reconnecting...")),
        ("after draw_reconnecting", lambda ui, st: dial_ui.draw_reconnecting(ui)),
        ("after render_gauge_bg", lambda ui, st: dial_ui.render_gauge_bg(
            ui, st.volume_db, st.muted)),
    ):
        state = _base_state()
        ui = dial_ui.init()
        state.volume_db = -25.0
        dial_ui.draw_main(ui, state)
        fn(ui, state)
        state.volume_db = -18.0
        dial_ui.draw_main(ui, state)

        ref = _fresh_full(state)
        d = _diff(ui["bmp"], ref["bmp"])
        total = len(ui["bmp"]._data)
        status = "PASS" if d == 0 else "FAIL"
        print(f"  [{status}] {name:38s} diff={d}/{total}")
        results.append(d == 0)

    print()
    if all(results):
        print(f"All {len(results)} scenarios pixel-identical to a full render.")
        return 0
    print(f"{results.count(False)} of {len(results)} scenarios DIFFER -- fast path is unsafe.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
