# deloop device configuration
# Values come from /settings.toml on the device (CircuitPython reads it at
# boot and exposes values via os.getenv). Defaults here are safe fallbacks.
# On the host Mac, os.getenv reads the shell environment; probe tools pass
# --host directly so they never rely on these values.

import os


def _get_bool(key, default):
    """Parse a settings.toml value as a bool. Accepts either a real TOML
    boolean (true/false, unquoted) or a quoted string ("true"/"false") --
    every other key in this file is a quoted string by convention, and a
    bare `if os.getenv(key)` would treat the *string* "false" as truthy,
    silently enabling something the user tried to turn off."""
    val = os.getenv(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() == "true"


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
# "ha": any Home Assistant media_player entity, via HA's REST API -- see
#       ha.py. No preset/filter support (no generic media_player
#       equivalent of Dirac Live/config slots).
# "wiim": a WiiM/LinkPlay streamer, direct over its own HTTPS API -- see
#         wiim.py. HTTPS-only with a self-signed (but fixed, not per-device)
#         cert -- see wiim.py's module docstring.
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

# Home Assistant settings
HA_HOST       = os.getenv("HA_HOST", "")
HA_PORT       = int(os.getenv("HA_PORT", "8123"))
HA_TOKEN      = os.getenv("HA_TOKEN", "")
HA_ENTITY_ID  = os.getenv("HA_ENTITY_ID", "media_player.office")
HA_TIMEOUT_MS = int(os.getenv("HA_TIMEOUT", "2000"))
# Play/pause status text + tap-to-toggle (see ha.py's get_status()) -- off by
# default. media_player playback state/control is a rougher fit than
# volume/mute/power/source: it depends on whatever source is currently
# selected (e.g. no-op on a plain analog input) and isn't tested as
# thoroughly. Opt in once you've confirmed it behaves well with your setup.
HA_MEDIA_CONTROLS = _get_bool("HA_MEDIA_CONTROLS", False)

# WiiM/LinkPlay streamer settings
WIIM_HOST       = os.getenv("WIIM_HOST", "")
WIIM_TIMEOUT_MS = int(os.getenv("WIIM_TIMEOUT", "2000"))
# Physical/network sources to offer in the Source menu, as switchmode keys
# (comma-separated). The default set (wifi, bluetooth, line-in, optical) was
# empirically confirmed against a WiiM Pro -- see wiim.py's module
# docstring. Other WiiM/LinkPlay models (Amp, Ultra, Pro Plus) may expose a
# different physical set; there's no API endpoint that reports it, so this
# is the escape hatch (same role MINIDSP_PRESET_NAMES plays for a similarly
# per-unit list).
WIIM_INPUTS = [s.strip() for s in
               os.getenv("WIIM_INPUTS", "wifi,bluetooth,line-in,optical").split(",")
               if s.strip()]
# WiiM-app Favorites (activated via MCUKeyShortClick:1-12) can't be listed
# back reliably over the plain HTTP API -- getPresetInfo's preset_list stays
# empty even with Favorites configured; real names require the much heavier
# UPnP/SOAP interface (GetKeyMapping), not implemented here. Same fallback
# minidsp.py already uses for its own API-can't-report-names gap: set the
# count you've actually configured and optionally name them.
WIIM_PRESET_COUNT = int(os.getenv("WIIM_PRESET_COUNT", "0"))
WIIM_PRESET_NAMES = [n.strip() for n in
                      os.getenv("WIIM_PRESET_NAMES", "").split(",")
                      if n.strip()]

# Polling interval -- display state is truth; poll is error correction only
POLL_INTERVAL_S  = float(os.getenv("POLL_INTERVAL", "30.0"))
POLL_INTERVAL_MS = int(POLL_INTERVAL_S * 1000)

# Volume limits and acceleration
# Range differs by backend: a Denon AVR's master volume is dB-relative-to-
# reference (0 dB = reference, positive = above it); a MiniDSP's master
# volume is dB-of-attenuation (0 dB = unity/max, no positive headroom); a
# Home Assistant media_player entity has no dB concept at all -- HA always
# normalizes volume_level to a 0.0-1.0 fraction, so ha.py maps that to a
# plain 0-100 percent range instead. A WiiM streamer lands on the same 0-100
# range but natively -- its API takes and reports a plain integer percent,
# with no conversion needed on wiim.py's side.
if DEVICE_DRIVER == "minidsp":
    VOLUME_MIN = float(os.getenv("VOLUME_MIN", "-127.0"))
    VOLUME_MAX = float(os.getenv("VOLUME_MAX", "0.0"))
    VOLUME_STEP      = float(os.getenv("VOLUME_STEP", "0.5"))
    VOLUME_STEP_FAST = float(os.getenv("VOLUME_STEP_FAST", "2.0"))
elif DEVICE_DRIVER in ("ha", "wiim"):
    VOLUME_MIN = float(os.getenv("VOLUME_MIN", "0.0"))
    VOLUME_MAX = float(os.getenv("VOLUME_MAX", "100.0"))
    VOLUME_STEP      = float(os.getenv("VOLUME_STEP", "2.0"))      # percent/tick normal
    VOLUME_STEP_FAST = float(os.getenv("VOLUME_STEP_FAST", "5.0")) # percent/tick fast spin
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

# Standby/off screen brightness, as a fraction of the user's own brightness
# setting (not a fixed value) -- dim room, dim standby indicator; bright
# room, brighter one. 0.0-1.0.
STANDBY_BRIGHTNESS_FRAC = float(os.getenv("STANDBY_BRIGHTNESS_FRAC", "0.25"))

# Mute "breathing" animation (the volume number slowly pulses while muted) --
# on by default. Turning this off also disables the trough-timed poll that
# rides along with it (see code.py's _pulse_mute) -- polling while muted
# falls back to the normal adaptive schedule instead, since there's no
# animation left to hide a poll's brief pause inside.
MUTE_PULSE = _get_bool("MUTE_PULSE", True)
