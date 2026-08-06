# Rendering: the gauge is composed, not painted

**Status:** landed 2026-08-05, running on hardware.

## What changed

The gauge used to live in one 240x240 4bpp `displayio.Bitmap` -- 28,800 bytes,
the largest allocation on the device -- and every change was re-derived in
Python: `_arc_solid`'s scanline edge-crossing math, `_restore_region`'s
erase-and-repaint, `_tri`/`_line`/`_thick_tick`/`_rect_outline`. An
immediate-mode canvas, whose maintenance was roughly 500 of `dial_ui.py`'s
1,600 lines.

It is now retained `vectorio` shapes, one `displayio.Group` per layer. Screens
compose themselves by setting `hidden` flags and colour indices. Nothing is
erased, because nothing is painted over.

## Measured on hardware

| | painted | composed |
|---|---|---|
| displayio pool | 28,800 bytes | 7,200 bytes |
| GC heap free, after `init()` | ~37KB (documented steady state) | 76,816 bytes |
| `draw_main` | ~350 ms | ~92 ms |
| `draw_volume` (encoder tick) | ~48 ms | ~17 ms |
| mute / busy transition | full repaint | `color_index` write, no repaint |

**Both pools improved.** The displayio saving was the goal; the GC-heap
result was a surprise and initially feared to be a regression, on the theory
that `vectorio` point lists would eat the heap the bitmap never touched. They
do live there -- but they cost far less than the ~500 lines of drawing code
that stopped being loaded. Net: roughly double the free GC heap.

**The speedup is real but was over-claimed at first.** An early pointer-only
spike measured 1.7ms/frame and that number briefly appeared here as the
headline. A real frame also sets label text, runs the status rows, and dirties
a much larger region. ~92ms and ~17ms are the honest figures. Do not quote
1.7ms.

## Why this mattered beyond smoothness

The main loop is cooperative and pumps the async status poll once per
iteration. At ~350ms per `draw_main` the loop ran at ~3 iterations/sec and each
poll -- needing ~3 pumps -- stretched to ~1000ms. That reads as "flaky
networking" while the network is perfectly healthy, and dropped touch events
have the identical cause. Frame cost is a correctness property of this loop,
not a nicety.

## Measuring: `gc.mem_free()` is not enough

**`gc.mem_free()` cannot see `displayio.Bitmap` allocations.** Confirmed three
times on 2026-08-04/05: `dial_ui.init()` allocating the ~28.8KB bitmap moved it
by ~3.8KB; a standalone `displayio.Bitmap(240, 240, 16)` reported a cost of
**432 bytes**; boot logs showed identical free memory on failing and succeeding
boots. displayio allocates outside the GC heap -- it survives soft reload,
which is the giveaway.

Measure that pool in its own currency instead: allocate
`displayio.Bitmap(120, 120, 16)` (7,200 bytes each) until `MemoryError` and
count them. `pool_probe()` in `tools/render_verify.py`. It priced the old gauge
bitmap at exactly 28,800 bytes -- four blocks, on the nose.

Both instruments are needed, and each is wrong for the other's question.
`gc.mem_free()` is blind to displayio; the pool probe is blind to the GC heap
where `vectorio` point lists and module bytecode live. Checking only one is how
this work first reported a saving it had not verified.

**A pool-probe run leaves the GC heap fragmented.** A `gc.mem_free()` taken
straight afterwards reads far too low (16KB was observed, against a real
76.8KB). Take heap readings before the probe, not after.

## This does NOT help networking

Measured directly: the allocatable pool with the radio down and associated is
identical (100,800 bytes both ways, 7,200-byte resolution). The WiFi driver
takes nothing measurable from the pool the gauge bitmap lived in. The original
framing of this work -- reclaim the bitmap, give the driver room -- was wrong.

## Traps

**Thin strokes do not survive integer rounding.** Both bugs found while
building this were this bug, and every future thin element will hit it:

- **Tick quads.** A symmetric `+/- thickness/2` half-width rounds outward on
  both sides and inflates every tick by a pixel each way; rounding inward
  collapses a 1px tick to zero area. Use asymmetric offsets `-t//2` and
  `-t//2 + t`.
- **Diagonal 1px strokes.** A 45-degree segment's unit normal is
  `(0.707, 0.707)`; `int()` truncates both offset points back onto the
  originals, giving a zero-area polygon that renders as **nothing at all**, with
  no error. One MENU-home leg vanished this way while the other survived,
  because its normal had a negative component that truncated to -1. Offset a
  whole pixel along the minor axis instead -- which is also what a Bresenham
  line produces.

Host-side rendering will not catch these: Pillow's polygon fill rule is not
vectorio's, so stroke-weight deltas off-device are noise. Confirm thin strokes
on the glass.

## Design notes

- **One shared `Palette`; every shape picks an index via `color_index`.**
- **Muted / busy are a `color_index` rewrite over the arc sectors.** No
  repaint. This is what retired `_scene_signature()`.
- **Hand the arc shapes back from the builder**, don't filter the group by
  colour afterwards -- the selected preset outline shares `_ORG` with the arc's
  orange band and would get recoloured on mute.
- **Arc sector count barely matters.** 4 sectors 1.74ms vs 24 sectors 1.65ms
  per frame; the bounding-box culling this was expected to need turned out to
  be irrelevant. `_ARC_SECTORS = 12` only places the colour boundaries.
- **6-degree polygon steps** keep the chord sagitta at r=102 under 0.6px.
- **Outlines are two nested `Rectangle`s** (fill + background inset), not four
  edges.
- **displayio does not clear uncovered pixels** -- the black background is a
  real full-screen `vectorio.Rectangle`. `show_splash()` always did the same.

## Visual verdict

Composed ticks render brighter and heavier than the painted ones, and that
reads **better** on the real panel -- confirmed on hardware. Do not chase
parity with the old tick weight; the difference is an improvement. Arc colour
boundaries are exact (384.00 / 408.00 / 435.92 degrees, verified numerically
against `_arc_color`).

## Races this exposed

Speeding the loop up unmasked two latent races, both of the same shape: a
local state change applied optimistically, then a poll issued *before* the
device acted writes the stale value straight back. Both had always been races;
both were previously hidden by ~1000ms poll latency giving the device time to
settle before the answer arrived.

- **Mute.** Root cause was sharper than "a poll landed": the breathing cycle is
  `frac = 0.5*(1 - cos(2*pi*t/8))`, which is 0 at t=0, so the cycle *starts* at
  a trough -- and `_pulse_mute` polls at the trough. `_start_mute_pulse` armed
  that poll, firing one milliseconds after the mute command. Fixed by starting
  with `mute_trough_polled = True`, skipping only that one trough. Backed up by
  `_MUTE_SETTLE_S` (1.5s), which also covers the unmute-on-volume-turn path.
- **Play/pause.** `state.media_state` is applied optimistically and
  `apply_status()` overwrites it. Same guard, `_MEDIA_SETTLE_S`. Only reachable
  on backends that report `media_state` (ha, wiim).

Audited and found **safe**: `preset`/`preset_enabled` (`apply_status()` never
writes them) and `volume_db` (`_poll_avr` skips while the encoder is moving,
and `recently_active` forces a 30s interval afterwards). `power` was already
guarded by `_POWER_SETTLE_S`.

Also fixed: `_handle_touch` treated any single untouched frame as a release, so
one FocalTouch dropout mid-press dispatched two taps -- a mute button that
toggled twice. Unreachable at ~3 iterations/sec; reachable once the loop
actually samples at 60Hz. `TOUCH_RELEASE_S` debounce plus a `TAP_RELOCK_S`
lockout.

**Assume there are more.** Every optimistic write is a candidate, and the
pattern only becomes visible when something gets faster.

## Tools

- **`tools/render_verify.py`** -- on-device check: pool cost, per-frame cost of
  `draw_main`/`draw_volume`, and every screen exercised once. Holds
  `pool_probe()`.
- **`tools/dial_sim.py`** -- renders every screen off-device as PNGs. Shims
  `vectorio` and walks the group tree honouring `hidden`. Good for geometry,
  colour and layout; not for stroke weight.
- **`tools/render_timing_fastpath.py`** -- frame cost under a real network
  load. Its "restore" column is structurally zero now and kept only so the
  historical output lines up.

Removed when this landed, all of them measuring or verifying code that no
longer exists: `render_timing_check.py`, `profile_arc.py`, `verify_draw_main.py`,
`chaos_stress_test_a2.py`, `make_gauge_bg.py`, `gauge_stream_check.py`,
`vector_gauge_spike.py`, `vector_gauge_preview.py`.

## Superseded: the flash-streaming plan

An earlier plan streamed the static gauge layers off flash with
`displayio.OnDiskBitmap`. It would have reclaimed most of the same RAM but kept
the immediate-mode model, kept `_restore_region` for the pointer, added four
generated BMP assets and a build step, and introduced a failure mode:
`tools/build_release_manifest.py` is an explicit allowlist that excludes binary
assets, so an OTA shipping changed gauge geometry would have left stale
backgrounds on the device with no error. Composing has none of that.

One idea from it survived and got better: muted/busy differ only in arc
*colour*, never geometry. That was going to be a palette swap on one BMP; it is
now a `color_index` write on 11 shapes.
