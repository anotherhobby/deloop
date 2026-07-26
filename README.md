# deloop

Firmware for the [M5 Dial](https://shop.m5stack.com/products/m5stack-dial-v1-1) that turns it into a
dedicated remote for a Denon/Marantz AVR — rotate to change volume, tap to mute, touch menu for
inputs / Dirac Live presets / device settings. Built because controlling a Denon AVR over Wi-Fi
apps or a universal remote is consistently awkward, and a physical dial with a live volume readout
isn't.

## Features

- Volume up/down via the rotary encoder, with acceleration on a fast spin
- Live circular gauge display of current volume (dB), synced against the AVR so external changes
  (app, another remote) show up too
- Tap to mute, long-press to power on/standby
- Touch menu: input selection, Dirac Live filter selection, brightness, click sound on/off
- Runs on [CircuitPython](https://circuitpython.org/board/m5stack_dial/) — no M5Stack SDK required

## Hardware

- M5 Dial ([docs](https://docs.m5stack.com/en/core/M5Dial))
- A Denon/Marantz AVR reachable over Wi-Fi (developed against an AVR-X4800H; see `src/denon.py`
  for the HTTP control API details)

## Setup

1. Flash the M5 Dial with CircuitPython.
2. Set up the host-side dev environment (one time):
   ```
   python -m venv .venv
   make bootstrap
   ```
3. Copy the settings template and fill in your Wi-Fi and AVR details:
   ```
   cp src/settings.toml.template src/settings.toml
   ```
   `src/settings.toml` is gitignored — it holds your Wi-Fi password and AVR IP and should never be
   committed.
4. With the Dial mounted as `/Volumes/CIRCUITPY`, deploy everything:
   ```
   make full-deploy
   ```

After the first deploy, `make deploy` is the fast path for iterating on firmware changes (skips
reinstalling CircuitPython libraries).

## Makefile targets

| Target         | Purpose                                                              |
|----------------|-----------------------------------------------------------------------|
| `bootstrap`    | Install host-side dev tools into `.venv` (run once per machine)      |
| `install-libs` | Install required CircuitPython libraries onto the mounted device    |
| `full-deploy`  | `install-libs` + copy all firmware files (fresh flash / onboarding)  |
| `deploy`       | Copy firmware files only (fast iteration)                            |
| `fonts`        | Regenerate the Inter PCF bitmap fonts from the TTF source            |
| `splash`       | Regenerate the splash screen BMP from `ui/hobbysprawl.png`           |
| `ls`           | List files on the mounted device                                    |
| `shell`        | Open an interactive CircuitPython REPL over USB serial               |
| `probe`        | Host-side HTTP probe against the AVR control API (`PROBE_ARGS=...`)  |
| `dump-avr`     | Dump full AVR state via `python-denonavr` (endpoint/version discovery) |

## Configuration

Settings live in `src/settings.toml` (see `src/settings.toml.template` for all available keys):
Wi-Fi credentials, AVR host/port, volume step sizes and acceleration thresholds, and display names
for the AVR's speaker presets. `src/config.py` reads these at boot and applies safe defaults for
anything left unset.

## Code layout

- `src/code.py` — CircuitPython entry point: input handling (encoder, touch), menu state machine,
  and the main loop
- `src/denon.py` — HTTP client for the Denon/Marantz control API (volume, power, mute, inputs,
  Dirac Live, speaker presets)
- `src/dial_ui.py` — display rendering: the circular gauge, volume readout, and menu overlay
- `src/state.py` — in-memory model of AVR state, reconciled against periodic polls
- `src/sound.py` — piezo buzzer click feedback for taps and menu actions
- `src/config.py` — settings loader/defaults
- `tools/` — host-side scripts used during development (AVR protocol probing, font/splash
  generation); not deployed to the device
