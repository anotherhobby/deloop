# ha_ui.py -- UI extension paired with ha.py (see driver.py's "UI-extension
# contract" comment for the interface this implements and why it lives in
# its own conditionally-imported file rather than in dial_ui.py).
#
# Everything here is specific to backends that can target more than one
# device (CAPS["player_select"]) and/or report playback state
# (state.media_state) -- today, only ha.py. The device-name/playback-status
# row swap, and the skip-track icons flanking it, live here instead of in
# dial_ui.py's generic chrome, so a denon/minidsp build never imports (and
# never pays the compile-time heap cost of) any of it.

import dial_ui as _ui

# Skip-track icons ('<<'/'>>') flanking the Play/Pause text, same row --
# checked before dial_ui.media_status_tap() in app.py so the narrow icon
# zones take priority over that zone's own full-row "anywhere toggles
# play/pause" target rather than shrinking it.
_MEDIA_SIDE_HALF_W = 20   # tap zone half-width around each icon
_STATUS_TAP_HALF_H = 13


def _player_name(state):
    """Friendly name of the currently targeted device -- shown so it's
    never ambiguous which one you're controlling. Blank if the id somehow
    isn't in the discovered list (load_players() always keeps at least one
    entry -- see ha.py -- but the lookup fails closed rather than showing a
    raw entity id)."""
    for pid, name in state.player_names:
        if pid == state.player_id:
            return name
    return ""


def _media_side_text(state):
    """('<<', '>>') skip-track icon text when the status row is showing
    Playing/Paused, else ('', '')."""
    if state.media_state in _ui._MEDIA_STATE_TEXT:
        return "<<", ">>"
    return "", ""


def draw_status_rows(ui, state, dim_color):
    """Device name takes the upper row (always relevant -- which device
    you're controlling), Playing/Paused moves to the lower row (more
    transient) -- swapped from dial_ui's generic single-row layout.
    media_prev_tap()/media_next_tap()/media_status_tap() below hard-code
    the lower row's Y, matching wherever this puts the text they flank.

    Playing/Paused itself is a drawn icon (play triangle / pause bars),
    not font text -- see dial_ui.draw_play_pause_icon(). Only a preset
    name (never populated for HA today -- no generic media_player
    equivalent of Dirac Live/config slots, see driver.py's contract) would
    still use the text label.
    """
    # Fitted: an HA friendly_name is whatever the user called it in HA, with no
    # length limit -- "Dining Room Speakers" is 215px against this row's 172px
    # and ran under the arc on both sides before this (found live 2026-08-07).
    ui["preset"].text  = _ui._fit_preset(_player_name(state))
    ui["preset"].color = _ui._C_DIM   # match the input row's brightness,
                                       # not the near-invisible MENU-hint dim

    name = _ui._preset_name(state)
    ui["player"].text  = _ui._fit_player(name)
    ui["player"].color = dim_color
    if not name and state.media_state in _ui._MEDIA_STATE_TEXT:
        ci = _ui._GRAY if dim_color == _ui._C_BUSY else _ui._TK_L
        _ui.draw_play_pause_icon(
            ui, _ui.CX, _ui._PLAYER_NAME_Y,
            state.media_state == "playing", ci,
        )

    icon_y = _ui._PLAYER_NAME_Y
    ui["media_prev"].anchored_position = (_ui.CX - _ui._MEDIA_SIDE_X, icon_y)
    ui["media_next"].anchored_position = (_ui.CX + _ui._MEDIA_SIDE_X, icon_y)
    ui["media_prev"].text, ui["media_next"].text = _media_side_text(state)
    ui["media_prev"].color = dim_color
    ui["media_next"].color = dim_color


def media_status_tap(x, y):
    """True if (x, y) falls within the play/pause status-text row."""
    return (_ui._PLAYER_NAME_Y - _STATUS_TAP_HALF_H) <= y <= (_ui._PLAYER_NAME_Y + _STATUS_TAP_HALF_H)


def _media_side_tap(x, y, center_x):
    if not ((_ui._PLAYER_NAME_Y - _STATUS_TAP_HALF_H) <= y <= (_ui._PLAYER_NAME_Y + _STATUS_TAP_HALF_H)):
        return False
    return abs(x - center_x) <= _MEDIA_SIDE_HALF_W


def media_prev_tap(x, y):
    """True if (x, y) falls on the '<<' skip-back icon."""
    return _media_side_tap(x, y, _ui.CX - _ui._MEDIA_SIDE_X)


def media_next_tap(x, y):
    """True if (x, y) falls on the '>>' skip-forward icon."""
    return _media_side_tap(x, y, _ui.CX + _ui._MEDIA_SIDE_X)


def standby_menu_tap(x, y):
    """Hit-test for the power-off screen's centered MENU zone -- the power
    ring's hollow middle, radius matching _PWR_R_IN so the tap target fills
    that empty space edge to edge without spilling onto the ring itself."""
    dx = x - _ui.CX
    dy = y - _ui.CY
    return (dx * dx + dy * dy) <= _ui._PWR_R_IN * _ui._PWR_R_IN


def standby_menu_pos():
    """Where the MENU hint sits on the power-off screen: dead center,
    inside the power ring's hollow middle -- see standby_menu_tap()."""
    return (0.5, 0.5), (_ui.CX, _ui.CY)
