# gauge_stream_check.py -- is a flash-streamed gauge background cheap enough?
#
# The gauge's 240x240 4bpp displayio.Bitmap is 28,800 bytes, the single
# largest allocation on a device that runs at ~37KB free. Almost all of it is
# static: the arc and ticks depend only on (muted, busy, power_off), and only
# the pointer moves frame to frame.
#
# displayio.OnDiskBitmap streams pixel rows off flash and costs no RAM for
# the image. It is read-only, so it cannot replace the bitmap we draw into --
# but it can sit underneath a much smaller writable bitmap holding just the
# pointer. That would reclaim most of the 28.8KB AND remove the arc repaint
# entirely.
#
# The one number that decides whether it is viable: what does display
# refresh() cost when the background has to be read from flash instead of
# RAM? Today a small-dirty-region refresh is ~16.5ms and a full-screen one is
# ~173ms (measured 2026-08-04, tools/render_timing_check.py). If streaming
# pushes every frame toward the latter, the idea is dead regardless of how
# much RAM it saves.
#
# Compares like for like: same overlay motion, same refresh call, once with a
# RAM bitmap background and once with a flash-streamed one.
#
# Requires /gauge_normal.bmp on the device (see tools/make_gauge_bg.py).
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/gauge_stream_check.py

import time
import gc
import board
import displayio

FRAMES = 20
BG_PATH = "/gauge_normal.bmp"

# Small writable overlay -- stands in for the pointer. Deliberately tiny:
# the point of the exercise is that the only thing needing RAM is whatever
# actually changes.
OV_W = OV_H = 40


def _mem():
    gc.collect()
    return gc.mem_free()


def _time_frames(display, move):
    """Run FRAMES refreshes, moving something each time. Returns ms stats."""
    times = []
    for i in range(FRAMES):
        move(i)
        t0 = time.monotonic_ns()
        display.refresh()
        times.append((time.monotonic_ns() - t0) / 1000000.0)
    times.sort()
    return times[0], sum(times) / len(times), times[-1]


def main():
    display = board.DISPLAY
    display.auto_refresh = False
    print("gauge stream check -- %d frames per scenario\n" % FRAMES)

    base = _mem()
    print("baseline free: %d\n" % base)

    # ---- A: today's shape -- full-screen writable bitmap in RAM ----------
    print("=== A: 240x240 writable Bitmap in RAM (current) ===")
    pal = displayio.Palette(16)
    for i in range(16):
        pal[i] = (i * 0x111111) & 0xFFFFFF
    bmp = displayio.Bitmap(240, 240, 16)
    g = displayio.Group()
    g.append(displayio.TileGrid(bmp, pixel_shader=pal))
    display.root_group = g
    after_a = _mem()
    print("  free after alloc: %d  (cost %d bytes)" % (after_a, base - after_a))

    def move_a(i):
        # Dirty a small region, the way a pointer redraw does.
        x = 20 + (i * 8) % 180
        for yy in range(100, 100 + OV_H):
            for xx in range(x, x + OV_W):
                bmp[xx, yy] = (i % 15) + 1

    lo, avg, hi = _time_frames(display, move_a)
    print("  refresh: avg %.1fms  min %.1f  max %.1f" % (avg, lo, hi))

    del g, bmp, pal
    display.root_group = displayio.Group()
    gc.collect()

    # ---- B: proposed -- flash-streamed background + small RAM overlay ----
    print("\n=== B: OnDiskBitmap background + %dx%d overlay ===" % (OV_W, OV_H))
    before_b = _mem()
    try:
        odb = displayio.OnDiskBitmap(BG_PATH)
    except Exception as e:
        print("  FAILED to open %s: %s %s" % (BG_PATH, type(e).__name__, e))
        print("  (run tools/make_gauge_bg.py and copy it to the device)")
        return

    g2 = displayio.Group()
    g2.append(displayio.TileGrid(odb, pixel_shader=odb.pixel_shader))

    ov_pal = displayio.Palette(16)
    for i in range(16):
        ov_pal[i] = (i * 0x111111) & 0xFFFFFF
    ov_pal.make_transparent(0)
    ov = displayio.Bitmap(OV_W, OV_H, 16)
    for yy in range(OV_H):
        for xx in range(OV_W):
            ov[xx, yy] = 7
    ov_tg = displayio.TileGrid(ov, pixel_shader=ov_pal, x=20, y=100)
    g2.append(ov_tg)
    display.root_group = g2

    after_b = _mem()
    print("  bg %dx%d streamed from flash, %d bpp" % (
        odb.width, odb.height, getattr(odb, "bits_per_pixel", 0)))
    print("  free after alloc: %d  (cost %d bytes)" % (after_b, before_b - after_b))

    def move_b(i):
        # Same motion, but achieved by moving the overlay rather than
        # repainting -- displayio has to recomposite the vacated and newly
        # covered rows, reading the background back out of flash to do it.
        ov_tg.x = 20 + (i * 8) % 180

    lo2, avg2, hi2 = _time_frames(display, move_b)
    print("  refresh: avg %.1fms  min %.1f  max %.1f" % (avg2, lo2, hi2))

    # ---- verdict ---------------------------------------------------------
    print("\nSUMMARY")
    print("  A  RAM bitmap    : %6.1fms/frame   %d bytes RAM" % (avg, base - after_a))
    print("  B  flash-streamed: %6.1fms/frame   %d bytes RAM" % (avg2, before_b - after_b))
    saved = (base - after_a) - (before_b - after_b)
    print("\n  RAM saved: %d bytes" % saved)
    if avg2 <= avg * 1.5:
        print("  Refresh cost comparable -- streaming looks viable.")
    else:
        print("  Refresh cost %.1fx worse -- streaming may not be worth it." % (avg2 / avg))


main()
