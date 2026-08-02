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
# "camilladsp": a CamillaDSP process (https://github.com/HEnquist/camilladsp)
#               running on some host, over its WebSocket control API -- see
#               camilladsp.py. That host must be running CamillaDSP and
#               reachable over the LAN, same topology as "minidsp". The only
#               backend with a hand-rolled (not adafruit_requests) transport
#               besides "wiim" -- see camilladsp.py's module docstring.
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

# CamillaDSP settings
CAMILLADSP_HOST       = os.getenv("CAMILLADSP_HOST", "")
CAMILLADSP_PORT       = int(os.getenv("CAMILLADSP_PORT", "1234"))
CAMILLADSP_TIMEOUT_MS = int(os.getenv("CAMILLADSP_TIMEOUT", "2000"))
# Switching the active config file (see camilladsp.py's presets) reloads the
# whole filter pipeline. Measured ~1-3ms live (2026-07-30) against a trivial
# test config (SignalGenerator capture + one filter, no FIR/convolution files
# to load) -- that's a lower bound, not a general answer, since a real room-
# correction config with large filter files could be much slower to load.
# Left at the same generous headroom minidsp.py needed for its own full-DSP-
# reconfiguration case until someone times a real production config.
CAMILLADSP_PRESET_TIMEOUT_MS = int(os.getenv("CAMILLADSP_PRESET_TIMEOUT", "10000"))
# Presets -- CamillaDSP config files to offer, as "Name:/path/to/config.yml"
# pairs, comma-separated, in menu order. Unlike MINIDSP_PRESET_NAMES/
# WIIM_PRESET_NAMES (a plain name list matched up against a separately
# configured slot count), CamillaDSP presets have no slot count at all --
# they're just config file paths on the host running CamillaDSP -- so name
# and path are configured together, one pair per preset. The path must be
# absolute and resolvable by the CamillaDSP process itself, not by this
# device. Splits on the first ":" only, so a path may not itself start with
# a colon-separated prefix (fine for normal Linux/Mac paths).
def _parse_camilladsp_presets(raw):
    presets = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, path = part.split(":", 1)
        name, path = name.strip(), path.strip()
        if name and path:
            presets.append((path, name))
    return presets


CAMILLADSP_PRESETS = _parse_camilladsp_presets(os.getenv("CAMILLADSP_PRESETS", ""))

# Main-screen quick-select buttons -- a subset of CAMILLADSP_PRESETS (by
# name, comma-separated, up to 4 -- see dial_ui.py's _DBTN_MAX and
# driver.py's CAPS["preset_quickbuttons"]/get_quick_presets() contract notes
# for why a subset at all: CamillaDSP presets have no real ceiling on count,
# so unlike minidsp.py/denon.py (whose full lists already fit the button
# row), CAMILLADSP_PRESETS itself stays reachable only through the
# scrollable Preset submenu, and this separate, deliberately small list is
# what gets main-screen buttons. Leave unset for no quick buttons at all
# (submenu-only). Names not found in CAMILLADSP_PRESETS are skipped with a
# warning printed at boot; more than 4 names are truncated to the first 4,
# also with a warning.
def _parse_camilladsp_quick_presets(raw, presets):
    by_name = {name: (path, name) for path, name in presets}
    quick = []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        pair = by_name.get(name)
        if pair is None:
            print("config: CAMILLADSP_QUICK_PRESETS name {!r} not found in "
                  "CAMILLADSP_PRESETS, skipping".format(name))
            continue
        quick.append(pair)
    if len(quick) > 4:
        print("config: CAMILLADSP_QUICK_PRESETS has {} entries, only the "
              "first 4 get main-screen buttons".format(len(quick)))
        quick = quick[:4]
    return quick


CAMILLADSP_QUICK_PRESETS = _parse_camilladsp_quick_presets(
    os.getenv("CAMILLADSP_QUICK_PRESETS", ""), CAMILLADSP_PRESETS)

# OTA self-update settings -- orthogonal to DEVICE_DRIVER, see ota.py. This
# updates deloop's own app files from a GitHub release, never CircuitPython
# firmware itself. Manual only: nothing here runs in the background -- the
# Update menu's "Check Now"/"Install Update" are the only entry points. See
# README.md's "Updating deloop" section.
OTA_ENABLED            = _get_bool("OTA_ENABLED", True)
OTA_REPO               = os.getenv("OTA_REPO", "anotherhobby/deloop")
OTA_CHECK_TIMEOUT_MS   = int(os.getenv("OTA_CHECK_TIMEOUT", "10000"))
# Generous headroom for a ~15-file sequential download over Wi-Fi -- not yet
# measured against real hardware (see CLAUDE.md's OTA section); revisit once
# it has been, same as every other backend's *_TIMEOUT_MS constants here.
OTA_INSTALL_TIMEOUT_MS = int(os.getenv("OTA_INSTALL_TIMEOUT", "60000"))

# NVM byte indices for the OTA action/result/version flags -- shared between
# app.py (normal-mode UI: reads result/version, writes a pending action to
# trigger a reload) and ota_boot.py (the actual lean network/install pass).
# Defined once here, in the one module both already import, so they can
# never drift apart. Bytes 0/1 are used elsewhere (brightness/sound, see
# app.py) -- not redefined here since only app.py touches those.
NVM_OTA_ACTION  = 2   # 0=idle, 1=pending check, 2=pending install
NVM_OTA_RESULT  = 3   # 0=none, 1=available, 2=up_to_date, 3=check_error,
                      # 4=install_ok, 5=install_failed, 6=eject_needed
NVM_OTA_VERSION = 4   # latest version found by a check (valid when result==1)

# Dev-only: read by boot.py to decide whether to call
# storage.disable_usb_drive() -- lets Install Update's storage.remount()
# succeed without physically ejecting/reconnecting CIRCUITPY for every
# test, while keeping the USB serial console (REPL/prints) fully available.
# Toggle via `make usb-drive-off`/`make usb-drive-on` (see Makefile) --
# both require a hard reset to take effect, since boot.py only runs then.
# Has zero effect on production/normal use: unset (0, the erased-flash
# default reads as 255 but boot.py only acts on an exact 1) means
# CIRCUITPY mounts exactly as it always has.
NVM_USB_DRIVE_DISABLED = 5

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
# with no conversion needed on wiim.py's side. CamillaDSP's SetVolume takes
# a real dB value like MiniDSP's, clamped server-side to -150..+50 (per its
# own docs), but -150..0 (the full attenuation-only portion of that) is a
# poor default in practice -- it's far wider than anyone actually uses, so
# most of the dial's rotation would land in inaudibly-quiet territory. -50..0
# is instead confirmed from a real, well-sourced reference point: CamillaGUI
# (HEnquist/camillagui-backend, the official companion web GUI, same author
# as CamillaDSP itself) ships this exact range as its own hardcoded default
# (`volume_range: 50` / `volume_max: 0` in config/gui-config.yml and
# backend/settings.py) -- confirmed by reading that repo directly, not a
# forum guess. It's also a coincidentally exact match for VOLUME_MIN=-50
# already used for `minidsp` here (see the "-127..0" default below, commonly
# narrowed to -50..0 in practice) -- worth knowing that match is about
# digital gain math, not necessarily identical loudness: both systems use
# the same 20*log10(amplitude) dB convention for volume (confirmed in
# CamillaDSP's own src/utils/decibels.rs), so -50dB attenuates the digital
# signal by the identical amount on both -- but how loud that actually
# sounds also depends on each system's downstream analog gain (DAC output
# level, amp gain, speaker sensitivity), which differs per setup and neither
# DSP's own volume number encodes. Override VOLUME_MAX if a config
# genuinely needs headroom above unity (positive gain risks clipping
# downstream unless deliberately configured for make-up gain).
if DEVICE_DRIVER == "minidsp":
    VOLUME_MIN = float(os.getenv("VOLUME_MIN", "-127.0"))
    VOLUME_MAX = float(os.getenv("VOLUME_MAX", "0.0"))
    VOLUME_STEP      = float(os.getenv("VOLUME_STEP", "0.5"))
    VOLUME_STEP_FAST = float(os.getenv("VOLUME_STEP_FAST", "2.0"))
elif DEVICE_DRIVER == "camilladsp":
    VOLUME_MIN = float(os.getenv("VOLUME_MIN", "-50.0"))
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
