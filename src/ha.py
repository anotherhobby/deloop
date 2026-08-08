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
#
# CAPS["player_select"] is always True -- discovering every media_player
# entity HA knows about and letting the user switch which one deloop
# targets is a backend-level feature, not something that depends on which
# entity happens to be active. Discovery uses POST /api/template (a single
# compact Jinja render) instead of GET /api/states (which would return
# every entity in the house, not just media players -- a real payload/heap
# risk on an ESP32):
#
#   POST /api/template
#   {"template": "{{ {\"ids\": states.media_player | map(attribute=\"entity_id\") | list,
#                      \"names\": states.media_player | map(attribute=\"name\") | list} | tojson }}"}
#   -> a JSON *string* body, but Content-Type: text/plain -- confirmed live,
#      so this must be parsed via json.loads(resp.text), not resp.json()
#      (which may care about content-type).
#
# Switching the active entity (set_player()) re-runs the same
# supported_features detection load_source_list() does at boot, for the
# newly selected entity -- CAPS actually changes at runtime for this
# backend, not just once at boot (see driver.py's contract note on this).

import json

import config

CAPS = {"power": False, "input_select": False, "presets": False,
        "preset_enable": False, "preset_select_enables": False,
        "player_select": True}
LABELS = {"input_select": "Source", "player_select": "Media Player"}

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

# The entity deloop currently targets -- starts at the configured default,
# but set_player() can repoint it at any entity load_players() discovered.
_current_entity = config.HA_ENTITY_ID


def init(session):
    """Supply the HTTP session. Must be called before any other function."""
    global _session
    _session = session


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_state():
    """GET the current entity's state+attributes. Raises on network failure."""
    url = _BASE + "/api/states/" + _current_entity
    resp = _session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    try:
        return resp.json()
    finally:
        resp.close()


def _call_service(domain, service, data):
    """POST a media_player service call against the current entity.
    Response discarded -- optimistic UI already covers the round trip,
    same as minidsp.py's _post_master."""
    url = _BASE + "/api/services/{}/{}".format(domain, service)
    body = {"entity_id": _current_entity}
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
    contract that dial_ui.py/app.py use to show a play/pause status line +
    touch target only when it's actually "playing"/"paused" (see
    state.py's apply_status(), which defaults it to "" for backends that
    don't return this key at all).

    That state check is the whole gate, and it needs no setting alongside
    it: an entity with nothing to pause reports "on" (or "idle"), never
    "playing", so the controls simply never draw for it.
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
        "media_state": state,
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


def media_previous():
    _call_service("media_player", "media_previous_track", {})


def media_next():
    _call_service("media_player", "media_next_track", {})


# ---------------------------------------------------------------------------
# Input (source) selection
# ---------------------------------------------------------------------------

_source_list = []   # [(name, name), ...] -- HA source names are already friendly


def _refresh_entity():
    """Query the current entity and derive CAPS["power"]/CAPS["input_select"]
    + the source list from its supported_features (see module docstring).
    Shared by load_source_list() (boot) and set_player() (runtime switch) --
    the entity's capabilities can genuinely differ from one media_player to
    another, so this needs to re-run every time the target changes, not
    just once at boot. Leaves CAPS at whatever it was on failure
    (conservative False defaults at boot; the prior entity's values if a
    switch's refresh fails) -- but PRINTS, it does not fail silently.

    That print matters more than it looks. This is the only thing that ever
    populates _source_list, so if it fails at boot the Source menu is empty
    for the rest of the session, and the next successful run only happens
    when the user switches media player. A silent return made that failure
    mode indistinguishable from "this entity has no sources" -- no log line,
    no screen change, just a menu that does nothing.
    """
    global _source_list
    try:
        body = _get_state()
    except Exception as e:
        print("ha: _refresh_entity failed for", _current_entity, "--", e)
        return

    attrs = body.get("attributes", {})
    if not attrs:
        # A 404 for an unknown entity_id still returns valid JSON
        # ({"message": "Entity not found."}), so it lands here rather than in
        # the except above -- worth naming, since a typo'd or renamed
        # HA_ENTITY_ID looks exactly like a device with no capabilities.
        print("ha: no attributes for", _current_entity,
              "-- check HA_ENTITY_ID; HA said:", body.get("message", body))
        return

    features = attrs.get("supported_features") or 0
    CAPS["power"] = bool(features & _SUPPORT_TURN_ON) and bool(features & _SUPPORT_TURN_OFF)

    names = attrs.get("source_list") or []
    CAPS["input_select"] = bool(features & _SUPPORT_SELECT_SOURCE) and bool(names)
    _source_list = [(name, name) for name in names]
    print("ha:", _current_entity, "features", features,
          "-- power", CAPS["power"], "input_select", CAPS["input_select"],
          "sources", len(names))


def load_source_list():
    """Fetch the boot-time entity's current state once at boot -- see
    _refresh_entity()."""
    _refresh_entity()


def get_inputs():
    """Return (current_source, [(name, name), ...])."""
    try:
        current = _get_state().get("attributes", {}).get("source") or ""
    except Exception:
        current = ""
    return current, list(_source_list)


def set_input(name):
    _call_service("media_player", "select_source", {"source": name})


# ---------------------------------------------------------------------------
# Media player discovery/switching -- see module docstring for the
# /api/template discovery call and why CAPS is genuinely dynamic here.
# ---------------------------------------------------------------------------

_players = []   # [(entity_id, friendly_name), ...]

_DISCOVER_TEMPLATE = (
    '{{ {"ids": states.media_player | map(attribute="entity_id") | list,'
    ' "names": states.media_player | map(attribute="name") | list} | tojson }}'
)


def load_players():
    """Discover every media_player entity HA knows about, once at boot.

    Falls back to a single entry for the configured HA_ENTITY_ID on any
    failure (bad response, network error, empty result) -- guarantees
    _players is never empty, so the Media Player submenu always has at
    least one selectable item instead of a blank list that would divide
    by zero when the menu tries to navigate it.
    """
    global _players
    try:
        url = _BASE + "/api/template"
        resp = _session.post(url, json={"template": _DISCOVER_TEMPLATE},
                              headers=_HEADERS, timeout=_TIMEOUT)
        try:
            # Content-Type on this endpoint is text/plain even though the
            # body is a JSON string (the template renders to one via
            # |tojson) -- parse the text directly rather than resp.json().
            data = json.loads(resp.text)
        finally:
            resp.close()
        ids   = data.get("ids") or []
        names = data.get("names") or []
        result = [(i, n) for i, n in zip(ids, names)]
    except Exception:
        result = []

    _players = result or [(config.HA_ENTITY_ID, config.HA_ENTITY_ID)]


def get_players():
    """Return (current_entity_id, [(entity_id, friendly_name), ...])."""
    return _current_entity, list(_players)


def set_player(entity_id):
    """Switch which entity deloop controls. No-op if entity_id wasn't in
    the discovered list. Re-derives CAPS/source list for the new entity --
    see _refresh_entity()."""
    global _current_entity
    if entity_id not in [i for i, _ in _players]:
        return
    _current_entity = entity_id
    _refresh_entity()
