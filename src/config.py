# deloop device configuration
# Values come from /settings.toml on the device (CircuitPython reads it at
# boot and exposes values via os.getenv). Defaults here are safe fallbacks.
# On the host Mac, os.getenv reads the shell environment; probe tools pass
# --host directly so they never rely on these values.

import os

# WiFi -- set in settings.toml
WIFI_SSID = os.getenv("WIFI_SSID", "")
WIFI_PASS = os.getenv("WIFI_PASS", "")

# Device backend -- which driver module (src/driver.py) talks to the amp.
# "denon" (default): direct WiFi to a Denon/Marantz AVR -- see denon.py.
# "minidsp": a minidsp-rs daemon (https://github.com/mrene/minidsp-rs)
#            running on a host machine with the MiniDSP attached over USB
#            -- see minidsp.py. That host must be reachable over the LAN;
#            minidsp-rs binds to 127.0.0.1:5380 by default, so its own
#            config needs `bind_address = "0.0.0.0:5380"` (or similar) for
#            deloop to reach it at all.
DEVICE_DRIVER = os.getenv("DEVICE_DRIVER", "denon")

# AVR network settings
# AVR-X4800H: control API is on port 8080, plain HTTP.
# Port 80 redirects to HTTPS; port 443 HTTPS returns 403 for /goform/ paths.
AVR_HOST   = os.getenv("AVR_HOST", "192.168.1.100")
AVR_PORT   = int(os.getenv("AVR_PORT", "8080"))
AVR_PORT_UI = 11080  # web UI port (inputs/sources, Dirac Live)
AVR_SCHEME = "http"
AVR_TIMEOUT_MS = 1000  # 1 s -- keep low so a slow AVR does not block the UI

# minidsp-rs daemon settings
MINIDSP_HOST          = os.getenv("MINIDSP_HOST", "192.168.1.101")
MINIDSP_PORT          = int(os.getenv("MINIDSP_PORT", "5380"))
MINIDSP_DEVICE_INDEX  = int(os.getenv("MINIDSP_DEVICE_INDEX", "0"))
# minidsp-rs's POST /devices/{index} blocks until the change is actually
# applied on the hardware, not just accepted -- volume/mute are near-instant
# (confirmed ~0.05-1.1s), but switching config slots/presets is a full DSP
# reconfiguration and measured ~4s on a real Flex. A too-short timeout here
# doesn't just show a stale UI -- the abandoned in-flight request seems to
# be able to crash minidsp-rs itself (observed once during testing).
MINIDSP_TIMEOUT_MS        = int(os.getenv("MINIDSP_TIMEOUT", "2000"))
MINIDSP_PRESET_TIMEOUT_MS = int(os.getenv("MINIDSP_PRESET_TIMEOUT", "10000"))
# Optional: pin to a specific unit by its serial number (see GET /devices'
# "version.serial", or `make probe-minidsp`) instead of MINIDSP_DEVICE_INDEX.
# minidsp-rs's /devices array order depends on USB enumeration, which isn't
# stable across reconnects/reboots when more than one unit is attached --
# leave blank if you only ever have one unit plugged in at a time.
MINIDSP_SERIAL        = os.getenv("MINIDSP_SERIAL", "")
# The HTTP API has no endpoint that reports how many config slots a unit
# actually has -- set this to match your device if it's not 4 (the max
# across MiniDSP's current lineup). See minidsp.py get_presets().
MINIDSP_PRESET_COUNT  = int(os.getenv("MINIDSP_PRESET_COUNT", "4"))
# Optional human-readable names for config slots 0..N-1, comma-separated
# and in order (e.g. "Movie,Music,Night,Flat") -- the API has no way to
# read back names, so this is the only way to label a slot. Any slot past
# the end of this list, or all of them if left unset, falls back to the
# "Preset N" default. See minidsp.py get_presets().
MINIDSP_PRESET_NAMES = [n.strip() for n in
                         os.getenv("MINIDSP_PRESET_NAMES", "").split(",")
                         if n.strip()]

# Polling interval -- display state is truth; poll is error correction only
POLL_INTERVAL_S  = float(os.getenv("POLL_INTERVAL", "30.0"))
POLL_INTERVAL_MS = int(POLL_INTERVAL_S * 1000)

# Volume limits and acceleration
# Range differs by backend: a Denon AVR's master volume is dB-relative-to-
# reference (0 dB = reference, positive = above it); a MiniDSP's master
# volume is dB-of-attenuation (0 dB = unity/max, no positive headroom).
if DEVICE_DRIVER == "minidsp":
    VOLUME_MIN = float(os.getenv("VOLUME_MIN", "-127.0"))
    VOLUME_MAX = float(os.getenv("VOLUME_MAX", "0.0"))
else:
    VOLUME_MIN = float(os.getenv("VOLUME_MIN", "-80.0"))
    VOLUME_MAX = float(os.getenv("VOLUME_MAX", "18.0"))
VOLUME_STEP      = float(os.getenv("VOLUME_STEP", "0.5"))      # dB/tick normal
VOLUME_STEP_FAST = float(os.getenv("VOLUME_STEP_FAST", "2.0")) # dB/tick fast spin
# Inter-tick interval (ms) below which fast mode kicks in.
# 50ms = ~20 ticks/sec = ~1.3 revolutions/sec -- feels like a deliberate quick sweep.
ACCEL_THRESHOLD_MS = int(os.getenv("ACCEL_THRESHOLD", "100"))
# When spinning fast and increasing volume, stop here. Prevents accidental blasting.
# Slow down to push past it intentionally.
ACCEL_SAFETY_CAP = float(os.getenv("ACCEL_SAFETY_CAP", "-15.0"))
