# chaos_stress_test_a.py -- "Test A" from the 2026-08-04 fragmentation
# investigation: does the mere PRESENCE of dial_ui's ~28.8KB gauge bitmap
# (allocated once, permanently, at boot) cap the largest allocatable block
# and cause the loop stalls chaos_stress_check.py found -- or is it the
# repeated *drawing into* that bitmap every chaos tick that does it?
#
# Identical to chaos_stress_check.py (same real denon.py networking engine,
# same alloc-churn chaos, same STALL/FRAG instrumentation) with exactly one
# change: dial_ui.init() still runs (bitmap/palette/TileGrid/Group/labels
# all allocated normally, same boot ordering as the real app), but the main
# loop never calls dial_ui.draw_main() -- the bitmap just sits there,
# allocated, never touched again after init.
#
# If the largest-block ceiling and stalls persist unchanged from
# chaos_stress_check.py's baseline run: the bitmap's mere presence/placement
# on the heap is implicated (heap topology from boot, not rendering churn).
# If they disappear/improve: the repeated fill_region()/bitmaptools calls
# during drawing are the actual cause, not the bitmap's existence.
#
# Also logs coarse startup heap layout (free heap + largest allocatable
# block at each boot phase boundary this script controls -- boot, before/
# after dial_ui.init(), before/after wifi, after networking setup) as a
# lighter-weight version of "Test D" (can't get finer than dial_ui.init()'s
# internals -- bitmap/palette/TileGrid/Group/fonts/labels are all allocated
# together inside that one call -- without instrumenting dial_ui.py itself,
# which this script deliberately avoids touching).
#
# WARNING: same as chaos_stress_check.py -- toggles AVR mute periodically.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/chaos_stress_test_a.py

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

STALL_THRESHOLD_S    = 0.03
CHAOS_INTERVAL_S      = 0.15
CMD_INTERVAL_S         = 8.0
REPORT_INTERVAL_S      = 2.0
PROBE_INTERVAL_S       = 5.0
POLL_LATENCY_WINDOW  = 20

ALLOC_MIN, ALLOC_MAX = 64, 1024
ALLOC_POOL_SIZE       = 12
FORCE_GC_EVERY_N       = 4


def _connect_wifi():
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


def _largest_allocatable_block(max_probe):
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


def _log_heap(label):
    gc.collect()
    free = gc.mem_free()
    largest = _largest_allocatable_block(free)
    print("HEAP %-24s free=%d largest=%d" % (label, free, largest))


_chaos_pool = [None] * ALLOC_POOL_SIZE


def _chaos_allocate():
    idx = random.randrange(ALLOC_POOL_SIZE)
    size = random.randint(ALLOC_MIN, ALLOC_MAX)
    _chaos_pool[idx] = bytearray(size)


def main():
    _log_heap("boot")

    ui = dial_ui.init()
    _log_heap("after dial_ui.init")

    _connect_wifi()
    print("wifi connected:", wifi.radio.ipv4_address)
    _log_heap("after wifi connect")

    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool)
    denon.init_transport(pool)
    driver.init(session)
    _log_heap("after networking setup")

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

    print("Starting Test A: bitmap allocated, NEVER redrawn -- chaos churn + networking only -- Ctrl-C to stop.")

    while True:
        t0 = time.monotonic()
        now = t0

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

        # --- chaos: heap churn only -- NO dial_ui.draw_main() call, this is
        # the one deliberate difference from chaos_stress_check.py (Test A).
        if now >= next_chaos:
            next_chaos = now + CHAOS_INTERVAL_S
            _chaos_allocate()
            chaos_tick_count += 1
            if chaos_tick_count % FORCE_GC_EVERY_N == 0:
                tg0 = time.monotonic()
                gc.collect()
                tg1 = time.monotonic()
                gc_ms = (tg1 - tg0) * 1000
                gc_count += 1
                gc_total_ms += gc_ms
                gc_max_ms = max(gc_max_ms, gc_ms)

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
            gc_count = 0
            gc_total_ms = 0.0
            gc_max_ms = 0.0
            stall_count = 0
            stall_max_ms = 0.0


if __name__ == "__main__":
    main()
