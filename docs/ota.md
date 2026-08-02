# deloop OTA Reference

See [docs/architecture.md](architecture.md) for the overall project layout, and
[docs/device-drivers.md](device-drivers.md) for the unrelated device-backend architecture.

## Self-Update (OTA) (added 2026-07-31)

`src/ota.py` -- deloop can update its own app files over Wi-Fi from this repo's GitHub Releases,
manual-check-only (no background polling), via a new **Update** entry in the top-level menu
(`config.OTA_ENABLED`, on by default). **Never touches CircuitPython firmware** -- only the app
files `make deploy` already ships. Orthogonal to `DEVICE_DRIVER`, same category as the
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

### Soft reload doesn't fully release WiFi/TLS state (found + partially fixed 2026-08-01)

**This cuts directly against the "never a hard reset" design principle above, in the opposite
direction from the original TLS-after-hard-reset finding.** That finding pushed this feature
toward soft-reload-only; this one shows soft-reload-only has its own real cost.

Confirmed live: after a Check Now attempt that failed via the retry path (see "Reliability
investigation" below), the immediate reboot loop was fixed (see next section) -- but the *next
normal boot* after that failed attempt then crashed in `dial_ui.init()` with `MemoryError:
memory allocation failed, allocating 28800 bytes` (the same ~28.8KB gauge bitmap allocation
described in [docs/architecture.md](architecture.md)'s "CircuitPython heap/boot-memory
guardrails"). `gc.mem_free()` at the top of `main()` had dropped from the clean-boot baseline of
`66208` to `64160` and **stayed there across two consecutive `supervisor.reload()` calls** with
zero new network activity in between -- confirmed by checking `wifi.radio.connected` (`False`) and
`gc.mem_free()` (`103648`, i.e. plenty in absolute terms) from the REPL, then soft-rebooting again
and watching the identical crash reproduce with the identical `50800` free-before-`dial_ui.init()`
number. Only a genuine power cycle (unplug/replug USB) cleared it -- the very next boot showed
`52880` free before `dial_ui.init()`, *above* the original clean baseline, and booted normally.

So `supervisor.reload()` genuinely does reset the Python/GC heap (confirmed by `gc.mem_free()`
matching a clean boot immediately after one), but something below that -- almost certainly
ESP-IDF's WiFi/TLS driver state, which lives in a different allocator than CircuitPython's GC arena
-- is not released by a soft reload once a network-heavy operation (repeated TLS handshake
attempts against a slow WAN host, in this case) has touched it. Being logically disconnected
(`wifi.radio.connected == False`) is not the same as that native state being released.

**Fix applied:** `_ota_reload_to_normal()` now calls `wifi.radio.enabled = False` (a harder radio
shutdown than merely disconnecting) right before its `gc.collect()` + `supervisor.reload()`, on
every exit path. **Not yet confirmed live whether this alone is sufficient** -- it wasn't in place
during the incident described above, and the fix hasn't been through a fresh
fail-then-normal-boot cycle yet. If this recurs after the fix is deployed, the next thing to try
is disabling *then re-enabling* the radio (a full power-cycle of just the radio peripheral, not
only a disable) before the reload, or -- if that still doesn't work -- accepting that this specific
failure mode needs a real hardware reset to recover from and surfacing that to the user explicitly
rather than silently reloading into a boot that's likely to fail.

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

*(2026-08-01 correction: the "worked end-to-end multiple times" claim just below did not hold up
-- per the user directly, OTA had in practice never successfully completed. Left as originally
written for the historical record of what this investigation believed at the time; see "Status as
of this writing" at the bottom of this file for what's actually confirmed.)*

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

**Resolved 2026-08-01 -- this was a real boot-loop bug, not just a "surfaces cleanly" case as
first assumed.** Confirmed live against real hardware after merging the fixes above and running
Check Now against a genuinely slow WAN round trip: the `MemoryError` from a failed-attempt retry
described above didn't just skip the retry -- it left the heap fragmented enough that
`_ota_lean_mode()`'s own *recovery* path (`_set_ota_result()`, then `_set_ota_action(0)` inside
`_ota_reload_to_normal()`) also failed its ~8KB nvm write with a second `MemoryError`, each caught
and printed but never raised further. Because `_set_ota_action(0)` never actually landed, the
pending-action flag stayed at "1" across the `supervisor.reload()` that followed, so the next boot
re-entered `_ota_lean_mode()` and repeated the identical failure -- a real infinite reboot loop on
real hardware (confirmed: "rebooted at least half a dozen times or more" before being caught, only
recoverable by breaking into the REPL and manually zeroing `microcontroller.nvm[2]`).

Root cause of the `MemoryError` itself: `_Fetcher._retry_wait()`'s `gc.collect()` ran *before*
`self.session` was dereferenced, so the failed session was still reachable and nothing was
actually freed -- building the replacement wifi/pool/ssl session then meant two full sessions
alive at once, right after a network-heavy failed attempt, which is what exhausted the heap. Fixed
by setting `self.session = None` before `gc.collect()`, the same "objects reachable from a live
reference are not garbage" principle already documented elsewhere in this file and in
[docs/device-drivers.md](device-drivers.md).

**The more important fix, structurally:** `_ota_lean_mode()` now calls `_set_ota_action(0)`
immediately after reading the pending action, before any network or filesystem work runs -- not
only at the end via `_ota_reload_to_normal()`. Memory is plentiful that early (~66KB free at boot,
confirmed live), so this write reliably succeeds regardless of what happens afterward. This means
no future bug anywhere later in this function -- caught or not -- can strand the device in a
reboot loop again; worst case is a single lost manual check/install attempt, trivially recoverable
by pressing the button again.

**Superseded 2026-08-01 -- the actual root cause was found, and it wasn't the network or the
ESP32's TCP stack at all.** Every theory above (ESP32 socket-timeout quirks, WAN round-trip
latency, `wifi.radio.power_management`) was a reasonable read of the *symptom* (slow, then
`ETIMEDOUT`) but none of them were it. Live testing (same session as the boot-loop/fragmentation
findings above) isolated it precisely: a raw manual socket request to `api.github.com` (TCP
connect, TLS handshake, send, recv) completed in **under 3 seconds, every time**, on the exact
same device/network/host where `adafruit_requests`-based requests were consistently taking 12+
seconds and eventually failing. Sending the identical request *through*
`adafruit_requests.Session.get()`, but with explicit `headers={"User-Agent": "deloop",
"Connection": "close"}`, also completed cleanly (2.5s headers + 0.6s body). Without those headers,
`adafruit_requests` appears to wait on a keep-alive-style response in a way that never correctly
detects the body's actual end, eventually hitting its own timeout instead -- which is exactly the
"connect succeeds, body read times out" pattern from the investigation above, just one layer
short of asking *why* the body read itself was slow.

**Fix:** `ota.py`'s `_Fetcher` now sends `_HEADERS = {"User-Agent": "deloop", "Connection":
"close"}` on every request (both `get_json()` and `download()`). No raw-socket rewrite needed --
`adafruit_requests` works fine once it's told not to keep the connection alive. This means the
WAN-latency framing throughout this whole section was likely wrong from the start: DNS, TLS, and
GitHub were never slow. `OTA_CHECK_TIMEOUT_MS`'s default (10000ms) was probably never actually too
short for a *real* request -- it was masking a request that would never complete regardless of how
long it waited.

**Correction, same day: this was still not the real root cause.** Re-confirmed through the actual
`_ota_lean_mode()` code path (not just manual REPL reproduction) and it failed identically, headers
and all. The headers stayed in (harmless, arguably correct regardless), but the deciding factor was
never them -- see "The real root cause: app.py's own imports" below for what actually explains
every failure this entire investigation chased.

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

*(Correction, 2026-08-01: the "power off screen flashes at boot" report referenced below was
re-investigated properly and was a real, reproducible bug -- see
[docs/architecture.md](architecture.md)'s "Power-off screen flashing at boot" for the actual root
cause and fix. It was never really explained by the timeout pattern; that conclusion doesn't hold
up and shouldn't be trusted. Left below for the historical record of what this section originally
claimed.)*

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

### The real root cause: app.py's own imports, not headers or timeouts (found + fixed 2026-08-01)

**This is the actual answer to why OTA had, by the user's own account, never once succeeded --
everything above this section chased real, legitimate secondary issues, but none of them were
the deciding factor.** Isolated through a careful process of elimination, live, after the headers
fix (above) was re-tested through the real code path and still failed identically:

1. A raw manual socket request (no `adafruit_requests` at all) to `api.github.com` completed in
   under 3 seconds, every time, from a REPL session with ~100KB+ free memory.
2. The exact same request, run through the real `app._ota_do_check()` -- the literal function the
   Check Now button calls -- failed with a 12+ second `ETIMEDOUT` followed by a bare `MemoryError`
   on retry, from a REPL session where `gc.mem_free()` read **30304 bytes**.
3. Re-running the raw manual socket request *at that same ~30KB-free level* (reusing the WiFi
   connection `app`'s own boot had already made) reproduced the failure: `ctx.wrap_socket()` --
   the TLS handshake itself, nothing to do with `adafruit_requests`, headers, or retry logic --
   threw a bare `MemoryError` immediately.

That third result is decisive: it rules out `adafruit_requests`'s allocation pattern, the missing
headers, and the retry/timeout logic as the deciding factor. The actual problem is that a TLS
handshake to GitHub needs more contiguous free memory than survives to the point Check
Now/Install Update actually runs -- and the reason so little survives is `import app` itself.

`_ota_lean_mode()` (as it existed before this fix, living inside `app.py`) was designed around
"skip `dial_ui.init()`'s big bitmap and driver/hardware setup" -- but `code.py` doing `import app;
app.main()` means Python must fully execute *all* of `app.py`'s top-level code -- including its
unconditional `import driver`, `import dial_ui`, `import sound`, `from state import AVRState` --
before `main()` ever gets a chance to check whether this boot is even for OTA. `driver.py` in turn
imports the active `config.DEVICE_DRIVER` backend module. None of that was ever skipped by "lean
mode" -- only the *function calls* were (`dial_ui.init()`, hardware/driver setup calls), not the
*imports*, which is where CircuitPython's real cost lives (see
[docs/architecture.md](architecture.md)'s "CircuitPython heap/boot-memory guardrails": "an unused
function still costs heap at import time"). This had been true since the OTA feature was first
built -- nothing about today's other fixes introduced it, they just kept obscuring it, because
adding retry logic and better error handling made failures look increasingly like a network/timing
problem rather than the memory-budget problem it actually was.

**Fix: give OTA its own boot path that never imports `app.py` at all.** `code.py` now reads the
pending-OTA nvm byte itself (`config.NVM_OTA_ACTION`, wrapped in a bare `try/except` so a boot-
critical read can never crash the boot) *before* deciding whether to `import app` or a new module,
`src/ota_boot.py`. `ota_boot.py` contains everything `_ota_lean_mode()` and its helpers used to
(the nvm read/write functions, `_new_session()`, `_do_check()`/`_do_install()`, `run()`,
`_reload_to_normal()` including the WiFi-radio-disable mitigation from the section above) --
verbatim in logic, just relocated -- plus its own minimal `_show_message()` using `terminalio`'s
built-in font and `vectorio` for a background rect, deliberately **not** `dial_ui.py` (which loads
three separate PCF font files at import time and is a large module in its own right, regardless of
which function is called). `app.py` keeps only what its own normal-mode UI still needs:
`_set_ota_action()` (to request a check/install), `_ota_result()`/`_ota_latest_version()` (to show
the outcome), all now reading `config.NVM_OTA_ACTION`/`NVM_OTA_RESULT`/`NVM_OTA_VERSION` -- moved
into `config.py` as the one shared source of truth so `app.py` and `ota_boot.py` can never drift
apart on the byte layout.

Confirmed via `py_compile` on every touched file (syntax only -- CircuitPython-specific modules
like `wifi`/`board` don't exist on desktop Python, so this can't run the real logic host-side).
**Not yet confirmed live on real hardware** -- this is the very next thing to test. If this fix is
right, `gc.mem_free()` right before the network call in `ota_boot.run()` should read something
much closer to the ~100KB+ a minimal REPL session sees, not the ~30KB `import app` left behind.

### A second real bug, found only after the memory fix made testing viable: cross-host socket reuse (found + fixed 2026-08-01)

With the memory fix in place, Check Now became reliably fast and successful -- which is what
finally made Install Update testable far enough to hit a *different* bug underneath. Every attempt
showed the same shape: the release-info fetch to `api.github.com` always succeeded, but the very
next call -- fetching `manifest.json` via `github.com/.../releases/download/...` (which always
302-redirects to a signed `release-assets.githubusercontent.com` URL, confirmed via a direct
`curl` check) -- intermittently failed with `OSError -12288`, the same low-level mbedtls error
code as the original hard-reset investigation, or got further and failed with a small,
oddly-specific `MemoryError` partway through the file-download loop.

That intermittency was the tell. Isolated by testing the *exact same request* three ways:
1. A raw socket straight to `github.com` (bypassing `adafruit_requests` entirely) -- succeeded
   cleanly every time, ruling out anything fundamentally wrong with `github.com`'s TLS/cert on
   this hardware.
2. The identical URL, with the identical headers, through `adafruit_requests.Session.get()` --
   but on a **brand-new session that had never made any other request** -- also succeeded
   cleanly, full manifest.json body back correctly. This ruled out `adafruit_requests`'s
   redirect-following logic itself as the bug.
3. The only thing left: `ota.apply()`'s `_Fetcher` reuses **one session across requests to
   multiple different hosts** within a single call -- `api.github.com` for release info, then
   `github.com` (redirecting to `release-assets.githubusercontent.com`) for the manifest, then
   again for every one of the ~16 file assets. That's the one condition neither isolated test
   reproduced.

**First fix attempt (confirmed live NOT sufficient):** by analogy with an issue this project
already found once for a different trigger (see `denon.py`'s `_reset_connections()`) -- suspected
`Response.close()` only marking a socket "available for reuse," not actually closing it, so a
socket whose TLS session was negotiated for `api.github.com` could get silently handed back to a
subsequent request for `github.com`. Added `_Fetcher._reset_connections()`, force-closing all
pooled sockets (`self.session._connection_manager._free_sockets(force=True)`) before every
request. **Deployed and re-tested live: `-12288` recurred identically, same host, same URL.** This
ruled out socket-pool reuse as the actual mechanism -- a real, useful negative result, not a wasted
step.

**Actual root cause, found by asking what that fix left untouched:** `self._ssl_context` itself --
the underlying mbedtls context object -- was still the *same object*, reused across every request
in the `_Fetcher`'s lifetime, regardless of how many sockets got closed. Re-examining every test
that had ever succeeded confirmed a pattern: every single one constructed a brand-new
`ssl.create_default_context()` for that one request alone. Nothing that ever succeeded reused an
`ssl_context` object across more than one `wrap_socket()` call to a different host.

**Fix:** `_reset_connections()` now rebuilds the *entire* session -- calling `_new_session()` in
full (fresh wifi connect, fresh socketpool, fresh `ssl_context`), not just closing sockets -- before
every single `get_json()`/`download()` attempt, matching the only configuration ever actually
confirmed to work. Kept the same "drop the reference and `gc.collect()` before building the
replacement" discipline from the earlier double-session MemoryError fix, since this rebuild now
happens far more often (every request, not just retries) and the same risk applies. This also made
`_retry_wait()`'s own rebuild-on-failure redundant (the next loop iteration's
`_reset_connections()` call already does it) -- removed, keeping the retry path to just logging and
the exponential-backoff-style sleep.

**Confirmed live NOT sufficient either -- rebuilding unconditionally traded one bug for another.**
Deployed and re-tested: `-12288` was gone, but `ota apply failed: MemoryError` (bare) fired right
after the *second* network call (the manifest.json fetch), before the file-download loop's own
instrumentation even printed once. Rebuilding wifi+socketpool+TLS-context back-to-back, ~18 times
across one `apply()` call with zero reload in between, accumulated enough of its own memory
pressure to fail -- the same "WiFi/TLS state doesn't fully release" theme as the earlier
soft-reload finding, just triggered by rapid rebuilds instead of a reload boundary.

**Refined fix (confirmed live NOT sufficient on its own):** `_Fetcher` was changed to only rebuild
when actually needed -- on any retry, or when the *outer* URL's host genuinely changes
(`_host_of()`, comparing hosts the same way `adafruit_requests` itself parses them). `api.github.com`
-> `github.com` triggers a rebuild; same-host repeated calls reuse the session. Deployed and
re-tested: **the exact same bare `MemoryError` recurred, right after the same second network call**
-- meaning it wasn't "too many rebuilds" (this path only does 2-3 total by that point) at all.

**Actual root cause, found by reading `adafruit_connection_manager.py`'s source directly:**
`get_connection_manager(pool)` caches one `ConnectionManager` per socket pool in a **module-level
global dict** (`_global_connection_managers`), keyed by the pool object itself. Every
`_new_session()` call creates a brand-new `SocketPool`, which permanently registers a new entry in
that global cache. Setting `self.session = None` and calling `gc.collect()` -- the fix already
applied for the "two full sessions alive at once" MemoryError earlier in this investigation --
only ever dropped *our own* reference; it could never free the pool/`ConnectionManager`, because
the global dict still held a strong reference to it as a key. This is a genuine, unbounded leak
across repeated session rebuilds, not a "too much reuse" pressure problem -- confirmed by failing
at rebuild #2-3, not needing anywhere near the ~18 rebuilds a full `apply()` would eventually do.

**First fix attempt (confirmed live NOT sufficient, but harmlessly so):**
`adafruit_connection_manager.connection_manager_close_all(release_references=True)` -- the
library's own documented function for releasing those global references -- raised `KeyError`
immediately, caught and logged, but that also meant the actual fix never ran. Root cause: that
function assumes pools were created via the library's own `get_socketpool()`/`get_ssl_context()`
factory functions, which register them in a *separate* tracking dict
(`_global_key_by_socketpool`). This project's pools are constructed directly
(`socketpool.SocketPool(wifi.radio)`), never through those factories, so they were never added to
that dict -- `release_references=True` tries to `.pop()` our pool from it with no default,
`KeyError`s, and never reaches the `_global_connection_managers.pop(pool, None)` call that
actually matters.

**Actual fix:** skip that incompatible helper entirely and pop our own pool directly out of
`_global_connection_managers` -- the only dict our pools were ever really registered in (via
`get_connection_manager()`, called internally by `adafruit_requests.Session.__init__`), and the
only one that matters for this specific leak. `_reset_connections()` now does
`self.session._connection_manager._free_sockets(force=True)` (closes sockets, as before) then
`adafruit_connection_manager._global_connection_managers.pop(pool, None)` directly, before
dropping the local reference and rebuilding. Both the host-gating (fewer rebuilds, still worth
keeping) and this leak fix (each rebuild that does happen is now actually clean) are in place
together.

**Confirmed live NOT sufficient either -- a third, different small-allocation MemoryError.**
Deployed and re-tested: no more `KeyError`, the pool-cleanup fix ran cleanly, and still the exact
same `MemoryError: memory allocation failed, allocating 3602 bytes` at the exact same point (the
manifest.json fetch), with ~80KB free reported shortly after -- same fragmentation signature as
every prior attempt, just from a source neither the ssl_context fix nor the connection-manager
leak fix touches.

**Suspected root cause:** by the time the manifest fetch runs, `wifi.radio.connect()` has already
been called three times in quick succession -- once in `_Fetcher.__init__`, then once per
`_reset_connections()` rebuild for each real host change (api.github.com -> github.com). Each is a
full WiFi re-association, even though every one reconnects to the *identical* network. This is
below the Python heap entirely -- ESP-IDF/lwIP-level state that no amount of `gc.collect()` or
global-dict cleanup can reach. Both confirmed bugs so far (ssl_context reuse, the
connection-manager leak) are strictly about the TLS/session layer; neither implicates the radio
connection itself, so nothing about fixing them required re-connecting wifi repeatedly.

**First fix attempt (confirmed live NOT sufficient -- fixed one symptom, reintroduced another):**
`ota_boot.py`'s `_new_session()` skipped `wifi.radio.connect()` entirely when `wifi.radio.connected`
was already `True`. Deployed and re-tested: the `MemoryError` was gone, but `OSError -12288` came
back on the very next attempt -- meaning the plain `wifi.radio.connect()` this replaced was
actually doing something that prevented `-12288`, not merely adding overhead. Neither "reconnect
every time" nor "skip the reconnect" gives a genuinely clean radio state on its own: the former
leaves the previous connection's socket/TLS-adjacent state resident when a new SocketPool/
ssl_context then tries to use the same radio (plausible source of `-12288`); the latter leaves that
same stale state in place even longer, on top of separately confirmed reconnect-accumulated
fragmentation.

**Fix attempt (confirmed live NOT sufficient by itself):** `_new_session()` does an explicit
`wifi.radio.enabled = False` then `= True` -- a genuine radio reset -- immediately before
reconnecting, every time. **Verified in isolation first**, via a manual REPL sequence replicating
the exact failing pattern (request to `api.github.com`, disable/enable/reconnect, request to the
`github.com` manifest URL) -- that succeeded cleanly (200, 2.9s). But deployed into the real
`apply()` flow, `-12288` still recurred identically.

**What the manual test didn't reproduce:** natural multi-second gaps between commands
(typing/pasting each one), versus the real code firing disable/connect/request back-to-back with
zero delay. Worse, `_Fetcher.__init__` was building a session eagerly (a full disable/enable
cycle) that then got immediately discarded by `_maybe_reset()`'s very first call (`_last_host`
starts `None`, guaranteeing a rebuild) -- meaning THREE rapid-fire radio resets happened before
the first real host-change reset even ran, not one. A physical radio power-down/up plausibly needs
real time to complete at the hardware level; reconnecting and immediately pushing data through
before that finishes is a plausible way to reproduce a low-level protocol error like this.

**Fix:** removed the redundant eager session build in `_Fetcher.__init__` (`self.session = None`
until the first `_maybe_reset()` call actually needs one), and added `time.sleep(0.3)` after both
`wifi.radio.enabled = False` and `= True` in `_new_session()`, giving the radio real settling time
on both sides of the reset. **Confirmed live NOT sufficient by itself** -- `-12288` recurred again,
identically, in the real `apply()` flow.

**Also confirmed live NOT the cause:** `storage.remount("/", readonly=False)`, called by the real
Install Update path before any network request at all (for file-write access). Replicated the
exact real call order manually (remount, then both requests, then remount back) -- succeeded
cleanly (200, 3.2s). Ruled out as a factor.

### Superseded by the section below (2026-08-01) -- this was not actually intermittent

Everything in this section was a reasonable read of the evidence available at the time (two
real-code tests, one needing a retry, one not), but it was wrong: with `_MAX_ATTEMPTS` raised to 10
and a real Install Update run against real hardware, the manifest.json fetch failed **10/10
attempts**, every single one in an near-identical **0.56-0.77s** -- not the shape of a probabilistic
hiccup at all. See "The real, deterministic answer" below for the actual root cause and fix. Left
in place rather than deleted per this project's docs policy of correcting in place, not silently
rewriting history.

### The real answer: it's an intermittent failure, and the retry mechanism already recovers from it (2026-08-01)

After every hand-typed replica of the real flow kept succeeding while the actual deployed code kept
failing -- a real, fair point raised directly: "I don't get why you can get this to work in repl
but not in code" -- the right move was to stop building external approximations and call the
**actual** `_Fetcher`/`_new_session` objects directly from a REPL, eliminating any chance of a
subtle mismatch between a hand-typed script and what's really deployed:

```python
import ota, ota_boot
f = ota._Fetcher(ota_boot._new_session)
r1 = f.get_json("https://api.github.com/repos/anotherhobby/deloop/releases/latest", 10000)
r2 = f.get_json("https://github.com/anotherhobby/deloop/releases/download/v6/manifest.json", 10000)
```

Result: attempt 1 of the manifest fetch failed with the same `OSError -12288` as every real
Install Update attempt -- but **attempt 2 (the automatic retry) succeeded cleanly**, real manifest
data back. This is the real, unmodified code, not a replica, and it recovered on its own.

**This reframes the whole investigation.** Every actual Install Update failure observed this
session hit `-12288` on *both* of only 2 total attempts before `_retry_wait()` gave up. A single
successful recovery on attempt 2 is direct evidence that the rebuild-and-retry mechanism (the
ssl_context-per-host rebuild, the connection-manager leak fix, the radio reset) genuinely works --
it's just that 2 total attempts is a thin margin against something that may be a real, but now
considerably less frequent, intermittent TLS hiccup rather than a fully deterministic bug with one
remaining root cause still to find. Today's fixes plausibly did reduce how often `-12288` happens,
even without eliminating it to zero -- consistent with every prior fix attempt narrowing the
failure without a full explanation ever clicking into place.

**Fix:** raised `_MAX_ATTEMPTS` from 2 to 10 (explicitly generous, for headroom while gathering
real failure-rate data rather than guessing at the "right" number). This is a deliberate change in
posture from "find the one remaining deterministic cause" to "the mechanism recovers; give it more
room to."

**Second data point, same day, same exact test repeated:** ran the identical real-code test
(`ota._Fetcher(ota_boot._new_session)` against the same two URLs) again -- this time attempt 1 of
the manifest fetch **succeeded immediately, no `-12288` at all**. Combined with the first run
(attempt 1 failed, attempt 2/the retry succeeded), that's two-for-two eventual success across the
only two real-code (non-replica) tests run, with the failure not even reproducing on one of them.
Consistent with a real but genuinely intermittent hiccup that today's fixes reduced the frequency
of, not a deterministic bug still waiting for its one remaining cause.

**Diagnostic loop, for a real failure-rate measurement instead of a handful of manual runs** --
paste into a REPL with wifi already connected (so every iteration exercises the radio disable/
enable reset path, matching the real bug's exact conditions, not the plain first-connect path):

```python
import wifi, config, ota_boot, time

if not wifi.radio.connected:
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)

N = 20
headers = {"User-Agent": "deloop", "Connection": "close"}
url = "https://github.com/anotherhobby/deloop/releases/download/v6/manifest.json"
successes = 0
fails = []
for i in range(N):
    t0 = time.monotonic()
    try:
        session = ota_boot._new_session()
        resp = session.get(url, headers=headers, timeout=20)
        ok = resp.status_code == 200
        resp.close()
        dt = time.monotonic() - t0
        if ok:
            successes += 1
        print(i, "OK" if ok else "BAD_STATUS", "%.2fs" % dt)
    except Exception as e:
        dt = time.monotonic() - t0
        fails.append((i, dt, type(e).__name__, str(e)))
        print(i, "FAIL", "%.2fs" % dt, type(e).__name__, e)

print("successes: {}/{}".format(successes, N))
print("failures:", fails)
```

This measures the true single-attempt (no retry) failure rate directly, using the real
`_new_session()` -- not a replica -- repeated N times against the exact URL that's failed. The
resulting number should drive whether 10 is generous, exactly right, or still not enough, instead
of continuing to guess.

### Superseded (2026-08-02) -- a real, confirmed library bug, but not the section that actually got Install Update working

Everything below about `Response.close()`/`free_socket()` and two TLS connections coexisting during
GitHub's redirect is real and confirmed (that IS what `adafruit_requests`'s own automatic redirect
handling does) -- but the specific fix described here (`_Fetcher._get_final()`, calling
`self.session.get()` with `allow_redirects=False` then manually following the redirect through
`adafruit_requests` a second time) was itself superseded before ever being confirmed working. See
"The real, final root cause" near the end of this file for what actually happened: a completely
separate bug (`adafruit_requests` choking on a ~3625-byte `Content-Security-Policy` header
`github.com` sends on every redirect response) turned out to be blocking `_get_final()` from ever
succeeding at all, which led to replacing it with `_Fetcher._raw_fetch_location()` -- a hand-rolled
raw-socket redirect resolver that never asks `adafruit_requests` to parse a `github.com` response in
the first place. That rewrite happens to *also* avoid the two-TLS-connections problem described
below (the raw socket is closed for real before the second, real request opens), but that was a
side effect of fixing the CSP-header bug, not the reason the fix was written. Left in place per this
project's docs policy of correcting in place, not silently rewriting history -- the mechanism
described below is real, just not what was actually blocking things by the time it mattered.

### The real, deterministic answer: two TLS connections held open at once during GitHub's redirect (found + fixed 2026-08-01)

With `_MAX_ATTEMPTS` raised to 10, a real Install Update run finally produced the data that had been
missing the whole time: the manifest.json fetch failed **all 10 attempts**, every one in a nearly
identical **0.56-0.77s** -- while the preceding `api.github.com` call (same run, same device,
seconds apart) succeeded in `2.13s` as it always has. Ten full session rebuilds (fresh wifi
disable/enable, fresh socketpool, fresh ssl_context -- everything `_reset_connections()` does) in a
row, all failing identically and fast, is not what a probabilistic hiccup looks like. It's what a
structural, repeatable mismatch looks like. Free memory (`91120` bytes, reported right after) was
never the constraint -- which should have been the tell all along: every fix attempt in this
investigation that touched `gc.collect()`/fragmentation/session-rebuild timing was chasing a Python
heap number that was fine the entire time.

**The actual mechanism, found by reading `adafruit_requests.py`'s `Response.close()` directly:**

```python
def close(self) -> None:
    if not self.socket:
        return
    if self._session:
        self._session._connection_manager.free_socket(self.socket)   # <-- not close_socket()
    else:
        self.socket.close()
    self.socket = None
```

`free_socket()` marks a socket "available for reuse" in the `ConnectionManager`'s bookkeeping -- it
never calls `socket.close()`. The actual TLS session stays fully allocated at the native
mbedtls/lwIP level.

Every asset URL GitHub hands back (`manifest.json`, and every one of the ~16 per-file assets) is a
`browser_download_url` of the form `https://github.com/<repo>/releases/download/vN/<file>`, which
**always** 302-redirects to a signed, per-download `release-assets.githubusercontent.com` URL on a
completely different host. `adafruit_requests.Session.request()` follows that redirect internally
(the default `allow_redirects=True`) by calling itself recursively for the new URL. Right before
that recursive call, it does exactly this:

```python
self._last_response = resp
resp = self.request(method, url, data, json, headers, stream, timeout)
```

and at the *top* of that recursive call:

```python
if self._last_response:
    self._last_response.close()
    self._last_response = None
```

-- `.close()` on the `github.com` response, which (per above) only frees it, never closes it. The
code then immediately opens a **second, simultaneous** TLS connection to the CDN host for the
redirect target, while the first TLS session to `github.com` is still fully live. `api.github.com`
never redirects -- exactly one connection, ever -- which is exactly why it has never once failed in
any test this project has run, isolated or otherwise. `github.com`'s release-download URLs always
redirect, so they've never once succeeded through the library's automatic redirect path.

This also explains why the earlier "confirmed safe in isolation" test (a single fresh session's
first-ever request following a `github.com` -> CDN redirect, quoted in `_maybe_reset()`'s old
docstring) succeeded: it was checking that a *cold* session could make it through one redirect at
all, not that doing so was actually resource-safe. It happened to work once; it does not work
reliably, and per the 10/10 real-hardware result above, arguably doesn't work *at all* reliably.

**Fix:** stop relying on `adafruit_requests`'s automatic redirect-following. `_Fetcher._get_final()`
(new) issues the initial request with `allow_redirects=False`; on a 3xx with a `location` header, it
explicitly closes that response, calls the existing `_reset_connections()` (the same full
wifi/socketpool/ssl_context rebuild already proven elsewhere in this investigation to actually tear
a TLS session down, which plain `.close()` does not), updates `self._last_host` to the redirect
target's host, and only then opens the second request. `get_json()`/`download()` both now call
`_get_final()` instead of `self.session.get()` directly. `_maybe_reset()`'s existing outer-host
comparison (`api.github.com` vs `github.com`) still does the right thing on top of this for free: it
was already forcing a reset whenever the *next* file's outer host didn't match `self._last_host`, and
since `_get_final()` now leaves `self._last_host` pointing at the *previous* file's CDN host, every
subsequent file's `github.com` fetch correctly triggers a reset too.

**Cost:** every file download now pays for two full session rebuilds instead of one (or zero) --
one for the `github.com` hop, one for the redirect target. That's real overhead across a ~17-file
manifest (each rebuild includes a wifi radio disable/enable cycle with settling delays), and Install
Update will be noticeably slower than the ~2s single-file Check Now case. Correctness first: no file
has ever downloaded successfully through the old implicit-redirect path, in any test, ever. Whether
the two rebuilds can be safely narrowed to something lighter than a full radio bounce is a fair
follow-up once a complete Install Update has actually succeeded at least once.

**Not yet confirmed live** -- this fix has not yet been deployed/tested on real hardware. That's the
very next thing to do.

### Status as of this writing

**Correcting the historical record:** earlier versions of this doc claimed Check Now was
"confirmed working, live, multiple times" from the original PR #9 session. Per the user directly,
2026-08-01: "near as I know we've never successfully downloaded a thing from github" -- i.e. OTA
had, in practice, never actually completed successfully before today. That earlier claim should
not have been trusted at face value; it's left here as a correction, not repeated as fact
elsewhere in this file. The real, load-bearing history starts with today's findings below.

**Shipped:** PR #10 (the retry/session-rebuild hardening plus the boot-loop fix described in
"Reliability investigation" -> "Resolved 2026-08-01" above) merged to `main` 2026-08-01, which
triggered the automated release workflow (`v6`, superseding `v5`). At the time this merged, the
actual root cause below was not yet known -- PR #10 made failures safer, not more likely to
succeed.

**Correction #2, same day:** the paragraph that used to be here claimed the `User-Agent`/
`Connection: close` headers were the root cause. Re-tested through the real code path, that fix
alone did not hold up either -- see "Reliability investigation" -> "Correction, same day: this was
still not the real root cause" above. The headers are still in place (harmless, arguably correct
regardless of impact) but were never the deciding factor.

**Actual root cause found and fixed, live, 2026-08-01:** `import app` alone -- regardless of
which branch `main()` took afterward -- pulled in the entire device-driver stack
(`driver.py`/`dial_ui.py`/the active backend module), leaving only ~30KB free by the time a Check
Now/Install Update tried to open a TLS connection to GitHub. A bare TLS handshake at that memory
level fails outright with a `MemoryError`, independent of `adafruit_requests`, headers, retries, or
timeouts -- all of which were real, legitimate things to fix, but none of which were *this*. See
"The real root cause: app.py's own imports" above for the full isolation. Fix: `code.py` now
decides whether to import `app.py` or the new, minimal `ota_boot.py` *before* either import
happens, based on the pending-action nvm byte -- so OTA never pays the backend-stack import cost
at all.

**Confirmed live, 2026-08-01, first success of this entire investigation:** Check Now through the
real menu (`ota_boot.py` deployed via `make deploy`) completed in `2.23s`
(`ota: GET+parse took 2.23s`), with **`72016` bytes free** right before the nvm result writes --
more than double the ~30KB `import app` left behind, and the fast completion this whole
investigation was looking for from the very first `ETIMEDOUT`. The very next normal boot also came
up clean (`56880` free before `dial_ui.init()`, no crash) -- most likely because a first-attempt
success never triggers the retry/session-rebuild path that caused the earlier fragmentation, so
there's nothing left to leak into the next boot. This is the first confirmed successful GitHub
round-trip in the project's history, by the user's own account of prior sessions.

**Superseded by "The real, final root cause" below** -- the numbered list that used to be here was
written before Install Update had ever succeeded even once. It has succeeded now, twice,
independently. See that section for the actual remaining root causes (there were two more after
this point) and their fixes.

### The real, final root cause: three more bugs found and fixed after this point (2026-08-01/02)

Getting from "Check Now works" to "Install Update completes, all 16 files, verified" took three more
root causes, found and fixed in this order. Each is real, confirmed live, and independent of the
others.

**Bug 1: `adafruit_requests` chokes on GitHub's `Content-Security-Policy` header (found + fixed
2026-08-01).** A raw-socket probe of `github.com`'s redirect response (bypassing `adafruit_requests`
entirely, to see the actual bytes) found a **3625-byte `Content-Security-Policy` header** -- one
single header line, not the whole response (total header block ~5.1KB across ~16 headers; the
`Location` header itself is a comparatively modest ~915 bytes). `adafruit_requests`'s
`Response._parse_headers()`/`_readto()` reads one line at a time by growing a single bytearray 32
bytes at a time, copying the entire buffer-so-far on every growth step -- for this one line alone,
~113 sequential allocate-and-copy cycles, each leaving the previous, smaller buffer as garbage on
this device's non-compacting allocator. Confirmed live this produces either a bare `MemoryError`
("allocating 3602 bytes", within one 32-byte step of the CSP header's real length) or `OSError
-12288`, depending on the heap's exact fragmentation state -- both are almost certainly the same
underlying cause showing up two different ways. This has nothing to do with which host a redirect
lands on, TLS connection lifecycles, or ssl_context reuse -- every theory this file documented
before finding this (two TLS connections held open, connection-manager leaks, radio-reset timing)
was a plausible read of the *symptom* (`-12288`, intermittent-looking failures) that never actually
explained *why* -- gc.mem_free() reading 80-90KB+ through every one of those failures should have
been the tell.

*Fix:* `_Fetcher._raw_fetch_location()` (`src/ota.py`) resolves the `github.com` -> CDN redirect via
a raw socket, scanning for the status line and `Location:` header incrementally as chunks arrive --
never accumulating more than one chunk plus a small carry-over fragment at a time, so the
3625-byte CSP line is scanned past and discarded without ever being fully reassembled.
`adafruit_requests` never touches a `github.com` response at all anymore; it only ever parses the
CDN's response (confirmed live to have normal-sized headers, ~874 bytes total, longest line 65
bytes -- this bug is specific to GitHub's own app server, not the CDN). `resolve_download_url()` is
the retry-wrapped public entry point `ota.py`'s module-level functions call.

**Bug 2: the first WiFi radio `stop_station()`/`start_station()` cycle after a fresh boot doesn't
finish settling (found + fixed 2026-08-01).** With bug 1 fixed, `-12288` was *still* 100%
reproducible on the manifest fetch -- but now clearly deterministic, not intermittent: 10/10
attempts, every one in a near-identical 0.56-0.77s. Exhaustive REPL isolation (real `_Fetcher`/
`_new_session` objects, not replicas) ruled out host identity, host order, redirect involvement, and
raw-socket-vs-`adafruit_requests` mechanics one at a time -- none of them mattered. The one variable
that did: adding a single extra, otherwise pointless session rebuild *before* the first real request
of the boot made the exact same failing host sequence succeed reliably afterward, regardless of
order. Whatever ESP-IDF/lwIP/mbedTLS state needs to finish initializing after a station
stop/start/reconnect cycle doesn't finish synchronously the first time it happens after a boot.

*Fix:* `ota_boot.run()` does one throwaway `_new_session()` call immediately after confirming
Wi-Fi is configured, before any real check/install work -- absorbing the one-time settling cost on
a session nothing else uses, so it never lands on whatever the first real request happens to be.

**Bug 3 (the final one): `apply()` kept the entire parsed GitHub release JSON alive for the whole
file-download loop (found + fixed 2026-08-02).** With bugs 1 and 2 fixed, Install Update got much
further -- as far as file 2 or 3 of 16 -- before `-12288` recurred. Systematic REPL experiments
(isolating `config.mpy`'s download alone: 10/10 clean; isolating the exact real sequence -- release
fetch, then repeated `config.mpy` downloads: clean; only reproduced by replicating the *actual*
`apply()` sequence with real, differently-sized files in real order) pointed at something specific
to the real flow, not bounce count, not connection count, not `download()` vs `get_json()`, not
generic streaming/hashing/file-I/O overhead. Direct `gc.mem_free()` accounting at each step of a
release fetch found the answer precisely:

| Step | Free memory | Recovered |
|---|---|---|
| Before release fetch | 89104 | -- |
| After fetch + `gc.collect()` (release still referenced) | 28208 | -- |
| After clearing `session._last_response` | 31424 | +3,216 bytes |
| After force-closing the idle socket (`_free_sockets(force=True)`) | 51104 | +19,680 bytes |
| After dereferencing the `release` object itself | 104496 | +53,392 bytes |

Three distinct, quantified holders -- none of them fragmentation, all of them genuine object
lifetime: `Session._last_response` (a small, real reference `adafruit_requests` itself keeps until
the *next* request on that session, confirmed but minor); the idle-but-not-truly-closed socket
(confirmed real, ~20KB, tracked as follow-up hardening -- see below); and by far the largest, the
full parsed `release` object itself -- ~53KB for this project's ~17-asset release, kept alive by
`apply()` for the entire file-download loop even though only each asset's `name`/
`browser_download_url` pair is ever read again (via the removed `_find_asset()` helper).

*Fix:* `ota._asset_url_map(release)` extracts only `{name: browser_download_url}` immediately after
the release fetch; `apply()` then sets `release = None` and calls `gc.collect()` before the
memory-sensitive manifest/file-download sequence begins, discarding ~53KB of GitHub metadata
(uploader info, timestamps, labels, ids, download counts, ...) that was never used again anyway.
`_find_asset()` is gone -- `_asset_url_map()`'s dict lookup replaced both of its call sites.

**Confirmed live, twice, independently, 2026-08-02: full Install Update sequence, all 16 files,
succeeds end to end.** First via a REPL test script replicating `apply()`'s exact structure (minus
the final commit, to avoid overwriting in-progress fixes with the old release); second via
`tools/ota_regression_check.py` (`make probe-ota-regression`), a new permanent on-device regression
tool written specifically because of this bug (see its module docstring). Both runs: 16/16 files
resolved, downloaded, and verified clean, ~270s, ~35 session rebuilds, no failures. This is the
first time in this feature's entire history that a complete Install Update sequence has succeeded.

**Deliberately not implemented (tracked, not blocking):** the ~20KB idle-socket finding is real and
quantified, but confirmed *not* necessary to fix the actual product flow -- a real Install Update
is exactly one sequence per boot (`ota_boot.run()`'s `_reload_to_normal()` always reboots
afterward), and that scenario passed cleanly with only the `_asset_url_map()` fix in place.
Artificially repeating the full sequence multiple times *without* a reboot in between (something
production never does) does still fail, with a bare `MemoryError`, a bit further in each additional
repeat -- interesting, but not a product requirement. Force-closing idle sockets after every
`get_json()`/`download()` call (not just on host-change/retry) would likely fix that too, but it
changes connection-lifecycle semantics in a lower-level networking component after the actual OTA
flow is already working -- deliberately kept as a separate, optional hardening change rather than
bundled with the fix that was actually needed, so a future regression is easy to attribute to one
change or the other. If ever implemented, track it as "reduce retained TLS memory between
sequential requests" and re-run `make probe-ota-regression` (both with and without a reboot between
repeats) to confirm it doesn't change behavior the current fix already depends on.

**Regression testing:** `tools/ota_regression_check.py` (`make probe-ota-regression`) runs the real
release-fetch -> `_asset_url_map()` -> manifest -> per-file resolve+download+verify sequence on
real hardware, over real Wi-Fi, using whatever `ota.py`/`ota_boot.py` is currently deployed -- stops
short of the final rename-into-place commit, so a pass never overwrites the files actually running
on the device. Run this after any change to `ota.py`'s `_Fetcher`/session/`apply()` internals; it
catches the class of regression this section documents far faster than a full manual Install
Update cycle (menu -> reboot -> device UI) would.
