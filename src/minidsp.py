# minidsp.py -- minidsp-rs HTTP client
# (https://github.com/mrene/minidsp-rs)
#
# Unlike denon.py, deloop never talks to the DSP hardware directly here --
# minidsp-rs is a daemon that must already be running on some host machine
# with the MiniDSP attached over USB, exposing an HTTP API deloop talks to
# over the LAN. Run with no --config at all and it already binds to
# 0.0.0.0:5380 (all interfaces) by default -- a config file only takes
# effect if passed explicitly via --config, and minidsp-rs's own example
# config ships with the restrictive bind_address = "127.0.0.1:5380" active.
#
# Confirmed against minidsp-rs's daemon/src/http/mod.rs (dev branch,
# github.com/mrene/minidsp-rs) and minidsp/src/model.rs / protocol/src/*:
#
#   Status poll:  GET  /devices/{index}   -> StatusSummary JSON:
#                 {"master": {"preset", "source", "volume", "mute", "dirac"},
#                  "input_levels": [...], "output_levels": [...]}
#                 (all "master" fields are individually optional on the wire)
#   Set state:    POST /devices/{index}   body: partial MasterStatus JSON,
#                 e.g. {"volume": -15.0} or {"mute": true} or {"preset": 2} --
#                 any subset; fields left out are untouched.
#   Device info:  GET  /devices           -> [{"url", "version": {"hw_id",
#                 "fw_major", "fw_minor", "dsp_version", "serial"}, "product_name"}]
#
# Differences from the Denon driver that shape the capability flags below:
#
#   - No power/standby concept at all -- the DSP is "on" whenever the host
#     and USB link are up. get_status() always reports power="ON", and
#     CAPS["power"] is False so code.py never offers a power gesture for it.
#
#   - "Input" means the DSP's source enum (Toslink/USB/Analog/...), not a
#     renameable HDMI-style source list. There's no HTTP endpoint that
#     enumerates which sources a given unit actually has, so the available
#     list is derived from hw_id via the same table minidsp-rs itself uses
#     internally (protocol/src/source.rs Source::mapping) -- see
#     _SOURCE_MAP below. hw_id 10 additionally depends on dsp_version.
#
#   - "Presets" always means the DSP's config-slot switching (0..N-1) --
#     the same "which slot" concept regardless of the unit -- the API has
#     no endpoint reporting how many slots a given unit actually has, so N
#     comes from config.MINIDSP_PRESET_COUNT (default 4, the max across
#     MiniDSP's current lineup); set it to match your unit if it has fewer.
#
#     Whether a slot can be *disabled in place* (CAPS["preset_enable"]) is
#     a separate, auto-detected dimension: if the unit's master status
#     includes a "dirac" field at all, that field is a real Dirac Live
#     on/off toggle independent of which config slot is loaded -- tapping
#     the active slot flips it without switching slots (get_preset_enabled()
#     / set_preset_enabled()). Units with no "dirac" field have no on/off
#     concept for a config slot at all -- it's just loaded or not.
#     Note that the upstream DeviceInfo::supports_dirac() dsp_version check
#     (61/94/95/101/105) predates the Flex family (dsp_version 100) and
#     doesn't cover it -- this driver deliberately ignores that helper and
#     checks the live "dirac" field instead, which is the value minidsp-rs
#     itself actually queried from the unit.

import config

# preset_select_enables=False: `preset` (config slot) and `dirac` are
# independent fields on the wire -- switching slots must never silently
# flip `dirac` on, since a slot may deliberately want it left off (e.g. a
# headphone config with no room correction).
CAPS = {"power": False, "input_select": True, "presets": True,
        "preset_enable": False, "preset_select_enables": False}
LABELS = {"input_select": "Input", "presets": "Preset"}

_session = None
_BASE    = "http://{}:{}".format(config.MINIDSP_HOST, config.MINIDSP_PORT)
_TIMEOUT = config.MINIDSP_TIMEOUT_MS // 1000
# Config-slot/preset changes are a full DSP reconfiguration on the device
# side and minidsp-rs's POST doesn't return until it's actually done --
# needs much more headroom than the near-instant volume/mute calls. See
# config.py's MINIDSP_PRESET_TIMEOUT_MS note.
_PRESET_TIMEOUT = config.MINIDSP_PRESET_TIMEOUT_MS // 1000


def init(session):
    """Supply the HTTP session. Must be called before any other function."""
    global _session
    _session = session


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_device_index = None   # resolved lazily on first use; see _device_path()


def _list_devices():
    resp = _session.get(_BASE + "/devices", timeout=_TIMEOUT)
    try:
        return resp.json()
    finally:
        resp.close()


def _device_path():
    """Return "/devices/{index}" for the configured unit.

    If MINIDSP_SERIAL is set, resolves it to an index by matching against
    GET /devices' "version.serial" once and caches the result -- see
    config.py's note on why index order alone isn't reliable across USB
    reconnects. Falls back to MINIDSP_DEVICE_INDEX if unset, or if the
    serial isn't found in the current device list.
    """
    global _device_index
    if _device_index is not None:
        return "/devices/{}".format(_device_index)

    if not config.MINIDSP_SERIAL:
        _device_index = config.MINIDSP_DEVICE_INDEX
        return "/devices/{}".format(_device_index)

    for i, dev in enumerate(_list_devices()):
        version = dev.get("version") or {}
        if str(version.get("serial")) == str(config.MINIDSP_SERIAL):
            _device_index = i
            return "/devices/{}".format(_device_index)

    print("minidsp: MINIDSP_SERIAL {} not found in /devices, falling back "
          "to MINIDSP_DEVICE_INDEX {}".format(config.MINIDSP_SERIAL, config.MINIDSP_DEVICE_INDEX))
    _device_index = config.MINIDSP_DEVICE_INDEX
    return "/devices/{}".format(_device_index)


def _get_master():
    """GET the device's StatusSummary; returns its "master" dict."""
    url = _BASE + _device_path()
    resp = _session.get(url, timeout=_TIMEOUT)
    try:
        return resp.json().get("master", {})
    finally:
        resp.close()


def _post_master(fields, timeout=None):
    """POST a partial MasterStatus update (only the given fields change)."""
    url = _BASE + _device_path()
    resp = _session.post(url, json=fields, timeout=timeout or _TIMEOUT)
    resp.close()


def _get_device_info():
    """Return this unit's "version" dict (hw_id etc), or {}."""
    idx = int(_device_path().rsplit("/", 1)[-1])
    devices = _list_devices()
    if idx < len(devices):
        return devices[idx].get("version") or {}
    return {}


# ---------------------------------------------------------------------------
# Public API -- volume / mute / status
# ---------------------------------------------------------------------------

def get_status():
    """Poll the DSP for current state.

    Returns a dict shaped like denon.get_status() -- power is always "ON"
    since minidsp-rs has no power/standby concept (see module docstring).
    Raises OSError on network failure.
    """
    master = _get_master()
    return {
        "volume_db": master.get("volume", config.VOLUME_MIN),
        "muted":     bool(master.get("mute", False)),
        "power":     "ON",
        "input":     master.get("source") or "",
    }


def set_volume(db):
    db = max(config.VOLUME_MIN, min(config.VOLUME_MAX, float(db)))
    _post_master({"volume": db})


def mute_on():
    _post_master({"mute": True})


def mute_off():
    _post_master({"mute": False})


# ---------------------------------------------------------------------------
# Input (source) selection
# ---------------------------------------------------------------------------

# Mirrors Source::mapping() in minidsp-rs's protocol/src/source.rs --
# the source names a given hw_id actually exposes, in the DSP's own order.
#
# Values are the *exact* Rust enum variant names, not lowercase: serde's
# default enum serialization keeps the Rust identifier casing (Source's
# `strum(serialize_all = "lowercase")` attribute only affects strum's own
# Display/FromStr used for CLI parsing -- it has no effect on the serde
# derive that actually produces this JSON). Confirmed against a live
# minidsp-rs daemon: GET returns "source": "Analog", and POSTing the
# lowercase form ("analog") is rejected with a 500 ParseError
# ("unknown variant `analog`, expected one of `NotInstalled`, `Analog`,
# `Toslink`, `Spdif`, `Usb`, `Aesebu`, `Rca`, `Xlr`, `Lan`, `I2S`,
# `Bluetooth`, `Hdmi`").
_SOURCE_MAP = {
    2:  ["Toslink", "Spdif"],
    11: ["Toslink", "Spdif"],
    1:  ["Spdif", "Toslink", "Aesebu"],
    4:  ["Spdif", "Toslink", "Aesebu"],
    5:  ["Spdif", "Toslink", "Aesebu"],
    14: ["Toslink", "Spdif", "Aesebu", "Rca", "Xlr", "Usb", "Lan"],
    17: ["Toslink", "Spdif", "Aesebu", "Usb", "Lan"],
    18: ["Toslink", "Spdif", "Aesebu", "Usb", "Lan"],
    27: ["Analog", "Toslink", "Spdif", "Usb", "Bluetooth"],
    32: ["Analog", "Toslink", "Spdif", "Usb", "Hdmi"],
}
# hw_id 10 splits on dsp_version: 100/101 use one set, everything else another.
_SOURCE_MAP_HW10_LEGACY = ["Analog", "Toslink", "Usb"]
_SOURCE_MAP_HW10        = ["I2S", "Toslink", "Usb"]

_SOURCE_FRIENDLY = {
    "NotInstalled": "Not Installed", "Analog": "Analog", "Toslink": "Toslink",
    "Spdif": "S/PDIF", "Usb": "USB", "Aesebu": "AES/EBU", "Rca": "RCA",
    "Xlr": "XLR", "Lan": "LAN", "I2S": "I2S", "Bluetooth": "Bluetooth",
    "Hdmi": "HDMI",
}

_source_list = []   # [(raw_name, friendly_name), ...] for this unit


def _capitalize(s):
    """Fallback title-casing for a single word, without str.title()/
    .capitalize() -- CircuitPython's built-in str doesn't implement either
    (confirmed live: it raises "'str' object has no attribute 'title'"),
    unlike desktop Python where this bug stayed hidden through host testing."""
    return s[:1].upper() + s[1:].lower() if s else s


def load_input_names():
    """Fetch this unit's hw_id/dsp_version and derive its source list.

    Silently does nothing on network failure (falls back to raw names).
    """
    global _source_list
    info = _get_device_info()
    hw_id = info.get("hw_id")
    if hw_id is None:
        return

    if hw_id == 10:
        dsp_version = info.get("dsp_version")
        names = _SOURCE_MAP_HW10_LEGACY if dsp_version in (100, 101) else _SOURCE_MAP_HW10
    else:
        names = _SOURCE_MAP.get(hw_id, [])

    # Not `.get(name, name.title())` -- dict.get() evaluates its default
    # argument eagerly regardless of whether the key is found, so that
    # would call the unsupported method on every single name, not just
    # unrecognized ones.
    _source_list = [(name, _SOURCE_FRIENDLY.get(name) or _capitalize(name)) for name in names]


def get_inputs():
    """Return (current_source, [(source, friendly_name), ...])."""
    try:
        current = _get_master().get("source") or ""
    except Exception:
        current = ""
    return current, list(_source_list)


def set_input(source):
    _post_master({"source": source})


def friendly_input(raw_name):
    """Return the user-visible name for a raw source string."""
    if not raw_name:
        return ""
    return _SOURCE_FRIENDLY.get(raw_name) or _capitalize(raw_name)


# ---------------------------------------------------------------------------
# Presets -- config-slot switching (0..N-1), always. Whether a slot can
# also be disabled in place (independent of which slot is selected) is a
# separate dimension -- see get_preset_enabled()/set_preset_enabled() and
# the module docstring.
# ---------------------------------------------------------------------------

def get_presets():
    """Return (current_value, [(value, name), ...]) for config slots 0..N-1.

    N comes from config.MINIDSP_PRESET_COUNT -- the API has no way to
    report how many slots a given unit actually has. Also updates
    CAPS["preset_enable"] from whether this unit's status includes a
    "dirac" field at all (see get_preset_enabled()).
    """
    try:
        master = _get_master()
    except Exception:
        master = {}

    CAPS["preset_enable"] = master.get("dirac") is not None
    current = str(master.get("preset", 0))
    names = [(str(i), "Preset {}".format(i + 1))
             for i in range(config.MINIDSP_PRESET_COUNT)]
    return current, names


def set_preset(value):
    """Switch to config slot `value`. Leaves the Dirac on/off state (if any)
    untouched -- see set_preset_enabled() for that independent dimension.
    Uses _PRESET_TIMEOUT -- this is a full DSP reconfiguration, not instant."""
    _post_master({"preset": int(value)}, timeout=_PRESET_TIMEOUT)


def get_preset_enabled():
    """True if Dirac Live is engaged, or always True on units with no
    "dirac" field (config slots there have no on/off concept to report)."""
    try:
        dirac = _get_master().get("dirac")
    except Exception:
        return True
    return True if dirac is None else bool(dirac)


def set_preset_enabled(enabled):
    """Toggle Dirac Live on/off without changing which config slot is active.
    Uses _PRESET_TIMEOUT -- confirmed this can take several seconds too."""
    _post_master({"dirac": bool(enabled)}, timeout=_PRESET_TIMEOUT)
