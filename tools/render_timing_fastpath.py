# render_timing_fastpath.py -- on-device timing of draw_main()'s INCREMENTAL
# fast path, the follow-up to tools/render_timing_check.py.
#
# render_timing_check.py measured the full repaint (2026-08-04): ~330-380ms,
# dominated by _arc_solid (~145ms) and display.refresh() (~173ms). Skipping
# that repaint when the static scene is unchanged (dial_ui.draw_main's
# _scene_signature fast path) took the chaos-harness poll rate from ~0.66/s
# to ~3.8/s with zero errors -- a real 5.8x win, but well short of the
# ~15-25ms/frame the dirty-region model predicted, and the loop still stalls
# on nearly every redraw.
#
# So the fast path itself is still expensive, and this measures where.
# Prime suspect: _restore_region() calls _arc_solid() for a narrow angular
# window, but _arc_solid iterates every row of the FULL annulus bounding box
# (range(CY - r_out, CY + r_out + 1), ~204 rows) computing per-row edge
# crossings regardless of how narrow the requested sweep is -- so a +/-5
# degree pointer window may cost nearly what the whole 240 degree arc does,
# while filling almost no pixels.
#
# Mirrors draw_main()'s fast-path body stage by stage, calling dial_ui's own
# unmodified private functions, with the volume moved every frame so the
# pointer genuinely relocates (a stationary pointer would understate
# _restore_region's real cost).
#
# WARNING: same as the other probes -- toggles AVR mute periodically.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/render_timing_fastpath.py

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

RENDER_INTERVAL_S = 0.15
CMD_INTERVAL_S    = 8.0
REPORT_INTERVAL_S = 5.0

_STAGES = ("restore", "pointer", "labels", "refresh")


def _connect_wifi():
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


class _Timings:
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
            name, self.total[name] / n / 1000.0,
            self.min[name] / 1000.0, self.max[name] / 1000.0)

    def total_avg_ms(self):
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

    # One full render first, so the fast path has a valid scene to resume
    # from -- exactly how draw_main() reaches it in the real app.
    dial_ui.draw_main(ui, state)

    timings = _Timings(_STAGES)
    loop_iters = 0
    poll_ok = poll_err = 0
    cmd_ok = cmd_err = 0
    poll_started_at = None
    poll_lat_total = 0.0
    poll_lat_n = 0
    vol_step = 0

    t_start = time.monotonic()
    next_cmd = t_start + CMD_INTERVAL_S
    next_render = t_start + RENDER_INTERVAL_S
    next_report = t_start + REPORT_INTERVAL_S
    cmd_toggle = False
    mute_on = "/goform/formiPhoneAppDirect.xml?MUON"
    mute_off = "/goform/formiPhoneAppDirect.xml?MUOFF"

    print("Starting fast-path timing check -- Ctrl-C to stop.")

    while True:
        now = time.monotonic()
        loop_iters += 1

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

        if now >= next_render:
            next_render = now + RENDER_INTERVAL_S

            # Walk the pointer across the dial so _restore_region does real
            # work every frame rather than repainting the same window.
            vol_step = (vol_step + 1) % 40
            vol = config.VOLUME_MIN + 10.0 + vol_step * 1.2

            old = ui["_ptr_angle"]
            t0 = time.monotonic_ns()
            if old is not None:
                dial_ui._restore_region(bmp, old, False)
            t1 = time.monotonic_ns(); timings.add("restore", (t1 - t0) // 1000)

            ang = dial_ui._vol_to_angle(vol)
            dial_ui._draw_pointer(bmp, ang, dial_ui._PTR)
            ui["_ptr_angle"] = ang
            t2 = time.monotonic_ns(); timings.add("pointer", (t2 - t1) // 1000)

            dial_ui._set_vol_labels(ui, vol, False)
            ui["input"].text = driver.friendly_input(state.input)
            ui["input"].color = dial_ui._C_DIM
            dial_ui._draw_status_rows(ui, state, dial_ui._C_DIM)
            t3 = time.monotonic_ns(); timings.add("labels", (t3 - t2) // 1000)

            display.refresh()
            t4 = time.monotonic_ns(); timings.add("refresh", (t4 - t3) // 1000)

        if now >= next_report:
            next_report = now + REPORT_INTERVAL_S
            lat_ms = (poll_lat_total / poll_lat_n * 1000.0) if poll_lat_n else 0.0
            print("--- t=%.0fs  loop=%.1f iters/s  fastpath_total=%.1fms" % (
                now - t_start, loop_iters / REPORT_INTERVAL_S, timings.total_avg_ms()))
            print("    " + "  ".join(timings.line(n) for n in _STAGES))
            print("    polls=%d ok/%d err  cmds=%d ok/%d err  poll_lat_avg=%.0fms  free=%d" % (
                poll_ok, poll_err, cmd_ok, cmd_err, lat_ms, gc.mem_free()))
            timings.reset()
            loop_iters = 0
            poll_lat_total = 0.0
            poll_lat_n = 0


if __name__ == "__main__":
    main()
