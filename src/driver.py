# driver.py -- selects and re-exports the active device backend.
#
# code.py and dial_ui.py talk to this module only, never to denon.py or
# minidsp.py directly. Swapping hardware is a one-line change in
# settings.toml (DEVICE_DRIVER); adding support for a new device means
# writing a new module that matches the contract below and pointing
# DEVICE_DRIVER at it -- nothing else in the codebase should need to change.
#
# ---------------------------------------------------------------------------
# The driver contract
# ---------------------------------------------------------------------------
# Every driver module exposes:
#
#   CAPS: dict of {"power": bool, "input_select": bool, "presets": bool,
#                  "preset_enable": bool, "preset_select_enables": bool,
#                  "player_select": bool, "preset_quickbuttons": bool}
#     Declares which optional features the device/backend actually supports.
#     code.py uses this to decide which menu items to build and whether the
#     power long-press / preset-disable gestures do anything -- it never
#     guesses based on which driver is loaded by name. A backend's CAPS
#     dict only needs to set the keys it has an opinion about; this module
#     fills in every other key's safe default (see _CAPS_DEFAULTS below),
#     so denon.py/minidsp.py have no "player_select" entry at all and it's
#     still safe to read CAPS["player_select"] on them. "preset_enable" may
#     be set after init() rather than at import time, if only a live query
#     can determine it (see minidsp.py, which discovers it from the
#     device's own reported state) -- code.py only reads it after boot.
#
#     "preset_select_enables" says whether set_preset(value) alone also
#     turns the preset on, i.e. whether "which slot" and "enabled" are
#     really the same underlying dimension on this backend (Denon: yes --
#     its single DiracLive value IS the filter selection, there's no way
#     to pick a filter without engaging it) or genuinely independent
#     (MiniDSP: `preset` and `dirac` are separate fields, and a config
#     slot may deliberately want Dirac left off -- e.g. a headphone
#     preset). code.py reads this to decide whether switching slots
#     should also flip state.preset_enabled to True, or leave it alone.
#
#     "preset_quickbuttons" (default True) says whether the main-screen
#     quick-select button row (dial_ui.py's _draw_preset_filter_buttons,
#     up to _DBTN_MAX=5 pre-allocated slots) should render CAPS["presets"]'
#     list at all. Denon (2 presets) and MiniDSP (<=4) leave this at the
#     default; wiim.py sets it False because a WiiM unit's favorites list
#     can be much longer than 5 -- past that point the quick-button row's
#     fixed-slot drawing and its tap-rect math (which lays out rects for
#     the *full* list length, not just the drawn slots) go out of sync.
#     Backends with this False still get their presets via the always-
#     generic, always-scrollable Preset submenu -- see CAPS["presets"]
#     above -- just never the main-screen shortcut.
#
#   LABELS: dict of {"input_select": str, "presets": str}
#     Display label for each optional feature's top-menu entry and submenu
#     title (e.g. Denon calls its filter picker "Dirac Live"; MiniDSP calls
#     its config-slot picker "Preset"). Only consulted for capabilities the
#     driver has set True in CAPS.
#
#   init(session)                          -- must be called before anything else
#   get_status() -> {"volume_db", "muted", "power", "input"}
#   set_volume(db) / mute_on() / mute_off() -- always required
#
#   get_status()'s dict may also include an optional "media_state" key --
#   the driver's raw playback-state string (e.g. "playing"/"paused"), for
#   backends whose underlying entity can report that. There's no CAPS flag
#   for this: state.py defaults it to "" via .get() for any backend that
#   doesn't return the key at all, and dial_ui.py/code.py only ever look
#   for the exact values "playing"/"paused" -- so a backend that never sets
#   it is simply never treated as playback-capable, no special-casing
#   needed elsewhere. media_play()/media_pause() are the matching optional
#   controls, called only when state.media_state is "playing"/"paused".
#
#   power_on() / power_standby()           -- required only if CAPS["power"]
#   load_input_names() / load_source_list() / get_inputs() -> (index, [(index, name)])
#     / set_input(index) / friendly_input(raw) -- required only if CAPS["input_select"]
#   get_presets() -> (value, [(value, name)]) / set_preset(value)
#     -- required only if CAPS["presets"]. `value` always names a real,
#     selectable slot -- there is no synthetic "off" entry in the list.
#   get_preset_enabled() -> bool / set_preset_enabled(bool)
#     -- whether the currently-selected preset is actually engaged, as a
#     dimension independent of *which* slot is selected (e.g. Dirac Live
#     on/off without forgetting which filter/config was chosen). Only
#     meaningful if CAPS["preset_enable"] is True; get_preset_enabled()
#     should still return a sane default (True) otherwise.
#
#   load_players() / get_players() -> (current_entity_id, [(entity_id, name)])
#     / set_player(entity_id) -- required only if CAPS["player_select"].
#     Lets a backend expose more than one controllable target discovered at
#     runtime (ha.py: every media_player entity in the Home Assistant
#     instance) instead of one fixed device. Unlike every other CAPS flag,
#     which is decided once (at import time or from one boot-time query)
#     and never revisited, a backend implementing this is expected to
#     re-derive its *other* CAPS entries (and anything else that varies per
#     target, like the input list) inside set_player() itself, since two
#     targets discovered this way can genuinely have different
#     capabilities -- see ha.py's set_player()/_refresh_entity(). This
#     works with zero changes to code.py's menu-building: CAPS is the same
#     dict object every reader sees (`driver.CAPS is _impl.CAPS`), and
#     code.py's menu functions already read it fresh on every render rather
#     than caching a snapshot from boot.
#   media_previous() / media_next() -- optional, alongside media_play()/
#     media_pause() above; called under the same "media_state is playing/
#     paused" condition, no separate CAPS flag.
#
# Capabilities a driver doesn't support simply aren't called: the getattr
# defaults below make the unsupported functions harmless no-ops so code.py
# and dial_ui.py don't need to sprinkle CAPS checks around every call --
# only around the menu-construction logic that decides which capabilities
# to offer in the first place (see code.py's _top_menu_entries()), and
# around the preset-disable tap gesture (see CAPS["preset_enable"] above).
#
# ---------------------------------------------------------------------------
# The UI-extension contract (optional, paired by convention with a driver)
# ---------------------------------------------------------------------------
# A backend with UI needs beyond dial_ui.py's generic gauge/volume/menu
# chrome gets its own paired <backend>_ui.py (e.g. ha_ui.py), imported here
# -- and ONLY here, exactly like the backend module itself -- so a backend
# without one (denon, minidsp: neither needs any UI beyond the generic
# chrome) never pays to import or compile it. This is the actual point:
# CircuitPython compiles a module's entire bytecode before running any of
# it, so an unimported file costs nothing, while an imported-but-unused one
# still costs full compile-time heap -- see local/agent/project-context.md's
# 2026-07-29 incident, where this module-bulk cost (not WiFi) was the real
# root cause of a boot-time MemoryError.
#
# ui_impl is None for a backend with no UI extension. dial_ui.py checks for
# that and calls through it -- see e.g. dial_ui.media_prev_tap(), which is
# a real, always-present function in dial_ui.py's public API regardless of
# backend, but only ever returns True by delegating to ui_impl when one is
# loaded. A <backend>_ui.py module exposes whatever subset of this it needs
# (nothing is required unconditionally, mirroring CAPS/LABELS above):
#
#   draw_status_rows(ui, state, dim_color)
#     Replaces dial_ui's default single-row preset/status rendering.
#   media_status_tap(x, y) / media_prev_tap(x, y) / media_next_tap(x, y)
#     Hit-tests for the play/pause row and its flanking skip icons.
#   standby_menu_tap(x, y) / standby_menu_pos() -> (anchor_point, position)
#     Hit-test and label placement for the power-off screen's MENU zone,
#     only relevant for backends that can reach the menu from standby
#     (CAPS["player_select"]).
#
# wiim_ui.py is the minimal-case example: it implements only
# draw_status_rows/media_status_tap/media_prev_tap/media_next_tap (no
# standby_menu_* -- wiim.py's CAPS["player_select"] is False, so app.py
# never calls those on it regardless of whether they exist).

import config

if config.DEVICE_DRIVER == "minidsp":
    import minidsp as _impl
    _ui_impl = None
elif config.DEVICE_DRIVER == "ha":
    import ha as _impl
    import ha_ui as _ui_impl
elif config.DEVICE_DRIVER == "wiim":
    import wiim as _impl
    import wiim_ui as _ui_impl
else:
    import denon as _impl
    _ui_impl = None

ui_impl = _ui_impl

# Every CAPS key this contract defines, with its safe "unsupported" default.
# A backend's own CAPS dict only needs to set the keys it actually has an
# opinion about -- anything it leaves out is filled in here rather than
# requiring every backend file to be touched each time a new key is added
# (see e.g. "player_select", which only ha.py cares about). setdefault()
# mutates _impl.CAPS in place rather than building a new dict, preserving
# the "driver.CAPS is _impl.CAPS" identity a backend like ha.py depends on
# to mutate its own capabilities live (see the "player_select" note below)
# and have every reader see it immediately.
_CAPS_DEFAULTS = {
    "power": False, "input_select": False, "presets": False,
    "preset_enable": False, "preset_select_enables": False,
    "player_select": False, "preset_quickbuttons": True,
}
for _key, _default in _CAPS_DEFAULTS.items():
    _impl.CAPS.setdefault(_key, _default)

CAPS   = _impl.CAPS
LABELS = _impl.LABELS

init       = _impl.init
get_status = _impl.get_status
set_volume = _impl.set_volume
mute_on    = _impl.mute_on
mute_off   = _impl.mute_off

power_on      = getattr(_impl, "power_on", lambda: None)
power_standby = getattr(_impl, "power_standby", lambda: None)

media_play  = getattr(_impl, "media_play", lambda: None)
media_pause = getattr(_impl, "media_pause", lambda: None)

load_input_names = getattr(_impl, "load_input_names", lambda: None)
load_source_list  = getattr(_impl, "load_source_list", lambda: None)
get_inputs        = getattr(_impl, "get_inputs", lambda: ("", []))
set_input         = getattr(_impl, "set_input", lambda index: None)
friendly_input    = getattr(_impl, "friendly_input", lambda raw: raw)

get_presets = getattr(_impl, "get_presets", lambda: ("", []))
set_preset  = getattr(_impl, "set_preset", lambda value: None)

get_preset_enabled = getattr(_impl, "get_preset_enabled", lambda: True)
set_preset_enabled = getattr(_impl, "set_preset_enabled", lambda enabled: None)

load_players = getattr(_impl, "load_players", lambda: None)
get_players  = getattr(_impl, "get_players", lambda: ("", []))
set_player   = getattr(_impl, "set_player", lambda entity_id: None)

media_previous = getattr(_impl, "media_previous", lambda: None)
media_next     = getattr(_impl, "media_next", lambda: None)
