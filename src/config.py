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

# Bounded connect-only timeout for denon.py's non-blocking status-poll
# engine (_AsyncRequest) -- see docs/architecture.md's touch-drop
# investigation. A literal settimeout(0) before connect() doesn't give a
# resumable non-blocking result on this CircuitPython build (raises
# ETIMEDOUT immediately instead), so connect() gets one short bounded
# blocking slice instead of being pumped tick-by-tick like send()/recv().
# 75ms is generous for a LAN round-trip to a literal-IP host but still far
# below the 1-3s stalls this engine exists to eliminate.
AVR_CONNECT_TIMEOUT_MS = 75

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
# An HA_MEDIA_CONTROLS opt-in flag lived here until 2026-08-06, defaulting to
# off. It predated the media_state design and was made redundant by it: the
# play/pause row already renders only while the entity reports the literal
# state "playing"/"paused", so a source with nothing to pause (a plain analog
# input reports "on") never draws the controls in the first place. The flag
# was a second gate on top of a check that already did the job.

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
# minidsp.py already uses for its own API-can't-report-names gap: name the
# Favorites you've actually configured, in order.
#
# WIIM_PRESET_NAMES IS the preset list -- its length is how many presets
# exist. There is deliberately no separate count setting: the names are the
# slots, so a count could only ever agree with len() or silently contradict
# it. (It did contradict it: a WIIM_PRESET_COUNT setting used to clamp this
# list, so naming 3 Favorites with a count of 2 silently hid the third. See
# docs/device-drivers.md.)
WIIM_PRESET_NAMES = [n.strip() for n in
                      os.getenv("WIIM_PRESET_NAMES", "").split(",")
                      if n.strip()]
# Which of those presets get a main-screen quick-select button, by NAME,
# comma-separated, in the order you want them drawn ("Night,Movie" draws
# Night first). Names must appear in WIIM_PRESET_NAMES.
#
# By name, not by position, to match CAMILLADSP_QUICK_PRESETS -- these two
# settings look parallel in settings.toml.template and should behave the
# same. WiiM did take positions briefly (a position here is also the WiiM
# Favorite number, since get_presets() numbers slots 1..N and that is what
# MCUKeyShortClick:<n> takes), which was defensible for WiiM alone but read
# as an inconsistency next to CamillaDSP. Names also survive reordering the
# list, and a rename breaks loudly with the boot print below rather than
# silently pointing at whatever preset moved into that slot.
#
# LEAVE THIS UNSET TO KEEP THE MEDIA CONTROLS. Buttons and the play/pause +
# skip row cannot share the screen -- they want the same band of pixels, and
# the media row wins every overlapping tap -- so this setting chooses between
# them: unset means media controls (the default), and naming any preset
# trades them for a button row laid out like denon/minidsp. See
# wiim_ui.py's _media_row_active() for the geometry, and
# docs/device-drivers.md for why WiiM is the first backend to have to choose.
#
# Names that aren't in WIIM_PRESET_NAMES, or are repeated, are dropped with a
# boot print rather than failing the boot.
#
# Four, NOT dial_ui._DBTN_MAX (which is 5). _DBTN_MAX is how many label slots
# are pre-allocated, not how many fit: measured 2026-08-06, five buttons span
# 166px across a row that has only 138px clear of the gauge arc's inner edge,
# so the outer two draw on top of the arc band. Four spans 130px and fits.
# CAMILLADSP_QUICK_PRESETS already caps at 4 for the same reason.
#
# Duplicated rather than imported because config.py is imported by every
# backend at boot and must not pull in the whole display stack to read one
# integer -- dial_ui.py independently clamps drawing and hit-testing to its
# own _DBTN_MAX, so that clamp is the safety net; this is the one that keeps
# the row inside the arc.
_DBTN_BUTTON_CAP = 4


def _parse_wiim_preset_buttons(raw, presets):
    """Resolve WIIM_PRESET_BUTTONS names to 1-based positions into `presets`.

    Returns positions rather than names because the position IS the WiiM
    Favorite number MCUKeyShortClick:<n> takes -- the name is the config
    surface, the number is the protocol value. Same resolve-at-import shape
    as _parse_camilladsp_quick_presets(), so wiim.py's get_quick_presets()
    stays a pure lookup with no validation of its own.

    Empty/unset returns nothing, which is what keeps the media controls --
    see the note above. There is deliberately no separate "off" value (an
    earlier draft took "0"/"none"): unset already means off, so a second
    spelling of the default would be config surface that can never change
    any behavior.
    """
    raw = raw.strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in presets:
            print("WIIM_PRESET_BUTTONS: name {!r} not in WIIM_PRESET_NAMES "
                  "-- ignoring".format(name))
            continue
        pos = presets.index(name) + 1
        if pos in out:
            print("WIIM_PRESET_BUTTONS: duplicate name {!r} -- ignoring".format(name))
            continue
        out.append(pos)
    if len(out) > _DBTN_BUTTON_CAP:
        print("WIIM_PRESET_BUTTONS: only", _DBTN_BUTTON_CAP, "buttons fit, dropping",
              [presets[p - 1] for p in out[_DBTN_BUTTON_CAP:]])
        out = out[:_DBTN_BUTTON_CAP]
    return out


WIIM_PRESET_BUTTONS = _parse_wiim_preset_buttons(
    os.getenv("WIIM_PRESET_BUTTONS", ""), WIIM_PRESET_NAMES)

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
# updates deloop's own app files from a flat S3 layout, never CircuitPython
# firmware itself. Manual only: nothing here runs in the background -- the
# Update menu's "Check Now"/"Install Update" are the only entry points. See
# README.md's "Updating deloop" section.
OTA_ENABLED            = _get_bool("OTA_ENABLED", True)
# CI-side only (release.yml/tools/probe_ota.py) -- the changelog/history
# GitHub Release published alongside each version. The device itself never
# reads this; see OTA_S3_BASE below.
OTA_REPO               = os.getenv("OTA_REPO", "anotherhobby/deloop")
# Bucket the device fetches releases from -- config.OTA_S3_BASE + "/latest.json"
# and ".../vN/<file>" (see ota.py's _s3_url()), a flat layout with no
# redirects: every request lands on this one host directly.
OTA_S3_BASE            = os.getenv("OTA_S3_BASE", "https://deloop.s3.us-east-1.amazonaws.com")
OTA_CHECK_TIMEOUT_MS   = int(os.getenv("OTA_CHECK_TIMEOUT", "10000"))
# Per-file timeout for a real install's sequential downloads -- real
# hardware measurements land well under this (largest file, ~14KB, took
# ~12s including sha256 verification), so this is comfortable headroom,
# not a tight budget.
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
# An ACCEL_SAFETY_CAP lived here until 2026-08-06 -- a ceiling a fast upward
# spin couldn't cross without slowing down, meant to prevent accidental
# blasting. Nothing ever read it: apply_volume_delta() clamps to VOLUME_MIN/
# VOLUME_MAX and nothing else, and it had been inert since the first POC
# snapshot. Removed rather than implemented, on the maintainer's call that the
# protection isn't needed now that the render loop is fast enough for a quick
# spin to stay controllable (see docs/rendering.md's 2026-08-05 rewrite).

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
