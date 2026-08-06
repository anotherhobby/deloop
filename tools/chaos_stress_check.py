# chaos_stress_check.py -- on-device combined rendering+networking stress
# test under synthetic allocation pressure, meant to reproduce the
# interaction instability that neither subsystem shows in isolation.
#
# Why this exists: the 2026-08-04 investigation isolated both halves of the
# real app and found each one solid on its own --
#   - AVR/network path alone (tools/avr_health_check.py, host-side, bypasses
#     the ESP32 entirely): 30/30 requests ok, 8-96ms latency.
#   - ESP32 networking alone, no rendering at all (tools/network_stress_check.py):
#     ~6000 polls + 14 commands over ~114s, 0 errors, 7-50ms latency, healthy
#     heap sawtooth.
# But the real app runs both together with free heap sitting around
# 33-36KB (the gauge bitmap alone is ~28.8KB), and that's where the actual
# instability shows up. This script deliberately recombines the two under
# WORSE conditions than the real app ever creates -- real dial_ui.draw_main()
# redraws firing on a fixed fast cadence regardless of whether anything
# changed, plus continuous random-sized bytearray churn to actively
# fragment the heap -- while running the same real denon.py async engine
# used by tools/network_stress_check.py. If this reproduces dropped
# polls/commands or long loop stalls that neither isolated test showed,
# that's real evidence of a rendering/networking interaction under memory
# pressure, not a bug in either subsystem alone.
#
# WARNING: same as network_stress_check.py -- toggles the AVR's mute on/off
# periodically (see CMD_INTERVAL_S). Audible if something's playing.
#
# Usage (no interactive REPL needed, just streams stdout):
#   ./.venv/bin/python -m mpremote connect auto run tools/chaos_stress_check.py
#
# Reports every REPORT_INTERVAL_S to serial: poll/command ok+error+streak
# counts, latency min/avg/max, free heap, an approximate largest-
# allocatable-block size (binary-searched bytearray allocation -- if free
# heap stays flat but this collapses, that's fragmentation, not exhaustion),
# forced-GC pause count/total/max time, and a stall counter using the exact
# same STALL_THRESHOLD_S concept app.py's real touch-drop instrumentation
# used (30ms) -- a loop iteration taking longer than that is the same class
# of delay that was previously confirmed to drop real taps. Runs forever --
# Ctrl-C (or power cycle) to stop.

import time
import gc
import random
import board
import wifi
import socketpool
import adafruit_requests

import config
import driver
import denon
import dial_ui
from state import AVRState

STALL_THRESHOLD_S    = 0.03   # matches app.py's real touch-drop instrumentation
CHAOS_INTERVAL_S      = 0.15   # redraw + allocate every ~150ms (100-200ms range)
CMD_INTERVAL_S         = 8.0    # mute toggle cadence, see WARNING above
REPORT_INTERVAL_S      = 2.0
PROBE_INTERVAL_S       = 5.0    # largest-allocatable-block binary search
POLL_LATENCY_WINDOW  = 20

ALLOC_MIN, ALLOC_MAX = 64, 1024   # random churn buffer size range (bytes)
ALLOC_POOL_SIZE       = 12         # live churn buffers held at once
FORCE_GC_EVERY_N       = 4          # force a gc.collect() every Nth chaos tick


def _connect_wifi():
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


def _largest_allocatable_block(max_probe):
    """Binary-search the largest bytearray CircuitPython will hand back
    right now. Distinguishes fragmentation (free heap high, this low) from
    real exhaustion (both low)."""
    lo, hi, best = 0, max_probe, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0:
            break
        try:
            buf = bytearray(mid)
            del buf
            best = mid
            lo = mid + 1
        except MemoryError:
            hi = mid - 1
    gc.collect()
    return best


_chaos_pool = [None] * ALLOC_POOL_SIZE


def _chaos_allocate():
    """Randomly replace one churn buffer -- continuous alloc/free noise to
    actively fragment the heap, on top of whatever rendering+networking are
    already doing."""
    idx = random.randrange(ALLOC_POOL_SIZE)
    size = random.randint(ALLOC_MIN, ALLOC_MAX)
    _chaos_pool[idx] = bytearray(size)


def main():
    # Same ordering as app.py's real boot: dial_ui.init()'s ~28KB bitmap
    # allocation happens BEFORE wifi.radio.connect(), which is the most
    # memory-fragile point in the real boot sequence (see app.py's comment
    # there) -- mirrored here so this test's boot conditions match reality.
    gc.collect()
    print("free mem before dial_ui.init:", gc.mem_free())
    ui = dial_ui.init()

    gc.collect()
    print("free mem before wifi connect:", gc.mem_free())
    _connect_wifi()
    print("wifi connected:", wifi.radio.ipv4_address)

    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool)
    denon.init_transport(pool)
    driver.init(session)

    state = AVRState()
    state.brightness = 1.0
    ui["display"].brightness = state.brightness

    poll_ok = poll_err = 0
    poll_streak = poll_streak_max = 0
    cmd_ok = cmd_err = 0
    cmd_streak = cmd_streak_max = 0
    poll_started_at = None
    latencies = []

    stall_count = 0
    stall_max_ms = 0.0
    gc_count = 0
    gc_total_ms = 0.0
    gc_max_ms = 0.0
    chaos_tick_count = 0
    largest_block = None
    frag_before = None

    t_start = time.monotonic()
    next_cmd = t_start + CMD_INTERVAL_S
    next_chaos = t_start + CHAOS_INTERVAL_S
    next_probe = t_start + PROBE_INTERVAL_S
    next_report = t_start + REPORT_INTERVAL_S
    cmd_toggle = False
    mute_on = "/goform/formiPhoneAppDirect.xml?MUON"
    mute_off = "/goform/formiPhoneAppDirect.xml?MUOFF"

    print("Starting chaos stress loop (rendering + networking + alloc churn) -- Ctrl-C to stop.")

    while True:
        t0 = time.monotonic()
        now = t0

        # --- networking: identical pattern to network_stress_check.py ---
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
                state.apply_status(payload)
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

        # --- chaos: real rendering + heap churn, on a fixed fast cadence ---
        if now >= next_chaos:
            next_chaos = now + CHAOS_INTERVAL_S
            _chaos_allocate()
            dial_ui.draw_main(ui, state)
            chaos_tick_count += 1
            if chaos_tick_count % FORCE_GC_EVERY_N == 0:
                tg0 = time.monotonic()
                gc.collect()
                tg1 = time.monotonic()
                gc_ms = (tg1 - tg0) * 1000
                gc_count += 1
                gc_total_ms += gc_ms
                gc_max_ms = max(gc_max_ms, gc_ms)

        # --- fragmentation-recovery probe: rare, own cadence ---
        # Measures the largest allocatable block BEFORE and immediately
        # AFTER a gc.collect(), to distinguish "fragmentation is just dead
        # objects waiting for collection" (recovers) from "something
        # long-lived is pinning the heap and preventing coalescing"
        # (doesn't recover) -- see 2026-08-04 chaos-mode findings.
        if now >= next_probe:
            next_probe = now + PROBE_INTERVAL_S
            frag_before = _largest_allocatable_block(gc.mem_free())
            gc.collect()
            frag_after = _largest_allocatable_block(gc.mem_free())
            largest_block = frag_after
            print("FRAG before=%d after_gc=%d free=%d recovered=%s" % (
                frag_before, frag_after, gc.mem_free(),
                "yes" if frag_after > frag_before * 1.5 else "no"))

        t_end = time.monotonic()
        dt = t_end - t0
        if dt >= STALL_THRESHOLD_S:
            stall_count += 1
            stall_max_ms = max(stall_max_ms, dt * 1000)
            print("STALL %dms free=%d" % (dt * 1000, gc.mem_free()))

        if t_end >= next_report:
            next_report = t_end + REPORT_INTERVAL_S
            elapsed = t_end - t_start
            free = gc.mem_free()
            lat_str = ""
            if latencies:
                lat_str = "lat min=%.0fms avg=%.0fms max=%.0fms" % (
                    min(latencies) * 1000,
                    sum(latencies) / len(latencies) * 1000,
                    max(latencies) * 1000)
            print(
                "t=%.0fs polls=%d ok/%d err (streak %d max %d) "
                "cmds=%d ok/%d err (streak %d max %d) free=%d frag_before=%s frag_after_gc=%s "
                "gc(n=%d tot=%.0fms max=%.0fms) stalls=%d stall_max=%.0fms  %s" % (
                    elapsed, poll_ok, poll_err, poll_streak, poll_streak_max,
                    cmd_ok, cmd_err, cmd_streak, cmd_streak_max, free,
                    frag_before if frag_before is not None else "?",
                    largest_block if largest_block is not None else "?",
                    gc_count, gc_total_ms, gc_max_ms, stall_count, stall_max_ms,
                    lat_str,
                )
            )
            # Reset the per-report accumulators (streak maxes and totals for
            # gc/stall are cumulative-since-last-report, everything else --
            # poll/cmd ok/err, latency window -- stays cumulative for the
            # whole run, matching network_stress_check.py's convention).
            gc_count = 0
            gc_total_ms = 0.0
            gc_max_ms = 0.0
            stall_count = 0
            stall_max_ms = 0.0


if __name__ == "__main__":
    main()
