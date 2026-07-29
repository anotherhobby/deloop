# ha.py -- Home Assistant media_player REST client
#
# Unlike denon.py, deloop never talks to the AVR (or whatever the entity
# actually is) directly here -- this drives any media_player entity through
# a Home Assistant instance reachable over the LAN, using HA's REST API
# with a long-lived access token. Same adaptive-poll model as denon.py and
# minidsp.py: get_status() is a plain synchronous GET, called on the same
# schedule as the other backends. No event subscription, no persistent
# connection -- deliberately kept as simple as the other two drivers.
#
# Confirmed against a live instance (2026-07-28), see tools/probe_ha.py:
#
#   Status:  GET  /api/states/{entity_id}
#            -> {"state": "on"/"off"/..., "attributes": {
#                 "volume_level": 0.0-1.0, "is_volume_muted": bool,
#                 "source": str, "source_list": [str, ...],
#                 "supported_features": int}}
#   Control: POST /api/services/media_player/{service}
#            body: {"entity_id": entity_id, ...service-specific fields}
#            services used: volume_set, volume_mute, turn_on, turn_off,
#            select_source
#
# Both need `Authorization: Bearer {token}` + `Content-Type: application/json`.
#
# volume_level is always a normalized 0.0-1.0 fraction -- HA has no concept
# of a device's real dB scale for a generic media_player entity, so this
# driver maps it to a plain 0-100 percent range via config.VOLUME_MIN/MAX
# (same trick minidsp.py uses to reuse the dB-shaped gauge for attenuation-dB).
#
# supported_features is a media_player-domain bitmask (stable across HA
# versions) used to auto-detect capabilities, the same live-detection
# pattern minidsp.py uses for CAPS["preset_enable"]:
#   SUPPORT_VOLUME_SET=4  SUPPORT_VOLUME_MUTE=8  SUPPORT_TURN_ON=128
#   SUPPORT_TURN_OFF=256  SUPPORT_SELECT_SOURCE=2048
# CAPS["presets"] is always False -- no generic media_player equivalent of
# Dirac Live/config-slot switching worth building; deliberately not wired
# to SUPPORT_SELECT_SOUND_MODE even though HA reports it.

import config

CAPS = {"power": False, "input_select": False, "presets": False,
        "preset_enable": False, "preset_select_enables": False}
LABELS = {"input_select": "Source"}

_SUPPORT_TURN_ON      = 128
_SUPPORT_TURN_OFF     = 256
_SUPPORT_SELECT_SOURCE = 2048

_session = None
_BASE    = "http://{}:{}".format(config.HA_HOST, config.HA_PORT)
_TIMEOUT = config.HA_TIMEOUT_MS // 1000
_HEADERS = {
    "Authorization": "Bearer " + config.HA_TOKEN,
    "Content-Type": "application/json",
}


def init(session):
    """Supply the HTTP session. Must be called before any other function."""
    global _session
    _session = session


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_state():
    """GET the entity's current state+attributes. Raises on network failure."""
    url = _BASE + "/api/states/" + config.HA_ENTITY_ID
    resp = _session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    try:
        return resp.json()
    finally:
        resp.close()


def _call_service(domain, service, data):
    """POST a media_player service call. Response discarded -- optimistic
    UI already covers the round trip, same as minidsp.py's _post_master."""
    url = _BASE + "/api/services/{}/{}".format(domain, service)
    body = {"entity_id": config.HA_ENTITY_ID}
    body.update(data)
    resp = _session.post(url, json=body, headers=_HEADERS, timeout=_TIMEOUT)
    resp.close()


# ---------------------------------------------------------------------------
# Public API -- volume / mute / power / status
# ---------------------------------------------------------------------------

def get_status():
    """Poll HA for the entity's current state.

    Returns a dict shaped like denon.get_status() -- volume_db is actually
    a 0-100 percent value here (see module docstring). Raises on network
    failure or if the entity reports "unavailable"/"unknown" -- treated as
    a connection error rather than displayed as a real state, same
    _MAX_ERRORS/MODE_ERROR path as the other backends.

    Also includes "media_state" -- the raw HA state string (e.g. "on",
    "playing", "paused", "idle"), an optional key beyond the base driver
    contract that dial_ui.py/code.py use to show a play/pause status line +
    touch target only when it's actually "playing"/"paused" (see
    state.py's apply_status(), which defaults it to "" for backends that
    don't return this key at all). Gated by config.HA_MEDIA_CONTROLS --
    reported as "" (same as "on"/idle/anything else) when that's off, so
    the status line/tap target never appear at all rather than appearing
    but silently doing nothing.
    """
    body = _get_state()
    state = body.get("state", "unknown")
    if state in ("unavailable", "unknown"):
        raise RuntimeError("ha: entity state is {}".format(state))

    attrs = body.get("attributes", {})
    volume_level = attrs.get("volume_level")
    volume_db = round(volume_level * 100.0, 1) if volume_level is not None else config.VOLUME_MIN

    return {
        "volume_db":   volume_db,
        "muted":       bool(attrs.get("is_volume_muted", False)),
        "power":       "STANDBY" if state == "off" else "ON",
        "input":       attrs.get("source") or "",
        "media_state": state if config.HA_MEDIA_CONTROLS else "",
    }


def set_volume(pct):
    pct = max(config.VOLUME_MIN, min(config.VOLUME_MAX, float(pct)))
    _call_service("media_player", "volume_set", {"volume_level": pct / 100.0})


def mute_on():
    _call_service("media_player", "volume_mute", {"is_volume_muted": True})


def mute_off():
    _call_service("media_player", "volume_mute", {"is_volume_muted": False})


def power_on():
    _call_service("media_player", "turn_on", {})


def power_standby():
    _call_service("media_player", "turn_off", {})


# ---------------------------------------------------------------------------
# Media playback (play/pause) -- see get_status()'s "media_state" key.
# Confirmed working services against a live instance: media_pause/media_play
# flip the entity's "state" between "playing"/"paused" as expected.
# ---------------------------------------------------------------------------

def media_play():
    _call_service("media_player", "media_play", {})


def media_pause():
    _call_service("media_player", "media_pause", {})


# ---------------------------------------------------------------------------
# Input (source) selection
# ---------------------------------------------------------------------------

_source_list = []   # [(name, name), ...] -- HA source names are already friendly


def load_source_list():
    """Fetch the entity's current state once at boot: populates the source
    list and derives CAPS["power"]/CAPS["input_select"] from
    supported_features (see module docstring). Silently does nothing on
    network failure -- CAPS stays at its conservative False defaults and
    the corresponding menu items/gestures just don't appear.
    """
    global _source_list
    try:
        body = _get_state()
    except Exception:
        return

    attrs = body.get("attributes", {})
    features = attrs.get("supported_features") or 0
    CAPS["power"] = bool(features & _SUPPORT_TURN_ON) and bool(features & _SUPPORT_TURN_OFF)

    names = attrs.get("source_list") or []
    CAPS["input_select"] = bool(features & _SUPPORT_SELECT_SOURCE) and bool(names)
    _source_list = [(name, name) for name in names]


def get_inputs():
    """Return (current_source, [(name, name), ...])."""
    try:
        current = _get_state().get("attributes", {}).get("source") or ""
    except Exception:
        current = ""
    return current, list(_source_list)


def set_input(name):
    _call_service("media_player", "select_source", {"source": name})
