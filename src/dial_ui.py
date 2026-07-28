# dial_ui.py – Premium circular-gauge UI for M5Dial (240 × 240 round display)
#
# Design language: Dieter Rams / Braun industrial – minimal, purposeful.
# Black canvas, precision arc gauge with gradient colour, wedge pointer,
# clean split-size typography. Every element earns its place.
#
# Public API (unchanged from v1 for code.py compatibility):
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
#   BRIGHTNESS_ON                      constant used by code.py
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

try:
    import bitmaptools as _bt
    _BT = True
except ImportError:
    _BT = False

import config
import driver as _driver

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
# below unity). Color bands below are proportional fractions of this span
# rather than fixed dB thresholds, so they scale to whatever range is active.
_VOL_MIN    = config.VOLUME_MIN
_VOL_MAX    = config.VOLUME_MAX

# ── Power button geometry ─────────────────────────────────────────────────────
_PWR_R_OUT  = 78    # ring outer radius
_PWR_R_IN   = 58    # ring inner radius    (ring width = 20 px)
_PWR_GAP    = 30    # half-angle of gap at 12 o'clock (degrees each side)
_PWR_STEM_TOP = CY - _PWR_R_OUT - 4   # y ≈ 38
_PWR_STEM_BOT = CY - _PWR_R_IN  + 4   # y ≈ 66

# ── Brightness ────────────────────────────────────────────────────────────────
BRIGHTNESS_ON = 1.0

# Standby screen brightness is relative to the user's own brightness setting,
# not a fixed value -- dim room, dim standby indicator; bright room, brighter one.
_STANDBY_BRIGHTNESS_FRAC = 0.25

# ── Menu geometry (exported so code.py can map touch_y → item index) ─────────
MENU_ITEM_Y0  = 44    # y-centre of first visible item
MENU_ITEM_DY  = 38    # vertical spacing between items
MENU_VISIBLE  = 5     # max items shown at once

# ── Preset quick-select button geometry ───────────────────────────────────────
# Row of small numbered buttons just below the preset name, letting a tap
# switch presets directly from the main screen instead of via the menu.
# Exported so code.py can split the main screen's tap zones: mute above this
# line, quick-select buttons (and nothing else) at or below it.
PRESET_NAME_Y = CY + 18
_DBTN_Y0   = 156   # top of the button row
_DBTN_H    = 22    # button height
_DBTN_W    = 22    # button width
_DBTN_GAP  = 14    # horizontal gap between buttons
_DBTN_MAX  = 5     # pre-allocated label slots -- real devices use 2-4

# ── Gauge bitmap palette (16-colour → 4-bit pixels, ~28 KB for 240×240) ───────
# _PRR and _BTNSEL_FILTER are aliases, not separate slots: both used to be
# their own shade of orange, but they're the same colour as _ORG now, so
# they just point at it instead of duplicating the palette entry.
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
]   # 12 entries; value_count=16 → 4 spare slots

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
# n is however many entries state.preset_names has -- the row is centred and
# evenly spaced for whatever n turns out to be, not hardcoded.

def _preset_btn_rects(n):
    """Return [(x0, y0, x1, y1), ...] for n evenly-spaced, centred buttons."""
    total_w = n * _DBTN_W + (n - 1) * _DBTN_GAP
    x_start = CX - total_w // 2
    rects = []
    for i in range(n):
        x0 = x_start + i * (_DBTN_W + _DBTN_GAP)
        rects.append((x0, _DBTN_Y0, x0 + _DBTN_W, _DBTN_Y0 + _DBTN_H))
    return rects


def preset_button_at(x, y, n):
    """Return the tapped quick-select button index [0..n-1], or -1."""
    for i, (x0, y0, x1, y1) in enumerate(_preset_btn_rects(n)):
        if x0 <= x <= x1 and y0 <= y <= y1:
            return i
    return -1

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


def _arc_color(angle):
    """Gradient palette index for an arc position (angle CW-from-top)."""
    vol  = _VOL_MIN + (angle - _ARC_START) / _ARC_SWEEP * (_VOL_MAX - _VOL_MIN)
    frac = (vol - _VOL_MIN) / (_VOL_MAX - _VOL_MIN)
    if   frac < _FRAC_AMBER:  return _GRN
    elif frac < _FRAC_ORANGE: return _AMB
    elif frac < _FRAC_RED:    return _ORG
    else:                     return _RED


def _thick_tick(bmp, angle_deg, ix, iy, ox, oy, thickness, ci):
    """Draw a radial tick with given pixel thickness by offsetting along the tangent."""
    if thickness <= 1:
        _line(bmp, ix, iy, ox, oy, ci)
        return
    rad = math.radians(angle_deg)
    px = math.cos(rad)
    py = math.sin(rad)
    half = thickness // 2
    for offset in range(-half, thickness - half):
        dx = int(round(offset * px))
        dy = int(round(offset * py))
        _line(bmp, ix + dx, iy + dy, ox + dx, oy + dy, ci)


def _line(bmp, x0, y0, x1, y1, idx):
    """1-pixel line; uses bitmaptools when available, Bresenham otherwise."""
    if _BT:
        x0 = max(0, min(_W - 1, x0)); y0 = max(0, min(_H - 1, y0))
        x1 = max(0, min(_W - 1, x1)); y1 = max(0, min(_H - 1, y1))
        _bt.draw_line(bmp, x0, y0, x1, y1, idx)
        return
    dx = abs(x1 - x0); dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        if 0 <= x0 < _W and 0 <= y0 < _H:
            bmp[x0, y0] = idx
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy: err -= dy; x0 += sx
        if e2 <  dx: err += dx; y0 += sy


def _rect_outline(bmp, x0, y0, x1, y1, idx):
    """1-pixel rectangle outline."""
    _line(bmp, x0, y0, x1, y0, idx)
    _line(bmp, x0, y1, x1, y1, idx)
    _line(bmp, x0, y0, x0, y1, idx)
    _line(bmp, x1, y0, x1, y1, idx)


def _arc(bmp, a_start, a_end, r_in, r_out, color, step=1.0):
    """Thick arc band.  color = palette index or callable(angle_deg)→index."""
    a = a_start
    while a <= a_end + step * 0.05:
        rad = math.radians(a)
        s = math.sin(rad); c = math.cos(rad)
        ix = int(CX + r_in  * s + 0.5); iy = int(CY - r_in  * c + 0.5)
        ox = int(CX + r_out * s + 0.5); oy = int(CY - r_out * c + 0.5)
        ci = color(a) if callable(color) else color
        _line(bmp, ix, iy, ox, oy, ci)
        a += step


def _arc_solid(bmp, a_start, a_end, r_in, r_out, color):
    """Scanline arc-band fill: every pixel covered exactly once, no artifacts.

    For each row in the bounding box, computes the annulus x-span, checks each
    pixel's angle with atan2, and flushes same-color runs with fill_region.
    Works for any arc range including those that cross the 0°/360° boundary.
    """
    r_out2  = r_out * r_out
    r_in2   = r_in  * r_in
    wraps   = a_end > 360.0
    a_end2  = a_end - 360.0 if wraps else a_end
    is_grad = callable(color)

    for y in range(max(0, CY - r_out), min(_H, CY + r_out + 1)):
        dy  = y - CY
        dy2 = dy * dy
        if dy2 >= r_out2:
            continue
        dx_out = int(math.sqrt(float(r_out2 - dy2)))
        dx_in  = int(math.sqrt(float(max(0.0, r_in2 - dy2)))) if dy2 < r_in2 else 0

        for xA, xB in ((CX - dx_out, CX - dx_in), (CX + dx_in, CX + dx_out)):
            ci_run = -1
            x_run  = xA
            for x in range(max(0, xA), min(_W, xB + 1)):
                dx = x - CX
                a  = math.degrees(math.atan2(dx, -dy))
                if a < 0.0:
                    a += 360.0
                ok = (a >= a_start or a <= a_end2) if wraps else (a_start <= a <= a_end2)
                arc_a = a if a >= a_start else a + 360.0   # unwrap into arc space
                ci = (color(arc_a) if is_grad else color) if ok else -1
                if ci != ci_run:
                    if ci_run >= 0:
                        if _BT:
                            _bt.fill_region(bmp, x_run, y, x, y + 1, ci_run)
                        else:
                            for xx in range(x_run, x):
                                bmp[xx, y] = ci_run
                    ci_run = ci
                    x_run  = x
            if ci_run >= 0:
                xe = min(_W, xB + 1)
                if _BT:
                    _bt.fill_region(bmp, x_run, y, xe, y + 1, ci_run)
                else:
                    for xx in range(x_run, xe):
                        bmp[xx, y] = ci_run


def _tri(bmp, x0, y0, x1, y1, x2, y2, idx):
    """Fill a triangle via scanline rasterisation."""
    pts = sorted(((x0, y0), (x1, y1), (x2, y2)), key=lambda p: p[1])
    (ax, ay), (bx, by), (cx, cy) = pts

    def _xi(y, p, q):
        if q[1] == p[1]:
            return float(p[0])
        return p[0] + (q[0] - p[0]) * float(y - p[1]) / (q[1] - p[1])

    for y in range(max(0, ay), min(_H, cy + 1)):
        if y <= by:
            xa = _xi(y, (ax, ay), (bx, by)); xb = _xi(y, (ax, ay), (cx, cy))
        else:
            xa = _xi(y, (bx, by), (cx, cy)); xb = _xi(y, (ax, ay), (cx, cy))
        x_lo = max(0, int(min(xa, xb))); x_hi = min(_W - 1, int(max(xa, xb)))
        for x in range(x_lo, x_hi + 1):
            bmp[x, y] = idx


def _lerp_color(c0, c1, frac):
    """Interpolate two 24-bit RGB ints. frac is clamped to [0, 1]."""
    frac = max(0.0, min(1.0, frac))
    r0, g0, b0 = (c0 >> 16) & 0xFF, (c0 >> 8) & 0xFF, c0 & 0xFF
    r1, g1, b1 = (c1 >> 16) & 0xFF, (c1 >> 8) & 0xFF, c1 & 0xFF
    r = int(r0 + (r1 - r0) * frac)
    g = int(g0 + (g1 - g0) * frac)
    b = int(b0 + (b1 - b0) * frac)
    return (r << 16) | (g << 8) | b


def _clear(bmp):
    if _BT:
        _bt.fill_region(bmp, 0, 0, _W, _H, _BG)
    else:
        for y in range(_H):
            for x in range(_W):
                bmp[x, y] = _BG

# ── Gauge scene rendering ─────────────────────────────────────────────────────

def _tick_values():
    """dB values for tick marks, every 5 dB, anchored at 0 -- not at
    _VOL_MIN. Denon's -80..20 range is a multiple of 5 either way, but a
    range like MiniDSP's -127..0 isn't: anchoring at 0 guarantees the 0 dB
    reference tick itself always lands exactly, wherever the scale starts."""
    n_below = int((0.0 - _VOL_MIN) / 5.0 + 0.5)
    n_above = int((_VOL_MAX - 0.0) / 5.0 + 0.5)
    for i in range(-n_below, n_above + 1):
        yield i * 5.0


def _draw_ticks(bmp):
    """Tick marks just outside the arc band, every 5 dB."""
    for db in _tick_values():
        angle = _vol_to_angle(db)
        if abs(db) < 0.01:
            r_out, ci, thick = _R_TK_ZERO, _TK_0, 3
        elif round(db) % 10 == 0:
            r_out, ci, thick = _R_TK_MAJOR, _TK_L, 2
        else:
            r_out, ci, thick = _R_TK_MINOR, _TK_L, 1
        ix, iy = _axy(_R_TK_BASE, angle)
        ox, oy = _axy(r_out, angle)
        _thick_tick(bmp, angle, ix, iy, ox, oy, thick, ci)


def _draw_pointer(bmp, angle, idx):
    """Inward-pointing wedge: base spread at display edge, tip aimed at outer arc."""
    tx, ty = _axy(_R_PTR_TIP,  angle)
    lx, ly = _axy(_R_PTR_BASE, angle - _PTR_HALF)
    rx, ry = _axy(_R_PTR_BASE, angle + _PTR_HALF)
    _tri(bmp, tx, ty, lx, ly, rx, ry, idx)


def _draw_power_symbol(bmp):
    """Classic power icon: scanline-filled ring + stem matched to ring stroke width."""
    # Ring: scanline fill → no radial-line gap artifacts
    _arc_solid(bmp, _PWR_GAP, 360.0 - _PWR_GAP, _PWR_R_IN, _PWR_R_OUT, _PRR)
    # Stem: same horizontal width as the ring's radial stroke
    stem_half = (_PWR_R_OUT - _PWR_R_IN) // 2   # 10 px each side = 20 px total
    x0 = max(0, CX - stem_half)
    x1 = min(_W, CX + stem_half)
    for y in range(max(0, _PWR_STEM_TOP), min(_H, _PWR_STEM_BOT + 1)):
        if _BT:
            _bt.fill_region(bmp, x0, y, x1, y + 1, _PRR)
        else:
            for x in range(x0, x1):
                bmp[x, y] = _PRR


def _draw_preset_buttons(bmp, n, selected_idx, selected_ci):
    """Outline the n quick-select buttons; selected_idx gets selected_ci."""
    for i, (x0, y0, x1, y1) in enumerate(_preset_btn_rects(n)):
        ci = selected_ci if i == selected_idx else _TK_L
        _rect_outline(bmp, x0, y0, x1, y1, ci)


def _restore_region(bmp, angle, muted):
    """Erase the pointer at `angle` by restoring the arc band and hollow.

    Only redraws the small angular window around the pointer – the bitmap
    dirty region stays tiny so the SPI transfer is near-instant.
    """
    a0 = angle - _PTR_HALF - 2
    a1 = angle + _PTR_HALF + 2
    arc_col = _BLUE if muted else _arc_color
    # 1. Erase with a slightly-expanded black triangle – same algorithm as draw,
    #    guarantees every pointer pixel is covered with no mismatched gaps.
    tx, ty = _axy(_R_PTR_TIP  - 3,  angle)
    lx, ly = _axy(_R_PTR_BASE + 2,  angle - _PTR_HALF - 1)
    rx, ry = _axy(_R_PTR_BASE + 2,  angle + _PTR_HALF + 1)
    _tri(bmp, tx, ty, lx, ly, rx, ry, _BG)
    # 2. Restore arc band (gap-free concentric sweep)
    _arc_solid(bmp, a0, a1, _R_IN, _R_OUT, arc_col)
    # 3. Redraw any tick marks that fall inside the window
    for db in _tick_values():
        ta = _vol_to_angle(db)
        if a0 <= ta <= a1:
            if abs(db) < 0.01:          r_out, ci, thick = _R_TK_ZERO, _TK_0, 3
            elif round(db) % 10 == 0:  r_out, ci, thick = _R_TK_MAJOR, _TK_L, 2
            else:                       r_out, ci, thick = _R_TK_MINOR, _TK_L, 1
            ix, iy = _axy(_R_TK_BASE, ta)
            ox, oy = _axy(r_out, ta)
            _thick_tick(bmp, ta, ix, iy, ox, oy, thick, ci)


def _render_gauge(bmp, vol_db, muted, power_off=False, busy=False):
    """Full re-render: clear, draw static gradient arc + ticks, then pointer.

    The arc is always drawn at full gradient intensity (like a traditional
    instrument scale). The pointer alone indicates the current level.
    vol_db < _VOL_MIN (sentinel −81) = startup: no pointer drawn.
    busy=True (see draw_busy()) takes precedence over muted -- flat gray
    instead of flat blue, no animation (just one static frame; the main
    loop can't animate while blocked on the device call this represents).
    """
    _clear(bmp)

    if power_off:
        _draw_power_symbol(bmp)
        return None

    # Full gradient arc – concentric-sweep fill, gap-free
    arc_col = _GRAY if busy else (_BLUE if muted else _arc_color)
    _arc_solid(bmp, _ARC_START, _ARC_START + _ARC_SWEEP, _R_IN, _R_OUT, arc_col)
    _draw_ticks(bmp)

    # Pointer at current volume
    if vol_db >= _VOL_MIN:
        vol_angle = _vol_to_angle(vol_db)
        _draw_pointer(bmp, vol_angle, _PTR)
        return vol_angle
    return None

# ── Volume formatting ─────────────────────────────────────────────────────────

_SENTINEL = _VOL_MIN - 1.0   # −81: "no volume known" (startup state)


def _split_vol(db):
    """Return (integer_str, decimal_str) for the split-size volume display."""
    if db <= _VOL_MIN:
        return "--", ".-"
    s = "{:.1f}".format(db)
    dot = s.index(".")
    return s[:dot], s[dot:]


def _preset_name(state):
    for val, name in state.preset_names:
        if val == state.preset:
            return name
    return ""

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
    ui["menu"].text    = ""
    for fl in ui["filters"]:
        fl.text = ""


def _draw_preset_filter_buttons(ui, state):
    """Draw the quick-select button outlines + numbers for state.preset_names.

    There's no separate "off" button -- tapping the already-selected slot
    toggles state.preset_enabled in place (see code.py), so the same slot
    stays highlighted either way: orange when engaged, light gray when
    selected but disabled -- a visual cue for on/off without losing track
    of which config/filter is loaded. While muted, an engaged slot goes
    blue too, matching the rest of the muted display; a disabled slot is
    unaffected by mute, so it's left alone either way.
    """
    n = min(len(state.preset_names), len(ui["filters"]))
    selected = -1
    for i, (val, _name) in enumerate(state.preset_names):
        if val == state.preset:
            selected = i
            break

    if not state.preset_enabled:
        sel_ci, sel_color = _BTNSEL_OFF, _C_BTN_SEL
    elif state.muted:
        sel_ci, sel_color = _BTNSEL_MUTED, _C_MUTED
    else:
        sel_ci, sel_color = _BTNSEL_FILTER, _C_WARN

    _draw_preset_buttons(ui["bmp"], n, selected, sel_ci)
    rects = _preset_btn_rects(n)
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

    # Full-screen gauge bitmap (4-bit pixels, ~28 KB)
    bmp = displayio.Bitmap(_W, _H, 16)

    group = displayio.Group()
    group.append(displayio.TileGrid(bmp, pixel_shader=palette))

    # ── Volume number: large integer left-of-centre, small decimal right ──────
    # vol_int  Inter 36 pt, anchor (1,1) → right-aligns to CX
    # vol_dec  Inter 24 pt, anchor (0,1) → left-aligns from CX (shorter)

    _vol_y = CY - 8
    vol_int = label.Label(
        _F_LG, text="--", color=_C_TEXT,
        anchor_point=(1.0, 1.0), anchored_position=(CX + 20, _vol_y),
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
        anchor_point=(0.5, 0.5), anchored_position=(CX, 62),
    )
    # Preset name sits below volume
    preset_lbl = label.Label(
        _F_SM, text="", color=_C_DIM,
        anchor_point=(0.5, 0.5), anchored_position=(CX, PRESET_NAME_Y),
    )
    group.append(input_lbl)
    group.append(preset_lbl)

    # ── Preset quick-select buttons: numbered labels over the bitmap outlines ──
    # Positioned dynamically each draw_main() call, since the row layout
    # depends on how many presets state.preset_names actually has.
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
        anchor_point=(0.5, 1.0), anchored_position=(CX, 222),
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
        anchor_point=(0.5, 0.0), anchored_position=(CX, 8),
    )
    group.append(status_lbl)

    display.root_group = group
    display.auto_refresh = False   # we call refresh() explicitly after every draw

    return {
        "display":    display,
        "group":      group,      # main UI group; needed to restore after splash
        "bmp":        bmp,
        "vol_int":    vol_int,
        "vol_dec":    vol_dec,
        "input":      input_lbl,
        "preset":     preset_lbl,
        "filters":    filter_btns,
        "menu":       menu_lbl,
        "items":      menu_items,
        "status":     status_lbl,
        "_ptr_angle": None,   # last-drawn pointer angle; None = no pointer
    }


def draw_main(ui, state):
    """Render current AVRState to the display."""
    ui["status"].text = ""
    for ml in ui["items"]:
        ml.text = ""

    if state.power != "ON":
        ui["display"].brightness = state.brightness * _STANDBY_BRIGHTNESS_FRAC
        _render_gauge(ui["bmp"], 0, False, power_off=True)
        ui["_ptr_angle"] = None
        _hide_vol_and_status(ui)
        ui["display"].refresh()
        return

    ui["display"].brightness = getattr(state, "brightness", BRIGHTNESS_ON)
    ptr = _render_gauge(ui["bmp"], state.volume_db, state.muted)
    ui["_ptr_angle"] = ptr
    _set_vol_labels(ui, state.volume_db, state.muted)
    ui["input"].text  = _driver.friendly_input(state.input)
    ui["input"].color = _C_DIM
    ui["preset"].text  = _preset_name(state)
    ui["preset"].color = _C_DIM
    _draw_preset_filter_buttons(ui, state)
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
    ui["status"].text = ""
    for ml in ui["items"]:
        ml.text = ""

    ptr = _render_gauge(ui["bmp"], state.volume_db, False, busy=True)
    ui["_ptr_angle"] = ptr
    _set_vol_labels(ui, state.volume_db, busy=True)
    ui["input"].text   = _driver.friendly_input(state.input)
    ui["input"].color  = _C_BUSY
    ui["preset"].text  = _preset_name(state)
    ui["preset"].color = _C_BUSY
    for fl in ui["filters"]:
        fl.color = _C_DIM
    ui["menu"].text = ""
    ui["display"].refresh()


def draw_volume(ui, state):
    """Fast path: move the pointer wedge, update labels. No full re-render.

    Only the ~20-pixel pointer region becomes dirty → near-instant SPI transfer.
    """
    bmp = ui["bmp"]
    old = ui["_ptr_angle"]
    if old is not None:
        _restore_region(bmp, old, state.muted)
    if state.volume_db >= _VOL_MIN:
        new = _vol_to_angle(state.volume_db)
        _draw_pointer(bmp, new, _PTR)
        ui["_ptr_angle"] = new
    else:
        ui["_ptr_angle"] = None
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

    Shows the full gauge layout with --.- and a progress message
    where the input line would be, per the design spec.
    """
    _render_gauge(ui["bmp"], _SENTINEL, False)   # arc + ticks, no pointer
    ui["_ptr_angle"]    = None
    ui["vol_int"].text  = "--"; ui["vol_int"].color = _C_DIM
    ui["vol_dec"].text  = ".-"; ui["vol_dec"].color = _C_TEXT
    ui["input"].text    = msg
    ui["input"].color   = _C_DIM
    ui["preset"].text    = ""
    ui["menu"].text     = ""
    ui["status"].text   = ""
    for ml in ui["items"]:
        ml.text = ""
    for fl in ui["filters"]:
        fl.text = ""
    ui["display"].refresh()


def draw_error(ui, msg):
    """Error state: message at the top of the display.  Tap anywhere to restart."""
    _render_gauge(ui["bmp"], _SENTINEL, False)
    ui["_ptr_angle"] = None
    _hide_vol_and_status(ui)
    ui["status"].text  = msg[:24]
    ui["status"].color = _C_WARN
    for ml in ui["items"]:
        ml.text = ""
    ui["menu"].text  = "tap to restart"
    ui["menu"].color = _C_WARN
    ui["display"].refresh()


def render_gauge_bg(ui, vol_db, muted):
    """Re-render the gauge into the bitmap without refreshing.
    Call before draw_menu when the brightness screen needs the gauge background."""
    _render_gauge(ui["bmp"], vol_db, muted)


def draw_menu(ui, title, items, cursor, clear_bg=False):
    """Menu overlay.  clear_bg=True paints the bitmap black before drawing."""
    global MENU_ITEM_Y0
    if clear_bg:
        _clear(ui["bmp"])
    ui["status"].text  = ""    # no title; context comes from the items themselves
    ui["vol_int"].text = ""
    ui["vol_dec"].text = ""
    ui["input"].text   = ""
    ui["preset"].text   = ""
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


def flash_power_on(ui):
    """Brief splash shown when the AVR transitions to powered-on.

    Shows the splash for ~1 s then silently restores the main UI group.
    The caller's subsequent draw_main() call does the final refresh, so
    the user sees splash → main screen with no intermediate flicker.
    """
    show_splash(ui["display"])
    time.sleep(1.0)
    ui["display"].root_group = ui["group"]   # restore; do NOT refresh yet
