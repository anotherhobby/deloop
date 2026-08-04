# chaos_stress_test_a2.py -- "Test A2" from the 2026-08-04 fragmentation
# investigation: Test A showed the ~28.8KB gauge bitmap's mere PRESENCE on
# the heap is cheap (polling recovered to ~42/s, near the fully-isolated
# ~52/s, when it's allocated but never redrawn). So the failure requires
# repeated EXECUTION of the redraw path, not just the bitmap's existence.
# This script bisects draw_main() into its two real sub-operations --
# label text updates vs. bitmap pixel mutation (_render_gauge/_arc_solid/
# _draw_menu_home) -- to find which one is actually responsible, by calling
# dial_ui's own (unmodified) private functions directly rather than the
# combined draw_main() entry point. dial_ui.py itself is NOT touched.
#
# Flip MODE below and rerun for each stage of the bisection:
#   "full"         -- bitmap + labels + refresh (matches chaos_stress_check.py)
#   "labels_only"  -- labels + refresh, bitmap never touched      (Test A2.1)
#   "bitmap_only"  -- bitmap + refresh, labels never touched      (Test A2.2)
#   "refresh_only" -- refresh() every tick, NEITHER labels nor bitmap touched
#                     (isolates the SPI/DMA display transfer itself)
#
# Independent of MODE, every PROBE_INTERVAL_S this script also runs a full
# labels->bitmap->refresh sequence once, with a largest-allocatable-block
# reading bracketing EACH stage (not just before/after the whole thing) --
# this directly answers "which specific operation collapses the largest
# contiguous block," logged as a STAGE line. Uses a coarse, cheap probe
# (fixed descending candidate sizes, not full binary search) so bracketing
# four points doesn't itself dominate the loop.
#
# WARNING: same as chaos_stress_check.py -- toggles AVR mute periodically.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/chaos_stress_test_a2.py

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

MODE = "bitmap_only"   # "full" | "labels_only" | "bitmap_only" | "refresh_only"

STALL_THRESHOLD_S    = 0.03
CHAOS_INTERVAL_S      = 0.15
CMD_INTERVAL_S         = 8.0
REPORT_INTERVAL_S      = 2.0
PROBE_INTERVAL_S       = 5.0
POLL_LATENCY_WINDOW  = 20

ALLOC_MIN, ALLOC_MAX = 64, 1024
ALLOC_POOL_SIZE       = 12
FORCE_GC_EVERY_N       = 4

_COARSE_SIZES = (28672, 24576, 20480, 16384, 12288, 8192, 4096, 2048, 1024, 512, 256)


def _connect_wifi():
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


def _largest_block_coarse():
    """Cheap, quantized largest-allocatable-block estimate -- fixed
    descending candidate sizes instead of a full binary search, so
    bracketing four separate points per probe tick stays affordable."""
    for size in _COARSE_SIZES:
        try:
            buf = bytearray(size)
            del buf
            return size
        except MemoryError:
            continue
    return 0


_chaos_pool = [None] * ALLOC_POOL_SIZE


def _chaos_allocate():
    idx = random.randrange(ALLOC_POOL_SIZE)
    size = random.randint(ALLOC_MIN, ALLOC_MAX)
    _chaos_pool[idx] = bytearray(size)


def _redraw_labels(ui, state):
    dial_ui._set_vol_labels(ui, state.volume_db, state.muted)
    ui["input"].text = driver.friendly_input(state.input)
    ui["input"].color = dial_ui._C_DIM
    dial_ui._draw_status_rows(ui, state, dial_ui._C_DIM)


def _redraw_bitmap(ui, state):
    ptr = dial_ui._render_gauge(ui["bmp"], state.volume_db, state.muted)
    ui["_ptr_angle"] = ptr
    dial_ui._draw_menu_home(ui["bmp"])


def main():
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
    last_stage = None

    t_start = time.monotonic()
    next_cmd = t_start + CMD_INTERVAL_S
    next_chaos = t_start + CHAOS_INTERVAL_S
    next_probe = t_start + PROBE_INTERVAL_S
    next_report = t_start + REPORT_INTERVAL_S
    cmd_toggle = False
    mute_on = "/goform/formiPhoneAppDirect.xml?MUON"
    mute_off = "/goform/formiPhoneAppDirect.xml?MUOFF"

    print("Starting Test A2 (MODE=%s) -- Ctrl-C to stop." % MODE)

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

        # --- chaos: heap churn + MODE-selected redraw sub-operation ---
        if now >= next_chaos:
            next_chaos = now + CHAOS_INTERVAL_S
            _chaos_allocate()
            if MODE == "full":
                _redraw_bitmap(ui, state)
                _redraw_labels(ui, state)
                ui["display"].refresh()
            elif MODE == "labels_only":
                _redraw_labels(ui, state)
                ui["display"].refresh()
            elif MODE == "bitmap_only":
                _redraw_bitmap(ui, state)
                ui["display"].refresh()
            elif MODE == "refresh_only":
                ui["display"].refresh()
            chaos_tick_count += 1
            if chaos_tick_count % FORCE_GC_EVERY_N == 0:
                tg0 = time.monotonic()
                gc.collect()
                tg1 = time.monotonic()
                gc_ms = (tg1 - tg0) * 1000
                gc_count += 1
                gc_total_ms += gc_ms
                gc_max_ms = max(gc_max_ms, gc_ms)

        # --- per-stage attribution probe: rare, own cadence, always runs
        # the full labels->bitmap->refresh sequence regardless of MODE ---
        if now >= next_probe:
            next_probe = now + PROBE_INTERVAL_S
            b0 = _largest_block_coarse()
            _redraw_labels(ui, state)
            b1 = _largest_block_coarse()
            _redraw_bitmap(ui, state)
            b2 = _largest_block_coarse()
            ui["display"].refresh()
            b3 = _largest_block_coarse()
            last_stage = (b0, b1, b2, b3)
            print("STAGE before=%d after_labels=%d after_bitmap=%d after_refresh=%d free=%d" % (
                b0, b1, b2, b3, gc.mem_free()))

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
            stage_str = "stage=%s" % (last_stage,) if last_stage else "stage=?"
            print(
                "t=%.0fs polls=%d ok/%d err (streak %d max %d) "
                "cmds=%d ok/%d err (streak %d max %d) free=%d %s "
                "gc(n=%d tot=%.0fms max=%.0fms) stalls=%d stall_max=%.0fms  %s" % (
                    elapsed, poll_ok, poll_err, poll_streak, poll_streak_max,
                    cmd_ok, cmd_err, cmd_streak, cmd_streak_max, free, stage_str,
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
