# Memory headroom: streaming static graphics off flash

**Status:** open investigation, groundwork built, not yet conclusive.
**Started:** 2026-08-05, out of the 2026-08-04/05 networking investigation.

## Why this matters

This started as a *networking* idea, not a graphics one.

deloop runs at roughly **37KB free** in steady state and ~42KB at the
tightest point of boot. A recurring, still-unexplained failure has the
device associate to WiFi, take a correct DHCP lease, and then fail every
subsequent request permanently until power-cycled. Several memory theories
around it have died (see "What we already ruled out"), but the general
principle stands: an ESP32-S3 running this close to the edge has no margin
for anything -- not the WiFi driver's buffers, not TLS, not a future
feature.

The single largest allocation in the whole program is the gauge bitmap:
**240 x 240 at 4bpp = 28,800 bytes**. If most of that could be reclaimed,
free memory would roughly double. That headroom is the point. Any
improvement to rendering speed is a bonus, not the goal.

## The critical measurement trap

**`gc.mem_free()` cannot see `displayio.Bitmap` allocations.** Confirmed
three separate times on 2026-08-04/05:

- `dial_ui.init()` allocates the ~28.8KB bitmap plus fonts and labels, and
  `gc.mem_free()` moves by only ~3.8KB.
- A standalone `displayio.Bitmap(240, 240, 16)` reported a cost of **432
  bytes**.
- Boot logs showed *identical* free memory on successful and failing boots,
  which is part of why the memory theories looked dead.

displayio allocates outside CircuitPython's GC heap (it survives soft
reload, which is the giveaway), and the ESP32 WiFi driver allocates from the
IDF heap, a third pool again. **Any future work here must not use
`gc.mem_free()` as the success metric.** It will report a saving of roughly
zero no matter how much RAM is actually freed. Find a way to measure the
real pool before trusting any result.

## The idea

`displayio.OnDiskBitmap` streams pixel rows off flash during refresh and
needs no RAM for the image. It is **read-only** -- it cannot replace the
bitmap we draw into.

But the gauge splits cleanly into static and dynamic layers, which is
exactly the split `dial_ui._scene_signature()` already exploits:

- **Static:** arc, tick marks, menu-home frame. Depend only on
  `(muted, busy, power_off)` -- four combinations in practice.
- **Dynamic:** the pointer. The only thing that moves frame to frame.

So: stream the static layer from flash as the background TileGrid, and keep
a *small* writable bitmap for the pointer on top.

Two wins if it works:

1. Most of the 28,800 bytes comes back.
2. `_restore_region()` disappears entirely -- there is no arc to repaint.
   That is currently 24ms of the ~48ms incremental redraw.

## What already exists

- **`tools/make_gauge_bg.py`** -- renders the static layers to 4bpp
  uncompressed BMPs, generated from `dial_ui`'s own index data via
  `tools/dial_sim.py`'s CircuitPython shims. The asset therefore comes from
  the exact code that draws the gauge today, not a hand-made copy. Emits
  four variants (normal / muted / busy / power_off), 28,918 bytes each, to
  `local/gauge_bg/`. Verified to round-trip correctly through PIL.
- **`tools/gauge_stream_check.py`** -- on-device A/B of refresh cost: same
  overlay motion over a RAM bitmap vs. an `OnDiskBitmap`. Run with
  `mpremote run`.

## Measurements so far (and why they are not conclusive)

First run, 20 frames per scenario:

```
A  RAM bitmap    : 12.0ms/frame   (min 4.6, max 151.1)
B  flash-streamed: 20.9ms/frame   (min 7.1, max 283.4)
```

~1.7x slower per refresh -- but the comparison is confounded, in ways that
all penalise B unfairly:

- B **moves a TileGrid**, dirtying two regions (vacated + newly covered).
  A dirties one.
- B's overlay calls `make_transparent(0)`, forcing alpha compositing that A
  never pays.
- The memory columns are meaningless -- see the measurement trap above.

Napkin arithmetic against today's measured ~48ms incremental redraw
(`_restore_region` 24ms + pointer 3ms + labels 4.5ms + refresh 16.5ms):

| stage | today | streamed |
|---|---|---|
| restore arc | 24ms | 0ms |
| pointer | 3ms | ~2ms |
| labels | 4.5ms | 4.5ms |
| refresh | 16.5ms | ~21ms |
| **total** | **~48ms** | **~28ms** |

That suggests it could be *faster* as well as smaller -- but this is
arithmetic layered on a confounded measurement, not a result. Treat it as a
reason to run a fair test, nothing more.

## Design problems still unsolved

1. **Pointer geometry is the hard part.** The pointer sweeps a ring whose
   bounding box is nearly the full screen, so a small *static* sprite is not
   enough. It needs a small bitmap that is **redrawn in local coordinates
   and repositioned** each frame. `_draw_pointer()` and `_restore_region()`
   both assume full-screen coordinates today.
2. **Other things are drawn into the gauge bitmap.** The preset quick-select
   button outlines (`_draw_preset_buttons`) and the play/pause icon
   (`draw_play_pause_icon`, used by `ha_ui.py`) both write into it. They
   would need to become labels, `vectorio` shapes, or a second small bitmap.
3. **Four variants means four assets**, and any change to gauge geometry or
   colours then means regenerating them rather than just editing code. The
   generator makes that cheap, but it is a new build step.
4. **Flash read cost under real motion** is the number that decides
   viability, and it has not been fairly measured yet.

## Suggested next steps

1. Fix the A/B harness: identical dirty-region behaviour on both sides, same
   transparency settings, and motion representative of a real pointer.
2. Find a way to measure the non-GC allocation pool so the RAM saving can be
   confirmed rather than assumed.
3. Only then prototype the moving-pointer bitmap.

## What we already ruled out (so it is not re-litigated)

From the 2026-08-04/05 networking investigation, all with evidence:

- **Socket/pool exhaustion** -- fails on the first request of a fresh stack;
  freshly created raw sockets fail too.
- **Access point choice** -- pinning association to the "bad" BSSID gave 4/4
  working TCP (`tools/wifi_pin_check.py`).
- **CircuitPython heap pressure** -- identical `gc.mem_free()` on success and
  failure (and see the measurement trap: that metric was never valid here).
- **Boot ordering** -- connecting WiFi before `dial_ui.init()` did not fix
  it, and then failed harder: the display allocation is the fragile one, so
  associating first left too little contiguous RAM and `dial_ui.init()`
  raised `MemoryError` on every boot.

The surviving lead on the networking bug is `EHOSTUNREACH` (errno 118)
appearing alongside `ETIMEDOUT` -- DHCP succeeds (broadcast) while every
unicast peer fails, including the gateway. That points at ARP resolution
failing, and is independent of this memory work.
