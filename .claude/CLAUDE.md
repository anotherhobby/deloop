# deloop Project Context

`deloop` is CircuitPython firmware for an M5Stack Dial that controls an amp/DSP (or a Home
Assistant `media_player` entity) over the local network -- originally a Denon/Marantz AVR, now
also a MiniDSP unit via minidsp-rs and any HA `media_player`, through a pluggable driver
(`src/driver.py`). The device is for an office AVR where normal control is awkward. Primary
interaction is rotary volume control; the volume display must always stay accurate even when
volume changes from outside the Dial.

## Hard rules (non-negotiable)

- **CircuitPython's `str` is not desktop Python's `str`** -- no `.title()`, `.capitalize()`,
  `.center()`, `.zfill()`. Code using these passes host-side tests and crashes only on real
  hardware. `grep` for them before shipping any backend/UI change.
- **`dict.get(key, expr)` evaluates `expr` unconditionally**, even on a hit. Use
  `d.get(key) or fallback()`, never `d.get(key, expensive_or_unsafe_call())`.
- **Treat non-ASCII in string literals as unsafe** -- CircuitPython can crash on it. Use `chr(N)`,
  never a literal Unicode character or a `\u` escape in source.
- **Every `adafruit_requests` response must be `.close()`'d** -- the socket pool is only ~4 deep.
- **Always use `board.I2C()`** (the singleton), never `busio.I2C(board.SCL, board.SDA)`.
- **Never call `microcontroller.reset()` anywhere in the OTA flow**, including for test setup --
  it breaks the first post-handshake TLS `send()` on the next boot. See [docs/ota.md](../docs/ota.md).
- **Never manually create a `vN` GitHub Release/tag for this repo** -- merging to `main` is the
  only version-numbering path (see [docs/ota.md](../docs/ota.md)).
- **`code.py` must stay tiny** (`import app; app.main()` only) -- it's the boot entry point and
  the one file that can't be `.mpy`-compiled.

## Build / deploy / test

- `make deploy` -- what the device should run day to day: precompiles everything except `code.py`
  to `.mpy` first (low heap footprint at boot). `make deploy-src` is the plain, uncompiled `.py`
  copy for fast iteration when `local/mpy-cross` isn't available -- **not** the day-to-day command,
  despite the name similarity. Confirm against the Makefile itself if in doubt; these target names
  changed once already (see [docs/hardware.md](../docs/hardware.md)'s "Deployment Goal").
- `make shell` -- opens an `mpremote` REPL over USB for live debugging.
- Flashing CircuitPython onto new hardware: see [docs/hardware.md](../docs/hardware.md).

## Docs index

Read these on demand when a task touches them -- don't pull their content back into this file.

- [docs/architecture.md](../docs/architecture.md) -- project summary, stack decision, project
  layout, input model, polling/socket/config details, boot-memory guardrails, current status and
  next-session prompt. Read for most general or cross-cutting tasks.
- [docs/hardware.md](../docs/hardware.md) -- flashing CircuitPython, pinout, module usage,
  libraries, touch controller, deployment mechanics. Read for anything touching physical hardware
  or the deploy pipeline.
- [docs/device-drivers.md](../docs/device-drivers.md) -- the pluggable backend architecture, the
  recipe for adding a 6th backend, hard lessons from Denon/MiniDSP/HA/WiiM/CamillaDSP, and each
  backend's protocol reference. Read before touching `driver.py` or any
  `<backend>.py`/`<backend>_ui.py`.
- [docs/ota.md](../docs/ota.md) -- self-update (OTA) design, versioning workflow, and the
  reliability investigation. Read before touching `ota.py`, `denon.py`'s retry logic, or `app.py`'s
  OTA lean-mode/menu code.

## How these docs are maintained

`docs/architecture.md` and the other topic docs are living documents: update them on major
learnings, or whenever explicitly asked. Treat pre-PR / before-shipping as a mandatory point to
re-read the relevant doc and prune anything stale, wrong, or superseded -- don't let it silently
drift.

This file stays deliberately short: hard rules, build commands, and a docs index only. When a
task touches a doc's topic, read that doc on demand -- don't copy its content back into this file
"for convenience," even temporarily.
