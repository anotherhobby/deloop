# deloop OTA Reference

See [docs/architecture.md](architecture.md) for the overall project layout, and
[docs/device-drivers.md](device-drivers.md) for the unrelated device-backend architecture.

## Self-Update (OTA) (added 2026-07-31)

`src/ota.py` -- deloop can update its own app files over Wi-Fi from this repo's GitHub Releases,
manual-check-only (no background polling), via a new **Update** entry in the top-level menu
(`config.OTA_ENABLED`, on by default). **Never touches CircuitPython firmware** -- only the app
files `make deploy`/`deploy-mpy` already ship. Orthogonal to `DEVICE_DRIVER`, same category as the
"MENU home" chrome: no backend-specific code anywhere in this feature.

This was a long debugging session, not a small feature -- most of what's below is *why* the design
looks the way it does, because every alternative that looks simpler on paper was tried first and
failed for a specific, confirmed reason. Read the "Hard lesson" callouts before changing any of
this; the reasoning is the load-bearing part, not just the resulting code.

### The core architecture: live remount + soft reload, never a hard reset

Two designs were tried before landing on the current one:

1. A `boot.py`-gated `storage.remount("/", readonly=False)`, triggered by an NVM flag and a
   `microcontroller.reset()` so `boot.py` re-runs with write access. **Abandoned entirely** --
   `boot.py` doesn't exist in this repo anymore. Superseded by:
2. **`storage.remount("/", readonly=False)` called live, with zero reboot**, confirmed to succeed
   as long as CIRCUITPY isn't actively host-mounted. This is the one real dependency the whole
   feature has, surfaced to the user as the Update menu's `Eject drive first` result rather than
   failing silently (`storage.remount()`'s exception is caught in `_ota_lean_mode()` and mapped to
   NVM result `6`/`eject_needed`).

**Hard lesson, the deepest bug of this whole feature:** an early version used
`microcontroller.reset()` to enter/exit the OTA flow (matching design 1 above). Every attempt to
actually download a release failed with `OSError -12288` on the *first send() after a fully
successful TLS handshake* -- confirmed via raw-socket diagnostics (`pool.socket()` +
`ssl_context.wrap_socket(...)`, same pattern as `wiim.py`'s transport) that TCP connect and the TLS
handshake itself both genuinely succeed; only the first post-handshake application-data `send()`
fails, 100% reproducibly. Root-caused to be tied specifically to **a genuine hardware reset having
occurred earlier in the same power cycle** -- not memory, not the specific reset trigger, not
timing/retries. A session reached via `supervisor.reload()` (CircuitPython's own soft-reboot
mechanism -- same as Ctrl-D in the REPL) instead completes a full TLS round trip reliably, every
time. Confirmed again independently on 2026-07-31/08-01 (see "Reliability investigation" below):
during that session's own test setup, a deliberate `microcontroller.reset()` (used just to force a
clean NVM state for testing) reproduced the identical `OSError -12288` on the very next OTA check --
direct proof this isn't a one-off, it's a real, repeatable property of a hard reset having happened
anywhere earlier in the boot session, whether deloop's own code triggered it or a test harness did.
**Never call `microcontroller.reset()` anywhere in the OTA flow, ever, including for testing setup
-- use a true power cycle (unplug/replug) instead if a clean NVM state is needed.**

The resulting design: `_ota_lean_mode()` (app.py) is a completely separate boot path, entered
before `dial_ui.init()`/driver setup ever run (checked at the very top of `main()`, right after the
splash). It never builds a `_Loop`, never imports a backend -- OTA doesn't need any of that, and
skipping it matters for memory (see below). Every exit from `_ota_lean_mode()`, success or failure,
ends in `_ota_reload_to_normal()` (`gc.collect()` then `supervisor.reload()`) -- never a hard reset,
on either the way in or the way out.

**NVM scheme** (byte 2 = action, byte 3 = result, byte 4 = latest-detected version -- bytes 0/1
already used for brightness/sound):
- `_NVM_OTA_ACTION`: `0` idle, `1` pending check, `2` pending install. Set by `_confirm_sub`'s
  "update" branch right before `supervisor.reload()`; read once at the top of `main()`.
- `_NVM_OTA_RESULT`: `0` none, `1` available, `2` up_to_date, `3` check_error, `4` install_ok,
  `5` install_failed, `6` eject_needed. Set by `_ota_lean_mode()` right before its own reload back
  to normal; read once on the very next normal boot, surfaced into `loop.ota_status`, then cleared
  to `0` -- so it survives exactly one reload boundary, never more.
- `_NVM_OTA_VERSION`: the latest version a successful check found, so the Update menu can show
  `Install Update (vN)` without needing a live network call just to render a menu label.

**Hard lesson, a real memory bug that looked like an infinite loop:** `_ota_lean_mode()`'s first
cut kept `pool`/`ssl_context`/`session` resident in the *same stack frame* that then tried to write
the NVM result -- even with a `gc.collect()` right before the write, those objects were still
reachable from the live frame and therefore not garbage, so the NVM write's own ~8KB allocation
(a real cost of `microcontroller.nvm[]`'s read-modify-write) failed with `MemoryError`, which then
got silently swallowed somewhere and looked like the device just looping between "Checking..." and
"Check Now" forever. Fixed by extracting the actual network work into its own functions
(`_ota_do_check()`/`_ota_do_install()`, each building its session via `_ota_new_session()`) so their
locals go out of scope -- and become genuinely collectible -- before any NVM write is attempted.
Same fix shape used again later for `denon.py` (see below): **when debugging a `MemoryError` or a
mystery loop near an NVM write, check whether the failing write shares a stack frame with anything
that just did network I/O, before assuming the bug is in the write itself.**

**Why the transient "Checking..."/"Updating..." screen uses `dial_ui.show_message()`, not
`dial_ui.init()`:** confirmed live that doing both `dial_ui.init()`'s ~28KB gauge bitmap allocation
*and* wifi+ssl+ota+adafruit_hashlib resident at the same time causes a real `MemoryError` during
OTA's already memory-heavier boot path. `show_message()` is a single centered label, no bitmap, no
label pool -- same "no large bitmap" reasoning as the existing `show_splash()`.

### Versioning: fully automatic, never hand-chosen

`.github/workflows/release.yml` triggers on **every push to `main`** (plus manual
`workflow_dispatch`) -- not on a pushed tag. It computes the release version as
**`(highest existing vN tag) + 1`**, always, via `git tag -l 'v[0-9]*' | sed 's/^v//' | sort -n |
tail -1`, then `gh release create vN --target $GITHUB_SHA --generate-notes`. `v1`-`v4` predate this
workflow (hand-created, no assets) and are handled fine -- the version step just sees them as
ordinary existing tags and continues from `v5`. A `concurrency: group: deloop-release` guard
serializes runs so two near-simultaneous triggers can't compute the same "next version" and race to
create the same tag. This was a deliberate redesign from an earlier tag-push-triggered version,
explicitly requested by the user: "an automated version release with a merge to main," specifically
so there's exactly one place version numbers ever come from, regardless of how the workflow was
invoked. **Never manually create a `vN` GitHub Release/tag for this repo again -- merging to `main`
is now the only version-numbering path**, or two different mechanisms will race to pick "the next
version."

The workflow bakes `CURRENT_VERSION` into `src/version.py` as a working-copy-only step (never
committed back) using a pinned, version-matched Linux amd64 `mpy-cross`
(`adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/linux-amd64/mpy-cross-linux-amd64-10.2.1.static`,
confirmed to exist at that exact path 2026-07-31) -- a different artifact than the macOS arm64 build
linked from the Makefile's local dev comment. `tools/build_release_manifest.py` (explicit file
allowlist, same "explicit over glob" convention as everywhere else in this project) builds
`manifest.json` (sha256 + size per file) from the compiled `dist/`; `gh release upload vN dist/*`
publishes everything as flat release assets. Confirmed live 2026-07-31: the very first automated
run produced `v5` with all 17 expected assets (16 `.mpy` files + `code.py` + `manifest.json`)
correctly.

### `driver.py`'s "capability" pattern applies here too -- but the real analog is `Response.close()`

Not literally a `CAPS` dict (OTA isn't a device backend), but the same "read the actual source,
don't trust a summarized doc" discipline (hard lesson #3 from the Denon→MiniDSP round, see
[docs/device-drivers.md](device-drivers.md)) mattered enormously here, against
`adafruit_requests`/`adafruit_connection_manager` this time instead of a device API. See
"Reliability investigation" immediately below.

### Reliability investigation (2026-07-31/08-01) -- read before touching ota.py's or denon.py's retry logic

The Update menu worked end-to-end multiple times this session, but not *reliably* on the first
try -- occasional `ETIMEDOUT` against `api.github.com`. Chasing this (with the user pushing back,
correctly, on reflexive "it's the network" explanations -- see "a smoking gun or nothing" below)
led to reading `adafruit_requests`/`adafruit_connection_manager`'s actual source
(github.com/adafruit/Adafruit_CircuitPython_Requests, .../Adafruit_CircuitPython_ConnectionManager)
rather than guessing, and turned up real, confirmed findings worth keeping permanently:

1. **`adafruit_requests.Session.request()` already retries connect+send internally.** It sends,
   reads one byte to confirm the socket's genuinely alive, and on failure calls
   `connection_manager.close_socket()` (properly evicting the dead socket) before retrying once
   with a fresh connection. `OutOfRetries("Repeated socket failures")` -- a literal string seen in
   this project's own logs (from `denon.py`'s `set_input`) -- is this library's own hardcoded
   message for when *both* of its internal attempts fail. This was not a bug in this project's
   code; the library already guards this specific phase.
2. **Reading the response body has zero retry protection anywhere in the library.** `resp.json()`/
   `resp.text`/`resp.iter_content()` are not covered by the retry loop above at all. Confirmed via a
   live capture: `ota: GET took 2.41s: https://api.github.com/...` (a full successful connect+send)
   immediately followed by `ota check failed: ETIMEDOUT` -- the connect succeeded; the *body read*
   afterward is what timed out, completely unprotected. This is the real gap both `ota.py`'s
   `_Fetcher` and `denon.py`'s `_request_text()` now close.
3. **`Response.close()` frees a socket for reuse, it does not actually close it** --
   confirmed from source: it calls `connection_manager.free_socket()`, not `close_socket()`. A body
   read failing mid-stream still calls `.close()` (in a `finally`), which just marks that same
   possibly-broken socket "available" for the *next* unrelated request to the same host -- silently
   handed back with zero health check. This is the real mechanism behind the stale-socket-reuse
   failure pattern the user specifically flagged from prior experience with ESP32 CircuitPython
   (citing github.com/adafruit/Adafruit_CircuitPython_Requests#191, a documented ESP32 TCP-timeout
   issue independent of any particular network).
4. **TCP_NODELAY is not reachable through this library at all** -- confirmed zero `setsockopt`
   calls anywhere in `adafruit_connection_manager.py`, and `Session` never exposes the raw socket
   to caller code. Getting it would require bypassing `adafruit_requests` entirely for a raw-socket
   HTTP client, the same scope of work `wiim.py` already did for an unrelated reason (SNI/self-signed
   cert). Explicitly parked by the user in favor of the cheaper, confirmed-real fix (#2/#3 above).
5. **`wifi.radio.power_management` defaults to `MIN`** (radio sleeps between the AP's DTIM beacons)
   rather than `NONE`. Plausible contributor to WAN-hop latency spikes even though it can't explain
   everything (LAN-local Denon polls kept succeeding cleanly in the same capture windows where the
   GitHub HTTPS call timed out). Set to `wifi.PowerManagement.NONE` in both `_ota_new_session()` and
   `_connect_wifi()`, wrapped in try/except since availability wasn't 100% confirmed for this exact
   CircuitPython build ahead of time.

**Fixes applied**, both following the same shape -- retry wraps the *entire* GET-and-read, not just
the connect, and a retry rebuilds rather than reuses:
- `ota.py`'s `_Fetcher` class: owns one `Session`, rebuilds it from scratch (via `new_session`, a
  zero-arg callable -- `app.py`'s `_ota_new_session`, passed by reference so it can be called again
  mid-operation) on any retry. `get_json()`/`download()` both wrap the *whole* request+body-read in
  their retry loop, not just `session.get()`.
- `denon.py`'s `_request_text()` + `_reset_connections()`: since `denon.py` only has a `_session`
  object (no wifi/socketpool access to rebuild a whole new session the way OTA can), the equivalent
  fix is `_session._connection_manager._free_sockets(force=True)` -- reaching into a private
  (single-underscore) library internal deliberately, force-closing *every* pooled socket rather than
  the free-for-reuse default, since this backend only ever talks to one host anyway.

**Still open, confirmed but not yet fixed:** rebuilding a full session (fresh socketpool + TLS
context) immediately after a failed ~12s attempt was itself observed to raise a bare `MemoryError`
once, live -- which isn't an `OSError` and so isn't caught by the retry's `except OSError`, meaning
it bypasses the retry and surfaces cleanly as a failed check (no crash, no loop) rather than getting
a second attempt. Also open: one measured GET took 12.13s against a *requested* 10s timeout --
directly matching the cited GitHub issue's complaint that ESP32 socket timeouts aren't strictly
honored by the underlying stack. Net effect as of this writing: Check Now is correct every time it
completes, and reload-safe (never hangs, never loops, never needs a hard reset to recover) even when
it fails -- but is not yet 100% reliable on a single attempt against GitHub's WAN round trip.
Retrying by hand (it's a manual, user-initiated action) has worked every time so far.

**A smoking gun or nothing:** worth internalizing as a standing rule for this project specifically,
stated directly by the user after an initial "transient network hiccup" explanation was offered
without real evidence: don't blame the network (or general ESP32 WiFi flakiness) without a specific,
measured finding backing it up -- "that is ALWAYS what engineers say when they get stuck." Every
theory above earned its place in this file by being traced to an actual measurement or a specific
line of vendor source, not a guess. The one exception that *is* real and citable: ESP32 CircuitPython
having documented, platform-level TCP quirks independent of any particular network (item 5 and the
cited GitHub issue) -- a specific, sourced claim, not a hand-wave.

### A real latent UI bug found while adding the version display

The user asked for the current version to be visible on the Update submenu *before* tapping
anything, styled to match the main screen's dim "input" label. Implementing this surfaced a real,
pre-existing bug: `dial_ui.draw_menu()`'s `title` parameter has **never been rendered** -- the
function unconditionally does `ui["status"].text = ""` with a comment "no title; context comes from
the items themselves." Every submenu's title string (`"DEVICE"`, `"BRIGHTNESS"`, and this feature's
own `"UPDATE (v0)"`) was being computed and passed in, then silently discarded, for every submenu,
the entire time this pattern has existed. Fixed by adding a genuine `version_text` parameter to
`draw_menu()` (styled `_C_DIM`, rendered via the previously-always-blanked `ui["status"]` label,
positioned at `(CX, 24)` -- moved down from an initial `(CX, 8)` after the user found text got
clipped by the round bezel that close to the top edge) -- threaded through every one of `app.py`'s
~8 `draw_menu()` call sites, all but one of which pass `version_text=""` (a no-op, matching prior
behavior) since only the Update submenu has anything to show there.

### Presets/Dirac-filter retry after a failed boot fetch (added 2026-08-01, separate from OTA)

Unrelated to OTA except for landing in the same session: `main()` fetches `driver.get_presets()`
exactly once, at boot -- if the backend isn't reachable yet, `state.preset_names` stays permanently
empty and the quick-select buttons never appear again, with nothing to ever retry it. Fixed with
`_retry_presets(loop, ui, state, now)`, called from the main loop alongside `_poll_avr`: retries on
its own short fixed interval (`_PRESET_RETRY_INTERVAL_S = 10.0`, independent of the normal adaptive
5s/30s status-poll interval, since a missing preset row is a visibly broken UI element worth
reconnecting for aggressively) and stops permanently the moment it succeeds once. Deployed; not yet
independently confirmed live in a scenario that starts with a genuine boot-time failure.

### Known-noisy diagnostic prints intentionally left in place

A per-poll `print()` in `_poll_now()` (added to trace a "power off screen flashes at boot" report,
which turned out to be explained by the timeout pattern in "Reliability investigation" above, not a
real bug) was found to cause real touch/encoder unresponsiveness -- CircuitPython's USB serial
`print()` can block when nothing is actively reading it, and the main loop is fully synchronous
(hard lesson #5 from the Denon→MiniDSP round, see [docs/device-drivers.md](device-drivers.md): no
per-frame animation during a blocking call -- same root cause applies to *any* blocking call, not
just network ones). **Removed** -- it ran unconditionally every 5-30s forever, unlike the ones kept
below. Left in place, since they only fire once at boot or on an actual error/retry (not in a hot
path): `gc.mem_free()` checkpoints at the top of `main()` and right before `dial_ui.init()`'s big
allocation; `ota.py`'s per-request timing (`ota: GET took Xs`/`GET failed after Xs`); `denon.py`'s
retry/reset failure prints; `power_management NONE failed` (only if the attribute genuinely doesn't
exist on a given build).

### Status as of this writing

**Confirmed working, live, multiple times:** Check Now, from a genuine cold boot, correctly detects
a newer release and reloads cleanly back to normal with the result showing. The automated
`v5` release (see "Versioning" above) has real, correct assets.

**Not yet confirmed:** a full Install Update (real file write + verify + rename + reload) has not
been run end-to-end against the *current* code -- every reliability fix in this section landed
after the last time Install Update was actually exercised, and the retry/session-rebuild rewrite
specifically touches the exact code path Install Update uses. This is the top priority for the next
session working on this feature. Also not yet committed: everything in this section past the
original OTA feature (PR #9, merged) is sitting uncommitted on the `ota` branch as of this writing --
commit/PR/merge it before considering any of today's hardening actually shipped (merging will itself
trigger another automated release, per "Versioning" above -- expect `v6`, not `v5`, once that
happens).
