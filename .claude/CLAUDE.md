# deloop Project Context

Last updated: 2026-07-29 (v2.2 -- four backends shipping (Denon, MiniDSP, HA media_player, WiiM/
LinkPlay); `code.py` is now a thin entry point importing `app.py`; the device runs precompiled
`.mpy` via `make deploy-mpy`, not plain `.py`)

This file captures the current project discussion so a future agent session can build a full project plan without needing the whole conversation repeated. This file itself lives at `.claude/CLAUDE.md` -- if you see an older reference to `local/agent/project-context.md` anywhere (in code comments, old commit messages, etc.), that's this same file before it moved.

**If you're adding a fifth device backend, skip to [Device Driver Architecture](#device-driver-architecture-v20) below** -- that section is the recipe (four backends already follow it: Denon, MiniDSP, HA, WiiM). Everything before it is v1.0 history (Denon-only POC) kept for hardware/protocol reference.

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
- Deployment via USB mass storage, but **not as plain `.py` day to day**: `make deploy-mpy`
  precompiles every module except `code.py` to `.mpy` first (`code.py` itself must stay
  uncompiled source -- it's the boot entry point) and is what the device should actually run; see
  "CircuitPython heap/boot-memory guardrails" below for why. Plain `make deploy` (no `.mpy`,
  `cp src/* /Volumes/CIRCUITPY/`-equivalent) still exists for fast dev iteration and doesn't
  require `local/mpy-cross` to be present, but isn't the shipped configuration.

### Why the pivot from UiFlow2

UiFlow2 firmware in WiFi mode does not expose a standard MicroPython REPL over USB serial. The serial port is a log-only output channel; `mpremote` cannot enter raw REPL mode. Switching to USB mode was not possible without reflashing. CircuitPython mounts the device as a USB drive (`CIRCUITPY`), making `make deploy` a simple file copy — exactly the workflow the project requires.

### How to flash CircuitPython

1. Unplug USB-C from the device.
2. Hold the **G0 button** on the M5StampS3 module (accessible through the housing).
3. Plug USB-C back in while holding G0, then release — screen stays blank.
4. Flash with esptool or the Adafruit WebSerial ESPTool:
   ```sh
   ./.venv/bin/pip install esptool
   ./.venv/bin/esptool.py --chip esp32s3 --port /dev/cu.usbmodem* \
     --before usb_reset write_flash -z 0x0 \
     adafruit-circuitpython-m5stack_dial-en_US-10.2.1.bin
   ```
5. Device reboots and mounts as `CIRCUITPY`.

## Why This Stack

UiFlow2 MicroPython appears to offer the best compromise between Python maintainability and M5 Dial hardware support:

- It has M5Stack-specific MicroPython APIs for the Dial.
- The docs show direct Python usage with `import M5`, `from M5 import *`, and `from hardware import *`.
- It includes Dial-specific rotary support via `Rotary()`.
- It supports display/widgets, buttons, touch, networking, and other M5 hardware features.
- It can run programs from the device filesystem, including `main.py`.
- It should allow the repo to remain normal Python if file upload works from standard MicroPython tooling.

Arduino/PlatformIO likely has the cleanest one-command firmware upload, but it is not a good maintainability fit for this user. CircuitPython likely has the cleanest file-copy workflow, but UiFlow2 may provide better M5Stack Dial hardware/UI APIs.

## Known UiFlow2 Details From Research

Relevant docs and findings:

- M5 Dial UiFlow2 firmware is installed with M5Burner.
- M5Burner supports a boot option to run `main.py` directly.
- UiFlow2 supports USB and Wi-Fi connection modes.
- UiFlow2's web IDE has Python mode, project files, `Run Once`, and `Run Always`.
- `Run Once` runs a temporary/test program.
- `Run Always` downloads the program to the device.
- UiFlow2 project import/export uses `.m5f2`, with version compatibility caveats.
- UiFlow2 file manager supports browsing device files and uploading/downloading files, but the docs describe this through the IDE rather than a clean CLI.
- UiFlow2 MicroPython firmware source is public at `m5stack/uiflow-micropython`.
- The firmware has releases and a documented build flow, but normal users should not need to build firmware for this project.
- M5Burner can export firmware `.bin` files, but the first-pass plan can rely on M5Burner directly.

## UiFlow2 MicroPython API Notes

The docs show code shaped like this:

```python
import M5
from M5 import *
from hardware import *

def setup():
    M5.begin()
    Widgets.fillScreen(0x222222)
    rotary = Rotary()

def loop():
    M5.update()
```

Useful Dial-related APIs found in docs:

- `M5.begin()`
- `M5.update()`
- `Widgets.fillScreen(...)`
- `Widgets.Label(...)`
- `BtnA.setCallback(...)`
- `Rotary()`
- `rotary.get_rotary_status()`
- `rotary.get_rotary_value()`
- `rotary.get_rotary_increments()`
- `rotary.reset_rotary_value()`
- `rotary.set_rotary_value(...)`

The API docs also list networking and software libraries such as WLAN, requests-style HTTP helpers, sockets, MQTT, display, touch, speaker, button, and rotary support.

## Deployment Goal

Day-to-day development:

```sh
make deploy
```

Underlying commands: see the Makefile's `_copy-files` target (plain `.py`, what `make deploy`
runs) directly rather than duplicating the list here -- it grows with every new source file and a
hand-copied list here would just drift. `make deploy-mpy` (`_copy-files-mpy` target) is what the
device should actually run; see "CircuitPython heap/boot-memory guardrails" below.

CircuitPython auto-reloads when files change on the drive. No reset command needed.

The entry point is `code.py` (CircuitPython convention), not `main.py`.

The serial REPL is still available over USB for debugging:

```sh
make shell   # opens mpremote REPL
```

## CircuitPython Hardware Reference

Board ID: `m5stack_dial`. Confirmed working with CircuitPython 10.2.1.

### Pin Names (from `pins.c` in the CircuitPython repo)

| Name | GPIO | Notes |
|---|---|---|
| `board.ENC_A` | 41 | Rotary encoder channel A |
| `board.ENC_B` | 40 | Rotary encoder channel B |
| `board.KNOB_BUTTON` | 42 | Encoder shaft press button |
| `board.BUTTON` / `board.BOOT0` | 0 | G0 boot button (also used to enter download mode) |
| `board.TOUCH_IRQ` | 14 | Touch controller interrupt |
| `board.SDA` / `board.SCL` | 11 / 12 | I2C bus (touch FT3267, RTC BM8563) |
| `board.PORTA_SDA` / `board.PORTA_SCL` | 13 / 15 | Port A I2C expansion |
| `board.PORTB_IN` / `board.PORTB_OUT` | 1 / 2 | Port B GPIO/ADC |
| `board.SPEAKER` | 3 | Buzzer/speaker |
| `board.NEOPIXEL` | 21 | RGB LED |
| `board.POWER_HOLD` | 46 | Hold high to stay on when battery-powered |
| `board.DISPLAY` | — | Pre-configured GC9A01 display (240×240) |
| `board.RFID_IRQ` | 10 | RFID module interrupt |

### CircuitPython Module Usage

```python
import board
import displayio
import rotaryio
import digitalio

# Display (pre-configured, no driver setup needed)
display = board.DISPLAY  # 240x240 GC9A01

# Rotary encoder
encoder = rotaryio.IncrementalEncoder(board.ENC_A, board.ENC_B)

# Knob press button
btn = digitalio.DigitalInOut(board.KNOB_BUTTON)
btn.direction = digitalio.Direction.INPUT
btn.pull = digitalio.Pull.UP
# btn.value is True when not pressed (active low)
```

### Libraries Needed (from CircuitPython bundle)

| Library | Purpose | Install via |
|---|---|---|
| `adafruit_display_text` | Text labels on screen | `circup install adafruit_display_text` |
| `adafruit_bitmap_font` | PCF/BDF font loading | `circup install adafruit_bitmap_font` |
| `adafruit_requests` | HTTP client (WiFi) | `circup install adafruit_requests` |
| `adafruit_focaltouch` | FT3267 touchscreen | `circup install adafruit_focaltouch` |

All installed via `make install-libs` (runs all four circup commands).

WiFi and sockets use built-in CircuitPython modules: `wifi`, `socketpool`.
`microcontroller.nvm` (built-in) is used for brightness persistence.

### Touch Controller Notes

- `Adafruit_FocalTouch(board.I2C(), address=0x38)` -- use `board.I2C()` singleton
- **Never** use `busio.I2C(board.SCL, board.SDA)` -- causes "pins already in use" errors
- If init fails with "No I2C device": power-cycle the device (stale I2C lock from REPL session)

### Font

`src/fonts/FreeMonoBold_36.pcf` -- extracted from the official Adafruit circuitpython-fonts
release bundle. Monospaced so volume digits never shift width. Deployed to
`/CIRCUITPY/fonts/FreeMonoBold_36.pcf` by `make deploy`.

## Hardware Bring-Up Notes

The M5 Dial arrived on 2026-07-23 and initially presented as `/dev/cu.usbmodem211101`, an Espressif USB JTAG/serial debug unit. `mpremote connect list` could see the device through the repo-local `.venv`:

```text
/dev/cu.usbmodem211101 AC:A7:04:01:70:B0 303a:1001 Espressif USB JTAG/serial debug unit
```

Running `mpremote connect /dev/cu.usbmodem211101 fs ls` against the brand-new device failed with `TransportError: could not enter raw repl`. This is expected because the device was still running the factory demo firmware that shows how the dial works, not UiFlow2 MicroPython firmware. Do not treat this as evidence that UiFlow2 lacks `mpremote` support. The real `mpremote` test should be repeated after flashing UiFlow2 firmware with M5Burner.

Use the repo-local environment explicitly when testing:

```sh
./.venv/bin/python -m mpremote connect list
./.venv/bin/python -m mpremote connect /dev/cu.usbmodem211101 fs ls
```

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
    settings.toml.template
  tools/
    probe_denon.py     # host-side Denon HTTP API probe
    probe_minidsp.py   # host-side minidsp-rs HTTP API probe
    probe_ha.py        # host-side HA REST API probe
    probe_wiim.py      # host-side WiiM/LinkPlay HTTPS API probe
    dump_denonavr.py
    dial_sim.py        # renders dial_ui.py off-device to PNGs (make renders)
    make_splash.py, gen_click.py
  Makefile
```

See [Device Driver Architecture](#device-driver-architecture-v20) below for what `driver.py`/`denon.py`/`minidsp.py`/`ha.py`/`wiim.py` actually look like and how to add a fifth backend, and "CircuitPython heap/boot-memory guardrails" for why `code.py`/`app.py` are split the way they are.

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

## POC Status (v1.0 -- COMPLETE as of 2026-07-24)

*Historical -- this was "done" before the driver split and MiniDSP support existed. See
[Device Driver Architecture](#device-driver-architecture-v20) and [Current Recommendation](#current-recommendation)
at the bottom of this file for the actual current status.*

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

## Configuration (settings.toml)

`src/settings.toml` (gitignored). Template: `src/settings.toml.template` -- treat that file as the
source of truth for the current key list; it's been through several rounds of change since this
table was first written (the `SPEAKER_PRESET_1/2` keys below, for instance, no longer exist --
speaker/Dirac presets were unified into the generic `preset`/`preset_names` model described in
[Device Driver Architecture](#device-driver-architecture-v20)).

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

## Device Driver Architecture (v2.0)

Added 2026-07-28 when the user wanted minidsp-rs (https://github.com/mrene/minidsp-rs) support
alongside Denon, explicitly asking for a real pluggable-backend design ("near 100% chance of
extending support in the future") rather than a one-off Denon-vs-MiniDSP branch. Three backends
now follow this recipe -- Denon, MiniDSP, and HA `media_player` (added 2026-07-28, see "Backend
#3: HA media_player" below) -- this section is what a fourth would follow.

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
   compile code it never uses; see "CircuitPython heap/boot-memory guardrails" for why that
   matters more than it sounds like it should).

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
exact same condition as that label already was (`state.media_state in ("playing", "paused")`, in
turn gated by `HA_MEDIA_CONTROLS`) -- no new setting. Tap zones for the two icons are checked
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
mute, always-on play/pause/skip (no opt-in flag like `HA_MEDIA_CONTROLS` -- a streaming source
either is or isn't currently playing, none of `ha`'s "rougher fit" caveat applies), input/source
selection, and presets -- but explicitly *not* device discovery/switching like `ha`'s Media Player
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
`CAPS["presets"] = config.WIIM_PRESET_COUNT > 0` (set once, at import time, not runtime-discovered
at all now), and `get_presets()` returns `("", [(str(i), name) for i in 1..COUNT])` with names from
`WIIM_PRESET_NAMES` falling back to `"Preset N"` -- same shape, same reasoning, different backend.

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

## CircuitPython heap/boot-memory guardrails (read before touching boot-path code)

The app once failed to boot on real hardware (`MemoryError` allocating the ~28.8KB gauge bitmap,
sometimes surfacing instead as a WiFi connect hang/failure depending on exactly how the heap
fragmented that boot) purely because the codebase had grown past a memory cliff on the ESP32-S3 --
not any single bug. Fixed via `.mpy` precompilation + splitting `code.py` down to a thin entry
point; see `Makefile` (`deploy-mpy` target) and `app.py`'s header comment for the mechanics. The
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
- **`.mpy`-compile everything else** (`make deploy-mpy`) before treating a boot-memory question as
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
- **Font glyph codepoint risk is documented in `Makefile`'s `fonts` target** -- read it before
  adding any character outside the existing ASCII 32-126 range.
- **Before declaring anything "fixed" or "root cause found": check the cheapest available number
  before and after the change**, not just after. A theory that's well-reasoned and even partially
  correct can still be the wrong fix if that check is skipped.

## Suggested Prompt For Next Session

> Read "CircuitPython heap/boot-memory guardrails" before touching boot-path code (`app.py`'s
> `main()`), `.mpy` deploy tooling, or the `driver.py`/`ha_ui.py`/`wiim_ui.py` UI-extension split.
> Otherwise: review "Device Driver Architecture (v2.0)" if the task involves adding or changing a
> device backend, and the "hard lessons" list before touching backend-specific code.
>
> If the WiiM backend's rewritten raw-socket transport hasn't been redeployed and booted fresh yet,
> that's the first thing to try -- the approach itself is confirmed working (see "Backend #4: WiiM
> / LinkPlay streamer"'s TLS section), but only via ad-hoc REPL snippets, not the actual shipped
> module code end to end. Check `gc.mem_free()` before/after on that first real boot regardless,
> same as any other memory theory in this file.

## Current Recommendation

The app boots and connects cleanly on real hardware, both `denon` and `ha` configs. `make
deploy-mpy` is what the device should run day to day; plain `make deploy` is for fast dev
iteration only (no `mpy-cross` needed) and should not be mistaken for the shipped configuration.

All four backends are implemented against the same
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
`adafruit_requests.Session` with `check_hostname = False`) reliably failed to boot -- see "HTTPS-
only API with a self-signed cert" above for the full debugging trail -- and the fix (a raw-socket
transport with a hardcoded SNI hostname, bypassing `adafruit_requests` for this backend only) is
confirmed working end-to-end via live REPL testing against the real M5 Dial and a real WiiM Pro
(full HTTP/1.0 response -- status line, headers, JSON body -- received correctly over the fixed
path). What's still outstanding: the *rewritten* `wiim.py`/`app.py` haven't been redeployed and
booted fresh yet with the fix in place -- the fix was validated via ad-hoc snippets typed directly
into the live REPL, not yet the actual shipped module code end to end, so that's the next concrete
step, not a re-litigation of whether the approach works. Also still unverified: the `getPresetInfo`
`preset_list` entry field names (no favorites were configured on the test unit yet).

Future work could include: input selection UI polish, sound
mode selection, multi-zone support, or wiring up MiniDSP's real Dirac-series per-slot filter names
if a future unit exposes more than a bare on/off `dirac` boolean.