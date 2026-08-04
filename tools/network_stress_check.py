# network_stress_check.py -- on-device network-only stress test, deliberately
# WITHOUT dial_ui.py or any rendering, to isolate networking behavior from
# rendering-caused memory pressure.
#
# Why this exists: 2026-08-04 investigation found two separate, real sources
# of instability -- expensive gauge rendering (fixed the same day, see
# dial_ui.py's _arc_solid()) and AVR-network flakiness (an intermittent
# ETIMEDOUT on one specific endpoint, ongoing) -- both of which pressure the
# same tight heap, making it hard to tell which lever is causing what when
# they're tangled together. This script pulls rendering out of the picture
# entirely (no dial_ui import at all, so no ~28KB gauge bitmap, no label
# objects beyond a handful of plain status lines) and just hammers the same
# real network path (denon.py's proven async engine -- the actual
# start_status_poll()/pump_status_poll()/start_command()/pump_command()
# app.py uses, not a reimplementation) continuously, so whatever instability
# shows up here is real network/heap behavior, not rendering competing for
# the same resources.
#
# WARNING: the command-exercise loop below toggles the AVR's mute on/off
# repeatedly (see CMD_INTERVAL_S) -- audible if something's actually
# playing. Run this when that's not a problem, or comment out the
# `denon.start_command(...)` call to go poll-only.
#
# Usage (no interactive REPL needed, just streams stdout):
#   ./.venv/bin/python -m mpremote connect auto run tools/network_stress_check.py
#
# Reports every REPORT_INTERVAL_S to serial AND to the physical screen
# (plain text lines, terminalio's built-in font -- no font file loading):
# poll/command success-fail counts, consecutive-failure streak, longest
# failure streak seen, and free heap. Runs forever -- Ctrl-C (or power
# cycle) to stop.

import time
import gc
import board
import wifi
import socketpool
import displayio
import terminalio
import adafruit_requests
from adafruit_display_text import label

import config
import denon

POLL_LATENCY_WINDOW = 20     # rolling window size for min/avg/max latency
CMD_INTERVAL_S       = 8.0   # how often to fire a command (see WARNING above)
REPORT_INTERVAL_S     = 2.0


def _connect_wifi():
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


def _make_display():
    display = board.DISPLAY
    display.auto_refresh = False
    group = displayio.Group()
    display.root_group = group
    lines = []
    for i in range(6):
        lbl = label.Label(terminalio.FONT, text="", color=0xFFFFFF,
                           anchor_point=(0.0, 0.0), anchored_position=(4, 4 + i * 16))
        group.append(lbl)
        lines.append(lbl)
    return display, lines


def main():
    display, lines = _make_display()
    lines[0].text = "connecting wifi..."
    display.refresh()

    _connect_wifi()
    lines[0].text = "wifi ok: " + str(wifi.radio.ipv4_address)
    display.refresh()
    print("wifi connected:", wifi.radio.ipv4_address)

    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool)
    denon.init(session)
    denon.init_transport(pool)

    poll_ok = poll_err = 0
    poll_streak = poll_streak_max = 0
    cmd_ok = cmd_err = 0
    cmd_streak = cmd_streak_max = 0
    poll_started_at = None
    latencies = []

    t_start = time.monotonic()
    next_cmd = t_start + CMD_INTERVAL_S
    next_report = t_start + REPORT_INTERVAL_S
    cmd_toggle = False
    mute_on = "/goform/formiPhoneAppDirect.xml?MUON"
    mute_off = "/goform/formiPhoneAppDirect.xml?MUOFF"

    print("Starting continuous poll+command stress loop -- Ctrl-C to stop.")

    while True:
        now = time.monotonic()

        # Continuous back-to-back polling: start_status_poll() is
        # idempotent (no-ops while one's already in flight), so calling it
        # every tick gives true continuous polling with zero idle gap.
        if poll_started_at is None:
            poll_started_at = now
        denon.start_status_poll(now)
        kind, payload = denon.pump_status_poll(now)
        if kind in ("done", "error"):
            latencies.append(now - poll_started_at)
            if len(latencies) > POLL_LATENCY_WINDOW:
                latencies.pop(0)
            poll_started_at = None
            if kind == "done":
                poll_ok += 1
                poll_streak = 0
            else:
                poll_err += 1
                poll_streak += 1
                poll_streak_max = max(poll_streak_max, poll_streak)
                print("poll error:", payload)

        if now >= next_cmd:
            cmd_toggle = not cmd_toggle
            denon.start_command(mute_on if cmd_toggle else mute_off, now)
            next_cmd = now + CMD_INTERVAL_S
        kind, payload = denon.pump_command(now)
        if kind == "done":
            cmd_ok += 1
            cmd_streak = 0
        elif kind == "error":
            cmd_err += 1
            cmd_streak += 1
            cmd_streak_max = max(cmd_streak_max, cmd_streak)
            print("cmd error:", payload)

        if now >= next_report:
            next_report = now + REPORT_INTERVAL_S
            elapsed = now - t_start
            free = gc.mem_free()
            lat_str = ""
            if latencies:
                lat_str = "lat min=%.0fms avg=%.0fms max=%.0fms" % (
                    min(latencies) * 1000,
                    sum(latencies) / len(latencies) * 1000,
                    max(latencies) * 1000)
            print("t=%.0fs polls=%d ok/%d err (streak %d, max %d) "
                  "cmds=%d ok/%d err (streak %d, max %d) free=%d  %s" % (
                      elapsed, poll_ok, poll_err, poll_streak, poll_streak_max,
                      cmd_ok, cmd_err, cmd_streak, cmd_streak_max, free, lat_str))

            lines[0].text = "uptime %ds  free %d" % (elapsed, free)
            lines[1].text = "poll ok=%d err=%d" % (poll_ok, poll_err)
            lines[2].text = "poll streak=%d max=%d" % (poll_streak, poll_streak_max)
            lines[3].text = "cmd  ok=%d err=%d" % (cmd_ok, cmd_err)
            lines[4].text = "cmd  streak=%d max=%d" % (cmd_streak, cmd_streak_max)
            lines[5].text = lat_str
            display.refresh()


if __name__ == "__main__":
    main()
