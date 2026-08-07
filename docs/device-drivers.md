# deloop Device Driver Architecture

The pluggable backend design (`src/driver.py` + one module per device), the recipe for adding a
new backend, hard lessons learned building the first five, and each backend's own protocol
reference and history. See [docs/architecture.md](architecture.md) for the overall project
layout/stack, and [docs/ota.md](ota.md) for the unrelated self-update feature.

## Denon/Marantz Control Direction

The Dial should talk directly to the AVR on the local network.

Initial implementation should prefer the simplest reliable Denon control surface:

- HTTP polling/control if sufficient for the POC
- Telnet/event socket only if needed for faster external state updates

Important caveat: some Denon/Marantz receivers may allow only one telnet/event client at a time. If Home Assistant or another integration already uses that channel, the Dial should avoid fighting it unless there is a deliberate architecture change.

Known Denon command families discussed:

- `PW` for power
- `MV` for master volume
- `MU` for mute
- `SI` for source/input
- `MS` and related commands may cover sound/surround modes, depending on model

Dirac preset and speaker preset commands need to be confirmed against the specific AVR model/protocol docs or by observing known working calls.

## AVR Details -- Denon AVR-X4800H (confirmed 2026-07-24)

- **Model**: Denon AVR-X4800H
- **API type**: `avr-x-2016` (identified by python-denonavr library)
- **Port 80**: redirects to HTTPS 443
- **Port 443**: HTTPS, returns 403 for all /goform/ paths
- **Port 8080**: plain HTTP -- the working control API
- **Port 11080**: plain HTTP -- the web UI SPA, also hosts speaker/Dirac API

### Port 8080 -- Status and control

Status: `POST http://<host>:8080/goform/AppCommand.xml`
- Body requires `\n` after XML declaration, no Content-Type header
- Max 5 `<cmd>` elements per `<tx>` block

```
<?xml version='1.0' encoding='utf-8'?>
<tx><cmd id="1">GetAllZonePowerStatus</cmd><cmd id="1">GetAllZoneSource</cmd><cmd id="1">GetAllZoneVolume</cmd><cmd id="1">GetAllZoneMuteStatus</cmd></tx>
```

Response `<cmd>` blocks in command order:
- Block 0 (power): `<zone1>ON</zone1>`
- Block 1 (source): `<zone1><source>SAT/CBL</source></zone1>` (raw name)
- Block 2 (volume): `<zone1><volume>-40.0</volume>...</zone1>`
- Block 3 (mute): `<zone1>off</zone1>`

Direct control (GET, port 8080):
- Volume: `/goform/formiPhoneAppDirect.xml?MVUP` / `?MVDOWN`
- Set volume: `/goform/formiPhoneAppVolume.xml?1+{db:.1f}`
- Mute: `/goform/formiPhoneAppDirect.xml?MUON` / `?MUOFF`
- Power: `/goform/formiPhoneAppDirect.xml?PWON` / `?PWSTANDBY`

Input friendly names: `POST /goform/AppCommand.xml` with `GetRenameSource`.
Source names use inconsistent slash order (SAT/CBL vs CBL/SAT in different
responses); normalize by sorting slash-separated parts uppercase before lookup.

### Port 11080 -- Speaker preset and Dirac Live

Endpoints use GET with URL-encoded XML data (jQuery AJAX default).
Data format: `Ee(element, value)` = `<Element>value</Element>` then URL-encoded.
No special headers required.

**Speaker Preset:**
- Get: `GET /ajax/speakers/get_config?type=11` -> `<SpeakerPreset>1</SpeakerPreset>`
- Set: `GET /ajax/speakers/set_config?type=11&data=%3CSpeakerPreset%3EVALUE%3C%2FSpeakerPreset%3E`
  Values: "1" = preset 1, "2" = preset 2

**Dirac Live:**
- Get: `GET /ajax/audio/get_config?type=14` -> `<DiracLive><Value>N</Value>...<Name display="3" index="0">FilterName</Name>...</DiracLive>`
  Only `display="3"` Names are available.
- Set: `GET /ajax/audio/set_config?type=14&data=%3CDiracLive%3EVALUE%3C%2FDiracLive%3E`
- Value "0" = HTTP 500 (invalid)
- Value "1" = Off (no filter)
- Value "2" = filter at Name index="0"
- Value "3" = filter at Name index="1"
- Formula: `value = int(Name.index) + 2`, Off is always "1"

Switching speaker preset changes available Dirac filters -- always reload
`get_dirac_filters()` after `set_speaker_preset()`.

## POC Status (v1.0 -- COMPLETE as of 2026-07-24)

*Historical -- this was "done" before the driver split and MiniDSP support existed. See
[Device Driver Architecture](#device-driver-architecture-v20) below and
[Current Recommendation](architecture.md#current-recommendation) in docs/architecture.md for the
actual current status.*

All originally requested features are working on hardware.

### Implemented and working
1. CircuitPython 10.2.1 on M5 Dial, `make deploy` via USB drive.
2. Volume display with FreeMonoBold 36pt font (monospaced, always fixed width).
3. Rotary encoder controls AVR volume: optimistic display update on every tick,
   single `set_volume` HTTP call 150ms after spin stops (debounced).
4. Encoder acceleration: ticks faster than 50ms apart use 2dB/tick. Safety cap
   at -15dB during fast upward spin (configurable via settings.toml).
5. Adaptive polling: 5s while idle or in standby, 30s while recently active.
   Poll never fires while encoder is spinning.
6. Touch long-press 0.5s = mute toggle.
7. Touch long-press 1.5s = power toggle (suppresses mute at same press).
8. All commands reset the poll timer to prevent back-to-back HTTP blocking.
9. Standby state: backlight dims to 5%, display shows "OFF" in white.
10. Power-on transition: shows "---.-" briefly, polls in 1s for real volume.
11. Friendly input names loaded at boot via GetRenameSource ("Desk" not "SAT/CBL").
12. Encoder press opens menu: Dirac Live, Speaker Preset, Brightness.
13. Dirac Live filter selection (custom names from AVR, Off always last).
14. Speaker preset selection (display names via SPEAKER_PRESET_1/2 in settings.toml).
15. Brightness adjustment (live preview in menu, saved to nvm on confirm).
16. Switching speaker preset auto-reloads Dirac filter list (they are tied).
17. Config via settings.toml (WiFi, AVR host, all tunable params).

## Device Driver Architecture (v2.0)

Added 2026-07-28 when the user wanted minidsp-rs (https://github.com/mrene/minidsp-rs) support
alongside Denon, explicitly asking for a real pluggable-backend design ("near 100% chance of
extending support in the future") rather than a one-off Denon-vs-MiniDSP branch. Five backends
now follow this recipe -- Denon, MiniDSP, CamillaDSP (added 2026-07-30, see "Backend #5:
CamillaDSP" below), HA `media_player`, and WiiM/LinkPlay -- this section is what a sixth would
follow.

### The shape

`src/driver.py` is the *only* module `app.py`/`dial_ui.py` import for device control -- they
never import `denon.py`/`minidsp.py`/`ha.py` directly. `driver.py` does a conditional import based
on `config.DEVICE_DRIVER` and re-exports each backend's functions, with `getattr(..., default_lambda)`
fallbacks for anything a given backend doesn't implement. That's the entire mechanism -- no
registry, no plugin discovery, just one `if` in `driver.py`. The same conditional also gates a
backend's optional paired UI extension (`driver.ui_impl`, e.g. `ha_ui.py`) -- see driver.py's
"UI-extension contract" comment, and step 8 below.

The **full contract is documented as a comment at the top of `src/driver.py`** -- read that first,
it's kept current on purpose. Summary: every backend exposes a `CAPS` dict of booleans
(`power`, `input_select`, `presets`, `preset_enable`, `preset_select_enables`) and a `LABELS` dict
of display strings, plus `init/get_status/set_volume/mute_on/mute_off` always, and
`power_on/power_standby`, `load_input_names/get_inputs/set_input/friendly_input`,
`get_presets/set_preset`, `get_preset_enabled/set_preset_enabled` as optional groups gated by the
matching `CAPS` key.

**Why capabilities are booleans in a dict, not driver-name checks:** `app.py` builds its menu
and gates gestures by reading `driver.CAPS[...]`, never by checking `config.DEVICE_DRIVER ==
"minidsp"`. That's what let the power long-press gesture, the Input menu entry, and the
preset-disable tap gesture all correctly disappear/appear per-backend with zero `if driver_name`
branches in the UI layer. Keep it that way for a fourth backend -- if it needs a new axis of
behavior, add a new `CAPS` key rather than a special case. HA already added one this way:
`CAPS["player_select"]` (see "Backend #3: HA media_player" below).

**Some CAPS keys are discovered at runtime, not import time.** `CAPS` is a plain module-level
dict, and `driver.py` does `CAPS = _impl.CAPS` -- same object reference, not a copy -- so a
backend can mutate its own `CAPS` dict *after* a live query and `driver.CAPS` sees the update
immediately. `minidsp.py` uses this for `CAPS["preset_enable"]`: it's `False` until the first
`get_presets()` call (normally at boot) checks whether the unit's status includes a `"dirac"`
field at all, then flips it. `ha.py` goes further -- `CAPS["player_select"]` lets it re-derive its
*other* CAPS entries again at runtime, not just once at boot, when the user switches which
`media_player` entity is targeted (see `ha.py`'s `set_player()`). `app.py` only reads CAPS fresh
on every menu render, never caches a snapshot, which is what makes both patterns work with zero
menu-building changes.

### Recipe for adding a backend

1. Write `src/newdevice.py` implementing the contract in `driver.py`'s docstring. Model it on
   whichever existing backend is a closer match -- `denon.py` if the device has one combined
   protocol value that means both "which preset" and "is it on", `minidsp.py` if those are
   independent fields.
2. Add the `elif config.DEVICE_DRIVER == "newdevice": import newdevice as _impl` branch in
   `driver.py`.
3. Add `NEWDEVICE_*` settings to `config.py` (host/port/whatever) and document them in
   `settings.toml.template`.
4. If volume range/semantics differ (they did between Denon's -80..+18dB-relative-to-reference
   and MiniDSP's -127..0dB-of-attenuation), add the per-driver default in `config.py`'s
   `VOLUME_MIN`/`VOLUME_MAX` block -- `dial_ui.py` already reads those generically and scales its
   color bands/ticks proportionally, no UI change needed.
5. Write `tools/probe_newdevice.py` mirroring `probe_denon.py`/`probe_minidsp.py` -- a host-side
   script that hits the same endpoints the driver will use and prints raw responses, so protocol
   assumptions get checked against a real unit *before* they're baked into CircuitPython code that's
   awkward to debug on-device.
6. Update the README's "Device backends" table (one column per backend) and add the new file to
   both `Makefile`'s `_copy-files` (plain `.py` deploy) *and* `MPY_MODULES` (`.mpy` deploy --
   easy to forget the second one since `deploy` alone won't catch it).
7. Update `tools/dial_sim.py`'s fixture state if the new backend changes what a "normal" state
   object looks like (it currently assumes `preset`/`preset_names`/`preset_enabled` exist, which
   should already be true for anything following the contract).
8. **Only if the backend needs UI beyond `dial_ui.py`'s generic gauge/volume/menu chrome** (HA
   needed this for the row-swap and skip icons; Denon/MiniDSP didn't need it at all): write a
   paired `src/newdevice_ui.py` implementing whatever subset of driver.py's "UI-extension
   contract" it needs, and add it to `driver.py`'s conditional import (`_ui_impl = None` for a
   backend without one). `ha_ui.py` is the reference example. Don't add backend-specific branches
   to `dial_ui.py` itself -- that defeats the point (a denon/minidsp build would then import and
   compile code it never uses; see [docs/architecture.md](architecture.md)'s "CircuitPython
   heap/boot-memory guardrails" for why that matters more than it sounds like it should).

### Hard lessons from the Denon -> MiniDSP round (read these before touching a new backend)

These cost real debugging cycles against live hardware. All of them held true through HA (backend
#3) too -- treat them as permanent, not MiniDSP-specific.

1. **CircuitPython's `str` is not desktop Python's `str`.** It's MicroPython's stripped-down
   string type and does not implement `.title()`, `.capitalize()`, `.center()`, `.zfill()`, and
   others that exist on every desktop CPython string. Code that calls one of these will pass every
   host-side test (real CPython in `.venv`) and then crash with
   `'str' object has no attribute 'title'` the first time it runs on the actual Dial. There is no
   static check for this in this project -- `grep` for `\.title(\|\.capitalize(\|\.center(\|\.zfill(`
   before considering a new backend done, and prefer manual `s[:1].upper() + s[1:].lower()` over
   any "convenience" string method you haven't specifically confirmed CircuitPython supports.
2. **`dict.get(key, expr)` evaluates `expr` unconditionally**, even when `key` is found. Writing
   `friendly.get(name, name.title())` calls `.title()` on *every* call, not just misses -- this is
   how lesson #1 above actually got triggered in `minidsp.py`. Use
   `friendly.get(name) or fallback(name)` (short-circuits) instead.
3. **Don't trust a summarized/fetched doc for exact wire behavior -- read the actual source, or
   test against a live unit.** Two real mistakes this round: (a) assumed minidsp-rs's `Source`
   enum serialized lowercase because of a `strum(serialize_all = "lowercase")` attribute on it --
   that attribute only affects `strum`'s own `Display`/`FromStr` (used for CLI parsing), not the
   separate `serde::Serialize` derive that actually produces the JSON, which keeps the literal
   Rust variant name (`"Analog"`, not `"analog"`) -- confirmed by a live AVR/minidsp-rs instance
   rejecting the lowercase form with an HTTP 500. (b) claimed minidsp-rs binds to `127.0.0.1:5380`
   by default based on a fetched summary of its README -- actually reading
   `daemon/src/config.rs`/`http/mod.rs` shows the real default is `0.0.0.0:5380`; the *doc's
   example config file* just happens to ship with the restrictive value active as its example.
4. **Don't assume request latency -- measure it against real hardware, per operation.** Denon's
   Dirac-filter control command (`PSDIRAC ...`, port 8080) returns immediately (fire-and-forget)
   but the AVR takes ~5-6s to actually apply the change internally -- so Denon needed *optimistic*
   local state updates in `app.py`, not a longer client timeout. MiniDSP is the opposite:
   minidsp-rs's `POST /devices/{i}` *blocks* until the DSP finishes reconfiguring -- measured
   ~4s+ for a config-slot switch, ~0.03-1.1s for volume/mute/dirac-only-toggle. A too-short client
   timeout there doesn't just show stale UI, it appears to have crashed the minidsp-rs daemon once
   (an abandoned in-flight request likely tripped a bug in its async request handling). This is why
   `minidsp.py` has a separate, much longer `_PRESET_TIMEOUT` just for `set_preset`/
   `set_preset_enabled`, distinct from the general `_TIMEOUT` used for volume/mute/status reads.
   **Time every write operation against a live unit before picking a timeout constant.**
5. **When a blocking call is unavoidably slow and the main loop is fully synchronous** (no
   `asyncio` anywhere in this codebase -- `app.py`'s loop is a plain `while True`), give visual
   feedback *before* making the call, since nothing can animate *during* it. `dial_ui.draw_busy()`
   paints one static all-gray frame right before a slow `driver.set_preset*()` call, and the
   normal `draw_main()` afterward swaps it back to color -- no per-frame animation, because there
   are no frames while blocked. This pattern (call `draw_busy()`, do the blocking thing, call
   `draw_main()` including in the `except` branch so a failure doesn't strand the UI on the gray
   frame) is worth reusing for any future backend operation that's confirmed slow.
6. **A device-behavior difference is a CAPS flag, not a "this is Denon" check in `app.py`.** The
   `preset_select_enables` flag exists because of a real semantic difference the user caught:
   on Denon, selecting a Dirac filter *is* enabling it (one combined protocol value, no way to
   pick a filter without engaging it) -- but on MiniDSP, `preset` (config slot) and `dirac`
   (on/off) are independent fields, and a slot may deliberately want Dirac left off (e.g. a
   headphone config with no room correction). The first fix (auto re-enable when switching slots
   from a disabled state) was *correct for Denon and wrong for MiniDSP*; the real fix was
   expressing "does selecting also enable?" as a per-backend boolean and having `app.py` read it,
   rather than hardcoding either behavior.
7. **Physical device topology varies per backend and matters for setup docs.** Denon: the Dial
   talks straight to the AVR over Wi-Fi, no extra host. MiniDSP: minidsp-rs must be running on
   *some* separate host with the unit attached over USB, reachable on the LAN, and multiple
   attached MiniDSP units make `/devices` array-index ordering unstable across reconnects (hence
   `MINIDSP_SERIAL` as a more robust alternative to `MINIDSP_DEVICE_INDEX`). A future backend
   might have yet another topology (e.g. its own always-on IP address vs. needing a bridge/gateway
   host) -- document whichever it turns out to be, don't assume it matches an existing backend.
8. **Test against real hardware, not just host-side simulation, before calling something done.**
   `tools/dial_sim.py` (renders `dial_ui.py` off-device to PNGs) and driver-level smoke tests
   against a fake HTTP server both caught real issues, but *every* bug in this list was only
   actually found by testing against live hardware (a real AVR, a real 2x4HD, a real Flex) -- the
   `.title()` crash and the timeout/crash issue both passed host-side testing cleanly. If real
   hardware is reachable (it was, directly from this dev machine, for both the AVR and
   minidsp-rs), use `curl`/direct Python driver calls against it before considering a fix verified.

## Backend #3: HA `media_player` (added 2026-07-28)

`src/ha.py`, `DEVICE_DRIVER = "ha"` -- controls any Home Assistant `media_player` entity over
HA's REST API, instead of talking to a device directly. Tested live against
`http://hobbyhub.cabin.hobbysprawl.com:8123`, entity `media_player.office` (HA's own `denonavr`
integration for the same physical AVR this project targets -- confirmed via `GET /api/config`'s
`components` list). Token in `local/agent/deloop-ha-token.txt` -- local test-only, gitignored,
never copied into `settings.toml.template` or committed.

**Two designs were explored and rejected before landing on plain REST, both poll-driven like
`denon.py`/`minidsp.py`** (full writeup was in this session's plan file,
`purring-beaming-pnueli.md`, since cleaned up by the plan-mode tooling -- summarized here so the
reasoning survives):
1. Event-subscription over a persistent WebSocket connection, with a new `driver.pump(now)` hook
   called every main-loop tick to drain incoming `state_changed` events into a cache that
   `get_status()` would just read from. Initially proposed because the user asked for "the Home
   Assistant WebSocket API" specifically. **Rejected by the user**: this project deliberately keeps
   every backend on the same simple adaptive-poll model (see `_poll_avr` in `app.py`) --
   introducing a live/event-driven connection (plus the new contract hook it requires) was
   unnecessary complexity and risk for an ESP32 running CircuitPython, and broke the "adding a
   backend never needs to touch `app.py`" property `driver.py`'s contract otherwise guarantees.
2. WebSocket transport kept synchronous/poll-per-call (no event subscription, same cadence as the
   other backends). Still rejected: CircuitPython has no built-in WebSocket support, and the two
   community CircuitPython WS client libraries found (`cpwebsockets`, `websockets-for-circuitpython`)
   are both work-in-progress and target Airlift co-processor boards, not the M5 Dial's native
   `wifi`/`socketpool`. REST does the identical request-per-poll/request-per-command job through
   the `adafruit_requests` session already wired up in `app.py` -- zero new dependencies, same
   call shape as the other two drivers, so it won on both simplicity and risk.

**Followed the "Recipe for adding a backend" above end-to-end**: `src/ha.py` implements the
contract (closer to `minidsp.py`'s shape -- capabilities auto-detected from a live query, not
hardcoded); `driver.py` got the `elif config.DEVICE_DRIVER == "ha"` branch; `HA_*` settings added
to `config.py` and `settings.toml.template`; `VOLUME_MIN`/`VOLUME_MAX` got an `"ha"` case (see
below); `tools/probe_ha.py` mirrors `probe_minidsp.py` and was run live (read-only, then with
`volume_set`/`volume_mute` writes) against `media_player.office` before writing `ha.py` itself;
README's backend table got a third column and `Makefile._copy-files` got `ha.py`. Checked
`tools/dial_sim.py` per step 7 -- no change needed, it builds fixture `AVRState` objects directly
rather than deriving them from `config.DEVICE_DRIVER`, and doesn't render a driver-specific state
HA's contract-compliant no-presets case doesn't already cover.

### Confirmed API shapes

```
GET /api/states/{entity_id}
Authorization: Bearer <token>
-> {"state": "on"/"off"/...,
    "attributes": {"volume_level": 0.0-1.0, "is_volume_muted": bool,
                    "source": str, "source_list": [str, ...],
                    "supported_features": int}}

POST /api/services/media_player/{service}
Authorization: Bearer <token>
Content-Type: application/json
body: {"entity_id": entity_id, ...fields}
services used: volume_set ({"volume_level": float}), volume_mute ({"is_volume_muted": bool}),
                turn_on / turn_off ({}), select_source ({"source": str})
```

Confirmed working end-to-end against `media_player.office`: GET status, `volume_set`,
`volume_mute` on/off (round-tripped cleanly, volume restored to its original value afterward).
`select_source` uses the identical `_call_service` path in `ha.py` and wasn't exercised live to
avoid disrupting the office AVR's actual input mid-session -- worth a real test on hardware before
calling this backend fully verified.

`volume_level` is always a normalized 0.0-1.0 fraction, never a real dB value -- generic
`media_player` entities have no dB concept regardless of what's underneath them. `ha.py` maps it
to a plain 0-100 percent range via `config.VOLUME_MIN=0.0`/`VOLUME_MAX=100.0` (new `"ha"` branch
in `config.py`'s volume-range `if`), reusing `dial_ui.py`'s existing dB-shaped gauge/tick/
split-digit-label code completely unchanged -- same trick `minidsp.py` already uses to display
attenuation-dB instead of reference-relative dB.

`supported_features` is a `media_player`-domain bitmask, stable across HA versions, used to
auto-detect `CAPS["power"]`/`CAPS["input_select"]` inside `load_source_list()` -- same
live-detection pattern as `minidsp.py`'s `CAPS["preset_enable"]` (see hard lesson #6 above: a
device-behavior difference is a CAPS flag, not a driver-name check):

| Bit | Value | Used for |
|---|---|---|
| `SUPPORT_VOLUME_SET` | 4 | (always assumed present -- every backend needs volume) |
| `SUPPORT_VOLUME_MUTE` | 8 | (always assumed present) |
| `SUPPORT_TURN_ON` | 128 | `CAPS["power"]` (needs both this and TURN_OFF) |
| `SUPPORT_TURN_OFF` | 256 | `CAPS["power"]` |
| `SUPPORT_SELECT_SOURCE` | 2048 | `CAPS["input_select"]` (needs non-empty `source_list` too) |
| `SUPPORT_SELECT_SOUND_MODE` | 65536 | deliberately unused -- no preset/sound-mode UI |

`media_player.office` reports `supported_features: 69004` = `65536 + 2048 + 1024 + 256 + 128 + 8
+ 4` (also includes `SUPPORT_VOLUME_STEP=1024`, unused by this driver) -- confirms it supports
power, source select, volume set/mute, matching the real AVR's actual capabilities.

`CAPS["presets"]` is always `False`, unconditionally -- explicit user decision, not auto-detected
from `SUPPORT_SELECT_SOUND_MODE` even though the office entity reports it. There's no generic
`media_player` equivalent of Dirac Live/config-slot switching worth building, so no preset menu
entry is ever drawn for this backend (`_top_menu_entries()`, now in `app.py`, already omits it
purely from reading `CAPS`, no menu-building code change needed -- confirms the CAPS-driven menu
design paid off exactly as intended for a third backend).

### Play/pause status (added same day, once music was actually playing on `media_player.office`)

`media_player.office`'s `state` isn't just `"on"`/`"off"` -- once its current source is actually
streaming something (e.g. `NET`/HEOS Music), HA reports `"playing"`/`"paused"` instead, confirmed
live. Note `supported_features` also changed between the two states observed this session: `69004`
(no `SUPPORT_PAUSE`/`SUPPORT_PLAY` bits) while the source was `Desk` (analog input, nothing to
pause), vs `90045` (adds `SUPPORT_PAUSE=1`, `SUPPORT_PLAY=16384`, `SUPPORT_STOP=4096`,
`SUPPORT_PLAY_MEDIA=512`, `SUPPORT_NEXT_TRACK=32`, `SUPPORT_PREVIOUS_TRACK=16`) once `NET` was
playing -- this bitmask is **not** a fixed per-entity constant, it varies with the currently
selected source. That ruled out gating play/pause on a boot-time `supported_features` check the
way `CAPS["power"]`/`CAPS["input_select"]` do; instead the feature is driven entirely by the
literal state string every poll, no CAPS flag at all:

- `driver.py` contract gained an optional `"media_state"` key in `get_status()`'s return dict (raw
  state string) and two new optional functions, `media_play()`/`media_pause()` -- both
  `getattr(..., lambda: None)`-defaulted like `power_on`/`power_standby`, so denon.py/minidsp.py
  needed zero changes.
- `state.py`'s `apply_status()` reads it via `status.get("media_state", "")`, so it defaults to
  `""` for any backend that doesn't return the key -- the UI/tap logic below is then naturally a
  no-op for denon/minidsp without any driver-name check anywhere.
- `dial_ui.py`: the preset-name text slot (`ui["preset"]`, at `PRESET_NAME_Y`) now shows the active
  preset name if the backend has any, else `"Playing"`/`"Paused"` if `state.media_state` matches
  one of those two exact strings, else blank -- see `_status_line()`. New `media_status_tap(x, y)`
  hit-tests that same text row (±13px around `PRESET_NAME_Y`, no x check -- same "generous target,
  ignore exact text width" approach the mute zone already uses).
- `app.py`'s `_tap_main_screen()` checks `state.media_state in ("playing", "paused")` +
  `dial_ui.media_status_tap(...)` *before* the existing above/below-`PRESET_NAME_Y` mute/preset-
  button split (the status row's tap zone overlaps the top of the mute zone) -- `_tap_toggle_playback()`
  calls `driver.media_pause()`/`driver.media_play()` and flips `state.media_state` optimistically,
  same pattern as `_tap_toggle_mute()`.

Confirmed live against `media_player.office` while actually playing: `media_pause`/`media_play`
both return `200` and flip the entity's `state` between `"playing"`/`"paused"` exactly as expected
(paused then restored to playing during this session, real audio interruption -- brief but real,
same caveat as any other live-hardware test in this file).

### Media player discovery/switching + skip controls (added same day)

The user's follow-up ask: discover every `media_player` entity HA knows about at connection time,
add a menu to switch which one deloop actively controls (`HA_ENTITY_ID` becomes just the startup
default), and -- explicitly requested, not assumed -- have that switch actually update
capabilities live, not just the target. Plus `<`/`>` skip-back/skip-forward flanking the
Playing/Paused status text.

**This is the first backend where `CAPS` genuinely changes at runtime, not just once at boot.**
Every prior CAPS mutation (MiniDSP's `preset_enable`) happens from one boot-time query and is
never revisited. `ha.py`'s `set_player(entity_id)` re-runs the exact same `supported_features`
detection `load_source_list()` does at boot -- factored into a shared `_refresh_entity()` -- for
whichever entity is now current. This needed **zero changes to `app.py`'s menu-building code**:
`_top_menu_entries()` already reads `driver.CAPS` fresh on every render rather than caching a
boot-time snapshot, and `driver.CAPS`/`_impl.CAPS` are the same dict object (`driver.py`'s
`CAPS = _impl.CAPS`, not a copy), so a mutation inside `ha.py` is visible everywhere immediately.
Confirmed live: switching from `media_player.office` (has a `source_list`, `CAPS["input_select"]`
True) to `media_player.shield` (an NVIDIA Shield entity, no `source_list` at all) flips
`CAPS["input_select"]` to `False` in real time -- the Input menu entry would actually disappear on
next menu open. Switching to a nonexistent entity ID is a silent no-op (stays on the current one).

**Discovery uses `POST /api/template`, not `GET /api/states`.** The latter returns every entity in
the house (lights, sensors, climate, everything), which on a real instance is a payload no ESP32
should try to parse just to build one menu. The template call renders directly to the compact shape
needed:
```
POST /api/template
{"template": "{{ {\"ids\": states.media_player | map(attribute=\"entity_id\") | list,
                   \"names\": states.media_player | map(attribute=\"name\") | list} | tojson }}"}
```
Confirmed live against the real house: 8 entities returned (`media_player.office`, `.sony_83`,
`.sony_65`, `.shield`, `.wiim_pro_38da`, `.dining_room`, `.sony_xr_83a80l`, `.cabin`) -- two of
which (`wiim_pro_38da` and `dining_room`) share the friendly name "Dining Room", which is expected
(HA's own naming, not something deloop tries to disambiguate). **Gotcha confirmed live: this
endpoint's response `Content-Type` is `text/plain`, not `application/json`**, even though the body
is a JSON string (because the template renders to one via the Jinja `|tojson` filter) --
`ha.py`'s `load_players()` parses `resp.text` with `json.loads()` explicitly rather than calling
`resp.json()`, which may assume/require a JSON content-type CircuitPython's `adafruit_requests`
might not tolerate.

**`load_players()` never leaves the list empty**, even on total discovery failure -- falls back to
a single entry for the configured `HA_ENTITY_ID`. This isn't defensive-for-its-own-sake: `app.py`'s
menu navigation does `cursor % len(items)`, which would divide by zero on a genuinely empty list --
existing latent behavior in the menu code, not something this task fixed generally, but new
discovery-dependent data must not be the thing that first triggers it.

**`CAPS["player_select"]` is the first CAPS key not every backend's dict defines by default** --
`denon.py`/`minidsp.py` got `"player_select": False` added explicitly to their `CAPS` literals
(one line each) specifically so every reader can keep using plain `CAPS["player_select"]`
indexing rather than mixing in `.get(..., False)` for just this one key.

Skip controls (`media_previous()`/`media_next()` -> `media_player.media_previous_track`/
`media_next_track`, confirmed real service names via live `GET /api/services`) render as plain
`<`/`>` text flanking the Playing/Paused label (`dial_ui.py`'s `_media_side_text()`), gated by the
exact same condition as that label already was (`state.media_state in ("playing", "paused")`)
-- no new setting. Tap zones for the two icons are checked
*before* the existing full-row `media_status_tap()` play/pause zone in `app.py`'s
`_tap_main_screen()`, so the original zone's generous "anywhere in the row" target isn't narrowed,
skip is just reachable from two specific flanking positions on top of it.

### Two follow-up UX papercuts from real use (added same day)

Both surfaced from the user actually using the Media Player switching feature, not from planning:

**Getting stuck on a powered-off device.** Switch to an HA entity that's off, and you land on the
standby screen -- which for every backend until now was deliberately just one big red power
button, nothing else (`local/agent/design.md`: "nothing else competes for attention"), because
there was only ever one device to power back on anyway. With multiple selectable players, "off"
can now mean "wrong device selected," and there was no way back to the menu without powering on a
device you might not want on at all. Fixed by letting backends with `CAPS["player_select"]` show a
MENU hint on the standby screen too (`dial_ui.py`'s `draw_main()` power-off branch) and open the
top-level menu from there (`app.py`). This touched three separate power-state guards that all
predated this feature and had never needed to distinguish "resting on the bare standby screen"
from "already inside a menu that happens to have been opened while standby": `_dispatch_tap()`,
`_handle_encoder_button()`, `_handle_encoder_rotation()`. All three now only block on
`state.power != "ON" and loop.mode == MODE_MAIN` (rotation additionally never opens the menu
itself, only navigates one already open) instead of a blanket `state.power != "ON"` -- so once the
menu is open, every input works normally regardless of power state, matching how it already worked
for backends where `CAPS["power"]` is simply always False (MiniDSP) and this was a non-issue.
Denon/MiniDSP's power-off screen is completely unchanged (`CAPS["player_select"]` is `False` for
both).

**Not knowing which device is currently targeted.** Now that switching is possible, the screen
never said which entity you were actually looking at -- confusing after a switch, or just after
not touching the Dial for a while. Added `dial_ui.py`'s `_player_name(state)` (looks up
`state.player_id` in `state.player_names`, blank unless `CAPS["player_select"]`) rendered in the
same dim `_C_MENU` gray as the MENU hint, on its own row just below the preset/status line
(`_PLAYER_NAME_Y = PRESET_NAME_Y + 24`) -- shown regardless of playback state, not just while
something's playing, since "which device" is relevant either way.

### UI polish lessons (HA row order, glyphs, skip icons, decimal display)

Durable facts and lessons from that work -- current behavior is in the code/comments, not
repeated here:

- **A label's `anchored_position` and its tap-zone hit-test are always separate things in this
  codebase that must be updated together** -- there's no shared source of truth keeping them in
  sync. Moving a label without updating its hit-test (or vice versa) is an easy, silent bug; this
  bit twice (the swapped status row, then the standby MENU hint) before becoming a habit to check.
- **Glyph presence in a font is not the same as glyph suitability.** Always check a Unicode
  candidate glyph's actual rendered height/proportions from the real generated font object (not
  just cmap presence in the source TTF, and not a Pillow/preview render) before committing to it
  for a tight embedded layout -- a glyph that "exists" can still render wildly taller than its
  neighbors. Play/pause and skip icons ended up plain ASCII (`"Play"`/`"II"`, `<<`/`>>`) after this
  check ruled out the Unicode alternatives.
- **Writing a Unicode escape sequence (e.g. `▶`-style) in generated output can get silently
  converted to the literal Unicode character before it reaches a file.** Confirmed via byte-level
  `grep -n -P '[^\x00-\x7F]'` checks. Use `chr(N)` instead when a CircuitPython source file needs a
  non-ASCII codepoint -- it's unambiguous, and verify any such edit with the same grep rather than
  trusting it stayed ASCII.

## Backend #4: WiiM / LinkPlay streamer (added 2026-07-29)

`src/wiim.py`, `DEVICE_DRIVER = "wiim"` -- controls a WiiM (or any LinkPlay-based) streamer
directly over its own `httpapi.asp` HTTP API, same "talk straight to the device" shape as
`denon.py` (no bridge/daemon like `minidsp.py`, no intermediary service like `ha.py`). Tested live
against a real WiiM Pro at `10.0.1.75` (firmware `Linkplay.4.8.814756`) throughout development --
see `tools/probe_wiim.py`. Requested by the user explicitly as "direct support," with the
`local/uc-intg-wiim` Unfolded Circle integration provided as a protocol reference (its
`client.py`/`device.py`/`const.py` supplied the confirmed command set and mode-code tables below).

Scope, confirmed with the user up front via a clarifying question before implementation: volume/
mute, always-on play/pause/skip (no opt-in flag -- a streaming source either is or isn't currently
playing; `ha` briefly had an `HA_MEDIA_CONTROLS` flag for this, removed 2026-08-06 as redundant
with the same state check both backends already use), input/source selection, and presets -- but explicitly *not* device discovery/switching like `ha`'s Media Player
menu (`CAPS["player_select"]` is `False`; WiiM only ever controls itself).

### HTTPS-only API with a self-signed cert -- the first backend needing TLS at all, and the first
### needing a raw-socket transport instead of the shared `adafruit_requests.Session`

Confirmed live: plain HTTP (port 80) returns nothing whatsoever; every command must go over HTTPS
on port 443. `denon.py`/`minidsp.py`/`ha.py` are all plain HTTP, so `app.py`'s single shared
`adafruit_requests.Session` had never needed an `ssl_context` before this backend.

The cert itself (confirmed via `openssl s_client -connect <host>:443`) is self-signed --
`CN=www.linkplay.com`, issued 2018-11-14, expires 2028-11-11. Per hard lesson #3 above (don't
trust a summarized doc, verify against the real thing): this is **LinkPlay's fixed firmware
cert, not generated per-device** -- confirmed by its subject and issuer both being the identical
generic `linkplay.com` identity rather than anything unit-specific (serial, MAC, hostname). That
means it can be embedded once, as `wiim.LINKPLAY_CA_PEM`, and trusted for any WiiM/LinkPlay unit
without asking every user to extract their own -- consistent with this project's "clone repo, plug
in device" simplicity goal.

**Hard lesson, confirmed live against a real M5 Dial (2026-07-29), that cost a real debugging
session and directly contradicts what the CircuitPython docs imply:** `ssl.SSLContext.check_hostname
= False` does **not** disable hostname/CN verification. The first implementation set it False,
loaded `LINKPLAY_CA_PEM` via `load_verify_locations(cadata=...)`, and passed that context into the
shared `Session` -- and every single poll failed with `OSError(-9984)`
(`MBEDTLS_ERR_X509_CERT_VERIFY_FAILED`, i.e. `-0x2700`), reliably, on a freshly booted device with
WiFi confirmed connected (ruled out via `wifi.radio.ipv4_address` and a raw TCP connect test first,
since the initial symptom -- the Dial hanging, then landing on the "no AVR" error screen -- looked
enough like a network problem to check that before touching TLS code at all).

Isolated by wrapping a raw socket manually (bypassing `adafruit_requests` entirely) and testing the
exact same handshake two ways: `ctx.wrap_socket(sock, server_hostname="10.0.1.75")` (the real IP)
failed with the identical `-9984` every time; `ctx.wrap_socket(sock,
server_hostname="www.linkplay.com")` (the cert's actual CN) succeeded. So the mbedtls binding
verifies the peer cert's CN against whatever `server_hostname` was passed to `wrap_socket()`
regardless of `check_hostname` -- that attribute exists and is assignable (no `AttributeError`),
it just doesn't do what its name says.

That alone would be fixable by always passing the right `server_hostname` -- except
`adafruit_connection_manager` (the library underneath `adafruit_requests.Session`, confirmed by
reading `_get_connected_socket` in its source) hardcodes `ssl_context.wrap_socket(socket,
server_hostname=host)` where `host` is the literal hostname parsed from the request URL. There is
no parameter anywhere in `Session.get()` to use a different string for TLS/SNI purposes than the
actual connection target. Since this backend necessarily connects by IP (`WIIM_HOST`) and the
cert's CN will never match an IP, **the shared `Session` can never work for this backend, no
matter how `ssl_context` is configured** -- this isn't a bug to work around, it's a real
architectural mismatch between "connect by IP" and "verify by hostname."

**The fix:** `wiim.py` bypasses `adafruit_requests` entirely and speaks raw HTTP/1.0 over a
manually TLS-wrapped socket (`init_transport(pool, ssl_context)` + `_request(cmd)`), forcing
`server_hostname="www.linkplay.com"` independently of `WIIM_HOST`. This is still real certificate
verification (the peer's chain/signature must still validate against `LINKPLAY_CA_PEM`) --
decoupling "which name the cert is checked against" from "which IP is actually dialed," not
disabling verification. `app.py` calls `wiim.init_transport(pool, ssl_context)` directly from its
`DEVICE_DRIVER == "wiim"` block (bypassing the generic `driver.init(session)` contract for this
one backend's transport setup; `wiim.py`'s `init(session)` still exists to satisfy the contract's
call signature, but is a no-op). CircuitPython's `SSLSocket` only exposes `send()`/`recv_into()`
(no `recv()`, confirmed by hitting `AttributeError: 'SSLSocket' object has no attribute 'recv'`
mid-debugging) -- `_request()` loops on `recv_into()` until it returns 0, relying on the
`Connection: close` header to signal EOF. Confirmed live end-to-end: full HTTP/1.0 response
(status line, `Content-Length` header, JSON body) received correctly.

**Debugging-session lessons worth keeping for next time a device needs live REPL diagnosis:**
- Ctrl-C'ing into the REPL mid-network-call can leave WiFi/socket state weird enough to produce a
  misleading `EHOSTUNREACH` on the next attempt -- reconnect explicitly
  (`wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)`) at the top of any REPL diagnostic
  rather than assuming whatever state an interrupt left behind.
- `mpremote`'s plain interactive REPL needs a blank line to close a compound statement
  (`while`/`if`/etc.) -- pasting a multi-line block with another statement immediately after it
  (no blank line) gets silently mis-parsed. Use paste mode (Ctrl-E, paste, Ctrl-D) for anything
  with a loop or nested block.
- `sys.print_exception` is not available on this CircuitPython build -- use `print(type(e), e)` in
  REPL diagnostics instead.

### Confirmed API shapes (all live against the real unit, not just read from the reference)

```
GET https://<host>/httpapi.asp?command=getPlayerStatus
  -> {"vol": "0"-"100" (plain int, NOT a 0.0-1.0 fraction like ha.py's volume_level),
      "mute": "0"/"1", "status": "play"/"pause"/"stop", "mode": "<code>"}
GET .../httpapi.asp?command=setPlayerCmd:vol:<0-100>
GET .../httpapi.asp?command=setPlayerCmd:mute:<0|1>
GET .../httpapi.asp?command=setPlayerCmd:resume|pause|prev|next
GET .../httpapi.asp?command=setPlayerCmd:switchmode:<source-key>
GET .../httpapi.asp?command=getPresetInfo -> {"preset_num": N, "preset_list": [...]}
GET .../httpapi.asp?command=MCUKeyShortClick:<1-based-number>   (no "setPlayerCmd:" prefix --
  confirmed against local/uc-intg-wiim's client.py; every other command above has one)
```
All confirmed to return `OK` (or JSON for the two GETs that return status) via live `curl`, then
again via `tools/probe_wiim.py --skip-write` and its write-testing mode (volume set to a target
and restored, mute toggled both ways and restored, source switched and restored). Track metadata
(`getMetaInfo`) comes back hex-encoded (e.g. `"Title": "556E6B6E6F776E"` = hex for "Unknown") --
confirmed live, not used by this driver (no title/artist display in scope), noted only so a future
session touching metadata isn't caught off guard by it.

### Input list: empirically confirmed per-unit, not assumed from the reference

The reference integration's `PHYSICAL_SOURCES` dict lists every source key LinkPlay's protocol
supports across all models (wifi, bluetooth, line-in, optical, HDMI, phono, udisk) with no attempt
to detect which ones a given unit actually has -- there's no capability-bitmask endpoint worth
reverse-engineering for this (`getStatusEx`'s `plm_support`/`streams` fields are undocumented
bitmasks). Per hard lesson #3, this was checked empirically instead of assumed: cycling every
source key live via `setPlayerCmd:switchmode:<key>` and reading back the resulting `getPlayerStatus`
`mode` afterward (then restoring the original mode) showed `wifi` (mode 10), `bluetooth` (41),
`line-in` (40, "AUX-In"), and `optical` (43) all really switch on this WiiM Pro; `co-axial`,
`udisk`, `PCUSB`, `HDMI`, `phono` all left `mode` at `0`/`status` `none` -- not present on this
unit. `config.WIIM_INPUTS` (default `"wifi,bluetooth,line-in,optical"`) is the configurable
override for other WiiM/LinkPlay models (Amp, Ultra, Pro Plus) that may expose a different
physical set -- same escape-hatch role `MINIDSP_PRESET_NAMES` already plays for a similarly
per-unit list that the API can't enumerate.

### Mode names must fit the round screen (2026-08-06)

`_MODE_NAME` (the mode-code -> "what's playing" table behind `friendly_input()`, ported wholesale
from the reference's `PLAYBACK_MODE_MAP`) carried three names too wide for the display: rendered in
`Inter_Medium_20`, "TIDAL Connect" is 144px, "Spotify Connect" 156px and "External Storage" 158px.
`dial_ui.py`'s input label sits at y=62 on a 240px **circle**, where the gauge arc's inner radius
(90) leaves only **~122px** clear -- so those three drew under the arc band and were then clipped by
the display circle. Shortened in place to "TIDAL", "Spotify" and "Storage"; all 16 entries now fit
with margin (widest is "Voice Mail" at 99px). These are display-only strings -- nothing on the wire
uses them -- so shortening them costs nothing, which is why this was fixed at the source rather than
by adding truncation to `dial_ui.py`.

The general lesson, not WiiM-specific: **available width is a function of the row's distance from
the screen centre**, and it is a pixel budget, not a character count (Inter's advances run 5px for
"i" to 20px for "W", so "10 characters" is anywhere from 50px to 200px). `tools/font_fit.py`
measures any string against the real `.pcf` the device loads -- `--list` prints every label row's
budget, `--table src/wiim.py:_MODE_NAME` checks a whole table at once. Run it before adding or
renaming any backend's `friendly_input()` or preset names.

Still unbounded by construction, and not solvable this way: `denon.py`'s `_input_names` is populated
live from the AVR's own renamed sources, and HA source names come from the user's HA config. Any
name long enough will overflow the same way. `friendly_input()`'s `"Unknown"` fallback (91px) means
an *unrecognized* WiiM mode code is always safe, but a recognized-but-renamed Denon input is not.
If that ever bites, the fix is a width-aware fallback in `dial_ui.py` that every backend inherits,
not another per-backend table edit.

### Presets: config-driven count/names, not auto-discovered -- quick-select buttons deliberately skipped

First implementation tried the same runtime-discovery pattern as `minidsp.py`'s
`CAPS["preset_enable"]`: have `get_presets()` set `CAPS["presets"] = bool(preset_list)` from
`getPresetInfo`, since it returned an empty `preset_list` on this unit at the time (no Favorites
configured yet). **That assumption turned out to be wrong, not just untested** -- confirmed live
after the user configured 2 real Favorites via the WiiM app's Favorites screen and rebooted the
Dial: `getPresetInfo` still reported `{"preset_num": 0, "preset_list": []}`. This isn't a
boot-timing issue (`get_presets()` is only called once at boot, which was considered, but a fresh
reboot after configuring Favorites ruled that out) -- the plain HTTP API genuinely does not report
this feature's contents. Confirmed via WiiM's own forum (a thread titled "Recall Presets using the
Wiim API"): retrieving real preset names requires WiiM's UPnP/SOAP interface (`GetKeyMapping` via
a `PlayQueueSCPD.xml` service) -- a materially heavier protocol than the GET-and-parse-JSON calls
everything else in this project uses. `MCUKeyShortClick:<1-12>` (`set_preset()`) is confirmed to
still work for *activating* a preset regardless -- it's specifically *listing* them back that the
plain API can't do.

Given a straight choice between implementing UPnP/SOAP (real names, zero settings.toml upkeep, but
a new protocol/complexity class on a memory-constrained device, untested against this unit's
actual UPnP service layout) and falling back to config, the user chose config. `wiim.py` now
mirrors `minidsp.py`'s own established answer to an analogous "API can't report names" gap exactly:
`CAPS["presets"] = len(config.WIIM_PRESET_NAMES) > 0` (set once, at import time, not
runtime-discovered at all now), and `get_presets()` returns `("", [(str(i), name)])` over that
list -- same shape, same reasoning, different backend.

**`WIIM_PRESET_NAMES` is the preset set; its length is the count.** This originally had a separate
`WIIM_PRESET_COUNT` alongside it, which clamped the name list -- so naming 3 Favorites with a count
of 2 silently hid the third. Corrected 2026-08-06 (see "Quick-select buttons" below for where that
count actually belonged). A count can only ever agree with `len()` or contradict it, and there is
nothing it can express that the list doesn't, because for WiiM the names *are* the slots: a name's
position in the list is the Favorite number `MCUKeyShortClick:<n>` takes.

The user's own framing, when asked what capabilities to add, was to make WiiM's presets "work like
the HA device selection" -- meaning reachable only through a scrollable list menu, not a fixed
row of buttons. This surfaced a real latent bug rather than just a preference: `getStatusEx`
reports `"preset_key": "12"` (12 favorite slots) on this unit, confirmed live, and the existing
main-screen quick-select button row (`dial_ui.py`'s `_draw_preset_filter_buttons`) has only
`_DBTN_MAX = 5` pre-allocated label slots -- fine for Denon (2 presets)/MiniDSP (<=4), but for a
list that size the row's drawing would silently truncate to 5 while `preset_button_at`'s tap-rect
math (driven by the *full* list length, not the drawn count) would still lay out rects across all
12 -- a real draw/tap mismatch, not just a cosmetic one. Fixed with a new capability flag,
**`CAPS["preset_quickbuttons"]`** (default `True` in `driver.py`'s `_CAPS_DEFAULTS`, `False` in
`wiim.py`), gating both `dial_ui.py`'s button-drawing call and `app.py`'s quick-button tap
dispatch. The existing scrollable Preset submenu (`app.py`'s `_open_submenu`/`_confirm_sub`,
already generic and already exercised by `ha.py`'s equally-unbounded player list) needed zero
changes to handle a 12-entry list correctly -- this is the same "device-behavior difference is a
CAPS flag, not a driver-name check" principle as every capability flag before it (hard lesson #6
in the Denon->MiniDSP section above), just applied to *how many* of something exists rather than
*whether* it exists at all.

### Quick-select buttons: a configured subset, not all-or-nothing (corrected 2026-08-06)

`wiim.py` hardcoded `CAPS["preset_quickbuttons"] = False` on the reasoning above: 12 favorites
against a 5-slot row, so show none. **That reasoning was already overturned during the CamillaDSP
work and the back-port was missed.** CamillaDSP's rejected-design (2) -- recorded in
`camilladsp.py`'s docstring, and it names `wiim.py` explicitly -- was "always submenu-only,
matching wiim.py exactly", rejected by the user on the grounds that the row has room for a few
buttons and there's no reason to surrender that shortcut just because the *full* list can be long.
CamillaDSP got `CAMILLADSP_QUICK_PRESETS`; WiiM was left on the design that had just been rejected,
while its own docstring went on arguing for it. A row with fewer slots than the list is an argument
for showing a **subset**, not for showing none.

`WIIM_PRESET_BUTTONS` is the fix, and it's where the old `WIIM_PRESET_COUNT` belonged all along --
the count was originally meant as "how many buttons do you want", and got wired to the menu list
instead, which is how it became a clamp. It takes **names from `WIIM_PRESET_NAMES`**, in draw
order, so `"Night,Movie"` draws Night first. Validation lives in `config.py`
(`_parse_wiim_preset_buttons`): unknown and repeated names are dropped with a boot print rather
than failing the boot, and the survivors are resolved to 1-based positions there, so
`wiim.py`'s `get_quick_presets()` is a pure lookup with no validation of its own.

**Four buttons is the ceiling, not five.** This first shipped capped at 5, reading `dial_ui.py`'s
`_DBTN_MAX = 5` as a usable maximum. It isn't -- it's the pre-allocated label-slot count. Measured
2026-08-06: at the button row (y=156..178) the gauge arc's inner edge leaves 138px clear, 4 buttons
span 130px and fit, 5 span 166px and draw over the arc band. `camilladsp.py` already capped its own
quick list at 4 and called it "the practical limit" without recording why; that reason is the arc,
and it's now measured rather than folklore. Both caps live in `config.py`
(`_DBTN_BUTTON_CAP`, `_parse_camilladsp_quick_presets`); `_DBTN_MAX` stays 5 as the allocation and
clamp bound, which is what makes an over-long list truncate safely instead of crashing.

**Buttons and media controls are mutually exclusive, and media controls are the default.** WiiM is
the first backend to want both -- `ha.py` has media controls but no presets;
`denon`/`minidsp`/`camilladsp` have presets but no media row -- so nothing before this had to
choose. They cannot coexist: the button row occupies y=156..178 (`_DBTN_Y0` + `_DBTN_H`) and
`wiim_ui._icon_row_y()` puts the play/pause icon at 164..180, with the `"<<"`/`">>"` labels taller
still. The 18px left between the buttons and the MENU roof (y=196) cannot fit a 20px font row, so
there is no "move it down" that works -- and the tap zones would overlap regardless, with
`_dispatch_tap` checking media first and silently swallowing every button tap.

So `WIIM_PRESET_BUTTONS` selects between the two layouts: unset keeps play/pause and skip (the
behavior WiiM always had), and naming any preset trades them for a denon/minidsp-style button
row. `wiim_ui.py`'s `_media_row_active()` is the single predicate, consulted by both
`draw_status_rows()` and all three media tap handlers -- if those ever disagree, the drawn UI and
the tap map diverge, which is the same class of bug `CAPS["preset_quickbuttons"]` was introduced
to fix in the first place.

### Quick presets are configured by name on every backend that has them (settled 2026-08-06)

`WIIM_PRESET_BUTTONS` first shipped taking **positions**, deliberately, against
`CAMILLADSP_QUICK_PRESETS`'s names. The argument was that a WiiM position is not an arbitrary
index: `get_presets()` numbers slots 1..N, that number is exactly what `MCUKeyShortClick:<n>`
sends, and the same digits appear in the WiiM app -- so positions introduced no second numbering,
where names would. CamillaDSP presets, by contrast, are config-file paths whose order is
incidental, so a position there means nothing.

That reasoning is sound about the *implementations* and wrong about the *settings*. The maintainer
hit it immediately: the two settings sit in adjacent blocks of `settings.toml.template` and look
parallel, so any difference in how they're written reads as inconsistency, whatever justifies it
underneath. **Both are names now.**

The position argument didn't lose anything, because it was never about the config surface -- the
name is what the user writes, and `config.py` resolves it to the Favorite number at import, so the
protocol value is still a position by the time `wiim.py` sees it. Names additionally survive
reordering `WIIM_PRESET_NAMES`, and a rename fails *loudly* (the "not in WIIM_PRESET_NAMES" boot
print, then a fall back to media controls) where a position list would have silently pointed at
whatever preset moved into that slot.

One real limitation of matching by name: two Favorites sharing a name are indistinguishable, and
only the first is reachable as a button (`presets.index(name)`). Positions could address both.
That's an accepted trade -- duplicate Favorite names are a user-side problem, and the loud-failure
property is worth more than addressing an ambiguous list.

The general rule for a sixth backend: **quick-preset subsets are configured by name.** If a
backend's presets have real numbering worth exposing, expose it in the *full* list's config (as
`WIIM_PRESET_NAMES`'s order does), not in the quick-subset setting.

### Media controls needed their own minimal UI extension, not just `state.media_state`

Surfaced during implementation, not anticipated from the contract docs alone: `dial_ui.py`'s
`media_status_tap`/`media_prev_tap`/`media_next_tap` **unconditionally delegate to
`driver.ui_impl`** and return `False` if it's `None` (`dial_ui.py`'s "the actual hit-tests... all
live in ha_ui.py" comment, written when `ha.py` was the only backend that needed any of this) --
even though the underlying status *text* (`dial_ui._status_line()`) already renders generically
off `state.media_state` with no UI extension required. A backend can report `media_state` and
still get completely dead play/pause/skip taps if it doesn't pair a UI module, which would have
silently broken "basic media controls" for this backend despite `wiim.py` doing everything the
driver contract asks of it.

Fixed with `src/wiim_ui.py` -- deliberately not a copy of `ha_ui.py`, but the minimal slice: just
`draw_status_rows`/`media_status_tap`/`media_prev_tap`/`media_next_tap`, reusing
`dial_ui._status_line()`/`_MEDIA_STATE_TEXT`/`_MEDIA_SIDE_X`/`PRESET_NAME_Y`/`CX` rather than
duplicating them. No `standby_menu_tap`/`standby_menu_pos` -- those are only ever called behind an
`if CAPS["player_select"]` check in both `app.py` and `dial_ui.py`, and `wiim.py`'s is `False`, so
they're simply never reached. This is `driver.py`'s "UI-extension contract" working as documented
("a `<backend>_ui.py` module exposes whatever subset of this it needs") -- `wiim_ui.py` is now the
reference example for the minimal case, the way `ha_ui.py` is for the full one.

### Two more real-use papercuts, from actually using it after it first booted

Both surfaced from live use, not planning, same as HA's "follow-up UX papercuts" round:

**A preset name and play/pause status turned out to need to coexist, not take turns.**
`dial_ui._status_line()`'s "same slot, whichever's relevant" precedence (preset name if one
exists, else a play/pause word, else blank) was designed around every backend before this one
having exactly one of those concepts, never both at once -- Denon/MiniDSP have presets and no
playback tracking, HA has playback tracking (`CAPS["presets"]` always `False`) and no presets.
WiiM has both, genuinely simultaneously (a favorite can be selected while something is actively
playing), so sharing one text slot meant one of them was always getting hidden.

This surfaced in three rounds, confirmed live each time:
1. First attempt kept them sharing `PRESET_NAME_Y` (matching the existing precedent) and just
   moved the skip icons to the row below (`_PLAYER_NAME_Y`) to stop their fixed `+-_MEDIA_SIDE_X`
   offsets from visually colliding with a preset name's text. That fixed the collision but not the
   underlying problem: with playback active, "Pause" permanently occupied the slot and the preset
   name (or the "(Preset)" placeholder, see below) never got a chance to show at all -- confirmed
   live ("the word pause is still on the line... preset is absent").
2. Split them onto genuinely separate rows, same structure `ha_ui.py` already established
   (persistent "identity" info above, transient status below) -- just with preset name/placeholder
   in the upper role instead of a device name. `wiim_ui.py`'s `draw_status_rows()` now puts the
   preset name (or a literal `"(Preset)"` placeholder while `CAPS["presets"]` is true and nothing's
   been picked yet -- `wiim.get_presets()` always returns `current_value=""`, since
   `MCUKeyShortClick` is a one-shot action with no persistent "active" concept to report) on
   `PRESET_NAME_Y` unconditionally, and draws play/pause as an *icon* (`dial_ui.draw_play_pause_icon()`,
   the same helper `ha_ui.py` already uses, not text) on a second row below it, flanked by the skip
   icons. Both are always visible together now -- no precedence, no hiding. First pass reused
   `dial_ui._PLAYER_NAME_Y` for that second row (simplest option, already existed) and centered it
   between `PRESET_NAME_Y` and the bottom MENU hint -- confirmed live as visually too far down
   ("my eyes don't like the spacing"), and reusing `_PLAYER_NAME_Y` at all was the wrong call
   regardless, since that constant is shared with `ha_ui.py`'s device-name row -- moving it to
   reposition WiiM's icon row would have shifted HA's layout too.
3. Final position: `wiim_ui.py`'s own `_icon_row_y()`, independent of `_PLAYER_NAME_Y` entirely, set
   to match the *existing* vertical rhythm instead of centering in leftover space -- measured via
   `tools/dial_sim.py`'s font metrics (rendering a real frame off-device and inspecting actual label
   bounding boxes, not guessing) that the gap between the volume number's bottom edge and the preset
   row's top edge is 16px, then reapplied that same 16px gap below the preset row before centering
   the play/pause icon there: `PRESET_NAME_Y + 34` (10 for the preset text's own half-height since
   it's center-anchored, + the 16px gap, + 8 for `_ICON_HALF_H`). Confirmed by re-measuring the
   re-rendered frame that both gaps come out to exactly 16px.

That last fix exposed a real bug, not just a positioning one: `_icon_row_y()`'s formula (originally
written as a module-level constant, `_ICON_ROW_Y = ...`) read `dial_ui.PRESET_NAME_Y` and
`dial_ui._MENU_POS_MAIN` *at import time*. `wiim_ui.py` is reached via a circular import
(driver.py -> wiim_ui.py -> dial_ui.py -> driver.py), which is safe *only* if nothing reads
`dial_ui`'s attributes until it has fully finished executing -- true for real boot order (`app.py`
imports `driver` first, which finishes importing `dial_ui` as a side effect, before `app.py`'s own
`import dial_ui` line ever runs), but `tools/dial_sim.py` imports `dial_ui` directly, hitting the
same circular chain from the *other* direction (dial_ui -> driver -> wiim_ui -> dial_ui, mid-import)
-- and crashed with `AttributeError: partially initialized module 'dial_ui' has no attribute
'PRESET_NAME_Y'` the moment this was actually tested via the render tool. Fixed by making it a
function (`_icon_row_y()`), computed on every call instead of once at import -- by the time any
draw/tap function actually runs, `dial_ui.py` has long since finished executing regardless of which
direction the circular chain was entered from. This is the exact hazard already flagged in this
file's "Note for future backend work" below, and it's worth re-reading before adding any new
module-level code to a `<backend>_ui.py` file that touches `dial_ui`/`driver` attributes.

Tap handling followed the row split: `media_status_tap`/`media_prev_tap`/`media_next_tap` all moved
their hit-test band from `PRESET_NAME_Y` to `_icon_row_y()` to match where the icon actually lives,
and `app.py`'s `_tap_main_screen` gained a *separate* zone that opens the Preset submenu directly via
`_tap_open_preset_menu()` (reusing `_open_submenu("preset", ...)`, the same call the top-menu path
already makes) -- gated on `CAPS["presets"] and not CAPS["preset_quickbuttons"]`, so Denon/MiniDSP
(which have quick buttons) and HA (no presets at all) are unaffected. That zone needed no fixed
lower bound at all in the end: `_tap_main_screen` is only ever called for `touch_y < MENU_TAP_Y` in
the first place (`_dispatch_tap` routes anything at or below that to the bottom MENU-tap zone
instead), and the media-tap checks run first in the same function and already claim their own
region whenever `state.media_state` qualifies -- so the preset-menu tap just fires for whatever's
left, with no coordinate math needed to avoid overlapping a row it doesn't actually know the
position of. This also removed an earlier stopgap in `_tap_main_screen` (gating the whole media-tap
block on `not dial_ui._preset_name(state)`) that was only ever needed because the two rows used to
share one slot -- with them genuinely separate now, both tap zones are just independently,
unconditionally live.

**Note for future backend work:** `wiim_ui.py` does `import driver as _driver` directly (needed
for the `CAPS["presets"]` check in `_preset_slot_text()`) -- this creates driver.py -> wiim_ui.py ->
driver.py, a circular import. That's safe *only* because the access happens inside a function body
(`_preset_slot_text()`), never at module level -- by the time that function actually runs, `driver.py`
has long finished executing. `dial_ui.py` already relies on this exact same property for its own
`import driver as _driver` (reached via driver.py -> ha_ui.py -> dial_ui.py -> driver.py, or
-> wiim_ui.py -> dial_ui.py -> driver.py) -- confirmed safe on real hardware for `ha`, and now for
`wiim` too. A future `<backend>_ui.py` needing `driver.CAPS` can follow the same pattern; just
never read `_driver.<anything>` outside a function.

## "MENU home" frame -- generic chrome, all backends (added 2026-07-29)

`dial_ui.py`'s `_draw_menu_home()` draws a small roof-line + two flared legs (no bottom edge)
behind the MENU hint on the main screen, in the same barely-visible dimness as the MENU text
itself (`_TK_MENU`, a new palette index 12 added specifically to match `_C_MENU`'s exact RGB
value -- bitmap-drawing primitives like `_line()` take palette indices, not the raw hex label
colors elsewhere in this file, so a new backend/UI element that needs to match a label color
exactly needs its own palette entry, not just the same-looking hex constant). Purely decorative,
generic to every backend (drawn unconditionally in `draw_main()`'s ON-and-MODE_MAIN path, not
gated by any `CAPS` flag) -- the goal, in the user's words, was to make the MENU tap zone "feel
more like it had more of a home" without adding visual noise.

**Notable for *how* this got designed, not just what shipped:** the user explicitly asked for
"mockups and confirmations before any code changes," and the whole thing went through several
rejected concepts before landing here entirely via off-device mockups -- zero iterations were
tested on real hardware, unlike nearly everything else in this file:
- First concept (from the user's own sketch in `ui/UIDiagram.drawio` -- a gray ellipse overlaid
  such that only its top arc pokes into the round screen) was mocked up by extracting the *exact*
  ellipse geometry from the `.drawio` XML itself (`<mxCell style="ellipse" ...>`'s `x/y/width/height`,
  converted from diagram-space to real 240x240 device pixels via the scale factor between the
  embedded screenshot's placed size and its native resolution) rather than eyeballing pixel
  positions from the rendered PNG -- reading the source file gave exact numbers instead of a
  guess. Rendered via `tools/dial_sim.py` (imported as a module from a throwaway script, which
  also gets its shims installed as an import-time side effect -- reusable pattern for any future
  one-off mockup). Confirmed by the user as the right size/position, but rejected outright once
  seen live: **"doesn't do it."**
- Pivoted to a trapezoid/"home" shape on the user's suggestion. Several variants (open vs. closed,
  which edge flared which way) were mocked up and compared before landing on: a top edge near the
  preset-button row + two legs flaring outward and down toward the rim, with **no bottom edge** --
  a fully closed trapezoid was tried too and read as "boxing in" rather than "grounding."
- The color was also wrong on the first pass (matched the preset-button frame's brighter tick-mark
  gray, since that's what "same thickness as the frames around the numbers" specified) -- the user
  asked for it dimmed to match the MENU text's own near-invisible brightness instead, confirmed via
  a zoomed-in crop of the re-rendered mockup before writing any real code.

Geometry (`_MENU_HOME_TOP_Y`/`_BOTTOM_Y`/`_TOP_HW`/`_LEG_HW`) is hardcoded to the values confirmed
in the final mockup, not derived from other layout constants -- same style as this file's other
hand-tuned pixel constants (`_DBTN_Y0`, `PRESET_NAME_Y`, etc.).

## Backend #5: CamillaDSP (added 2026-07-30)

`src/camilladsp.py`, `DEVICE_DRIVER = "camilladsp"` -- controls a
[CamillaDSP](https://github.com/HEnquist/camilladsp) process (volume, mute, config-file presets).
Written with zero live-hardware access, then verified end-to-end the same day: `make
probe-camilladsp` confirmed every command/reply shape against a real `camilladsp` 4.1.3 process,
and the backend booted and ran correctly on a real M5 Dial against that same process (volume, mute,
and touch all functioning, audible on the test Mac). This is the reference example for how this
project develops a backend without hardware in hand first -- read the "Live testing" subsection
below before assuming the same shortcut is safe for a future backend; it worked here because every
design choice made blind was flagged explicitly and then actually checked, not because skipping
live testing is generally fine (hard lesson #8 in the Denon->MiniDSP section still holds).

### Why this backend needed its own from-scratch WebSocket client

Unlike MiniDSP (a hardware DSP driven via a host daemon, minidsp-rs, that exposes both a WebSocket
*and* a plain HTTP API), CamillaDSP is itself a software DSP process, and its control API is
**WebSocket-only** -- confirmed via its own docs and by reading `local/pycamilladsp` (the vendor's
official Python client, downloaded but gitignored, not vendored into this repo). There is no HTTP
fallback the way minidsp-rs has one, which is the same situation that ruled out two earlier
WebSocket-based designs for the HA backend (see "Backend #3: HA media_player" above) --
CircuitPython has no usable native WebSocket client for this board; the community libraries are
work-in-progress and target Airlift co-processor boards, not the M5 Dial's plain `wifi`/
`socketpool` stack. Given that, and that deloop's whole architecture is already poll-per-call with
no persistent connections anywhere (`_poll_avr` in `app.py`), `camilladsp.py` implements a minimal
hand-rolled WebSocket client scoped to exactly what a one-shot request/reply call needs: one fresh
TCP connection + WS handshake per call (or small batch of calls), no keepalive, no reconnect-on-
drop, and Ping frames read but never replied to with a Pong (every connection here closes within
milliseconds of opening, so there's not expected to be a window where the server needs one
answered -- unverified beyond the short live test session, see "Still open" below). Full protocol
details and every scope-reduction live in `src/camilladsp.py`'s own module docstring, not
duplicated here.

Command/reply shapes (`GetVolume`/`SetVolume`, `GetMute`/`SetMute`, `GetConfigFilePath`/
`SetConfigFilePath`+`Reload`, and the `{command: {"result": ..., "value": ...}}` envelope) were
read directly out of `local/pycamilladsp/tests/test_camillaws.py` -- the vendor's own test
fixtures, a materially stronger source than a summarized doc (hard lesson #3) -- and have since
been reconfirmed byte-for-byte live via `tools/probe_camilladsp.py` against a real instance.

### Current design decisions (and the "why" worth keeping)

- **Presets are a flat name/path list, not a numbered-slot scheme.** MiniDSP/WiiM presets use
  `PRESET_COUNT` + optional `PRESET_NAMES` indexed by position because neither API can enumerate or
  name its own slots. CamillaDSP presets are just config file paths on the host running CamillaDSP
  -- there's no slot count concept at all -- so `config.CAMILLADSP_PRESETS` is a flat
  `"Name:/path,Name2:/path2"` list parsed into `(path, name)` pairs directly (hard lesson #6: a
  device-behavior difference should drive a design difference, not laziness).
- **The current preset value is a real live query (`GetConfigFilePath`), not a placeholder** --
  unlike MiniDSP's Dirac-enable state or WiiM's "which favorite is active" gap, CamillaDSP actually
  reports which config is loaded. Confirmed live: `GetConfigFilePath` echoes back exactly the
  string last passed to `SetConfigFilePath`, including a full absolute path -- so `get_presets()`'s
  current-value comparison against `CAMILLADSP_PRESETS` correctly highlights the active preset once
  the Dial has made at least one `SetConfigFilePath` call. At boot, before any switch, it reflects
  whatever path the process was originally launched with -- if that doesn't string-match a
  `CAMILLADSP_PRESETS` entry, the Preset menu just opens with nothing pre-highlighted, the same
  graceful "no match" behavior `get_presets()` was already designed to tolerate.
- **Main-screen quick-select buttons are a separately configured subset, not the full list and not
  nothing.** `CAMILLADSP_QUICK_PRESETS` (config.py) names up to 4 entries from `CAMILLADSP_PRESETS`
  to get buttons; the full list (which can be arbitrarily long -- CamillaDSP presets are just named
  config files, no real ceiling) stays reachable only through the scrollable Preset submenu
  regardless. This landed after two rejected shapes, both instructive: showing buttons for the
  whole list (wrong -- unlike MiniDSP/Denon's small, physically-fixed slot counts, a long
  `CAMILLADSP_PRESETS` would hit the same button-row/tap-rect overflow `wiim.py` avoids by going
  submenu-only) and no quick buttons at all, matching `wiim.py` exactly (rejected by the user: the
  row has room for a few buttons, no reason to give up the shortcut just because the *full* list can
  be long). `driver.py` gained a `get_quick_presets()` contract entry for this -- defaults to
  reusing `get_presets()`'s own list when a backend doesn't define it (exactly correct for
  `denon.py`/`minidsp.py`, whose full lists already fit the row), so only `camilladsp.py` needed to
  actually implement it. `state.preset_quick_names` (new field, `state.py`) is what `dial_ui.py`'s
  button row and `app.py`'s quick-button tap handler read; the scrollable submenu still reads
  `state.preset_names`, unaffected.
- **`VOLUME_MIN`/`VOLUME_MAX` default to -50/0 dB**, not the full attenuation-only portion of
  CamillaDSP's documented -150..+50 `SetVolume` range -- confirmed by cloning and reading
  `HEnquist/camillagui-backend` (the official companion web GUI, same author as CamillaDSP), whose
  own default config hardcodes `volume_range: 50` / `volume_max: 0`. Also confirmed (from
  `camilladsp`'s own `src/utils/decibels.rs`) that CamillaDSP and MiniDSP use the identical
  `20*log10(amplitude)` dB convention, so -50dB is mathematically the same digital attenuation on
  both -- not necessarily the same perceived loudness, since that also depends on each system's
  downstream DAC/amp analog gain, which neither DSP's own volume number encodes.
- **`CAMILLADSP_PRESET_TIMEOUT_MS`** stays at minidsp.py's own generous headroom (10s) as a
  starting point. Live-measured at ~1-3ms against a trivial `SignalGenerator`-based test config (see
  "Live testing" below) -- a lower bound, not a general answer, since a real room-correction config
  with large filter files to load could be much slower.
- **`get_status()`'s "input" field is repurposed for DSP processing status**, not a real input --
  CamillaDSP has no input/source concept at all (`CAPS["input_select"]` stays False), so
  `dial_ui.py`'s top status label was otherwise permanently blank for this backend. Added on the
  user's request rather than left unused: the processing rate (e.g. `"96khz"`) while `GetState` is
  `"Running"`, or the raw `GetState` word (`Paused`/`Inactive`/`Starting`/`Stalled`) otherwise --
  CamillaDSP's own vocabulary, not reworded, since anyone running CamillaDSP already knows what
  those mean. Sample rate (`GetCaptureRate`) and processing state (`GetState`) are polled every
  time, batched into the same `get_status()` WS connection as `GetVolume`/`GetMute` (four commands,
  one connection). First version combined channel count into the same string
  (`"<channels>ch - <rate>khz"`) -- confirmed live on real hardware to be visibly too wide, so
  channel count was pulled back out into its own thing (see next bullet) and the display is rate-only.
- **Channel count is a separate, generic contract key now, not folded into any one backend's display
  string.** `get_status()`'s optional `"channels"` key (`driver.py`, same no-CAPS-flag shape as
  `"media_state"`) is a `state.channels` field with no UI consumer yet -- the user wants it
  displayed somewhere else, not decided as of this writing. `camilladsp.py`'s own
  `_fetch_channels()`/`_channels` cache (from `GetConfigJson`'s `devices.capture.channels` -- the
  only way to get it, since no dedicated command reports it and `GetChannelLabels` is unreliable,
  labels being optional in the config) was kept exactly as built, just wired into this new key
  instead of the status string. Fetched once and cached, not every poll, since it's static per
  loaded config; `set_preset()` invalidates the cache since a different config can have a different
  channel count.
- **Checked whether the other four backends could fill in "channels" too -- none currently can**,
  each for a different confirmed reason (see `driver.py`'s contract comment for the full writeup,
  not repeated here): `denon.py` has no known API command for it, and this is unresolved even in
  the wider HA/Denon integration community, not just unchecked by this project; `minidsp.py`'s
  HTTP API never serializes channel count at all (confirmed from minidsp-rs's own
  `daemon/src/http/mod.rs`), even though the hardware's fixed channel definitions do exist as
  compile-time static data per hw_id; `ha.py` genuinely has no such attribute in HA's own
  `media_player` entity schema (confirmed by reading `homeassistant/components/media_player/
  __init__.py` directly -- `media_channel`, the one lookalike name, is a broadcast/TV channel, not
  an audio channel count); `wiim.py`'s `getMetaInfo` has documented `sampleRate`/`bitDepth`/
  `bitRate` fields (community API docs, unverified live) -- genuinely relevant "audiophile info,"
  just not a channels field, and a different feature scope than what was actually asked for here.
  Fully unit-tested host-side (state-word fallback for every non-Running state, missing-rate
  fallback, the lazy-fetch-then-cache behavior for channels, cache invalidation on preset switch)
  but not yet confirmed on real hardware -- add to "Still open" below once it has been.

### Live testing (2026-07-30)

Host-side, against a real `camilladsp` 4.1.3 (arm64, macOS) binary, before touching the Dial: three
throwaway test configs in `local/camilladsp/` (gitignored) -- `flat.yml`/`quiet.yml`/`muffled.yml`,
all `SignalGenerator` (synthetic 1kHz sine, no real mic/input needed) into `CoreAudio` default
output, so testing needed no microphone permission prompt and no real audio hardware beyond the
Mac's own speakers. All three passed `camilladsp --check`; `flat.yml` produced real audible output.
`make probe-camilladsp` (against both `127.0.0.1` and the Mac's real LAN IP, matching what the Dial
actually uses) confirmed every reply envelope byte-for-byte against `camilladsp.py`'s
`_parse_reply`.

On the Dial itself: `DEVICE_DRIVER = "camilladsp"` against that same Mac-hosted process, confirmed
live -- encoder volume changes update both the display and (audibly) the actual output; touch
mute/menu controls work. First successful end-to-end run of the hand-rolled WebSocket client on
real CircuitPython hardware against a real server -- the one thing no amount of host-side testing
could confirm.

Regenerating `ui/*.png` reference screenshots for this backend surfaced two real process bugs
(a stale `tools/dial_sim.py` fixture, and a config-derived `CAPS` flag a fixture's `state.*` alone
couldn't fake) that motivated turning that into a permanent tool -- see "ui/ screenshot
regeneration" right below for the full writeup.

### Still open

- Whether CamillaDSP's server tolerates never receiving a Pong in reply to a Ping, under conditions
  the short live test session didn't exercise (e.g. a connection that happens to stay open longer).
- `CAMILLADSP_PRESET_TIMEOUT_MS` against a real production config with actual FIR/convolution
  filter files to load, not just the trivial `SignalGenerator`-based test configs.
- ~~The DSP-status display is unit-tested host-side only, not yet confirmed live~~ -- **resolved.**
  Deployed, and the Dial showed a permanent "Starting" with no channel/rate info -- looked like a
  bug, wasn't one. Root-caused by reading CamillaDSP 4.1.3's own source directly
  (`src/generatordevice.rs`): the `SignalGenerator` capture backend `local/camilladsp/*.yml`'s test
  configs use (chosen specifically to avoid a real audio device / mic-permission prompt) never
  writes `ProcessingState::Running` at all -- confirmed by grep, every *other* capture backend
  (CoreAudio/Alsa/Wasapi/Asio/Pulse/PipeWire/File) does. `GetCaptureRate` similarly always reports
  `0` for the same reason (no real clock to measure on a synthetic source). Confirmed the fix
  isn't needed by testing a real backend instead: swapped to `RawFile` capture reading a generated
  test tone (`local/camilladsp/rawfile_verify.yml` + `test_tone.raw`, gitignored) -- `GetState`
  reached `"Running"` within ~1s and `GetCaptureRate` reported real fluctuating numbers around the
  nominal rate, and `GetConfigJson`'s `devices.capture.channels` read back correctly too. So
  `camilladsp.py`'s code is confirmed correct end-to-end; `local/camilladsp/flat.yml` et al will
  permanently show "Starting" and that's expected, not a regression -- see
  `local/camilladsp/README.md`'s "Known quirk" section. Worth remembering generally: a synthetic/
  mock capture/data source chosen for testing convenience can fail to exercise a real backend's
  full behavior in ways that only show up as "looks like a bug" until traced to source -- the
  fastest way through was reading the vendor's actual Rust source, not guessing from the symptom.
- The *rate-only* status string and the new generic `"channels"`/`state.channels` contract key
  (both post-date the "Starting" investigation above) haven't been redeployed/reconfirmed on the
  Dial yet -- the width fix and the contract generalization are host-side-verified only.

## ui/ screenshot regeneration -- `tools/render_ui_screenshots.py` / `make ui-renders` (added 2026-07-30)

`ui/ui-denon.png`, `ui-minidsp.png`, `ui-wiim.png`, `ui-camilladsp.png`, `ui-muted.png`,
`ui-standby.png` -- the polished, per-backend screenshots README.md actually embeds -- are real
renders of `dial_ui.py`'s drawing code, same "not a mockup" claim `tools/dial_sim.py` already made
for its own `local/renders/` scenario set, but with one real difference: `dial_sim.py`'s
`_base_state()` fixture is Denon-shaped and stays that way no matter which `DEVICE_DRIVER` is
active, which produces real nonsense on another backend (confirmed live: `minidsp.py`'s
`friendly_input()` mangling the raw Denon-style input "SAT/CBL" into "Sat/cbl"; `wiim.py` showing
"Unknown" input and a blank `"--"` volume because -20.5 isn't valid on its 0-100 range).
`render_ui_screenshots.py` is the fix: one `_fixture_<backend>()` function per backend, each with
state realistic for that backend specifically (real source/input names, a volume level sane for
its own `VOLUME_MIN`/`VOLUME_MAX`, real preset shapes).

**One process renders exactly one backend, always -- there is no way around this.**
`config.py`/`driver.py`/`dial_ui.py` all bind to `DEVICE_DRIVER` (and any `VOLUME_MIN`/`VOLUME_MAX`
override) at first import, and Python caches imported modules -- so looping over backends by
mutating `os.environ` and re-importing inside one process silently does nothing (the cached module
stays bound to whichever backend was active on its *first* import). The script takes a required
`--backend` flag, cross-checked against the actual `DEVICE_DRIVER` env var as a safety net, and
`make ui-renders` in the Makefile is what actually loops -- one full `$(PYTHON)
tools/render_ui_screenshots.py --backend X` subprocess per backend, each with its own env line.

**Real bug caught building this, twice, both worth remembering for the next backend/field added:**
1. `dial_sim.py`'s `_base_state()` had never been updated when `state.preset_quick_names` was
   added earlier the same session -- every render using it (i.e. every backend) silently showed an
   empty quick-button row. This is exactly the "update `tools/dial_sim.py`'s fixture state" step
   the "Recipe for adding a backend" section calls out, missed because a new `state.*` field
   doesn't only affect the backend that motivated adding it. Fixed in `_base_state()` by setting
   `state.preset_quick_names = list(state.preset_names)`, matching what `denon`/`minidsp` actually
   get from `driver.get_quick_presets()`'s default-reuse fallback on real hardware.
2. `make ui-renders`'s first version rendered `camilladsp` with no `CAMILLADSP_PRESETS`/
   `CAMILLADSP_QUICK_PRESETS` env vars set -- the resulting screenshot looked complete but had no
   quick-select buttons at all, caught by the user on sight. Root cause:
   `camilladsp.py`'s `CAPS["preset_quickbuttons"]` is derived from
   `len(config.CAMILLADSP_QUICK_PRESETS)` *at import time*, so it came out `False` regardless of
   `render_ui_screenshots.py`'s fixture explicitly setting `state.preset_quick_names` to a non-empty
   list -- CAPS gates whether `dial_ui.py` draws the row at all, and nothing in `state` can override
   that. Fixed by passing representative `CAMILLADSP_PRESETS`/`CAMILLADSP_QUICK_PRESETS` values in
   the Makefile line itself, with a comment on that target explaining why they're not optional.
   General lesson: a fixture that only sets `state.*` fields is not sufficient for any backend whose
   `CAPS` is itself config-derived -- the environment has to actually reflect a real config too.

**Realism review, same session:** the user caught two more things on sight that weren't bugs, just
unrepresentative choices. WiiM's fixture originally left its preset config (then
`WIIM_PRESET_COUNT`/`WIIM_PRESET_NAMES`, now just `WIIM_PRESET_NAMES`) unset, which produced a real (not broken) but visually sparse screenshot -- an empty gap where
`wiim_ui.py`'s `"(Set Preset)"` placeholder normally sits, since that placeholder only renders when
`CAPS["presets"]` is true. Fixed by setting representative values in the Makefile's `wiim` line,
matching the shape of the user's real `settings.toml.wiim`. Separately, the fixture's input was
raw mode `"10"`, which `wiim.py`'s `_MODE_NAME` table really does map to the literal string "WiiM"
(confirmed live, not a bug) -- but that reads oddly as a "current input" in a screenshot, so it was
swapped to mode `"1"` ("AirPlay", equally confirmed live) purely for recognizability. Neither
required a code change, both are in `_fixture_wiim()`'s comments -- worth remembering that "is it
technically accurate" and "is it a good illustration" are different bars for this particular tool.

**Silent rot, found 2026-08-06:** every screenshot this tool produced between 2026-08-05 and
2026-08-06 was the "lost connection / reconnecting..." screen, for all five backends. `25e601c`
added `state.power_known` (`draw_main()` refuses to render device state it hasn't heard from the
device), and `dial_sim._base_state()` sets it -- but `render_ui_screenshots.py`'s `_fixture_*`
functions build `AVRState()` directly and bypass that helper entirely, so none of them had it. The
committed PNGs were fine only because nobody re-ran `make ui-renders` in that window. Two things
made it invisible: the failure mode is a *plausible screen* rather than a crash or a blank, and
nothing runs this tool automatically. `power_known` is now set once in `main()` rather than
per-fixture, so a new fixture can't reintroduce it -- but the general hazard stands for any state
field `draw_main()` gates on: **the fixtures duplicate `_base_state()`'s setup instead of building
on it**, and the next such field will break them the same way.

## Gauge arc overshoot at min/max volume -- all backends (found + fixed 2026-07-30)

User-reported: spinning the encoder to max volume made the red end of the gauge arc visibly creep
further around the ring than it should, by roughly 5%. Real bug, `dial_ui.py`, not backend-specific
-- affects every backend, just most visible on ranges where red/green sit right at an extreme
(`camilladsp`'s -50..0, where max volume = the very end of the sweep, made it easy to notice).

**Why single-frame renders couldn't catch this.** `tools/dial_sim.py` and `render_ui_screenshots.py`
always call `dial_ui.draw_main()` -- a full, correctly-bounded re-render (`_render_gauge()`'s
`_arc_solid(bmp, _ARC_START, _ARC_START+_ARC_SWEEP, ...)`, exact bounds every time). The real device
never does that while the encoder is actively spinning -- `app.py`'s `_handle_encoder_rotation()`
calls `dial_ui.draw_volume()` per tick instead, the fast incremental path that erases the *old*
pointer position and draws the new one (`_restore_region()` + `_draw_pointer()`) without a full
redraw. A bug living only in that incremental path is invisible to every render tool in this
project, which only ever exercises the full-redraw path. First actually confirmed by simulating the
real sequence off-device -- one `draw_main()` to establish a starting pointer position, then many
`draw_volume()` calls in a loop stepping toward max, same as a live encoder spin -- and diffing the
result against a clean `draw_main()` at that same final volume. Before the fix: real, non-trivial
pixel differences (not antialiasing noise -- full-strength `(0,153,64)` green / `(170,24,0)` red at
the arc's two tips). After the fix: pixel-identical, both directions (tested spinning to max and to
min).

**Root cause:** `_restore_region(bmp, angle, muted)` (the erase-old-pointer step) computes its
redraw window as `angle ± (_PTR_HALF + 2)` with no bounds-check against the gauge's actual sweep
(`_ARC_START` to `_ARC_START + _ARC_SWEEP`). When the pointer sits near either end of the range
(near min or max volume), that window extends past the true arc boundary. Two things then combine
to make the overshoot visible and *persistent*: `_arc_solid()` is a generic scanline-fill primitive
with no bounds-check of its own -- it paints exactly the range it's told to, including past the
gauge's real limits -- and `_arc_color(angle)` still returns a real color (green or red, whichever
extreme) for an angle outside the intended range, rather than "no color," because its `frac`
calculation isn't clamped either. So every incremental tick near an extreme repainted a few extra
colored pixels just past the true tip, and because the fast path never does a full bounded redraw
mid-spin, nothing ever corrected it back -- each tick could only make the overshoot the same or
worse, never better, until the next full `draw_main()` (menu open, poll, etc.) reset it.

**Fix:** clamp `_restore_region()`'s window to `[_ARC_START, _ARC_START + _ARC_SWEEP]` before
calling `_arc_solid()`. Three lines. Confirmed by rerunning the same off-device spin-simulation
diff both directions (toward max: red/right tip; toward min: green/left tip, the same bug mirrored,
less noticed but equally real) -- both pixel-identical to a clean render afterward.

**General lesson:** this project's rendering tools (`dial_sim.py`, `render_ui_screenshots.py`) are
excellent for "does a given static state render correctly" but structurally cannot catch a bug that
only lives in a *sequence* of incremental draw calls, because they only ever call the full-redraw
path. If a future bug report describes something that changes *during* interaction rather than in
one static frame (a gauge, an animation, anything with an incremental/fast-path redraw), simulate
the actual call sequence off-device (a loop of the real fast-path function calls) and diff against
a clean full render, the same way this one was actually confirmed -- don't rely on single-frame
renders to rule it in or out.
