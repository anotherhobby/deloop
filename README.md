# deloop

deloop is Firmware for the [M5 Dial](https://shop.m5stack.com/products/m5stack-dial-v1-1) that turns it into a dedicated remote and volume knob for a Denon/Marantz AVR, intended for desktop use. You rotate the encoder to change volume, tap the scren to mute, and there is touch menu for inputs / Dirac Live presets / and device settings.

It also has experimental support for a [MiniDSP](https://www.minidsp.com/) unit via [minidsp-rs](https://github.com/mrene/minidsp-rs), and for any [Home Assistant](https://www.home-assistant.io/) `media_player` entity -- see [Device backends](#device-backends) below for what each one does and doesn't get you.

![device](ui/device.jpeg)

## How was AI used in developing deloop?

Not vibecoded, but step-by-step agentically assisted/written by Claude and an experienced engineer who has also been building hobby projects on ESP32 since shortly after release.

## Features

- Volume up/down via the rotary encoder, with acceleration on a fast spin
- State display: Volume, Current Input, Current Preset (Dirac Live filter on Denon)
- Live circular color gauge display of volume (dB) with a moving indicator
- Main screen tap controls: mute, preset selection, menu, power (long press, Denon only)
- Tap the already-active preset to disable it in place (e.g. Dirac Live off) without losing which one is loaded -- it stays highlighted, just in gray instead of orange
- Screen grays out (a static frame, no pulse) while a preset change is slow to apply (confirmed several seconds on MiniDSP config-slot switches), then returns to color once it's done
- Touch menu: input selection, preset selection, brightness, click sound on/off, restart device
- Synced against the device so external changes (app, another remote) update deloop.
- Runs on [CircuitPython](https://circuitpython.org/board/m5stack_dial/)
- Pluggable device backend (`src/driver.py`) -- see [Device backends](#device-backends)
- Note that deloop is not currently designed to support displaying and/or updating Audyssey filters and I don't know if it works.

## User Interface

The main screen's color bar has a white triangular pointer that rotates around the color bar live as you adjust the volume with the rotary encoder, indicating current position within the active backend's volume range (`VOLUME_MIN`/`VOLUME_MAX` in `src/config.py`). The color bands are proportional to that range -- bottom 60% green, next 10% amber, next 10% orange, top 20% red -- which on a Denon AVR's -80dB..+18dB range works out to:

- green:  -80dB to -20dB
- yellow: -20dB to -10dB
- orange: -10dB to 0dB
- red: 0dB to +18dB
- Major ticks indicate 10dB increemnts
- Minor ticks indicate 5dB increments

The range is configurable via settings file. For example, I like a volume range of -50 to 0 on miniDSP, even though the device goes to -127 dB.

![Main screen at a normal volume](ui/main_diagram.png)

### Mute

Tapping the screen area anywhere above the preset name will mute the device. While muted, the volume number and all color elemnts on the display appear blue, and the number will slowly pulsate like a sleep indicator. Set `MUTE_PULSE = "false"` in `settings.toml` to keep it a static blue instead.

![Main screen muted](ui/main_muted.png)

### Standby

Denon only -- see [Device backends](#device-backends). When the AVR is in standby mode, a dim power button is displayed. A long press will power the AVR on and cycle back to the main screen.

![Standby screen](ui/power_off.png)

Note: the screen shots are pixel-accurate renders of `dial_ui.py`'s actual drawing code (see `make renders` below), not mockups.

## Hardware

- Only tested on the M5Dial ([docs](https://docs.m5stack.com/en/core/M5Dial)). The M5Dial is a panel mount device that mounts thru a 45mm hole. There are other ESP32 rotary encoders out there, but I would not expect them work out of the box with this project.
- A modern Denon/Marantz AVR reachable over Wi-Fi (developed against an AVR-X4800H; see `src/denon.py` for the HTTP control API details) -- **or** a MiniDSP unit driven via minidsp-rs, see below.

## Device backends

deloop talks to the amp through a swappable driver module (`src/driver.py`), selected by the `DEVICE_DRIVER` key in `settings.toml`. `code.py` and `dial_ui.py` never talk to a specific backend directly -- they read a `CAPS` dict the active driver exports to decide which menu items and gestures to offer, so a backend that can't do something (e.g. no power state) simply doesn't advertise that capability rather than needing special-casing throughout the UI code. See the contract documented at the top of `src/driver.py` if you want to add another backend.

|                    | `denon` (default)                          | `minidsp`                                          | `ha`                                                |
|--------------------|---------------------------------------------|-----------------------------------------------------|------------------------------------------------------|
| Talks to           | The AVR directly over Wi-Fi                  | A [minidsp-rs](https://github.com/mrene/minidsp-rs) daemon on some host machine with the MiniDSP attached over USB | A [Home Assistant](https://www.home-assistant.io/) instance's REST API, controlling any `media_player` entity |
| Extra dependency   | None                                          | That host must be running and reachable on the LAN -- run with no `--config` at all and it already binds to `0.0.0.0:5380` (all interfaces); a config file only takes effect if passed explicitly via `--config`, and its example ships with the restrictive `127.0.0.1:5380` active by default | A Home Assistant instance reachable on the LAN and a long-lived access token (Profile -> Security -> Long-Lived Access Tokens) |
| Power/standby      | Yes (long-press gesture)                     | No -- the DSP is "on" whenever the host + USB link are up; the long-press gesture is disabled | Auto-detected from the entity's `supported_features` -- on if it supports both `turn_on` and `turn_off`, off otherwise |
| Input selection     | Named HDMI-style sources, renamed by the AVR | The DSP's source enum (Toslink/USB/Analog/...), derived from the unit's hw_id -- see `src/minidsp.py` | The entity's own `source_list`, auto-detected the same way as power |
| Presets            | Dirac Live filter picker. Selecting a filter always engages it -- there's no separate on/off bit, so switching filters and enabling are the same action | Always the DSP's config-slot switching (0..N-1) -- slot count isn't discoverable via the API, set `MINIDSP_PRESET_COUNT` to match your unit. Names default to "Preset 1", "Preset 2", etc. since the API can't read slot names back either -- set `MINIDSP_PRESET_NAMES` to override. If the unit also reports a `dirac` field (e.g. a Flex with a Dirac license), tapping the *active* slot additionally toggles Dirac on/off in place; switching to a *different* slot deliberately leaves that on/off state untouched, since a slot may want Dirac off on purpose (e.g. a headphone config) -- see `CAPS["preset_select_enables"]` in `src/driver.py` | Not supported -- there's no generic `media_player` equivalent of Dirac Live/config slots, so no preset menu is drawn at all |
| Preset switch speed | Near-instant                                | Confirmed ~4s+ on real hardware -- minidsp-rs's POST blocks until the DSP finishes reconfiguring. The screen shows a static gray frame for the duration (see `dial_ui.draw_busy`); raise `MINIDSP_PRESET_TIMEOUT` if your unit is slower than the ~10s default | N/A -- no presets |
| Volume range       | -80dB to +18dB (dB relative to reference)    | -127dB to 0dB (dB of attenuation below unity) -- the gauge's color bands and tick marks scale to whichever range is active | 0 to 100 percent -- HA always normalizes `volume_level` to a 0.0-1.0 fraction regardless of the underlying device, so there's no real dB value to show |
| Playback control    | N/A                                            | N/A                                                   | Off by default -- set `HA_MEDIA_CONTROLS = true` to opt in. When on, the status line below the volume shows "Playing"/"Paused" (tap to toggle) whenever the entity reports one of those two states; blank/no tap target otherwise. Rougher than the other fields -- whether it does anything real depends on the currently selected source (e.g. a no-op on a plain analog input) |

The `minidsp` backend derives its input list and Dirac/preset behavior from what the unit itself reports (hw_id, dsp_version, and whether a "dirac" field comes back at all) rather than hardcoding one model, so it's meant to work across whatever minidsp-rs itself supports, not just the two units it's been verified against so far (a 2x4HD and a Dirac-licensed Flex).

If more than one MiniDSP is ever attached to the same host at once, set `MINIDSP_SERIAL` instead of relying on `MINIDSP_DEVICE_INDEX` -- minidsp-rs's `/devices` array order follows USB enumeration order, which is not guaranteed stable across reconnects.

The `ha` backend works with any `media_player` entity, not just Denon/Marantz ones -- it's the same REST API regardless of what HA integration actually created the entity. It's plain synchronous polling on the same adaptive schedule as the other two backends (no event subscription, no persistent connection) -- deliberately as simple as `denon`/`minidsp`, just pointed at HA instead of the device directly.

Before flashing, `make probe-minidsp` / `make probe-ha` (host-side, needs the daemon/HA instance reachable) hit the same JSON endpoints `src/minidsp.py`/`src/ha.py` use and print the raw responses, so you can confirm the shapes match your setup before wiring up the device.

## Setup

1. Flash the M5 Dial with CircuitPython.
2. Set up the host-side dev environment (one time):
   ```
   python -m venv .venv
   make bootstrap
   ```
3. Copy the settings template and fill in your Wi-Fi and your device settings (AVR IP, or `DEVICE_DRIVER = "minidsp"` plus the `MINIDSP_*` keys, or `DEVICE_DRIVER = "ha"` plus the `HA_*` keys):
   ```
   cp src/settings.toml.template src/settings.toml
   ```
   `src/settings.toml` is gitignored — it holds your Wi-Fi password and device address(es) and should never be committed.
4. With the Dial mounted as `/Volumes/CIRCUITPY`, deploy everything:
   ```
   make full-deploy
   ```

After the first deploy, `make deploy` is the fast path for iterating on firmware changes if you decide make your own tweaks (skips reinstalling CircuitPython libraries).

## Makefile targets

| Target         | Purpose                                                              |
|----------------|-----------------------------------------------------------------------|
| `bootstrap`    | Install host-side dev tools into `.venv` (run once per machine)      |
| `install-libs` | Install required CircuitPython libraries onto the mounted device    |
| `full-deploy`  | `install-libs` + copy all firmware files (fresh flash / onboarding)  |
| `deploy`       | Copy firmware files only (fast iteration)                            |
| `fonts`        | Regenerate the Inter PCF bitmap fonts from the TTF source            |
| `splash`       | Regenerate the splash screen BMP from `ui/hobbysprawl.png`           |
| `renders`      | Render dial_ui.py's screens to PNGs without the device, into `local/renders/` (`tools/dial_sim.py`) |
| `ls`           | List files on the mounted device                                    |
| `shell`        | Open an interactive CircuitPython REPL over USB serial               |
| `probe`        | Host-side HTTP probe against the AVR control API (`PROBE_ARGS=...`)  |
| `dump-avr`     | Dump full AVR state via `python-denonavr` (endpoint/version discovery) |
| `probe-minidsp`| Host-side HTTP probe against a minidsp-rs daemon (`PROBE_ARGS=...`)  |
| `probe-ha`     | Host-side REST probe against a Home Assistant instance (`PROBE_ARGS=...`) |

`fonts` and `renders` need the Inter font family locally -- it's not shipped with the project. Grab v4.1 from [github.com/rsms/inter/releases](https://github.com/rsms/inter/releases) and extract it to `local/Inter-4.1` (gitignored). Neither target is required for normal flashing/use, only for regenerating fonts or screenshots.

## Configuration

Settings live in `src/settings.toml` (see `src/settings.toml.template` for all available keys): Wi-Fi credentials, which device backend to use (`DEVICE_DRIVER`), that backend's host/port, and volume step sizes and acceleration thresholds. `src/config.py` reads these at boot and applies safe, per-backend defaults for anything left unset.

## Code layout

- `src/code.py` — CircuitPython entry point: input handling (encoder, touch), menu state machine, and the main loop
- `src/driver.py` — selects the active device backend and documents the driver contract other backends implement
- `src/denon.py` — HTTP client for the Denon/Marantz control API (volume, power, mute, inputs, Dirac Live presets)
- `src/minidsp.py` — HTTP client for a [minidsp-rs](https://github.com/mrene/minidsp-rs) daemon (volume, mute, input source, config-slot presets)
- `src/ha.py` — REST client for a [Home Assistant](https://www.home-assistant.io/) `media_player` entity (volume, mute, power, source; no presets)
- `src/dial_ui.py` — display rendering: the circular gauge, volume readout, and menu overlay
- `src/state.py` — in-memory model of device state, reconciled against periodic polls
- `src/sound.py` — piezo buzzer click feedback for taps and menu actions
- `src/config.py` — settings loader/defaults
- `tools/` — host-side scripts used during development (AVR/minidsp-rs protocol probing, font/splash generation, off-device screen rendering); not deployed to the device
- `local/` — gitignored, not shipped: the Inter font family download and `make renders` output land here

## What is hobbysprawl?

It's the umbrella name for my creations.