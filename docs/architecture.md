# deloop Architecture

The living design doc for deloop: project summary, stack decision, current layout, input model,
runtime architecture notes, configuration reference, boot-memory guardrails, and current status.
See [docs/hardware.md](hardware.md) for physical/flashing details, and
[docs/device-drivers.md](device-drivers.md) for the pluggable backend architecture and each
backend's protocol reference.

Last updated: 2026-08-05 (v2.6 -- the gauge is composed from `vectorio` shapes instead of painted
into a 240x240 bitmap: 28,800 bytes of the displayio pool reclaimed, GC heap free roughly doubled,
`draw_main` ~350ms -> ~92ms, and ~500 lines of raster primitives deleted. See
[docs/rendering.md](rendering.md). That speedup unmasked three latent bugs -- two optimistic-write
races and a touch double-dispatch -- all fixed. Separately, the long-running cold-boot networking
failure stopped entirely once an overlapping AP was disabled; see the next-session prompt for why
that does not contradict the earlier elimination.)

Previously: 2026-08-03 (v2.5 -- self-update (OTA) now fetches from a flat S3 layout: deloop can
pull app-file releases over Wi-Fi via a manual Update menu, live `storage.remount()` +
`supervisor.reload()` only (never a hard reset -- see why below), with a fully automated "merge to
main = new release" versioning workflow. Confirmed live, real hardware, full 16-file Install Update
through the real menu path -- see [docs/ota.md](ota.md) for the full design.)

This file (now `docs/architecture.md`) captures the current project discussion so a future agent
session can build a full project plan without needing the whole conversation repeated. It moved
out of `.claude/CLAUDE.md` on 2026-08-01 as part of a context-efficiency reorg (see
`.claude/CLAUDE.md`'s docs index) -- if you see an older reference to
`local/agent/project-context.md`, that's this same material from before an even earlier move.

## Project Summary

`deloop` is firmware for an M5Stack Dial that controls an amp/DSP (or a Home Assistant
`media_player` entity) over the local network -- originally a Denon/Marantz AVR, now also a
MiniDSP unit via minidsp-rs and any HA `media_player`, through a pluggable driver
(`src/driver.py`). The device is for an office AVR where normal control is awkward.

The primary interaction is rotary volume control. The most important display requirement is that the current AVR volume is always visible and stays accurate even when the volume changes from outside the Dial.

Initial proof-of-concept features from the README:

- Power on/off
- Volume up/down with current volume display
- Volume display updates when state changes externally
- Mute toggle
- Speaker preset selection
- Dirac preset selection
- Input selection

Longer term, this could become a more complete Denon/Marantz remote, but the project should start with a tight POC centered on volume and state feedback.

## User Preferences And Constraints

- The user is primarily a Python developer.
- C/C++ is possible, but should not be the default because maintainability would depend too much on AI assistance.
- The user prefers owning normal source files in a Git repo over vendor IDE project blobs.
- VS Code is the preferred development environment.
- Direct Dial-to-Denon control is preferred. The Denon modes are well understood, so there is no current reason to push Denon logic into Home Assistant or another service layer.
- Home Assistant can remain useful as a reference point for feature parity, but should not be the primary architecture unless a concrete limitation appears.
- Using M5Burner is acceptable. It is M5Stack's hardware and firmware tooling, so that coupling is not considered a major problem.
- The important sharing goal is: clone the repo, plug in the device, run a command to push the app files after the firmware is installed.

## Current Stack Decision

**CircuitPython 10.2.1 on M5 Dial** (pivoted from UiFlow2 on 2026-07-23).

- M5Stack Dial hardware
- CircuitPython 10.2.1 firmware (official board: `m5stack_dial`)
- Plain CircuitPython `.py` files in this repo as the source of truth -- edited and version
  controlled as `.py`, always
- Deployment via USB mass storage, but **not as plain `.py` day to day**: `make deploy`
  precompiles every module except `code.py` to `.mpy` first (`code.py` itself must stay
  uncompiled source -- it's the boot entry point) and is what the device should actually run; see
  "CircuitPython heap/boot-memory guardrails" below for why. Plain, uncompiled deployment is
  `make deploy-src` (fast dev iteration, doesn't require `local/mpy-cross` to be present), but
  isn't the shipped configuration. (Corrected 2026-08-01 -- these target names swapped meaning at
  some point after this was first written; see [docs/hardware.md](hardware.md)'s "Deployment
  Goal" for the full story and a live case of the stale version almost causing a wrong diagnosis.)

Flashing steps and the full hardware/pin/library reference live in
[docs/hardware.md](hardware.md); deploy-command mechanics (`make deploy`/`make shell`) are also
there.

### Why the pivot from UiFlow2

UiFlow2 firmware in WiFi mode does not expose a standard MicroPython REPL over USB serial. The serial port is a log-only output channel; `mpremote` cannot enter raw REPL mode. Switching to USB mode was not possible without reflashing. CircuitPython mounts the device as a USB drive (`CIRCUITPY`), making `make deploy` a simple file copy — exactly the workflow the project requires.

## Actual Project Shape (as of v2.2)

The original proposed shape below was Denon-only and pre-dates the driver split; kept for history. The real, current layout is:

```text
deloop/
  README.md
  .claude/
    CLAUDE.md         # this file
  local/               # gitignored -- mpy-cross binary, build staging, misc dev artifacts
  src/
    code.py          # CircuitPython entry point -- ONLY `import app; app.main()`. Must
                      # stay this thin: it's the one file that can't be .mpy-compiled.
    app.py           # the real entry-point logic -- input handling, menu state machine,
                      # main loop. What code.py used to be, before the boot-memory fix.
    driver.py         # selects the active backend module (+ its UI extension, if any);
                      # documents both the driver contract and the UI-extension contract
    denon.py          # Denon/Marantz backend
    minidsp.py         # minidsp-rs backend
    camilladsp.py       # CamillaDSP backend -- WebSocket-only, hand-rolled client,
                      # confirmed working on real hardware 2026-07-30 (see Backend #5 below)
    ha.py              # Home Assistant media_player backend
    ha_ui.py           # HA's paired UI extension (row-swap, skip icons, play/pause icon) --
                      # only imported when DEVICE_DRIVER=ha; see driver.py's UI-extension contract
    wiim.py            # WiiM/LinkPlay streamer backend -- first backend needing TLS
    wiim_ui.py         # WiiM's paired UI extension (play/pause + skip tap targets only) --
                      # only imported when DEVICE_DRIVER=wiim
    dial_ui.py        # display rendering -- generic gauge/volume/menu chrome only
    state.py          # in-memory device state model
    sound.py          # piezo click feedback
    config.py         # settings.toml loader/defaults
    ota.py             # self-update: check/download/verify/install a release from S3 --
                      # see docs/ota.md. Pure network+filesystem, no NVM/reset.
    ota_boot.py          # code.py imports THIS instead of app.py when an OTA action is
                      # pending -- never imports driver.py/dial_ui.py/the backend module,
                      # since importing those alone leaves too little free memory for a
                      # reliable TLS handshake. See docs/ota.md.
    version.py          # CURRENT_VERSION placeholder (real value baked in by the release
                      # workflow, never committed back)
    settings.toml.template
  tools/
    probe_denon.py     # host-side Denon HTTP API probe
    probe_minidsp.py   # host-side minidsp-rs HTTP API probe
    probe_ha.py        # host-side HA REST API probe
    probe_wiim.py      # host-side WiiM/LinkPlay HTTPS API probe
    probe_ota.py        # host-side sanity check for the CI-published GitHub Release
    build_release_manifest.py   # builds manifest.json from a built dist/ dir (make build-manifest)
    dump_denonavr.py
    dial_sim.py        # renders dial_ui.py off-device to PNGs (make renders)
    render_ui_screenshots.py    # renders README's per-backend screenshots (make ui-renders)
    make_splash.py, gen_click.py
  .github/
    workflows/release.yml   # builds + publishes a new OTA release on every push to main
  Makefile
```

See [docs/device-drivers.md](device-drivers.md)'s "Device Driver Architecture" section for what
`driver.py`/`denon.py`/`minidsp.py`/`ha.py`/`wiim.py` actually look like and how to add a sixth
backend, and "CircuitPython heap/boot-memory guardrails" below for why `code.py`/`app.py` are
split the way they are.

### Original proposed shape (v1.0, historical -- superseded)

```text
deloop/
  README.md
  agent/
    project-context.md
  src/
    main.py
    config.py
    denon.py
    state.py
    controls.py
    dial_ui.py
  tools/
    detect_port.py        # optional, only if mpremote auto is not enough
  Makefile
  pyproject.toml          # optional host-side tooling only
```

(`main.py` never happened -- CircuitPython's entry point convention is `code.py`, which is what shipped. `controls.py` input handling ended up living in `code.py` itself rather than a separate module.)

## Input Model (final)

| Gesture | Action |
|---|---|
| Rotate encoder | Volume (MAIN); navigate menu (MENU) |
| Encoder press | Open menu; enter submenu; confirm selection |
| Touch hold 0.5s | Mute toggle (MAIN only) |
| Touch hold 1.5s | Power toggle (MAIN only, suppresses mute) |
| Touch tap (<0.5s) | Close menu |
| 8s idle in menu | Auto-close menu |

## Architecture Notes

### Polling strategy
- State is **optimistic**: display updates immediately, poll is error-correction.
- Adaptive interval: standby -> 5s; recently active -> 30s; truly idle -> 5s.
- Poll never runs while encoder is moving.
- Any AVR command resets `last_poll` to prevent back-to-back HTTP calls.

### Socket management (CircuitPython)
Every `adafruit_requests` response MUST be `.close()`d. CircuitPython has
a small socket pool (~4). Unclosed responses cause "Out of sockets" errors.
All `denon.py` helpers close in `finally` blocks.

### Non-ASCII in source files
CircuitPython rejects non-ASCII even in comments. Use ASCII only:
`--` not em-dash, `->` not right-arrow, `+` not box-drawing characters.

**Discrepancy found 2026-07-28, unresolved:** `dial_ui.py` already had plenty of non-ASCII
characters in *comments* (em-dashes, right-arrows, degree signs, box-drawing section dividers)
well before this session, and the device has been deployed/tested repeatedly throughout this
project without a reported boot crash traceable to them. That contradicts "even in comments" as
written. Possible explanations, unconfirmed: the original crash that produced this lesson was
actually in a string literal, not a comment, and got written up imprecisely; or comments get
stripped before whatever check chokes on non-ASCII and only literals are actually at risk. Until
someone confirms which, treat *string literals* as the confirmed-dangerous case (see the
`chr()`-not-`\u`-escape lesson two sections below, which sidesteps this regardless of which
explanation is right) and don't treat existing non-ASCII comments elsewhere in the file as
something that needs fixing on sight.

### Brightness persistence
Stored in `microcontroller.nvm[0]` as `int(brightness * 100)`.
255 = uninitialized (defaults to 1.0). Saved on menu confirm (encoder press).

### Touch controller
Always use `board.I2C()` (singleton), never `busio.I2C(board.SCL, board.SDA)`.
"No I2C device at 0x38" on boot = stale lock from REPL session; power-cycle.

### Menu tap hit-testing must be bounded by the menu's real item count (found + fixed 2026-08-01)
`app.py`'s `_menu_item_at_y(y)` used to scan all `MENU_VISIBLE` (5) slot positions unconditionally,
regardless of how many items the current menu actually has. A menu with fewer than 5 items --
the Update submenu's single "Check Now" row is the extreme case -- had "phantom" hit zones below
its real content: a tap landing near one of those unused slot positions returned a real index that
then failed the caller's `tapped < actual count` bounds check, which was indistinguishable from
"tapped outside every item" and closed the menu entirely. User-reported symptom: "the first few
taps always just throw me out of the menu," specifically right after a fresh boot into the Update
submenu (one item, a 34px-tall real target near screen center out of the whole touch area) --
resolved on its own once a check/install result added a second item and widened/recentred the
real target. Fixed by requiring every caller (`_tap_menu_top`, `_tap_menu_dev`, `_tap_menu_sub`) to
pass its own real, currently-visible item count into `_menu_item_at_y(y, max_items)` -- no default,
so a future caller can't reintroduce this by forgetting to pass one. Affects all three menu levels
in principle, not just Update -- that submenu just made it obvious by being the one place a menu
can have just one item.

**Confirmed live NOT sufficient by itself -- a second, independent bug was also in play.**
Deployed and re-tested: tapping "Check Now" still closed the menu immediately, identically. Traced
to `_handle_touch_down()`: `touch_y` was set only once, on the very first frame of a touch-down,
while `touch_x` kept updating on every later frame (for swipe detection). If that first frame's
read came back phantom/empty -- the FocalTouch driver can report `touched > 0` with an empty
`touches` list after filtering invalid points, confirmed reachable -- `touch_y` stayed stuck at its
reset value of `0` for the entire gesture, even once a later frame got a real reading. Since every
real menu item sits near vertical screen center, a stuck `touch_y=0` never matches any item's hit
band regardless of how many items exist or how correctly `_menu_item_at_y` is bounded --
indistinguishable from "tapped outside everything," closing the menu on release. `touch_x_start`
(the swipe-detection baseline) had the same latent gap for the same reason. Fixed: both are now set
on whichever frame first produces a real point, not necessarily the literal first frame, and
`touch_x`/`touch_y` both keep updating on every later frame a valid point comes back, matching what
`touch_x` already did on its own. Both fixes are needed together -- neither alone was sufficient,
which is exactly why the symptom looked identical before and after the first fix alone. Not yet
confirmed live.

**Root-caused and fixed 2026-08-04/05.** For the record, this was real, 100% reproducible, and
predated the session that first documented it -- an early theory that `mpremote exec` was
interrupting `app.py` was wrong and was correctly rejected by the user. Symptom as reported:
opening the top-level menu took "tons of tries," and taps in short submenus reliably dropped back
to the main screen. There were **two independent causes**, which is why earlier single-cause fixes
kept coming up short:

1. **Rendering was starving the cooperative main loop.** `draw_main()` repainted the entire gauge
   on every call: ~330-380ms measured on hardware, of which `_arc_solid` was ~145ms and
   `display.refresh()` ~173ms. The loop pumps the async status poll once per iteration, so a
   ~350ms redraw dropped it to ~3 iterations/sec -- which drops touch frames *and* stretches each
   poll to ~1000ms. The "flaky networking" and the dropped taps were the same bug. Fixed by
   `_scene_signature()` (skip the static repaint when only the pointer moved) plus
   `_arc_window_rows()` (bound the row scan to the window actually touched). Poll throughput went
   0.66/s -> ~33/s with zero errors; redraws ~350ms -> ~48ms. See "Rendering cost is a networking
   problem" below.
2. **The quick-select buttons' hit target was far too small.** Drawn at 22px wide with 14px gaps --
   about 3mm on this panel, well under the ~7-9mm a fingertip reliably lands on. Logging every tap
   coordinate showed taps landing within ~20px of a button centre but outside the rect, so nothing
   happened. `preset_button_at()` now picks the nearest button within tolerance instead of
   requiring a rect hit, and caps `n` at `_DBTN_MAX` so the hit-test can't use a layout that was
   never drawn.

Methodological note worth keeping: the first cause was only found by *measuring each render stage
on hardware* (`tools/render_timing_check.py`), after a Mac-side profile had suggested the arc fix
was already sufficient. It wasn't -- the on-device numbers were ~50x the host's, and
`display.refresh()` turned out to be the largest single stage, which no amount of reading the code
would have revealed.

### Power-off screen flashing at boot (found + fixed 2026-08-01)
User-reported: "it goes to the power off screen before the menu screen. it's not powered off."
An earlier, unrelated doc note ([docs/ota.md](ota.md)'s "Known-noisy diagnostic prints") had
attributed a similar-sounding report to OTA's network-timeout investigation -- that conclusion
was wrong and didn't hold up under actual investigation. Two independent, confirmed causes:

1. **`state.py`'s `AVRState.__init__` defaults `self.power = "STANDBY"`**, with no "not yet known"
   sentinel distinguishing "haven't polled yet" from "genuinely off." Any render that happens
   before the first real status poll lands will show the power-off screen, because that default
   is indistinguishable from a real reading.
   **Fixed properly 2026-08-05: `AVRState.power_known`** is False until `apply_status()` has
   actually heard from the device, and `draw_main()` refuses to render the standby ring without
   it -- falling through to `dial_ui.draw_reconnecting()` instead. `app.py` clears the flag again
   when a run of failed polls means contact is lost, so a stale "it was off last time we heard"
   can't keep rendering as fact. The check lives inside `draw_main()` deliberately: there are a
   dozen call sites and any of them could otherwise get it wrong. This mattered in practice --
   during the 2026-08-05 session an unreachable device and a genuinely powered-off one were
   indistinguishable on screen, and it misled both the user and the assistant for several rounds
   of debugging.
2. **`denon.py`'s `get_status()` used to silently convert any unparseable power reading into
   `"STANDBY"`** -- `raw_power = _tag(power_block, "zone1") or "STANDBY"`, then `if power not in
   ("ON", "OFF"): power = "STANDBY"` again. A missing `<zone1>` tag or a truncated/malformed
   response (boot is this codebase's most heap-fragmented, most truncation-prone moment -- see
   "CircuitPython heap/boot-memory guardrails" below) got silently smoothed into a real, definite-
   looking "STANDBY" instead of raising -- so a genuinely truncated first poll would show the
   power-off screen, then correct itself a few seconds later once a clean poll landed, exactly
   matching the reported symptom. Fixed: an unparseable power value now raises `RuntimeError`
   instead, routing through `_poll_now()`'s existing retry/error-count path (confirmed to already
   handle this correctly: no render happens on a failed poll, and `loop.first_poll` correctly
   stays `True` until a poll actually succeeds) rather than fabricating a wrong-but-plausible
   answer.
3. **`app.py`'s `_retry_presets()` rendered unconditionally** the moment preset names arrived,
   with no check on `loop.first_poll` -- and its own short retry interval could genuinely fire
   before the first real status poll completes, painting the power-off screen from the still-
   default `state.power`. This is universal across backends (every backend boots with the
   fabricated "STANDBY" default until its own first poll lands), not denon-specific like #2.
   Fixed by gating that render on `not loop.first_poll`.

All three landed together; #2 and #3 are independent triggers for the same underlying exposure (#1)
and both needed fixing since either alone can cause the flash. Not yet confirmed live.

### Rendering cost is a networking problem (2026-08-04)

The main loop is cooperative and single-threaded. It pumps `denon.py`'s async status-poll state
machine exactly once per iteration, so a poll's wall-clock time is roughly

    poll time  ~=  (pumps needed per poll)  x  (loop iteration time)

That model predicted every measurement taken that day to within ~30%. The practical consequence is
blunt: **any per-frame rendering cost is multiplied into apparent network latency**, and shows up
as "the network is flaky" while the network is provably fine (0 errors throughout the same runs).

Numbers, measured on hardware with `tools/render_timing_check.py`:

| loop state | iters/sec | poll rate | poll latency |
|---|---|---|---|
| no rendering at all | ~189 | 42/s | 13-25ms |
| labels only | ~133 | 29.5/s | ~14ms |
| full gauge repaint (~350ms) | ~3 | 0.64/s | ~1100ms |

Two things follow, and both are easy to get wrong:

- **Latency metrics measured this way are measuring the loop, not the network.** Before assuming a
  network problem, check the loop rate.
- **`display.refresh()` cost scales with the dirty region, not with screen size.** The gauge bitmap
  is 4-bit indexed, so displayio converts every dirty pixel to RGB565 in software: a full-screen
  dirty region costs ~173ms, a labels-only frame ~5ms. Anything that calls `_clear()` therefore
  pays twice -- once to redraw, once because it dirtied everything. This is why `draw_main()`'s
  fast path exists.

Regression protection: `tools/verify_draw_main.py` requires an **exact zero-pixel diff** between
the incremental path and a clean full render across 17 scenarios (long volume sweeps, both
extremes, every scene-change field, and resuming after each of the other bitmap-writing screens).
Unlike `tools/profile_arc.py`, a nonzero diff there is a real failure, not rasterisation variance.

## Configuration (settings.toml)

`src/settings.toml` (gitignored). Template: `src/settings.toml.template` -- treat that file as the
source of truth for the current key list; it's been through several rounds of change since this
table was first written (the `SPEAKER_PRESET_1/2` keys below, for instance, no longer exist --
speaker/Dirac presets were unified into the generic `preset`/`preset_names` model described in
[docs/device-drivers.md](device-drivers.md)'s "Device Driver Architecture" section).

Original v1.0 key list (historical, Denon-only, some now removed):

| Key | Default | Notes |
|---|---|---|
| `WIFI_SSID` | (required) | |
| `WIFI_PASS` | (required) | |
| `AVR_HOST` | `192.168.1.100` | |
| `SPEAKER_PRESET_1` | `Preset 1` | **removed** -- superseded by generic presets |
| `SPEAKER_PRESET_2` | `Preset 2` | **removed** -- superseded by generic presets |
| `VOLUME_STEP` | `0.5` | dB/tick at normal speed |
| `VOLUME_STEP_FAST` | `2.0` | dB/tick at fast speed |
| `ACCEL_THRESHOLD` | `100` | ms between ticks for fast mode |
| `ACCEL_SAFETY_CAP` | `-15.0` | Max volume during fast upward spin |
| `POLL_INTERVAL` | `30.0` | Seconds between polls when recently active |

As of v2.2, `config.py` also has: `DEVICE_DRIVER` (`"denon"`, `"minidsp"`, `"ha"`, or `"wiim"`),
per-driver `VOLUME_MIN`/`VOLUME_MAX` defaults, the `MINIDSP_*` keys (`HOST`, `PORT`,
`DEVICE_INDEX`, `SERIAL`, `PRESET_COUNT`, `TIMEOUT`, `PRESET_TIMEOUT`), the `HA_*` keys (`HOST`,
`PORT`, `TOKEN`, `ENTITY_ID`, `TIMEOUT`), and the `WIIM_*` keys (`HOST`, `TIMEOUT`, `INPUTS`) --
see `settings.toml.template` for current defaults and comments on each.

`config.py` also has `OTA_ENABLED`/`OTA_S3_BASE`/`OTA_REPO`/`OTA_CHECK_TIMEOUT_MS`/
`OTA_INSTALL_TIMEOUT_MS`, orthogonal to `DEVICE_DRIVER` -- see [docs/ota.md](ota.md).

## CircuitPython heap/boot-memory guardrails (read before touching boot-path code)

The app once failed to boot on real hardware (`MemoryError` allocating the ~28.8KB gauge bitmap,
sometimes surfacing instead as a WiFi connect hang/failure depending on exactly how the heap
fragmented that boot) purely because the codebase had grown past a memory cliff on the ESP32-S3 --
not any single bug. Fixed via `.mpy` precompilation + splitting `code.py` down to a thin entry
point; see `Makefile` (`deploy` target -- renamed from `deploy-mpy` at some point after this was
written, see "Current Stack Decision" above) and `app.py`'s header comment for the mechanics. The
lasting rules that came out of it:

- **CircuitPython compiles a module's *entire* bytecode before running any of it**, and holds it
  resident for the module's lifetime. An unused function still costs heap at import time -- "it's
  behind a conditional/never called" does not make a module cheap; not *importing* it at all is
  the only thing that does. This is why `driver.py` only imports a backend (and, per its
  "UI-extension contract" comment, that backend's paired UI file) when it's actually the active
  one -- keep new backends and UI extensions following that same pattern.
- **`code.py` must stay tiny.** It's the one file CircuitPython requires as uncompiled source (the
  boot entry point), so it's the one file that can never be `.mpy`-compiled. Real logic belongs in
  `app.py` or another importable module. If `code.py` grows past a few lines again, that's a
  regression -- move the new code, don't leave it there.
- **`.mpy`-compile everything else** (`make deploy`) before treating a boot-memory question as
  answered. Needs `local/mpy-cross`, version-matched to the device's exact CircuitPython build
  (`cat /Volumes/CIRCUITPY/boot_out.txt`) -- the generic `pip install mpy-cross` package targets
  vanilla MicroPython and produces `.mpy` files this device won't load; get the real one from
  Adafruit's S3 bucket, linked from
  https://learn.adafruit.com/welcome-to-circuitpython/library-file-types-and-frozen-libraries.
  Compile to a local staging dir first, not directly onto the mounted CIRCUITPY drive -- writing
  `.mpy` output straight to the USB volume was observed to take 100+ seconds per file.
- **`gc.mem_free()` reports total free memory, not the largest contiguous block.** CircuitPython's
  allocator is non-compacting, so a "plenty free" number does not rule out a specific large
  allocation failing -- don't trust that number alone to declare a memory theory confirmed or
  refuted; a fragmentation problem can look identical to "no problem" until the one allocation that
  actually needs a big contiguous chunk hits it.
- **`gc.mem_free()` is blind to `displayio` and to the WiFi driver entirely.** There are three
  separate pools: CircuitPython's GC heap (what `gc.mem_free()` reports), displayio's allocations
  (which survive soft reload -- that's the giveaway), and the ESP32 IDF heap the WiFi driver takes
  its buffers from. Confirmed three times on 2026-08-04/05: `dial_ui.init()` allocating the ~28.8KB
  gauge bitmap moved `gc.mem_free()` by only ~3.8KB; a standalone `displayio.Bitmap(240, 240, 16)`
  reported a cost of **432 bytes**; and failing vs. succeeding boots showed *identical* free
  memory. This invalidated three separate conclusions in one session. **Never use `gc.mem_free()`
  as the metric for a displayio or WiFi memory question** -- it will report ~zero change no matter
  what actually happened. **Use the pool probe instead**: allocate `displayio.Bitmap(120, 120, 16)`
  in a loop until `MemoryError` and count them (`pool_probe()` in
  `tools/vector_gauge_spike.py`). That priced the gauge bitmap at exactly 28,800 bytes on 2026-08-05
  -- and showed the radio takes nothing measurable from that same pool, which kills the
  "reclaim display RAM to help WiFi" theory outright.
  See [docs/rendering.md](rendering.md).
- **RESOLVED 2026-08-05: the fragile display allocation is gone.** This slot used to hold a
  guardrail -- "do not fix boot by connecting WiFi first, the ~28.8KB gauge bitmap needs its
  contiguous chunk before the radio claims anything, or `dial_ui.init()` raises `MemoryError` on
  every boot." That was true, and it was tried and reverted the same day. It no longer applies:
  the gauge is composed from `vectorio` shapes and there is no 28.8KB contiguous allocation left
  to fail (see [docs/rendering.md](rendering.md)). The `show_splash()` -> `dial_ui.init()` ->
  `connect()` order is retained because it is also the right *user-facing* order, not because
  memory demands it. Kept here rather than deleted so the reasoning isn't rediscovered and
  re-applied to a constraint that has since evaporated.
- **Speeding up the main loop exposes latent races. This has now happened twice.** The pattern:
  a local state change applied optimistically, then a poll issued *before* the device acted writes
  the stale value straight back. These were always races; ~1000ms poll latency simply gave the
  device time to settle before the answer arrived.
    - 2026-08-04, power: the long-press applies optimistically and the confirming poll landed
      first, so `_apply_poll_result` read the disagreement as "powered on externally" and played
      the splash. Guarded by `_POWER_SETTLE_S` (10s).
    - 2026-08-05, mute and play/pause: same shape, after composing the gauge took `draw_main`
      ~350ms -> ~92ms. Mute's root cause was sharper than "a poll landed" -- the breathing cycle
      starts *at* a trough and `_pulse_mute` polls at troughs, so muting armed a poll milliseconds
      after its own command. Fixed at the source plus `_MUTE_SETTLE_S`/`_MEDIA_SETTLE_S` (1.5s).
      Audited safe: `preset`/`preset_enabled` (never written by `apply_status()`) and `volume_db`
      (poll skipped while the encoder moves). See [docs/rendering.md](rendering.md).
    - Same speedup also unmasked a touch bug: any single untouched frame counted as a release, so
      one FocalTouch dropout mid-press dispatched two taps. Unreachable at ~3 iterations/sec.
  **Every optimistic write is a candidate. Re-audit them after anything gets faster.**
- **Font glyph codepoint risk is documented in `Makefile`'s `fonts` target** -- read it before
  adding any character outside the existing ASCII 32-126 range.
- **Before declaring anything "fixed" or "root cause found": check the cheapest available number
  before and after the change**, not just after. A theory that's well-reasoned and even partially
  correct can still be the wrong fix if that check is skipped.

## Suggested Prompt For Next Session

> **The cold-boot networking failure has not recurred since 2026-08-05.** For months, roughly half
> of power-ons (50-90% across sessions) associated to WiFi, took a *correct* DHCP lease, and then
> failed every subsequent request permanently until power was cut. On 2026-08-05 the user disabled
> a second, poorly-placed AP that overlapped the primary in the same space. **Every boot since has
> succeeded -- not one miss.**
>
> Read that carefully before concluding "it was the network, nothing to do with us," and before
> concluding the opposite. Two things are true at once:
>
> - **This does not contradict the earlier "access-point choice" elimination.**
>   `tools/wifi_pin_check.py` pinned association to the suspect BSSID and got 4/4 working TCP,
>   which ruled out *which AP we associate with*. It could not rule out *co-channel interference
>   between two overlapping APs* -- a different mechanism entirely, and one that a pinning test is
>   structurally unable to see. The elimination was sound for what it tested; the conclusion drawn
>   from it was broader than the evidence.
> - **The resilience work was still worth doing.** The user's stance throughout was "even if it is
>   the network, we need to be resilient," and the stack that came out of it is materially more
>   reliable regardless of what the trigger was. Do not treat the AP fix as a reason to unwind any
>   of it.
>
> The surviving technical lead, if it ever returns: **`EHOSTUNREACH` (errno 118) alongside
> `ETIMEDOUT`** while DHCP succeeds. DHCP is broadcast; every failing peer is unicast, including
> the gateway -- which points at **ARP resolution failing** for on-link addresses. Consistent with
> an interference-driven cause: ARP is small, unacknowledged at the IP layer, and exactly what a
> marginal RF environment loses first.
>
> Still eliminated with evidence, do not re-open without new data: socket/pool exhaustion,
> CircuitPython heap pressure, boot ordering, BSSID pinning, and display memory pressure on the
> radio (measured directly -- see [docs/rendering.md](rendering.md)).
>
> Note also that the device has **no network recovery at all**: once wedged, nothing ever
> re-associates. Whatever the root cause turns out to be, a recovery path is worth having. A
> gateway `ping()` cleanly distinguishes "our stack is broken" from "the target is switched off"
> (remember `gc.collect()` first -- `ping()` raises `MemoryError` when the heap is tight).
>
> Read [docs/ota.md](ota.md) before touching `ota.py`/`ota_boot.py`'s Fetcher/session logic or
> `app.py`'s Update-menu code. OTA is implemented and confirmed working end to end on real
> hardware, fetching from S3 -- see that doc for the design.
>
> Read "CircuitPython heap/boot-memory guardrails" above before touching boot-path code (`app.py`'s
> `main()`), `.mpy` deploy tooling, or the `driver.py`/`ha_ui.py`/`wiim_ui.py` UI-extension split.
> Otherwise: review [docs/device-drivers.md](device-drivers.md)'s "Device Driver Architecture
> (v2.0)" if the task involves adding or changing a device backend, and its "hard lessons" list
> before touching backend-specific code.
>
> If the WiiM backend's rewritten raw-socket transport hasn't been redeployed and booted fresh yet,
> that's the first thing to try -- the approach itself is confirmed working (see
> docs/device-drivers.md's "Backend #4: WiiM / LinkPlay streamer" -> TLS section), but only via
> ad-hoc REPL snippets, not the actual shipped module code end to end. Check `gc.mem_free()` before/
> after on that first real boot regardless, same as any other memory theory in this file.
>
> The CamillaDSP backend (`src/camilladsp.py`, see docs/device-drivers.md's "Backend #5" section)
> has booted successfully on real M5 Dial hardware against a real `camilladsp` process --
> volume/mute/touch all confirmed working. The final quick-preset-button design
> (`CAMILLADSP_QUICK_PRESETS`/`get_quick_presets()`/`state.preset_quick_names`) is verified
> host-side only -- redeploying and confirming it on the Dial is the next concrete step for this
> backend. Still open beyond that: preset-switch timing was only measured against a trivial
> synthetic test config, not a real production one with actual filter files, and
> `CAMILLADSP_PRESET_TIMEOUT_MS` is still a guess for that case.

## Current Recommendation

**Self-update (OTA)**: implemented and confirmed working end to end on real hardware -- Check Now
and a full 16-file Install Update both succeed through the real Update menu, fetching from S3. See
[docs/ota.md](ota.md) for the full design (architecture, versioning, S3 layout, the power-cycle
requirement for Install Update, and known deferred limitations).

The app boots and connects cleanly on real hardware, both `denon` and `ha` configs. `make deploy`
(`.mpy`-compiled) is what the device should run day to day; `make deploy-src` is for fast dev
iteration only (no `mpy-cross` needed) and should not be mistaken for the shipped configuration.

All five backends are implemented against the same
pluggable-driver contract; Denon and MiniDSP are verified against real hardware (AVR-X4800H,
MiniDSP 2x4HD, MiniDSP Flex). HA `media_player` (discovery/switching, skip controls, the
standby-screen menu access fix, and the row/glyph/spacing polish) has had real on-device use --
that's how the standby MENU tap-zone bug, the oversized pause glyph, and the font-size regression
all got caught, even though that last one turned out not to be the WiFi bug itself. Other than the
WiFi outage, still outstanding: a live `select_source` check (never exercised live, only the
identical-shaped `_call_service` path other calls already confirmed working), and confirming the
`<<`/`>>` skip icons plus the centered-integer volume display look right on the actual physical
screen (renders via `tools/dial_sim.py` and direct `.pcf` bitmap dumps both look correct, but
neither is the real GC9A01 display).

`wiim` is implemented, and its TLS transport was a real find: the first implementation (a shared
`adafruit_requests.Session` with `check_hostname = False`) reliably failed to boot -- see
docs/device-drivers.md's "HTTPS-only API with a self-signed cert" section for the full debugging
trail -- and the fix (a raw-socket transport with a hardcoded SNI hostname, bypassing
`adafruit_requests` for this backend only) is confirmed working end-to-end via live REPL testing
against the real M5 Dial and a real WiiM Pro (full HTTP/1.0 response -- status line, headers, JSON
body -- received correctly over the fixed path). What's still outstanding: the *rewritten*
`wiim.py`/`app.py` haven't been redeployed and booted fresh yet with the fix in place -- the fix
was validated via ad-hoc snippets typed directly into the live REPL, not yet the actual shipped
module code end to end, so that's the next concrete step, not a re-litigation of whether the
approach works. Also still unverified: the `getPresetInfo` `preset_list` entry field names (no
favorites were configured on the test unit yet).

`camilladsp` is implemented and confirmed working end-to-end on real M5 Dial hardware against a
real `camilladsp` 4.1.3 process -- volume, mute, and touch all functioning -- see
docs/device-drivers.md's "Backend #5: CamillaDSP" section for the full design rationale and what's
still open (Pong/ping tolerance beyond the short test session, and preset-switch timing against a
real production config rather than the trivial synthetic test configs in `local/camilladsp/`). One
piece is verified host-side only, not yet redeployed to the Dial: the final quick-preset-button
design (`CAMILLADSP_QUICK_PRESETS`, `get_quick_presets()`, `state.preset_quick_names`) -- that's
the next concrete step for this backend, not a question of whether the approach works.

Future work could include: input selection UI polish, sound
mode selection, multi-zone support, wiring up MiniDSP's real Dirac-series per-slot filter names
if a future unit exposes more than a bare on/off `dirac` boolean, or -- once verified -- extending
`camilladsp.py`'s design (name/path preset lists, live-reported current preset) back as an option
for other backends if a similar gap ever comes up.
