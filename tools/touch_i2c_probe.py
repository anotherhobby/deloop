# touch_i2c_probe.py -- characterise the FocalTouch I2C read fault.
#
# app.py's _handle_touch now survives these errors (a failed read skips the
# frame instead of reporting a release -- see docs/architecture.md's "Touch
# controller"), but that fixed the INTERPRETATION, not the fault. This asks
# the questions the fix deliberately left alone:
#
#   - how often does the read actually fail?
#   - do failures cluster into bursts, or arrive independently?
#   - does display activity make it worse? (the obvious suspect for bursts
#     that cluster right after boot, when the UI is drawing hardest)
#
# Burst structure is the interesting part. Independent random failures would
# be mostly runs of 1. The live capture that found this bug showed 8 failed
# reads back to back, which is not what independent errors look like -- so
# either something holds the bus, or one failure disturbs the next read.
#
# NOT tested here: bus frequency. CLAUDE.md's hard rule is that this project
# always uses board.I2C() (the singleton), never busio.I2C(...), so changing
# the frequency would mean violating it -- and the singleton is shared with
# whatever else is on the bus. If the numbers below point at bus speed, that
# is a decision to make deliberately, not a knob to twiddle from a probe.
#
# RESULTS, 2026-08-06 (20,000 reads per phase, finger off the glass):
#
#   PHASE 1  idle       830/sec   3 failures (0.015%)  longest burst 2
#   PHASE 2  display    316/sec   5 failures (0.025%)  longest burst 1
#
# Two conclusions, one of them a retraction:
#
#   - Display activity is NOT implicated. An earlier run of this probe showed
#     a 37x higher per-read failure rate under display load, but that run
#     dirtied the whole screen and so sampled at 7 reads/sec against 807 --
#     an artifact of the sampling rate, not a real effect. With the rates
#     brought within ~2.6x, the difference disappears into noise (n=3 vs n=5;
#     Poisson error bars on those overlap completely).
#   - The baseline fault is real but RARE: ~0.02% of reads, roughly one
#     failure per 5,000. At the app's ~60Hz touch sampling that is one failed
#     read every ~50 seconds, which is nowhere near enough to lose a tap by
#     chance. Something must concentrate them.
#
# What this probe could NOT reproduce: the bursts of 8 consecutive failures
# seen live in app.py. The longest burst here is 2. The obvious untested
# variable is the one this probe deliberately excludes -- A FINGER ON THE
# GLASS. During a real touch the driver also reads the larger `touches`
# register block, and the live failures clustered around actual taps. That is
# the next experiment, and it needs a human tapping, so it is not scripted
# here.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/touch_i2c_probe.py
#
# NOTE: mpremote run soft-reboots and leaves the device at a REPL rather than
# running code.py. Put the app back afterwards with:
#   ./.venv/bin/python -m mpremote connect auto exec "import supervisor; supervisor.reload()"

import time

import board
import displayio
import vectorio
from adafruit_focaltouch import Adafruit_FocalTouch

# Both phases must run at a COMPARABLE read rate or the comparison is
# meaningless. The first version of this probe dirtied the full 240x240 screen
# each iteration, which cost 133ms per refresh and dropped phase 2 to 7
# reads/sec against phase 1's 807 -- so the two phases sampled completely
# different timescales, and normalising per-read vs per-second gave opposite
# answers. displayio's refresh cost scales with the DIRTY REGION, not screen
# size (docs/rendering.md), so a small rect keeps the rates close.
READS = 20000
DIRTY = 24        # px square redrawn in phase 2


def _summarise(name, errs, bursts, elapsed, n):
    pct = (100.0 * errs / n) if n else 0.0
    rate = (n / elapsed) if elapsed else 0.0
    print("\n%s" % name)
    print("  reads        : %d in %.2fs (%.0f/sec)" % (n, elapsed, rate))
    print("  failures     : %d (%.2f%%)" % (errs, pct))
    if not bursts:
        print("  bursts       : none")
        return
    hist = {}
    for b in bursts:
        hist[b] = hist.get(b, 0) + 1
    print("  bursts       : %d, longest %d" % (len(bursts), max(bursts)))
    print("  burst sizes  : %s" % ", ".join(
        "%dx%d" % (hist[k], k) for k in sorted(hist)))


def phase(name, touch, n, work=None):
    errs = 0
    bursts = []
    run = 0
    last_err = None
    t0 = time.monotonic()
    for _i in range(n):
        try:
            touch.touched
            if run:
                bursts.append(run)
                run = 0
        except Exception as e:
            errs += 1
            run += 1
            last_err = e
        if work is not None:
            work()
    if run:
        bursts.append(run)
    _summarise(name, errs, bursts, time.monotonic() - t0, n)
    if last_err is not None:
        print("  last error   : %s %s" % (type(last_err).__name__, last_err))
    return errs


def main():
    print("\nFocalTouch I2C probe -- keep your finger OFF the glass")
    print("(a real touch changes what the chip reports, not whether the")
    print(" read succeeds, but it keeps the comparison clean)")

    i2c = board.I2C()   # hard rule: the singleton, never busio.I2C(...)
    touch = Adafruit_FocalTouch(i2c, address=0x38)

    total = 0
    total += phase("PHASE 1: idle bus", touch, READS)

    # Phase 2: same reads, but with the display doing real work between each
    # one. vectorio + a refresh is what the shipped gauge actually costs
    # (docs/rendering.md), so this is representative rather than synthetic.
    pal = displayio.Palette(2)
    pal[0] = 0x000000
    pal[1] = 0x202020
    group = displayio.Group()
    rect = vectorio.Rectangle(pixel_shader=pal, width=DIRTY, height=DIRTY,
                              x=120 - DIRTY // 2, y=120 - DIRTY // 2)
    group.append(rect)
    board.DISPLAY.auto_refresh = False
    board.DISPLAY.root_group = group

    state = [0]

    def redraw():
        state[0] ^= 1
        rect.color_index = state[0]
        try:
            board.DISPLAY.refresh()
        except Exception:
            pass   # refresh() raises if called faster than the panel allows

    total += phase("PHASE 2: display refreshing between reads", touch, READS,
                   work=redraw)

    print("\n%s" % ("-" * 52))
    print("Check the reads/sec of both phases FIRST. If they differ by more")
    print("than roughly 2x, the phases sampled different timescales and the")
    print("per-read rates are not comparable -- normalising per-read and")
    print("per-second can give opposite answers. Fix the rates, then compare.")
    if total == 0:
        print("No failures in %d reads. Either the fault is rarer than this"
              % (READS * 2))
        print("sample, or it needs conditions this probe does not reproduce")
        print("(boot-time contention, WiFi active, a finger on the glass).")
        print("Do NOT conclude the bus is clean -- the live capture that")
        print("found the bug saw bursts of 8.")
    else:
        print("Compare the two phases: a materially higher failure rate in")
        print("PHASE 2 implicates display/bus contention. Similar rates mean")
        print("the fault is independent of display activity, and bus speed or")
        print("pull-ups become the next thing to look at.")


main()
