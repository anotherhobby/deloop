# wiim_ui.py -- minimal UI extension paired with wiim.py (see driver.py's
# "UI-extension contract" comment for the interface this implements).
#
# Unlike ha_ui.py, this backend never targets more than one device
# (CAPS["player_select"] is False), so there's no standby-screen MENU
# access to implement -- but it DOES need a real two-row split, for a
# reason ha_ui.py never hit: WiiM can have a real preset name AND active
# playback at the same time, and dial_ui.py's default single-slot
# precedence (dial_ui._status_line() -- preset name, else play/pause word,
# picking one to show) would mean losing one of them constantly, not just
# in an edge case. So this mirrors ha_ui.py's row-split structure --
# persistent "identity" info on the upper row, transient status below --
# just with preset name/placeholder instead of a device name on top:
#   PRESET_NAME_Y  (ui["preset"]): preset name, or "(Preset)" placeholder
#                   while CAPS["presets"] is true and nothing's picked yet
#                   (a WiiM preset is a one-shot MCUKeyShortClick action,
#                   not a persistent mode, so state.preset stays "" until
#                   actually chosen -- blank there reads as broken, not
#                   "nothing chosen").
#   _icon_row_y() (icon draw, not ui["player"] text): play/pause icon via
#                   dial_ui.draw_play_pause_icon() -- same helper ha_ui.py
#                   uses -- flanked by the skip icons. Deliberately its own
#                   function, not dial_ui._PLAYER_NAME_Y -- that constant is
#                   shared with ha_ui.py's device-name row, so moving it to
#                   reposition WiiM's icon row would also shift HA's layout.
#                   Positioned to match the existing vertical rhythm (the
#                   same gap already used between the volume number and the
#                   preset row, reapplied below it) rather than centered in
#                   the leftover space toward the MENU hint -- see
#                   _icon_row_y()'s docstring for the exact derivation.

import dial_ui as _ui
import driver as _driver

_MEDIA_SIDE_HALF_W = 20   # tap zone half-width around each icon, matches ha_ui.py
_STATUS_TAP_HALF_H = 13


def _icon_row_y():
    """Computed lazily, not as a module-level constant -- driver.py ->
    wiim_ui.py -> dial_ui.py -> driver.py is a circular import (see the
    "Note for future backend work" in .claude/CLAUDE.md's WiiM section),
    and dial_ui.PRESET_NAME_Y doesn't exist yet on a partially-initialized
    dial_ui module. That's harmless as long as nothing reads it until
    dial_ui.py has fully finished executing -- true for every *function*
    call in this module (real boot order: app.py imports driver, which
    finishes importing dial_ui as a side effect, before app.py's own
    `import dial_ui` line ever runs) -- but reading it at wiim_ui.py's own
    module level broke exactly this way under tools/dial_sim.py, which
    imports dial_ui directly and hits the circular chain from the other
    direction (dial_ui -> driver -> wiim_ui -> dial_ui, mid-import).

    +34 matches the existing vertical rhythm rather than centering in the
    remaining space below (confirmed too far down, live) -- measured via
    tools/dial_sim.py's font metrics: the gap between the volume number's
    bottom edge and the preset row's top edge is 16px. Reapplying that same
    16px gap below the preset row's own bottom edge (PRESET_NAME_Y + ~10,
    half its own line height, since it's center-anchored) and then
    centering the play/pause icon there (_ICON_HALF_H = 8) lands the
    icon's center at PRESET_NAME_Y + 10 + 16 + 8 = PRESET_NAME_Y + 34.
    """
    return _ui.PRESET_NAME_Y + 34


def _preset_slot_text(state):
    name = _ui._preset_name(state)
    if name:
        return name
    if _driver.CAPS["presets"]:
        return "(Set Preset)"
    return ""


def _media_row_active():
    """False when quick-select buttons own the space the media row needs.

    Preset buttons and the media row cannot coexist: the button row occupies
    y=156..178 (dial_ui._DBTN_Y0 + _DBTN_H) and _icon_row_y() puts the
    play/pause icon at 164..180, with the "<<"/">>" labels taller still. The
    18px left between the buttons and the MENU roof (y=196) does not fit a
    20px font row, so there is no "move it down" that works -- and the two
    tap zones would overlap regardless, with _dispatch_tap checking media
    first and silently swallowing button taps.

    So it is either/or, and the media row is the default: leave
    WIIM_PRESET_BUTTONS unset and WiiM keeps play/pause and skip, exactly as
    it behaved before quick buttons existed; name any position in it and
    WiiM lays out like denon/minidsp instead (preset name over a button row),
    giving up the media row for it. WiiM is the first backend to want both --
    ha.py has media controls but no presets, and denon/minidsp/camilladsp
    have presets but no media row -- which is why nothing before this had to
    choose.
    """
    return not _driver.CAPS["preset_quickbuttons"]


def draw_status_rows(ui, state, dim_color):
    ui["preset"].text  = _ui._fit_preset(_preset_slot_text(state))
    ui["preset"].color = dim_color
    ui["player"].text  = ""
    ui["player"].color = _ui._C_MENU

    if not _media_row_active():
        # dial_ui._draw_status_rows() already blanked the play/pause icon
        # before delegating here, so leaving it undrawn is the whole job.
        ui["media_prev"].text = ui["media_next"].text = ""
        return

    if state.media_state in _ui._MEDIA_STATE_TEXT:
        ci = _ui._GRAY if dim_color == _ui._C_BUSY else _ui._TK_L
        _ui.draw_play_pause_icon(
            ui, _ui.CX, _icon_row_y(),
            state.media_state == "playing", ci,
        )

    icon_y = _icon_row_y()
    ui["media_prev"].anchored_position = (_ui.CX - _ui._MEDIA_SIDE_X, icon_y)
    ui["media_next"].anchored_position = (_ui.CX + _ui._MEDIA_SIDE_X, icon_y)
    prev_txt, next_txt = ("<<", ">>") if state.media_state in _ui._MEDIA_STATE_TEXT else ("", "")
    ui["media_prev"].text, ui["media_next"].text = prev_txt, next_txt
    ui["media_prev"].color = dim_color
    ui["media_next"].color = dim_color


def media_status_tap(x, y):
    """True if (x, y) falls within the play/pause icon row (_icon_row_y())."""
    if not _media_row_active():
        return False
    return (_icon_row_y() - _STATUS_TAP_HALF_H) <= y <= (_icon_row_y() + _STATUS_TAP_HALF_H)


def _media_side_tap(x, y, center_x):
    # Must match draw_status_rows(): app.py's _dispatch_tap checks the media
    # taps BEFORE the quick-button row, so claiming this zone while the
    # buttons are drawn would eat every button tap (the two zones overlap --
    # see _media_row_active()).
    if not _media_row_active():
        return False
    if not ((_icon_row_y() - _STATUS_TAP_HALF_H) <= y <= (_icon_row_y() + _STATUS_TAP_HALF_H)):
        return False
    return abs(x - center_x) <= _MEDIA_SIDE_HALF_W


def media_prev_tap(x, y):
    """True if (x, y) falls on the '<<' skip-back icon (_icon_row_y() row)."""
    return _media_side_tap(x, y, _ui.CX - _ui._MEDIA_SIDE_X)


def media_next_tap(x, y):
    """True if (x, y) falls on the '>>' skip-forward icon (_icon_row_y() row)."""
    return _media_side_tap(x, y, _ui.CX + _ui._MEDIA_SIDE_X)
