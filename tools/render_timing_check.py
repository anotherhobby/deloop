# render_timing_check.py -- on-device WALL-CLOCK timing of each individual
# render stage, to settle the cooperative-loop-starvation hypothesis.
#
# Why this exists: the 2026-08-04 chaos/A2 bisection showed poll throughput
# collapsing ~80x (52/s isolated -> 0.64/s with gauge redraws) with latency
# rising to ~1,100ms -- but NOT from allocator failure. No MemoryError ever
# appeared, GC pauses stayed at 4-6ms, and the *worse*-performing run
# (A2.2, bitmap) actually had a HEALTHIER largest allocatable block (16KB)
# than the better-performing one (A2.1, labels, 4KB). What does fit every
# data point is much simpler: denon.py's async engine is a cooperative
# state machine pumped once per main-loop iteration, so
#
#     poll wall-clock time ~= (pumps per poll) x (loop iteration time)
#
# which predicted all three runs' measured latency to within ~30%. If that's
# right, rendering isn't starving the network of MEMORY, it's starving it of
# LOOP ITERATIONS -- and the same blocking is what drops touch events.
#
# The one number never actually measured on hardware: how long each render
# stage really takes. The 2026-08-04 _arc_solid() rewrite (per-pixel atan2 ->
# row-boundary tan crossings, ~16x fewer trig calls) was validated on the Mac
# via tools/profile_arc.py and confirmed on-device only by the ABSENCE of
# STALL lines -- never with a millisecond figure. This script gets that
# figure, per stage, by calling dial_ui's own unmodified private functions
# directly (dial_ui.py is NOT touched) rather than the combined draw_main().
#
# Uses time.monotonic_ns() rather than time.monotonic() -- the latter is a
# float that loses sub-millisecond resolution as uptime grows, which is
# exactly the range being measured here.
#
# Networking runs concurrently (same real denon.py engine as the other
# probes) so loop-iteration rate and poll throughput can be read off the
# same run and checked against the model above.
#
# WARNING: same as the other chaos probes -- toggles AVR mute periodically.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/render_timing_check.py

import time
import gc
import board
import wifi
import socketpool
import adafruit_requests

import config
import driver
import denon
import dial_ui
from state import AVRState

RENDER_INTERVAL_S  = 0.15   # matches the A2 bisection's redraw cadence
CMD_INTERVAL_S     = 8.0
REPORT_INTERVAL_S  = 5.0

# Stage names in execution order -- _render_gauge()'s internals broken out
# individually, then the label pass, then the display transfer.
_STAGES = ("clear", "arc", "ticks", "pointer", "menu_home", "labels", "refresh")


def _connect_wifi():
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


class _Timings:
    """Per-stage count/total/min/max in microseconds, reset each report."""

    def __init__(self, names):
        self.names = names
        self.reset()

    def reset(self):
        self.count = {n: 0 for n in self.names}
        self.total = {n: 0 for n in self.names}
        self.min   = {n: 0 for n in self.names}
        self.max   = {n: 0 for n in self.names}

    def add(self, name, us):
        self.count[name] += 1
        self.total[name] += us
        if self.min[name] == 0 or us < self.min[name]:
            self.min[name] = us
        if us > self.max[name]:
            self.max[name] = us

    def line(self, name):
        n = self.count[name]
        if n == 0:
            return "%s=--" % name
        return "%s(avg=%.1f min=%.1f max=%.1f)ms" % (
            name,
            self.total[name] / n / 1000.0,
            self.min[name] / 1000.0,
            self.max[name] / 1000.0,
        )

    def total_avg_ms(self):
        """Sum of every stage's average -- i.e. what one full redraw costs."""
        out = 0.0
        for n in self.names:
            if self.count[n]:
                out += self.total[n] / self.count[n] / 1000.0
        return out


def main():
    gc.collect()
    ui = dial_ui.init()
    gc.collect()
    _connect_wifi()
    print("wifi connected:", wifi.radio.ipv4_address)

    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool)
    denon.init_transport(pool)
    driver.init(session)

    state = AVRState()
    state.brightness = 1.0
    display = ui["display"]
    bmp = ui["bmp"]
    display.brightness = state.brightness

    timings = _Timings(_STAGES)
    loop_iters = 0
    renders = 0
    poll_ok = poll_err = 0
    cmd_ok = cmd_err = 0
    poll_started_at = None
    poll_lat_total = 0.0
    poll_lat_n = 0

    t_start = time.monotonic()
    next_cmd = t_start + CMD_INTERVAL_S
    next_render = t_start + RENDER_INTERVAL_S
    next_report = t_start + REPORT_INTERVAL_S
    cmd_toggle = False
    mute_on = "/goform/formiPhoneAppDirect.xml?MUON"
    mute_off = "/goform/formiPhoneAppDirect.xml?MUOFF"

    print("Starting render timing check -- Ctrl-C to stop.")

    while True:
        now = time.monotonic()
        loop_iters += 1

        # --- networking: same real async engine as the other probes ---
        if poll_started_at is None:
            poll_started_at = now
        denon.start_status_poll(now)
        kind, payload = denon.pump_status_poll(now)
        if kind in ("done", "error"):
            poll_lat_total += now - poll_started_at
            poll_lat_n += 1
            poll_started_at = None
            if kind == "done":
                poll_ok += 1
                state.apply_status(payload)
            else:
                poll_err += 1
                print("poll error:", payload)

        if now >= next_cmd:
            cmd_toggle = not cmd_toggle
            denon.start_command(mute_on if cmd_toggle else mute_off, now)
            next_cmd = now + CMD_INTERVAL_S
        kind, payload = denon.pump_command(now)
        if kind == "done":
            cmd_ok += 1
        elif kind == "error":
            cmd_err += 1
            print("cmd error:", payload)

        # --- the measurement: one full redraw, stage by stage ---
        # Mirrors _render_gauge()'s own body (clear -> arc -> ticks ->
        # pointer) plus draw_main()'s _draw_menu_home/label work, calling
        # dial_ui's unmodified private functions so each piece is timed
        # separately instead of as one opaque draw_main() call.
        if now >= next_render:
            next_render = now + RENDER_INTERVAL_S
            renders += 1

            t = time.monotonic_ns()
            dial_ui._clear(bmp)
            t2 = time.monotonic_ns(); timings.add("clear", (t2 - t) // 1000)

            arc_col = dial_ui._BLUE if state.muted else dial_ui._arc_color
            dial_ui._arc_solid(bmp, dial_ui._ARC_START,
                               dial_ui._ARC_START + dial_ui._ARC_SWEEP,
                               dial_ui._R_IN, dial_ui._R_OUT, arc_col)
            t3 = time.monotonic_ns(); timings.add("arc", (t3 - t2) // 1000)

            dial_ui._draw_ticks(bmp)
            t4 = time.monotonic_ns(); timings.add("ticks", (t4 - t3) // 1000)

            if state.volume_db >= dial_ui._VOL_MIN:
                ang = dial_ui._vol_to_angle(state.volume_db)
                dial_ui._draw_pointer(bmp, ang, dial_ui._PTR)
                ui["_ptr_angle"] = ang
            t5 = time.monotonic_ns(); timings.add("pointer", (t5 - t4) // 1000)

            dial_ui._draw_menu_home(bmp)
            t6 = time.monotonic_ns(); timings.add("menu_home", (t6 - t5) // 1000)

            dial_ui._set_vol_labels(ui, state.volume_db, state.muted)
            ui["input"].text = driver.friendly_input(state.input)
            ui["input"].color = dial_ui._C_DIM
            dial_ui._draw_status_rows(ui, state, dial_ui._C_DIM)
            t7 = time.monotonic_ns(); timings.add("labels", (t7 - t6) // 1000)

            display.refresh()
            t8 = time.monotonic_ns(); timings.add("refresh", (t8 - t7) // 1000)

        if now >= next_report:
            next_report = now + REPORT_INTERVAL_S
            elapsed = now - t_start
            iters_per_s = loop_iters / REPORT_INTERVAL_S
            lat_ms = (poll_lat_total / poll_lat_n * 1000.0) if poll_lat_n else 0.0
            print("--- t=%.0fs  loop=%.1f iters/s  renders=%d  redraw_total=%.1fms" % (
                elapsed, iters_per_s, renders, timings.total_avg_ms()))
            print("    " + "  ".join(timings.line(n) for n in _STAGES))
            print("    polls=%d ok/%d err  cmds=%d ok/%d err  poll_lat_avg=%.0fms  free=%d" % (
                poll_ok, poll_err, cmd_ok, cmd_err, lat_ms, gc.mem_free()))
            timings.reset()
            loop_iters = 0
            renders = 0
            poll_lat_total = 0.0
            poll_lat_n = 0


if __name__ == "__main__":
    main()
