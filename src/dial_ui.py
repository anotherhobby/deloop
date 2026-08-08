# dial_ui.py – Premium circular-gauge UI for M5Dial (240 × 240 round display)
#
# Design language: Dieter Rams / Braun industrial – minimal, purposeful.
# Black canvas, precision arc gauge with gradient colour, wedge pointer,
# clean split-size typography. Every element earns its place.
#
# Public API (app.py calls all of these; the backend UI extensions in
# ha_ui.py/wiim_ui.py reach further in -- see driver.py's UI-extension
# contract):
#   init()                              → ui dict
#   draw_main(ui, state)
#   draw_busy(ui, state)                static all-gray "please wait" frame
#   draw_volume(ui, state)              fast path – encoder ticks
#   pulse_mute(ui, elapsed_s)           mute "breathing" animation, per tick
#   mute_pulse_at_trough(elapsed_s)     phase test, no render -- call before pulse_mute
#   fade_power_off(ui, elapsed_s, from_brightness, state)   one-shot power-off ease
#   draw_status(ui, msg)               startup / progress
#   draw_error(ui, msg)
#   draw_menu(ui, title, items, cursor)
#   exit_menu(ui)
#   preset_button_at(x, y, n)          hit-test for the quick-select buttons
#   media_status_tap(x, y)             hit-test for the play/pause status row
#   media_prev_tap(x, y) / media_next_tap(x, y)  hit-test for the </>
#                                       skip-track icons flanking that row
#   menu_standby_tap(x, y)             hit-test for the centered MENU zone
#                                       on the power-off screen
#   BRIGHTNESS_ON                      constant used by app.py
#
# Screen layout:
#   Upper 75 %: circular arc gauge (8 o'clock → 4 o'clock, 240° sweep)
#   Centre:     volume number  large-int + small-decimal, mute-pulsed when muted
#   Below:      input name / preset name / quick-select preset buttons
#   Bottom:     MENU hint

import math
import time
import board
import displayio
import terminalio
from adafruit_display_text import label
from adafruit_bitmap_font import bitmap_font

import vectorio

import config
# driver is deliberately NOT imported at module level. Every function that
# needs it (media tap-zones, draw_status_rows, draw_main, draw_busy) does
# its own local `import driver as _driver`; Python caches the import, so
# this costs nothing once it's genuinely needed and keeps the cost of
# importing dial_ui.py alone down to dial_ui.py.
#
# This is not what makes OTA's memory budget work -- ota_boot.py simply
# never imports dial_ui.py, driver.py or app.py at all (see its module
# docstring). This is just tidiness that happens to compose with it.

# ── Screen ────────────────────────────────────────────────────────────────────
CX = CY = 120
_W = _H = 240

# ── Gauge arc geometry ────────────────────────────────────────────────────────
# All radii in pixels; angles in degrees clockwise from 12 o'clock.
_R_OUT      = 102   # outer radius of arc band
_R_IN       = 90    # inner radius              (band width = 12 px)
_R_PTR_TIP  = 105   # pointer tip, aimed at the outer arc edge (inward-pointing)
_R_PTR_BASE = 118   # pointer base, spread at the display edge
_PTR_HALF   = 3     # pointer half-angle in degrees (total wedge = 6°)

_R_TK_BASE  = 105   # tick marks start just outside the arc
_R_TK_MINOR = 110   # minor tick outer radius  (every 5 dB)
_R_TK_MAJOR = 114   # major tick outer radius  (every 10 dB)
_R_TK_ZERO  = 117   # 0 dB tick outer radius   (longest + brightest)

_ARC_START  = 240.0  # 8 o'clock
_ARC_SWEEP  = 240.0  # total span → ends at 4 o'clock

# Gauge range comes straight from config.py -- it differs by driver (a Denon
# AVR's volume is dB-relative-to-reference; a MiniDSP's is dB-of-attenuation
# below unity; an HA media_player entity is a 0-100 percent scale with no dB
# concept at all). Color bands below are proportional fractions of this span
# rather than fixed dB thresholds, so they scale to whatever range is active.
_VOL_MIN    = config.VOLUME_MIN
_VOL_MAX    = config.VOLUME_MAX

# 0 dB is only a meaningful reference point (unity gain) when it actually
# falls inside or at the top of the range -- true for Denon (mid-range) and
# MiniDSP (0 dB = max, no attenuation), false for HA's 0-100 percent scale,
# where 0 is just the bottom of the range and has no special meaning beyond
# that (highlighting it would just double-mark the arc's own start). Derived
# from the range itself, not a driver-name check, so any future all-positive
# range gets the same treatment automatically.
_HAS_ZERO_REF = _VOL_MIN < 0.0 <= _VOL_MAX

# A fractional decimal digit is only worth showing if the configured step
# size can actually land on one -- Denon's default 0.5dB/tick genuinely
# produces values like -28.5, but HA's default 2 percent/tick never does
# (always whole numbers), making a trailing ".0" pure noise. Derived from
# config.VOLUME_STEP, not a driver-name check, same reasoning as
# _HAS_ZERO_REF above -- a backend configured with a fractional step (of
# any kind) gets the decimal back automatically.
_SHOW_DECIMAL = (config.VOLUME_STEP % 1.0) != 0.0

# ── Power button geometry ─────────────────────────────────────────────────────
_PWR_R_OUT  = 78    # ring outer radius
_PWR_R_IN   = 58    # ring inner radius    (ring width = 20 px)
_PWR_GAP    = 30    # half-angle of gap at 12 o'clock (degrees each side)
_PWR_STEM_TOP = CY - _PWR_R_OUT - 4   # y ≈ 38
_PWR_STEM_BOT = CY - _PWR_R_IN  + 4   # y ≈ 66

# ── Brightness ────────────────────────────────────────────────────────────────
BRIGHTNESS_ON = 1.0

# Standby screen brightness is relative to the user's own brightness setting,
# not a fixed value -- dim room, dim standby indicator; bright room, brighter
# one. Configurable via STANDBY_BRIGHTNESS_FRAC in settings.toml.
_STANDBY_BRIGHTNESS_FRAC = config.STANDBY_BRIGHTNESS_FRAC

# ── Menu geometry (exported so app.py can map touch_y → item index) ─────────
MENU_ITEM_Y0  = 44    # y-centre of first visible item
MENU_ITEM_DY  = 38    # vertical spacing between items
MENU_VISIBLE  = 5     # max items shown at once

# ── Preset quick-select button geometry ───────────────────────────────────────
# Row of small numbered buttons just below the preset name, letting a tap
# switch presets directly from the main screen instead of via the menu.
# Exported so app.py can split the main screen's tap zones: mute above this
# line, quick-select buttons (and nothing else) at or below it.
PRESET_NAME_Y = CY + 18
# Input/source name row, above the volume number -- halfway between it and the
# arc gap. Named rather than inlined at its label so _row_budget() and
# tools/font_fit.py can both refer to the same number.
_INPUT_NAME_Y = 62
# Second text row, just below PRESET_NAME_Y. Which content lands in which
# row depends on the backend -- see _draw_status_rows(): normally
# PRESET_NAME_Y holds the preset/status text and this row is unused, but
# for backends that can target more than one device (CAPS["player_select"],
# e.g. HA) the two are swapped -- the device name (always relevant) takes
# the upper PRESET_NAME_Y row, and Playing/Paused (more transient) moves
# here instead. Never collides with the button row below: no current
# backend has both CAPS["presets"] and CAPS["player_select"] true at once.
_PLAYER_NAME_Y = PRESET_NAME_Y + 24
_DBTN_Y0   = 156   # top of the button row

# MENU hint position -- bottom-anchored on the normal/error screens (a
# near-invisible tap-zone cue below everything else). On the power-off
# screen, a backend with a UI extension (driver.ui_impl) can move it
# instead -- see draw_main()'s power-off branch and ha_ui.standby_menu_pos(),
# which centers it in the power icon's hollow middle for player_select
# backends; a backend with none stays put, since standby's whole point
# otherwise is "one button, nothing else competes for attention."
_MENU_ANCHOR_MAIN    = (0.5, 1.0)
_MENU_POS_MAIN       = (CX, 222)
_DBTN_H    = 22    # button height
_DBTN_W    = 22    # button width
_DBTN_GAP  = 14    # horizontal gap between buttons
_DBTN_MAX  = 5     # pre-allocated label slots -- NOT how many actually fit.
                   # Four is the real ceiling: at this row the gauge arc's
                   # inner edge leaves 138px clear, and 4 buttons span 130px
                   # while 5 span 166px and draw over the band (measured
                   # 2026-08-06). Backends cap themselves at 4 accordingly
                   # (config.py's _DBTN_BUTTON_CAP, _parse_camilladsp_quick_
                   # presets); this stays 5 as the allocation/clamp bound.

# ── Shape palette (16 colours; every vectorio shape indexes into this one) ───
# _PRR and _BTNSEL_FILTER are aliases of _ORG, not separate slots -- give
# either its own shade only by adding a real palette entry for it.
_BG    = 0   # black background
_GRN   = 1   # green   (bottom 60% of the volume range)
_AMB   = 2   # amber   (next 10%)
_ORG   = 3   # orange  (next 10%); also power ring + selected-filter button
_RED   = 4   # red     (top 20%)
_TK_L  = 5   # tick mark (minor + major both use this)
_TK_0  = 6   # 0 dB tick (bright)
_PTR   = 7   # pointer wedge
_BLUE  = 8   # muted arc fill
_BTNSEL_OFF    = 9    # preset quick-select button: selected, but disabled
_BTNSEL_MUTED  = 10   # preset quick-select button: selected preset, muted
_GRAY  = 11  # busy arc fill -- see draw_busy()
_TK_MENU = 12  # MENU-home frame -- see _build_menu_home()
_PRR            = _ORG   # power button ring / stem
_BTNSEL_FILTER  = _ORG   # preset quick-select button: selected preset, enabled

_PALETTE = [
    0x000000,  #  0 BG
    0x009940,  #  1 GREEN
    0xBB8800,  #  2 AMBER
    0xFF5500,  #  3 ORANGE (also power ring, selected-filter button outline)
    0xAA1800,  #  4 RED
    0x474747,  #  5 tick mark
    0xDDDDDD,  #  6 zero-dB tick
    0xEEEEEE,  #  7 pointer
    0x0055BB,  #  8 blue (muted)
    0xA7A7A7,  #  9 preset button selected outline, disabled        (matches _C_BTN_SEL)
    0x2277CC,  # 10 preset button selected outline, preset + muted  (matches _C_MUTED)
    0x555555,  # 11 gray (busy -- a blocking device call is in flight)
    0x383838,  # 12 MENU-home frame -- matches _C_MENU exactly, not close to
               #    any existing entry, so the frame reads as the same
               #    barely-visible dimness as the MENU text itself
]   # 13 entries; value_count=16 → 3 spare slots

# ── Label colours ─────────────────────────────────────────────────────────────
_C_TEXT    = 0xEEEEEE
_C_DIM     = 0x606060
_C_WARN    = 0xFF5500
_C_MENU    = 0x383838   # barely-visible menu hint
_C_MUTED   = 0x2277CC   # static blue for muted state
_C_BUSY    = 0x999999   # static gray while a blocking device call is in flight
_C_BTN_SEL = 0xA7A7A7   # preset quick-select button, selected: midpoint of _C_DIM/_C_TEXT

# ── Mute pulse ────────────────────────────────────────────────────────────────
# Breathing brightness for the volume number while muted. A cosine wave
# naturally eases to a stop at both the peak and the trough -- no separate
# acceleration curve needed. The trough is also where callers should slip in
# any slow work (e.g. an AVR poll): the animation is barely moving there
# regardless, so a brief stall blends into the ease instead of reading as
# a stutter.
_PULSE_PERIOD_S  = 8    # full breathe cycle, in seconds
_PULSE_FLOOR     = 0.18   # dimmest point, as a fraction of full brightness -- never fully black
_PULSE_TROUGH_TH = 0.04   # phase fraction below which pulse_mute() reports "at trough"


def _pulse_color(frac):
    """frac 0..1 (0 = floor, 1 = full mute-blue) -> interpolated RGB."""
    level = _PULSE_FLOOR + (1.0 - _PULSE_FLOOR) * frac
    return _lerp_color(0x000000, _C_MUTED, level)

# ── Power fade ────────────────────────────────────────────────────────────────
# One-shot brightness ease when powering off, toward the static standby
# level (state.brightness * _STANDBY_BRIGHTNESS_FRAC, see draw_main). A
# continuous breathe on the icon itself was tried and dropped -- pulsing the
# icon's palette color forces a full-bitmap SPI refresh every frame (it
# lives in the same bitmap as the arc gauge), and even after switching to a
# brightness-only pulse it still read as distracting rather than restful.
_POWER_FADE_S = 1.2   # duration of the power-off brightness ease-down


def fade_power_off(ui, elapsed_s, from_brightness, state):
    """One frame of the power-off brightness ease-down, toward the same
    static standby level draw_main() settles on.

    from_brightness: display brightness at the moment the fade started.
    Returns True once the fade has reached the floor -- the caller should
    swap to the power icon.
    """
    target = state.brightness * _STANDBY_BRIGHTNESS_FRAC
    if elapsed_s >= _POWER_FADE_S:
        ui["display"].brightness = target
        return True
    ease = 0.5 * (1.0 - math.cos(math.pi * elapsed_s / _POWER_FADE_S))
    ui["display"].brightness = from_brightness - (from_brightness - target) * ease
    return False

# ── Preset quick-select buttons ───────────────────────────────────────────────
# n is however many entries state.preset_quick_names has -- the row is
# centred and evenly spaced for whatever n turns out to be, not hardcoded.

def _preset_btn_rects(n):
    """Return [(x0, y0, x1, y1), ...] for n evenly-spaced, centred buttons."""
    total_w = n * _DBTN_W + (n - 1) * _DBTN_GAP
    x_start = CX - total_w // 2
    rects = []
    for i in range(n):
        x0 = x_start + i * (_DBTN_W + _DBTN_GAP)
        rects.append((x0, _DBTN_Y0, x0 + _DBTN_W, _DBTN_Y0 + _DBTN_H))
    return rects


# The quick-button HIT target is deliberately much larger than the drawn
# button. The drawn square is _DBTN_W=22px -- about 3mm on this 240px 1.28"
# panel, well under the ~7-9mm a fingertip reliably lands on. Confirmed live
# 2026-08-04 by logging every tap's coordinate against the rects: aiming at
# the first button (centre x=84) produced taps at x=63-69 and x=96-103, all
# within ~20px of centre but all outside the 73-95 rect, so nothing happened
# and the row read as "locked out" while volume and mute kept working.
#
# Widening is free here: app.py routes the whole PRESET_NAME_Y..MENU_TAP_Y
# band (y 138-195) to this function and nothing else competes for it, so a
# tap that misses every button currently does nothing at all.
_DBTN_HIT_PAD_Y = 14                       # vertical slack around the drawn row
_DBTN_HIT_MAX_DX = _DBTN_W + _DBTN_GAP // 2  # 29px: outward reach past the end buttons


def preset_button_at(x, y, n):
    """Return the tapped quick-select button index [0..n-1], or -1.

    Nearest-button-within-tolerance rather than a strict rect test, so the
    gaps between buttons aren't dead zones -- interior boundaries land on
    the true midpoint between adjacent centres either way, while
    _DBTN_HIT_MAX_DX additionally extends the reach past the outermost
    buttons (see the note above for the measurements behind this).

    n is capped at _DBTN_MAX because that's how many buttons are actually
    drawn (_draw_preset_filter_buttons caps the same way). Without the cap a
    longer preset list hit-tests against a layout that was never rendered --
    the exact drawn-vs-tap-rect divergence driver.py's CAPS note warns about.
    """
    n = min(n, _DBTN_MAX)
    if n <= 0:
        return -1
    if not (_DBTN_Y0 - _DBTN_HIT_PAD_Y <= y <= _DBTN_Y0 + _DBTN_H + _DBTN_HIT_PAD_Y):
        return -1
    best, best_dx = -1, None
    for i, (x0, _y0, x1, _y1) in enumerate(_preset_btn_rects(n)):
        dx = abs(x - (x0 + x1) // 2)
        if best_dx is None or dx < best_dx:
            best, best_dx = i, dx
    return best if best_dx is not None and best_dx <= _DBTN_HIT_MAX_DX else -1


# Play/pause status row -- shares the same text slot as the preset name
# (see _status_line() below), only ever populated for backends that set
# state.media_state to "playing"/"paused" (currently ha.py -- see
# driver.py's contract note). Lives at _PLAYER_NAME_Y, not PRESET_NAME_Y --
# see _draw_status_rows()'s note on the swapped row order for HA.
#
# 42px is a middle ground for the skip icons' x-offset: the center content
# isn't a fixed width ("Play" the word vs "II" the pause icon -- see
# _PLAY_CHAR/_PAUSE_CHAR), and 42 was confirmed by rendering both states at
# several offsets side by side as the value that keeps "Play" from touching
# the icons without leaving "II" looking too sparse. Kept here (not in
# ha_ui.py) because init() needs it below for initial label placement --
# ha_ui.py's draw_status_rows() repositions from it every draw.
_MEDIA_SIDE_X = 42   # x-offset from center for each icon

# The actual hit-tests, row-swap logic, and standby-menu placement for
# player_select backends all live in ha_ui.py (see driver.py's
# "UI-extension contract") -- these four stay here, with these exact names,
# because they're real entries in dial_ui's public API that app.py always
# calls regardless of backend; they just no-op (return False) when no UI
# extension is loaded, instead of app.py needing to know which backends
# have one.

def media_status_tap(x, y):
    """True if (x, y) falls within the play/pause status-text row. Only
    meaningful when state.media_state is "playing"/"paused" -- see
    app.py's _tap_main_screen, which checks that first."""
    import driver as _driver
    return _driver.ui_impl is not None and _driver.ui_impl.media_status_tap(x, y)


def media_prev_tap(x, y):
    """True if (x, y) falls on the '<<' skip-back icon."""
    import driver as _driver
    return _driver.ui_impl is not None and _driver.ui_impl.media_prev_tap(x, y)


def media_next_tap(x, y):
    """True if (x, y) falls on the '>>' skip-forward icon."""
    import driver as _driver
    return _driver.ui_impl is not None and _driver.ui_impl.media_next_tap(x, y)


def menu_standby_tap(x, y):
    """True if (x, y) falls within the power-off screen's centered MENU
    zone. Only meaningful when driver.CAPS["player_select"] -- see app.py's
    _dispatch_tap, which checks that first."""
    import driver as _driver
    return _driver.ui_impl is not None and _driver.ui_impl.standby_menu_tap(x, y)

# ── Fonts ─────────────────────────────────────────────────────────────────────
_GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-. +/&()"

def _load_font(path, glyphs=None):
    try:
        f = bitmap_font.load_font(path)
        if glyphs:
            f.load_glyphs(glyphs)   # pre-cache only when explicitly requested
        return f
    except Exception:
        return terminalio.FONT

# Pre-cache only the volume digits (used every encoder tick).
# All other glyphs load lazily from flash on first render – negligible delay,
# significant RAM saving that keeps network buffers available at boot.
_F_LG = _load_font("/fonts/Inter_Medium_36.pcf", "0123456789-. ")  # volume integer
_F_MD = _load_font("/fonts/Inter_Medium_24.pcf")   # status labels, menu
_F_SM = _load_font("/fonts/Inter_Medium_20.pcf")   # volume decimal, hints

# ── Low-level drawing primitives ──────────────────────────────────────────────

def _axy(r, deg):
    """Polar → screen (x, y).  deg is degrees clockwise from 12 o'clock."""
    rad = math.radians(deg)
    return int(CX + r * math.sin(rad) + 0.5), int(CY - r * math.cos(rad) + 0.5)


def _vol_to_angle(db):
    frac = (max(_VOL_MIN, min(_VOL_MAX, db)) - _VOL_MIN) / (_VOL_MAX - _VOL_MIN)
    return _ARC_START + frac * _ARC_SWEEP


# Color band boundaries, as fractions of the [_VOL_MIN, _VOL_MAX] span --
# equivalent to -20/-10/0 dB on Denon's original -80..20 range, but
# expressed proportionally so a different range (e.g. MiniDSP's -127..0)
# still puts red at "near max" rather than at a dB value that may not
# even be reachable.
_FRAC_AMBER  = 0.60
_FRAC_ORANGE = 0.70
_FRAC_RED    = 0.80

# Amber/orange above are just gradient waypoints with no dB meaning, but
# red's start line IS meaningful whenever the range has an interior 0 dB
# reference (_HAS_ZERO_REF): 0 dB is unity/reference level, a natural
# "you're now loud" cutoff. That only lined up with the fixed 0.80 by
# coincidence for one specific range (Denon's original -80..20); it drifted
# audibly off 0 dB once VOLUME_MIN/MAX became configurable per-backend
# (and per-install, via settings.toml). Realign red's start to the actual
# 0 dB fraction instead -- but only when 0 dB sits in the range's interior,
# past the orange waypoint and short of the range's own top extreme
# (MiniDSP's 0 dB = max is already exactly the top of the arc; there's
# nothing to realign there, and forcing it would make red vanish).
if _HAS_ZERO_REF:
    _zero_frac = (0.0 - _VOL_MIN) / (_VOL_MAX - _VOL_MIN)
    if _FRAC_ORANGE < _zero_frac < 0.999:
        _FRAC_RED = _zero_frac


def _arc_color(angle):
    """Gradient palette index for an arc position (angle CW-from-top)."""
    vol  = _VOL_MIN + (angle - _ARC_START) / _ARC_SWEEP * (_VOL_MAX - _VOL_MIN)
    frac = (vol - _VOL_MIN) / (_VOL_MAX - _VOL_MIN)
    if   frac < _FRAC_AMBER:  return _GRN
    elif frac < _FRAC_ORANGE: return _AMB
    elif frac < _FRAC_RED:    return _ORG
    else:                     return _RED


# ── Shape geometry ────────────────────────────────────────────────────────────
# These return point lists for vectorio.Polygon. Nothing here rasterises:
# displayio composites the shapes in C during refresh(), so the gauge costs
# no pixel storage at all -- worth 28.8KB of heap and ~27ms a frame against
# painting it into a bitmap. See docs/rendering.md.
#
# The one hazard, and both bugs found while proving this out were it:
# THIN STROKES DO NOT SURVIVE INTEGER ROUNDING. A fractional offset that
# rounds to the same integer on both sides gives a zero-area polygon, which
# renders as nothing at all -- silently, with no error. Every helper below
# that produces a 1px feature offsets by whole pixels for that reason.

def _sector_points(a0, a1, r_in, r_out, step=6.0):
    """Annular sector: out along the outer edge, back along the inner one.

    step=6 degrees keeps the chord sagitta at r=102 under 0.6px, below where
    a flat-shaded edge starts to read as faceted.
    """
    n = max(2, int((a1 - a0) / step + 0.999))
    pts = []
    for i in range(n + 1):
        pts.append(_axy(r_out, a0 + (a1 - a0) * i / n))
    for i in range(n, -1, -1):
        pts.append(_axy(r_in, a0 + (a1 - a0) * i / n))
    return pts


def _tick_quad(angle, r0, r1, thickness):
    """Radial tick as a 4-point quad, offset along the tangent.

    Offsets are asymmetric (-t//2 and -t//2 + t) rather than +/- t/2:
    rounding a symmetric half-width outward inflates the tick by a pixel on
    each side, and rounding it inward collapses a 1px tick to zero area.
    This lands the width on exactly `thickness`.
    """
    rad = math.radians(angle)
    tx, ty = math.cos(rad), math.sin(rad)          # tangent unit vector
    o0 = -(thickness // 2)
    o1 = o0 + thickness
    ix, iy = _axy(r0, angle)
    ox, oy = _axy(r1, angle)
    return [
        (int(round(ix + tx * o0)), int(round(iy + ty * o0))),
        (int(round(ox + tx * o0)), int(round(oy + ty * o0))),
        (int(round(ox + tx * o1)), int(round(oy + ty * o1))),
        (int(round(ix + tx * o1)), int(round(iy + ty * o1))),
    ]


def _stroke_quad(x0, y0, x1, y1):
    """A 1px line as a quad, offset a WHOLE pixel along the minor axis.

    Not a unit normal: on a 45-degree segment that is (0.707, 0.707), and
    truncating it to int lands both offset points back on the originals --
    zero area, invisible. Stepping one pixel perpendicular to the dominant
    axis also matches what a Bresenham line actually produces.
    """
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) >= abs(dy):
        ox, oy = 0, 1
    else:
        ox, oy = 1, 0
    return [(int(x0), int(y0)), (int(x1), int(y1)),
            (int(x1 + ox), int(y1 + oy)), (int(x0 + ox), int(y0 + oy))]


def _pointer_points(angle):
    """Inward-pointing wedge: base spread at the display edge, tip at the arc."""
    return [
        _axy(_R_PTR_TIP,  angle),
        _axy(_R_PTR_BASE, angle - _PTR_HALF),
        _axy(_R_PTR_BASE, angle + _PTR_HALF),
    ]


def _colour_bands():
    """(a0, a1, palette_index) per gradient band, from the same fractions
    _arc_color() uses -- so the boundaries land exactly where they always
    have rather than wherever a uniform subdivision happens to put them."""
    out = []
    edges = ((0.0, _FRAC_AMBER, _GRN), (_FRAC_AMBER, _FRAC_ORANGE, _AMB),
             (_FRAC_ORANGE, _FRAC_RED, _ORG), (_FRAC_RED, 1.0, _RED))
    for f0, f1, ci in edges:
        if f1 > f0:
            out.append((_ARC_START + f0 * _ARC_SWEEP,
                        _ARC_START + f1 * _ARC_SWEEP, ci))
    return out



def _lerp_color(c0, c1, frac):
    """Interpolate two 24-bit RGB ints. frac is clamped to [0, 1]."""
    frac = max(0.0, min(1.0, frac))
    r0, g0, b0 = (c0 >> 16) & 0xFF, (c0 >> 8) & 0xFF, c0 & 0xFF
    r1, g1, b1 = (c1 >> 16) & 0xFF, (c1 >> 8) & 0xFF, c1 & 0xFF
    r = int(r0 + (r1 - r0) * frac)
    g = int(g0 + (g1 - g0) * frac)
    b = int(b0 + (b1 - b0) * frac)
    return (r << 16) | (g << 8) | b


def _tick_values():
    """dB values for tick marks, every 5 dB, anchored at 0 -- not at
    _VOL_MIN. Denon's -80..20 range is a multiple of 5 either way, but a
    range like MiniDSP's -127..0 isn't: anchoring at 0 guarantees the 0 dB
    reference tick itself always lands exactly, wherever the scale starts."""
    n_below = int((0.0 - _VOL_MIN) / 5.0 + 0.5)
    n_above = int((_VOL_MAX - 0.0) / 5.0 + 0.5)
    for i in range(-n_below, n_above + 1):
        yield i * 5.0



# ── Layer builders ────────────────────────────────────────────────────────────
# Each returns a Group so a whole layer can be shown or hidden with one flag.
# Nothing is ever erased, it is just not composited.

# Arc sectors exist only to place the colour boundaries. Subdividing further
# buys nothing -- measured 4 sectors at 1.74ms vs 24 at 1.65ms per frame, so
# there is no bounding-box culling to gain here.
_ARC_SECTORS = 12


def _build_arc(pal):
    """The gradient arc band. Returns (group, shapes, colour_indices)."""
    g = displayio.Group()
    shapes, cis = [], []
    for a0, a1, ci in _colour_bands():
        share = max(1, int(round((a1 - a0) / _ARC_SWEEP * _ARC_SECTORS)))
        for k in range(share):
            s0 = a0 + (a1 - a0) * k / share
            s1 = a0 + (a1 - a0) * (k + 1) / share
            if k < share - 1 or a1 < _ARC_START + _ARC_SWEEP:
                s1 += 0.4        # overlap the next piece; no seam shows through
            s = vectorio.Polygon(pixel_shader=pal,
                                 points=_sector_points(s0, s1, _R_IN, _R_OUT),
                                 x=0, y=0, color_index=ci)
            g.append(s)
            shapes.append(s)
            cis.append(ci)
    return g, shapes, cis


def _build_ticks(pal):
    """Tick marks just outside the arc band, every 5 dB."""
    g = displayio.Group()
    for db in _tick_values():
        angle = _vol_to_angle(db)
        if _HAS_ZERO_REF and abs(db) < 0.01:
            r_out, ci, thick = _R_TK_ZERO, _TK_0, 3
        elif round(db) % 10 == 0:
            r_out, ci, thick = _R_TK_MAJOR, _TK_L, 2
        else:
            r_out, ci, thick = _R_TK_MINOR, _TK_L, 1
        g.append(vectorio.Polygon(
            pixel_shader=pal, points=_tick_quad(angle, _R_TK_BASE, r_out, thick),
            x=0, y=0, color_index=ci))
    return g


# "Home" frame behind the MENU hint -- a roof line + two flared legs, no
# bottom edge. Meant to make the bottom MENU tap zone feel anchored/grounded
# rather than a floating label, without reading as a competing UI element --
# same barely-visible dimness as the MENU text itself (_TK_MENU == _C_MENU).
# An earlier ellipse/arc concept (a partial ring bracketing MENU, sized to
# match the preset button frame thickness) was mocked up off-device via
# tools/dial_sim.py and rejected as visual noise; this shape and its exact
# geometry were chosen the same way -- mocked up and confirmed before
# writing this real version. Legs deliberately don't close into a full
# trapezoid (no bottom line) -- confirmed live that a fully enclosed shape
# read as "boxing in" rather than "grounding."
_MENU_HOME_TOP_Y     = 196   # roof line, just below the preset button row
_MENU_HOME_BOTTOM_Y  = 236   # where the legs end, near the visible rim
_MENU_HOME_TOP_HW    = 28    # roof line half-width
_MENU_HOME_LEG_HW    = 68    # leg half-width at the bottom


def _build_menu_home(pal):
    tl = (CX - _MENU_HOME_TOP_HW, _MENU_HOME_TOP_Y)
    tr = (CX + _MENU_HOME_TOP_HW, _MENU_HOME_TOP_Y)
    br = (CX + _MENU_HOME_LEG_HW, _MENU_HOME_BOTTOM_Y)
    bl = (CX - _MENU_HOME_LEG_HW, _MENU_HOME_BOTTOM_Y)
    g = displayio.Group()
    for p0, p1 in ((tl, tr), (tr, br), (bl, tl)):
        g.append(vectorio.Polygon(
            pixel_shader=pal, points=_stroke_quad(p0[0], p0[1], p1[0], p1[1]),
            x=0, y=0, color_index=_TK_MENU))
    return g


def _build_power(pal):
    """Classic power icon: ring with a gap at 12 o'clock, plus a stem.

    The ring is one polygon rather than a scanline fill -- the gap makes it a
    simple (non-self-intersecting) outline, so it needs no special handling.
    """
    g = displayio.Group()
    g.append(vectorio.Polygon(
        pixel_shader=pal,
        points=_sector_points(_PWR_GAP, 360.0 - _PWR_GAP,
                              _PWR_R_IN, _PWR_R_OUT, step=8.0),
        x=0, y=0, color_index=_PRR))
    stem_half = (_PWR_R_OUT - _PWR_R_IN) // 2   # 10 px each side = 20 px total
    g.append(vectorio.Rectangle(
        pixel_shader=pal, width=stem_half * 2,
        height=_PWR_STEM_BOT - _PWR_STEM_TOP + 1,
        x=CX - stem_half, y=_PWR_STEM_TOP, color_index=_PRR))
    return g


def _build_presets(pal):
    """Pre-allocate _DBTN_MAX button outlines, two Rectangles each.

    An outline is a filled rect with a background-coloured rect inset inside
    it -- two shapes, versus four if each edge were its own. Positions and
    colours are set per draw by _draw_preset_filter_buttons(); the row layout
    depends on how many presets the backend actually reports.
    """
    g = displayio.Group()
    pairs = []
    for _i in range(_DBTN_MAX):
        outer = vectorio.Rectangle(pixel_shader=pal, width=_DBTN_W,
                                   height=_DBTN_H, x=0, y=_DBTN_Y0,
                                   color_index=_TK_L)
        inner = vectorio.Rectangle(pixel_shader=pal, width=_DBTN_W - 2,
                                   height=_DBTN_H - 2, x=1, y=_DBTN_Y0 + 1,
                                   color_index=_BG)
        g.append(outer)
        g.append(inner)
        pairs.append((outer, inner))
    return g, pairs


# Play/pause icon geometry -- shapes rather than a font glyph, which keeps
# the fonts at pure ASCII (see the note above _PLAY_CHAR/_PAUSE_CHAR and the
# Makefile's `fonts` target). Sized to roughly match the row's font-rendered
# text height (~16px); anything taller collides with the row above.
_ICON_HALF_H = 8    # half-height of both shapes
_ICON_HALF_W = 7    # play triangle: tip-to-base half-width
_ICON_BAR_W  = 4    # pause: width of each bar
_ICON_GAP    = 4    # pause: gap between the two bars


def _build_icon(pal):
    """Play triangle + two pause bars, all three hidden until asked for.

    Returns (group, play_shape, (bar0, bar1)). Points are local to the
    shape's own x/y so repositioning is two integer writes, not a rebuild.
    """
    g = displayio.Group()
    play = vectorio.Polygon(
        pixel_shader=pal,
        points=[(-_ICON_HALF_W, -_ICON_HALF_H), (-_ICON_HALF_W, _ICON_HALF_H),
                (_ICON_HALF_W, 0)],
        x=CX, y=CY, color_index=_TK_L)
    bars = []
    for _i in range(2):
        bars.append(vectorio.Rectangle(
            pixel_shader=pal, width=_ICON_BAR_W, height=_ICON_HALF_H * 2,
            x=CX, y=CY, color_index=_TK_L))
    g.append(play)
    g.append(bars[0])
    g.append(bars[1])
    g.hidden = True
    return g, play, bars


def draw_play_pause_icon(ui, cx, cy, playing, ci):
    """Show a play triangle or pause bars centered at (cx, cy).

    playing=True draws pause bars, not a play triangle -- a play/pause
    control always shows the action a tap performs, not the current state
    (same convention as _MEDIA_STATE_TEXT below).

    Callers are the backend UI extensions (ha_ui.py, wiim_ui.py) -- see
    driver.py's UI-extension contract. They only ever SHOW the icon; hiding
    it is _draw_status_rows()'s job, which blanks it before delegating so a
    backend that stops reporting media_state doesn't leave one stranded.
    """
    play, bars = ui["icon_play"], ui["icon_bars"]
    ui["l_icon"].hidden = False
    play.color_index = ci
    bars[0].color_index = ci
    bars[1].color_index = ci
    if playing:
        play.hidden = True
        for k, bx in enumerate((cx - _ICON_GAP // 2 - _ICON_BAR_W,
                                cx + _ICON_GAP // 2)):
            bars[k].hidden = False
            bars[k].x = bx
            bars[k].y = cy - _ICON_HALF_H
    else:
        play.hidden = False
        play.x, play.y = cx, cy
        bars[0].hidden = True
        bars[1].hidden = True


def _hide_icon(ui):
    ui["l_icon"].hidden = True


# ── Gauge scene control ───────────────────────────────────────────────────────
# No pixels are produced here: these set visibility, colour indices and the
# pointer's three points, and displayio does the rest during refresh().

def _set_arc_mode(ui, muted, busy):
    """Recolour the arc band. Gradient normally; flat blue muted; flat gray
    busy (busy takes precedence -- see draw_busy). This is the whole cost of
    a mute or busy transition: no repaint, no re-derived geometry."""
    flat = _GRAY if busy else (_BLUE if muted else None)
    for i, s in enumerate(ui["arc"]):
        s.color_index = ui["arc_ci"][i] if flat is None else flat


def _set_pointer(ui, vol_db):
    """Place the pointer, or hide it. vol_db < _VOL_MIN (sentinel -81) means
    startup: no pointer. Returns the angle drawn, or None."""
    if vol_db < _VOL_MIN:
        ui["l_ptr"].hidden = True
        return None
    angle = _vol_to_angle(vol_db)
    ui["ptr"].points = _pointer_points(angle)
    ui["l_ptr"].hidden = False
    return angle


def _show_gauge(ui, arc=True, ticks=True, presets=False, menu_home=False,
                power=False):
    """Set which gauge layers are composited. Everything defaults off except
    the arc and ticks, so each caller states exactly what its screen has."""
    ui["l_arc"].hidden      = not arc
    ui["l_ticks"].hidden    = not ticks
    ui["l_presets"].hidden  = not presets
    ui["l_home"].hidden     = not menu_home
    ui["l_power"].hidden    = not power


def _hide_gauge(ui):
    """Blank the whole gauge -- the menu overlay's background clear."""
    _show_gauge(ui, arc=False, ticks=False)
    ui["l_ptr"].hidden = True
    _hide_icon(ui)


# ── Volume formatting ─────────────────────────────────────────────────────────

# "No volume known" (startup / error screens). Below _VOL_MIN by definition,
# which is what _set_pointer() tests to hide the pointer entirely.
_SENTINEL = _VOL_MIN - 1.0


def _split_vol(db):
    """Return (integer_str, decimal_str) for the split-size volume display.

    decimal_str is always "" when _SHOW_DECIMAL is False -- vol_int is
    also repositioned to sit centered rather than left-of-center in that
    case (see init()), so there's no leftover gap where the decimal used
    to go.
    """
    if not _SHOW_DECIMAL:
        if db <= _VOL_MIN:
            return "--", ""
        return str(int(round(db))), ""
    if db <= _VOL_MIN:
        return "--", ".-"
    s = "{:.1f}".format(db)
    dot = s.index(".")
    return s[:dot], s[dot:]


# ── Text fitting ──────────────────────────────────────────────────────────────
# The screen is a 240px CIRCLE, so a text row's usable width depends on how far
# it sits from the centre, and the gauge arc (inner radius _R_IN) eats into it
# further. Names that come from the user's own gear -- an HA entity's friendly
# name, a renamed Denon input, a config-supplied preset name -- have no length
# limit, so any of them can run under the arc band and then get clipped by the
# display circle -- "Dining Room Speakers" is 215px into a 172px row. Names
# deloop owns (wiim.py's _MODE_NAME, say) can be shortened at the source;
# these can't, so every user-supplied string goes through fit_text().
#
# tools/font_fit.py measures any string against these same budgets off-device.

_ELLIPSIS = "..."   # ASCII only, deliberately. The .pcf fonts carry codepoints
                    # 32-126 and nothing else: a single out-of-range glyph
                    # (U+2026 would be one) forces PCF's encoding table to span
                    # the whole gap and takes every font from ~8KB to ~20KB --
                    # see the Makefile's `fonts` target, where exactly that once
                    # ate enough heap at boot to take WiFi down.


def _row_budget(y, half_h=8):
    """Usable width (px) for a centred text row at `y`, clear of the arc band.

    The row's glyphs span y +/- half_h, so the binding constraint is whichever
    edge sits furthest from the centre -- that's where the circle is narrowest.
    """
    dy = max(abs(CY - (y - half_h)), abs(CY - (y + half_h)))
    if dy >= _R_IN:
        return 0
    return int(2 * math.sqrt(_R_IN * _R_IN - dy * dy))


# Derived once at import from this module's own geometry, never written down
# as literals -- the rows move (PRESET_NAME_Y is CY-relative), so a copied
# number goes stale silently. Currently: input 122px, preset 172px, player
# 150px.
_BUDGET_INPUT  = _row_budget(_INPUT_NAME_Y)
_BUDGET_PRESET = _row_budget(PRESET_NAME_Y)
_BUDGET_PLAYER = _row_budget(_PLAYER_NAME_Y)


def _text_width(font, s):
    """Advance width of `s`, matching how label.Label lays out one line."""
    w = 0
    for ch in s:
        g = font.get_glyph(ord(ch))
        if g is not None:
            w += g.shift_x
    return w


def fit_text(font, s, max_px):
    """Return `s`, shortened with an ellipsis if it would exceed `max_px`.

    Cuts mid-word rather than at a word boundary: HA names in particular are
    distinguished by their tails ("Dining Room Speakers" vs "Dining Room TV"),
    so keeping as many characters as fit preserves more of what tells two
    devices apart than falling back to the last space would.
    """
    if not s:
        return s
    if _text_width(font, s) <= max_px:
        return s
    avail = max_px - _text_width(font, _ELLIPSIS)
    if avail <= 0:
        return _ELLIPSIS
    w, cut = 0, 0
    for i in range(len(s)):
        g = font.get_glyph(ord(s[i]))
        adv = g.shift_x if g is not None else 0
        if w + adv > avail:
            break
        w += adv
        cut = i + 1
    out = s[:cut]
    # Drop any trailing space so the result reads "Dining Room..." rather than
    # "Dining Room ...". Done by hand rather than with rstrip() -- CircuitPython's
    # str is not desktop Python's, and this is one character of work either way.
    while out and out[-1] == " ":
        out = out[:-1]
    return out + _ELLIPSIS


def _fit_input(s):
    return fit_text(_F_SM, s, _BUDGET_INPUT)


def _fit_preset(s):
    return fit_text(_F_SM, s, _BUDGET_PRESET)


def _fit_player(s):
    return fit_text(_F_SM, s, _BUDGET_PLAYER)


def _preset_name(state):
    for val, name in state.preset_names:
        if val == state.preset:
            return name
    return ""


# Plain ASCII fallback text -- only rendered by the default (no UI
# extension) path below, for a backend that reports state.media_state
# without pairing a UI extension the way ha_ui.py/wiim_ui.py do. Those
# draw real shapes instead (draw_play_pause_icon() above), which sidesteps
# the font question entirely: a codepoint outside the base ASCII range
# balloons every generated .pcf's encoding table regardless of how good the
# glyph looks, and once took down WiFi on real hardware. See the Makefile's
# `fonts` target.
_PLAY_CHAR  = "Play"
_PAUSE_CHAR = "Pause"
# Shows the action a tap performs, not the current state -- same convention
# as every other play/pause button (Spotify, YouTube, etc. show a pause
# icon while playing, a play icon while paused). Inverted from a naive
# state-name mapping on purpose.
_MEDIA_STATE_TEXT = {"playing": _PAUSE_CHAR, "paused": _PLAY_CHAR}


def _status_line(state):
    """Text for the row below the volume number: the active preset/filter
    name if this backend has any, else a play/pause status word if the
    backend reports one (state.media_state), else blank -- same slot,
    whichever's relevant for the active backend."""
    name = _preset_name(state)
    if name:
        return name
    return _MEDIA_STATE_TEXT.get(state.media_state, "")


def _draw_status_rows(ui, state, dim_color):
    """Render the text row(s) below the volume number, plus the skip icons.

    Default (no UI extension loaded): preset/status text in the upper row
    (PRESET_NAME_Y), lower row and skip icons blank -- one row, like the
    original v1 layout. A backend with a paired UI extension (see driver.py's
    "UI-extension contract") can replace this entirely -- ha_ui.py's
    draw_status_rows() swaps in a two-row layout (device name up top,
    Playing/Paused below it) plus the skip icons; see that module for why.
    """
    import driver as _driver
    # Blank the play/pause icon first. Extensions only ever SHOW it (see
    # draw_play_pause_icon), and nothing repaints over it now that the scene
    # is retained -- so a backend that stops reporting media_state would
    # otherwise leave one stranded on screen forever.
    _hide_icon(ui)
    if _driver.ui_impl is not None:
        _driver.ui_impl.draw_status_rows(ui, state, dim_color)
        return
    ui["preset"].text  = _fit_preset(_status_line(state))
    ui["preset"].color = dim_color
    ui["player"].text  = ""
    ui["player"].color = _C_MENU
    ui["media_prev"].text = ""
    ui["media_next"].text = ""

# ── Label helpers ─────────────────────────────────────────────────────────────

def _set_vol_labels(ui, db, muted=False, busy=False):
    i_str, d_str = _split_vol(db)
    ui["vol_int"].text = i_str
    ui["vol_dec"].text = d_str
    if busy:
        color = _C_BUSY
    elif muted:
        color = _C_MUTED
    else:
        color = _C_TEXT
    ui["vol_int"].color = color
    ui["vol_dec"].color = color


def _hide_vol_and_status(ui):
    ui["vol_int"].text = ""
    ui["vol_dec"].text = ""
    ui["input"].text   = ""
    ui["preset"].text   = ""
    ui["media_prev"].text = ""
    ui["media_next"].text = ""
    ui["player"].text  = ""
    ui["menu"].text    = ""
    for fl in ui["filters"]:
        fl.text = ""


def _draw_preset_filter_buttons(ui, state):
    """Draw the quick-select button outlines + numbers for
    state.preset_quick_names -- usually the same list as state.preset_names,
    but can be a smaller, separately-configured subset (see driver.py's
    get_quick_presets() contract note; currently only camilladsp.py uses
    that distinction).

    There's no separate "off" button -- tapping the already-selected slot
    toggles state.preset_enabled in place (see app.py), so the same slot
    stays highlighted either way: orange when engaged, light gray when
    selected but disabled -- a visual cue for on/off without losing track
    of which config/filter is loaded. While muted, an engaged slot goes
    blue too, matching the rest of the muted display; a disabled slot is
    unaffected by mute, so it's left alone either way.
    """
    n = min(len(state.preset_quick_names), len(ui["filters"]))
    selected = -1
    for i, (val, _name) in enumerate(state.preset_quick_names):
        if val == state.preset:
            selected = i
            break

    if not state.preset_enabled:
        sel_ci, sel_color = _BTNSEL_OFF, _C_BTN_SEL
    elif state.muted:
        sel_ci, sel_color = _BTNSEL_MUTED, _C_MUTED
    else:
        sel_ci, sel_color = _BTNSEL_FILTER, _C_WARN

    rects = _preset_btn_rects(n)
    for i, (outer, inner) in enumerate(ui["presets"]):
        if i < n:
            x0, y0, x1, y1 = rects[i]
            outer.hidden = inner.hidden = False
            outer.x, outer.y = x0, y0
            outer.color_index = sel_ci if i == selected else _TK_L
            inner.x, inner.y = x0 + 1, y0 + 1
        else:
            outer.hidden = inner.hidden = True
    for i, fl in enumerate(ui["filters"]):
        if i < n:
            x0, y0, x1, y1 = rects[i]
            fl.anchored_position = ((x0 + x1) // 2, (y0 + y1) // 2)
            fl.text  = str(i + 1)
            fl.color = sel_color if i == selected else _C_DIM
        else:
            fl.text = ""

# ── Public API ────────────────────────────────────────────────────────────────

def init():
    """Create display group and all UI elements.  Returns ui dict."""
    display = board.DISPLAY

    palette = displayio.Palette(16)
    for i, c in enumerate(_PALETTE):
        palette[i] = c

    group = displayio.Group()

    # ── Gauge layers ──────────────────────────────────────────────────────────
    # Retained vectorio shapes, not a bitmap. Appended back-to-front; each
    # layer is its own Group so a screen can compose itself by setting
    # `hidden` flags instead of repainting. See docs/rendering.md for the
    # measurements behind this (28,800 bytes reclaimed, 29.2ms -> 1.7ms).
    #
    # displayio does not clear uncovered pixels, so the black background has
    # to be a real shape -- one full-screen Rectangle, no pixel storage.
    # (show_splash() has always done the same thing for the same reason.)
    bg = vectorio.Rectangle(pixel_shader=palette, width=_W, height=_H,
                            x=0, y=0, color_index=_BG)
    group.append(bg)

    l_arc, arc_shapes, arc_cis = _build_arc(palette)
    l_ticks = _build_ticks(palette)
    l_power = _build_power(palette)
    l_presets, preset_pairs = _build_presets(palette)
    l_home = _build_menu_home(palette)
    l_icon, icon_play, icon_bars = _build_icon(palette)

    # The pointer gets its own Group purely so it can be hidden as a unit.
    l_ptr = displayio.Group()
    ptr = vectorio.Polygon(pixel_shader=palette,
                           points=_pointer_points(_ARC_START),
                           x=0, y=0, color_index=_PTR)
    l_ptr.append(ptr)
    l_ptr.hidden = True

    for layer in (l_arc, l_ticks, l_ptr, l_power, l_presets, l_home, l_icon):
        group.append(layer)
    l_power.hidden = True
    l_presets.hidden = True
    l_home.hidden = True

    # ── Volume number: large integer left-of-centre, small decimal right ──────
    # (or, when _SHOW_DECIMAL is False, just the integer dead-centered --
    # see _split_vol()). vol_int Inter 36 pt; vol_dec Inter 24 pt.

    _vol_y = CY - 8
    if _SHOW_DECIMAL:
        _vol_int_anchor, _vol_int_pos = (1.0, 1.0), (CX + 20, _vol_y)
    else:
        _vol_int_anchor, _vol_int_pos = (0.5, 1.0), (CX, _vol_y)
    vol_int = label.Label(
        _F_LG, text="--", color=_C_TEXT,
        anchor_point=_vol_int_anchor, anchored_position=_vol_int_pos,
    )
    vol_dec = label.Label(
        _F_MD, text=".-", color=_C_TEXT,
        anchor_point=(0.0, 1.0), anchored_position=(CX + 20, _vol_y),
    )

    group.append(vol_int)
    group.append(vol_dec)

    # ── Status info: input name & preset name ─────────────────────────────────
    # Input sits above the volume number, halfway between it and the arc gap.
    input_lbl = label.Label(
        _F_SM, text="", color=_C_DIM,
        anchor_point=(0.5, 0.5), anchored_position=(CX, _INPUT_NAME_Y),
    )
    # Preset name sits below volume
    preset_lbl = label.Label(
        _F_SM, text="", color=_C_DIM,
        anchor_point=(0.5, 0.5), anchored_position=(CX, PRESET_NAME_Y),
    )
    # Skip-track icons flank the preset/status row -- only populated when
    # state.media_state is "playing"/"paused" (see draw_main()).
    media_prev_lbl = label.Label(
        _F_SM, text="", color=_C_DIM,
        anchor_point=(0.5, 0.5), anchored_position=(CX - _MEDIA_SIDE_X, PRESET_NAME_Y),
    )
    media_next_lbl = label.Label(
        _F_SM, text="", color=_C_DIM,
        anchor_point=(0.5, 0.5), anchored_position=(CX + _MEDIA_SIDE_X, PRESET_NAME_Y),
    )
    # Device name, just below that -- see ha_ui.py's _player_name(), the
    # only thing that ever populates it.
    player_lbl = label.Label(
        _F_SM, text="", color=_C_TEXT,
        anchor_point=(0.5, 0.5), anchored_position=(CX, _PLAYER_NAME_Y),
    )
    group.append(input_lbl)
    group.append(preset_lbl)
    group.append(media_prev_lbl)
    group.append(media_next_lbl)
    group.append(player_lbl)

    # ── Preset quick-select buttons: numbered labels over the shape outlines ──
    # Positioned dynamically each draw_main() call, since the row layout
    # depends on how many presets state.preset_quick_names actually has.
    filter_btns = []
    for i in range(_DBTN_MAX):
        fl = label.Label(
            _F_SM, text="", color=_C_DIM,
            anchor_point=(0.5, 0.5), anchored_position=(CX, _DBTN_Y0),
        )
        group.append(fl)
        filter_btns.append(fl)

    # ── Menu hint (bottom centre, near-invisible – tap-zone cue) ─────────────
    menu_lbl = label.Label(
        _F_SM, text="MENU", color=_C_MENU,
        anchor_point=_MENU_ANCHOR_MAIN, anchored_position=_MENU_POS_MAIN,
    )
    group.append(menu_lbl)

    # ── Menu overlay: up to 5 items ───────────────────────────────────────────
    menu_items = []
    for i in range(5):
        ml = label.Label(
            _F_MD, text="", color=_C_DIM,
            anchor_point=(0.5, 0.5), anchored_position=(CX, 44 + i * 38),
        )
        group.append(ml)
        menu_items.append(ml)

    # ── Top overlay: startup messages & menu title ────────────────────────────
    status_lbl = label.Label(
        _F_MD, text="", color=_C_WARN,
        anchor_point=(0.5, 0.0), anchored_position=(CX, 24),
    )
    group.append(status_lbl)

    display.root_group = group
    display.auto_refresh = False   # we call refresh() explicitly after every draw

    return {
        "display":    display,
        "group":      group,      # main UI group; needed to restore after splash
        # Gauge layers. `arc`/`arc_ci` are handed back as a list rather than
        # recovered by walking l_arc: the selected preset outline shares
        # _ORG with the arc's orange band, so filtering shapes by colour
        # would silently pick it up and recolour it on mute.
        "l_arc":      l_arc,
        "arc":        arc_shapes,
        "arc_ci":     arc_cis,
        "l_ticks":    l_ticks,
        "l_ptr":      l_ptr,
        "ptr":        ptr,
        "l_power":    l_power,
        "l_presets":  l_presets,
        "presets":    preset_pairs,
        "l_home":     l_home,
        "l_icon":     l_icon,
        "icon_play":  icon_play,
        "icon_bars":  icon_bars,
        "vol_int":    vol_int,
        "vol_dec":    vol_dec,
        "input":      input_lbl,
        "preset":     preset_lbl,
        "media_prev": media_prev_lbl,
        "media_next": media_next_lbl,
        "player":     player_lbl,
        "filters":    filter_btns,
        "menu":       menu_lbl,
        "items":      menu_items,
        "status":     status_lbl,
    }


def draw_main(ui, state):
    """Render current AVRState to the display."""
    import driver as _driver
    ui["status"].text = ""
    for ml in ui["items"]:
        ml.text = ""

    # Never claim the device is off on state we have not actually heard from
    # it. state.power initialises to "STANDBY", so without this check a
    # device we simply cannot reach renders the identical standby power ring
    # -- indistinguishable from a real power-off, from every call site (see
    # state.AVRState.power_known).
    if not getattr(state, "power_known", True):
        ui["display"].brightness = getattr(state, "brightness", BRIGHTNESS_ON)
        draw_reconnecting(ui)
        return

    if state.power != "ON":
        ui["display"].brightness = state.brightness * _STANDBY_BRIGHTNESS_FRAC
        _show_gauge(ui, arc=False, ticks=False, power=True)
        ui["l_ptr"].hidden = True
        _hide_icon(ui)
        _hide_vol_and_status(ui)
        # Backends that can switch to a different target (CAPS["player_select"],
        # e.g. HA's Media Player list) get a way back into the menu from here --
        # otherwise standby stays the deliberately bare "one button, nothing
        # else competes for attention" screen (see local/agent/design.md).
        if _driver.CAPS["player_select"] and _driver.ui_impl is not None:
            anchor, pos = _driver.ui_impl.standby_menu_pos()
            ui["menu"].anchor_point      = anchor
            ui["menu"].anchored_position = pos
            ui["menu"].text  = "MENU"
            ui["menu"].color = _C_MENU
        ui["display"].refresh()
        return

    ui["display"].brightness = getattr(state, "brightness", BRIGHTNESS_ON)

    # No fast path, and none needed: this whole function sets a handful of
    # visibility flags, colour indices and three pointer points. Nothing is
    # rasterised here (see docs/rendering.md).
    #
    # Resist adding a change-detection cache in front of it. Keeping every
    # frame cheap is a correctness property of the main loop, not a nicety:
    # that loop is cooperative and pumps the async status poll once per
    # iteration, so an expensive draw_main() drags the whole loop down to a
    # few iterations/sec, which stretches each poll to ~1000ms and silently
    # drops touch events. Both read as flaky hardware or networking while
    # the real cause is frame cost.
    quick = _driver.CAPS["preset_quickbuttons"]
    _show_gauge(ui, presets=quick, menu_home=True)
    _set_arc_mode(ui, state.muted, False)
    _set_pointer(ui, state.volume_db)
    if quick:
        _draw_preset_filter_buttons(ui, state)

    _set_vol_labels(ui, state.volume_db, state.muted)
    ui["input"].text  = _fit_input(_driver.friendly_input(state.input))
    ui["input"].color = _C_DIM
    # Restore the preset label's normal row position before drawing into it:
    # draw_error/draw_reconnecting recenter this same label for their own
    # layout, and this is the only thing that puts it back -- without it the
    # label stays stranded mid-screen on the first main render afterwards.
    # Set before _draw_status_rows so a UI extension's own placement wins.
    ui["preset"].anchor_point      = (0.5, 0.5)
    ui["preset"].anchored_position = (CX, PRESET_NAME_Y)
    _draw_status_rows(ui, state, _C_DIM)
    ui["menu"].anchor_point      = _MENU_ANCHOR_MAIN
    ui["menu"].anchored_position = _MENU_POS_MAIN
    ui["menu"].text  = "MENU"
    ui["menu"].color = _C_MENU
    ui["display"].refresh()


def draw_busy(ui, state):
    """Static all-gray frame for a blocking device call known to take a
    while (e.g. a MiniDSP preset/config-slot switch -- confirmed
    multi-second on real hardware, see driver.py callers). deloop's main
    loop is fully synchronous, so nothing can animate while that call is
    in flight -- this paints once, unlike pulse_mute()'s per-frame
    breathing effect, and the caller swaps back to normal color via
    draw_main() once the blocking call returns.
    """
    import driver as _driver
    ui["status"].text = ""
    for ml in ui["items"]:
        ml.text = ""

    _show_gauge(ui)
    _set_arc_mode(ui, False, True)
    _set_pointer(ui, state.volume_db)
    _set_vol_labels(ui, state.volume_db, busy=True)
    ui["input"].text   = _fit_input(_driver.friendly_input(state.input))
    ui["input"].color  = _C_BUSY
    _draw_status_rows(ui, state, _C_BUSY)
    for fl in ui["filters"]:
        fl.color = _C_DIM
    ui["menu"].text = ""
    ui["display"].refresh()


def draw_volume(ui, state):
    """Fast path: move the pointer wedge, update labels. No full re-render.

    Only the ~20-pixel pointer region becomes dirty → near-instant SPI transfer.
    """
    _set_pointer(ui, state.volume_db)
    _set_vol_labels(ui, state.volume_db, state.muted)
    ui["display"].refresh()


def mute_pulse_at_trough(elapsed_s):
    """True near the trough of the mute breathing cycle -- the natural
    low-motion point where a caller can do slow work (e.g. an AVR poll)
    without a visible stutter -- see the "Mute pulse" note above."""
    t = elapsed_s % _PULSE_PERIOD_S
    frac = 0.5 * (1.0 - math.cos(2.0 * math.pi * t / _PULSE_PERIOD_S))
    return frac < _PULSE_TROUGH_TH


def pulse_mute(ui, elapsed_s):
    """Render one frame of the mute breathing animation.

    elapsed_s: seconds since the pulse phase started; wraps internally.
    Callers that poll on the trough (mute_pulse_at_trough) should call this
    LAST each tick, after the poll -- otherwise a poll-triggered draw_main()
    can stomp this frame's color with the static muted color.
    """
    t = elapsed_s % _PULSE_PERIOD_S
    frac = 0.5 * (1.0 - math.cos(2.0 * math.pi * t / _PULSE_PERIOD_S))
    color = _pulse_color(frac)
    ui["vol_int"].color = color
    ui["vol_dec"].color = color
    ui["display"].refresh()


def draw_status(ui, msg):
    """Startup / connection-progress state.

    Shows the full gauge layout with a placeholder volume (matching
    _split_vol()'s own sentinel formatting -- "--.-" or just "--", see
    _SHOW_DECIMAL) and a progress message where the input line would be,
    per the design spec.
    """
    _show_gauge(ui)                 # arc + ticks, no pointer
    _set_arc_mode(ui, False, False)
    _set_pointer(ui, _SENTINEL)
    _hide_icon(ui)
    ui["vol_int"].text  = "--"; ui["vol_int"].color = _C_DIM
    ui["vol_dec"].text  = ".-" if _SHOW_DECIMAL else ""
    ui["vol_dec"].color = _C_TEXT
    ui["input"].text    = msg
    ui["input"].color   = _C_DIM
    ui["preset"].text    = ""
    ui["media_prev"].text = ""
    ui["media_next"].text = ""
    ui["player"].text   = ""
    ui["menu"].text     = ""
    ui["status"].text   = ""
    for ml in ui["items"]:
        ml.text = ""
    for fl in ui["filters"]:
        fl.text = ""
    ui["display"].refresh()


def draw_error(ui, msg):
    """Error state: message and restart hint centered on the display, one
    above the other, in the same small dim style as the main screen's
    input line (font _F_SM, color _C_DIM).  Tap anywhere to restart.

    Reuses the "preset" and "menu" labels (both already _F_SM) rather than
    "status" (_F_MD, too big) or "input" (_F_SM, but its position is never
    reset elsewhere -- see its own label comment -- so repositioning it
    here would leak into the next draw_main()).
    """
    _show_gauge(ui)
    _set_arc_mode(ui, False, False)
    _set_pointer(ui, _SENTINEL)
    _hide_icon(ui)
    _hide_vol_and_status(ui)
    for ml in ui["items"]:
        ml.text = ""
    ui["status"].text = ""
    ui["preset"].anchor_point      = (0.5, 0.5)
    ui["preset"].anchored_position = (CX, CY - 20)
    # Budget computed for THIS row, not _BUDGET_PRESET -- the label is moved to
    # CY-20 here, not its usual PRESET_NAME_Y. (The two happen to land within a
    # pixel of each other, which is exactly why it's worth being explicit.)
    ui["preset"].text  = fit_text(_F_SM, msg, _row_budget(CY - 20))
    ui["preset"].color = _C_DIM
    ui["menu"].anchor_point      = (0.5, 0.5)
    ui["menu"].anchored_position = (CX, CY + 20)
    ui["menu"].text  = "Tap to Restart"
    ui["menu"].color = _C_DIM
    ui["display"].refresh()


def draw_reconnecting(ui):
    """Connection-lost state: two centered lines, nothing to tap.

    Distinct from draw_error(): that one is for fatal boot-time problems the
    user must act on, and offers a restart. This is a transient state the
    device is expected to come out of on its own, so it deliberately offers
    no affordance -- it is a status report, not a prompt.

    Wording is backend-neutral on purpose: the thing that went quiet may be
    a Denon, a MiniDSP, a WiiM, a CamillaDSP host or a Home Assistant
    entity, so naming any one of them would be wrong for most users.
    """
    _show_gauge(ui)
    _set_arc_mode(ui, False, False)
    _set_pointer(ui, _SENTINEL)
    _hide_icon(ui)
    _hide_vol_and_status(ui)
    for ml in ui["items"]:
        ml.text = ""
    ui["status"].text = ""
    ui["preset"].anchor_point      = (0.5, 0.5)
    ui["preset"].anchored_position = (CX, CY - 12)
    ui["preset"].text  = "lost connection"
    ui["preset"].color = _C_DIM
    ui["menu"].anchor_point      = (0.5, 0.5)
    ui["menu"].anchored_position = (CX, CY + 12)
    ui["menu"].text  = "reconnecting..."
    ui["menu"].color = _C_DIM
    ui["display"].refresh()


def render_gauge_bg(ui, vol_db, muted):
    """Show the gauge behind a menu without refreshing.
    Call before draw_menu when the brightness screen needs the gauge background."""
    _show_gauge(ui)          # arc + ticks only, no preset row or menu-home
    _set_arc_mode(ui, muted, False)
    _set_pointer(ui, vol_db)
    _hide_icon(ui)


def draw_menu(ui, title, items, cursor, clear_bg=False, version_text=""):
    """Menu overlay.  clear_bg=True paints the bitmap black before drawing.

    version_text is the only thing ui["status"] ever shows here -- title
    itself is never rendered (context normally comes from the items
    themselves); the Update submenu is the one exception that needs a
    persistent, no-tap-required label, styled to match the main screen's
    dim "input" label rather than a bright title."""
    global MENU_ITEM_Y0
    if clear_bg:
        _hide_gauge(ui)
    else:
        # Keep whatever render_gauge_bg() left showing, but never the preset
        # row or menu-home frame -- their labels are blanked just below, and
        # an outline with no number in it reads as a rendering fault.
        ui["l_presets"].hidden = True
        ui["l_home"].hidden = True
    ui["status"].text  = version_text
    ui["status"].color = _C_DIM
    ui["vol_int"].text = ""
    ui["vol_dec"].text = ""
    ui["input"].text   = ""
    ui["preset"].text   = ""
    ui["media_prev"].text = ""
    ui["media_next"].text = ""
    ui["player"].text  = ""
    ui["menu"].text    = ""
    for fl in ui["filters"]:
        fl.text = ""
    # Center N items vertically: for odd N the middle item sits at CY;
    # for even N the midpoint between the two centre items sits at CY.
    n_vis = len(items)
    MENU_ITEM_Y0 = int(CY - (n_vis - 1) / 2 * MENU_ITEM_DY)
    for i, ml in enumerate(ui["items"]):
        if i < n_vis:
            ml.anchored_position = (CX, MENU_ITEM_Y0 + i * MENU_ITEM_DY)
            ml.text  = items[i]
            ml.color = _C_WARN if (cursor >= 0 and i == cursor) else _C_TEXT
        else:
            ml.text = ""
    ui["display"].refresh()


def exit_menu(ui):
    """Clear menu overlay when returning to normal display."""
    for ml in ui["items"]:
        ml.text = ""
    ui["status"].text = ""


def show_splash(display):
    """Startup splash: 'deloop' / 'by hobbysprawl' / logo.

    Creates a temporary displayio group, sets it as the display root, and
    calls refresh() once.  Returns immediately – the caller is responsible
    for sleeping the desired duration before calling init().
    """
    splash = displayio.Group()

    # Black background via vectorio (no large bitmap allocation)
    try:
        import vectorio
        bg_pal = displayio.Palette(1)
        bg_pal[0] = 0x000000
        splash.append(vectorio.Rectangle(
            pixel_shader=bg_pal, width=_W, height=_H, x=0, y=0))
    except Exception:
        pass

    # "deloop" – large font, centred
    splash.append(label.Label(
        _F_LG, text="deloop", color=_C_TEXT,
        anchor_point=(0.5, 0.5), anchored_position=(CX, 55),
    ))

    # "by hobbysprawl" – small font, dimmed
    splash.append(label.Label(
        _F_SM, text="by hobbysprawl", color=_C_DIM,
        anchor_point=(0.5, 0.5), anchored_position=(CX, 83),
    ))

    # Logo image (4-bit BMP, orange on black)
    try:
        import adafruit_imageload
        logo_bmp, logo_pal = adafruit_imageload.load(
            "/splash_logo.bmp",
            bitmap=displayio.Bitmap,
            palette=displayio.Palette,
        )
        splash.append(displayio.TileGrid(
            logo_bmp, pixel_shader=logo_pal,
            x=CX - logo_bmp.width // 2,
            y=105,
        ))
    except Exception as e:
        print("splash logo:", e)

    display.auto_refresh = False
    display.root_group = splash
    display.refresh()


def show_message(display, text):
    """Minimal single-line status screen: takes the raw `display`, not a
    `ui` dict, and builds its own throwaway group.

    Used by app.py's Update menu to show "Checking..." in the moment
    between requesting a check and the supervisor.reload() that hands off
    to ota_boot.py. That reload discards the display group anyway, so
    there is nothing to restore afterwards and no reason to route it
    through the retained scene every other screen here uses.

    ota_boot.py has its own near-identical copy of this rather than
    importing dial_ui -- deliberately, since importing this module at all
    would pull in three PCF fonts it has no memory budget for. See its
    module docstring.
    """
    group = displayio.Group()
    try:
        import vectorio
        bg_pal = displayio.Palette(1)
        bg_pal[0] = 0x000000
        group.append(vectorio.Rectangle(
            pixel_shader=bg_pal, width=_W, height=_H, x=0, y=0))
    except Exception:
        pass
    group.append(label.Label(
        _F_SM, text=text, color=_C_TEXT,
        anchor_point=(0.5, 0.5), anchored_position=(CX, CY),
    ))
    display.auto_refresh = False
    display.root_group = group
    display.refresh()


def flash_power_on(ui):
    """Brief splash shown when the AVR transitions to powered-on.

    Shows the splash for ~1 s then silently restores the main UI group.
    The caller's subsequent draw_main() call does the final refresh, so
    the user sees splash → main screen with no intermediate flicker.
    """
    show_splash(ui["display"])
    time.sleep(1.0)
    ui["display"].root_group = ui["group"]   # restore; do NOT refresh yet
