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
#                  "preset_enable": bool, "preset_select_enables": bool}
#     Declares which optional features the device/backend actually supports.
#     code.py uses this to decide which menu items to build and whether the
#     power long-press / preset-disable gestures do anything -- it never
#     guesses based on which driver is loaded by name. "preset_enable" may
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
# Capabilities a driver doesn't support simply aren't called: the getattr
# defaults below make the unsupported functions harmless no-ops so code.py
# and dial_ui.py don't need to sprinkle CAPS checks around every call --
# only around the menu-construction logic that decides which capabilities
# to offer in the first place (see code.py's _top_menu_entries()), and
# around the preset-disable tap gesture (see CAPS["preset_enable"] above).

import config

if config.DEVICE_DRIVER == "minidsp":
    import minidsp as _impl
elif config.DEVICE_DRIVER == "ha":
    import ha as _impl
else:
    import denon as _impl

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
