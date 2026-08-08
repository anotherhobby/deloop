# deloop Hardware Reference

Physical hardware, flashing, pinout, and the deploy mechanics that depend on the physical device.
See [docs/architecture.md](architecture.md) for the software stack/layout, and
[docs/device-drivers.md](device-drivers.md) for backend-specific protocol details.

## How to flash CircuitPython

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

Confirmed working with CircuitPython 10.2.1, board ID `m5stack_dial`.

## Deployment Goal

Day-to-day development -- and what the device should actually run:

```sh
make deploy
```

**Corrected 2026-08-01: the target names below flipped meaning at some point after this doc's
content was first written**, and the stale version briefly made it back into `.claude/CLAUDE.md`
during the docs reorg -- caught live when it nearly sent a debugging session down the wrong path.
`make deploy` now runs `_copy-files-mpy` (every module except `code.py` precompiled to `.mpy`) --
this is the reliable, low-heap-footprint path and is what the device should run day to day. The
Makefile's own comment on that target explains why the rename happened: the old naming (`deploy`
= plain `.py`, `deploy-mpy` = compiled) let people run plain `deploy` "out of habit" and silently
regress the device back into the exact low-headroom boot-memory state described in
[docs/architecture.md](architecture.md)'s "CircuitPython heap/boot-memory guardrails". Plain,
uncompiled `.py` deployment is now `make deploy-src` (`_copy-files` target) -- for when
`local/mpy-cross` isn't available, or when chasing a traceback where uncompiled source gives a
clearer on-device error. **Always confirm against the Makefile itself before trusting a deploy
command from this doc or CLAUDE.md** -- this is exactly the kind of fact that drifts.

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

### Fonts

Three Inter Medium PCF bitmaps -- `Inter_Medium_20/24/36.pcf` -- generated from the TTF by
`make fonts` and deployed to `/CIRCUITPY/fonts/` by `make deploy`. `dial_ui.py` loads them as
`_F_SM`/`_F_MD`/`_F_LG`; only the volume digits are pre-cached, everything else loads lazily from
flash to keep boot-time heap free for the WiFi stack.

The Makefile's `FONT_SIZES` must stay exactly the set `dial_ui.py` loads. Generating a size
nothing opens is dead flash on the device, and the character set is capped at printable ASCII
32-126 for a reason -- see the `fonts` target's own comment, which documents a real production
WiFi outage caused by one out-of-range codepoint tripling every font's size.

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
