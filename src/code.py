# code.py -- deloop main app (CircuitPython entry point)
#
# Input model:
#   - Rotate encoder     -> volume (in MAIN mode) or navigate menu (in MENU mode)
#                           (if muted, stays blue during the turn and
#                           unmutes with a full-color reveal once it stops)
#   - Encoder press      -> open menu / select item / confirm
#   - Touch tap          -> above the preset name line: mute toggle; at/below
#                           it: preset quick-select button, else no-op
#                           (MAIN mode); select / close (MENU mode)
#   - Touch 1.5 s hold   -> power toggle (MAIN mode only, if the driver
#                           supports it -- see driver.CAPS["power"])
#   - Poll adaptive      -> device ground-truth sync

import time
import gc
import board
import rotaryio
import digitalio
import microcontroller
import wifi
import socketpool
import adafruit_requests
from adafruit_focaltouch import Adafruit_FocalTouch

import config
import driver
import dial_ui
import sound
from state import AVRState

_MAX_ERRORS = 5

# Menu mode constants
MODE_MAIN     = 0
MODE_MENU_TOP = 1   # top-level menu
MODE_MENU_DEV = 2   # Device submenu (Brightness, Sound, Restart)
MODE_MENU_SUB = 3   # leaf submenu (inputs, presets, brightness adjuster, sound picker)
MODE_ERROR    = 4   # network/AVR error – tap anywhere to restart

# NVM slot for persisting brightness (byte 0 = brightness * 100)
_NVM_BRIGHTNESS = 0
# NVM slot for persisting sound on/off (byte 1: 0=off, any other=on)
_NVM_SOUND      = 1

# Encoder debounce (volume mode only)
ENC_DEBOUNCE_S = 0.15

# Touch long-press / tap thresholds
TOUCH_POWER_S   = 1.5
TOUCH_MIN_S     = 0.05  # minimum tap duration; filters electrical noise
MENU_TAP_Y      = 195   # pixels from top: taps below this open the menu
SWIPE_THRESHOLD = 50    # pixels; horizontal delta to register a swipe

# Menu auto-close
MENU_TIMEOUT = 8.0   # auto-close menu after this many seconds idle

# Mute pulse: throttle how often we bother re-rendering / testing the trough.
# The animation itself (period, floor, trough width) lives in dial_ui.pulse_mute().
PULSE_FRAME_S = 1.0 / 30   # ~30 fps

# Touch poll: the main loop has no sleep and spins as fast as the CPU allows,
# but the FocalTouch controller itself only reports at ~60-120Hz -- polling
# every iteration (thousands/sec while a finger is down) just burns CPU and
# allocates a fresh touches list on every call for no benefit. Gate it like
# the pulse frame rate above.
TOUCH_FRAME_S = 1.0 / 60   # ~60 fps


def _load_brightness():
    try:
        b = microcontroller.nvm[_NVM_BRIGHTNESS]
        if b == 255:  # uninitialized flash (erased state)
            return dial_ui.BRIGHTNESS_ON
        return max(0.05, min(1.0, b / 100.0))
    except Exception:
        return dial_ui.BRIGHTNESS_ON

def _save_brightness(brightness):
    try:
        microcontroller.nvm[_NVM_BRIGHTNESS] = int(round(brightness * 100))
    except Exception as e:
        print("save_brightness:", e)


def _load_sound():
    try:
        return microcontroller.nvm[_NVM_SOUND] != 0  # 255=erased=on, 0=off
    except Exception:
        return True

def _save_sound(val):
    try:
        microcontroller.nvm[_NVM_SOUND] = 1 if val else 0
    except Exception as e:
        print("save_sound:", e)


def _connect_wifi(ui):
    if not config.WIFI_SSID:
        dial_ui.draw_error(ui, "No WiFi cfg")
        raise RuntimeError("WIFI_SSID not set in settings.toml")
    dial_ui.draw_status(ui, "WiFi...")
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


def _top_menu_entries():
    """(sub_type, label) pairs for the top-level menu, filtered by what the
    active driver actually supports -- see driver.CAPS / driver.LABELS."""
    entries = []
    if driver.CAPS["input_select"]:
        entries.append(("input", driver.LABELS["input_select"]))
    if driver.CAPS["presets"]:
        entries.append(("preset", driver.LABELS["presets"]))
    entries.append(("device", "Device"))
    return entries


def _build_top_menu():
    return [label for _, label in _top_menu_entries()]


def _build_dev_menu():
    return ["Brightness", "Sound", "Restart"]


def _build_sub_items(sub_type, state):
    """Return list of display strings for a submenu."""
    if sub_type == "input":
        return [name for _, name in state.input_names]
    if sub_type == "preset":
        return [name for _, name in state.preset_names]
    if sub_type == "brightness":
        pct = int(round(state.brightness * 100))
        return ["            {} %\n(rotate to adjust)".format(pct)]
    if sub_type == "sound":
        return ["On", "Off"]
    return []


def _open_submenu(sub_type, label, state):
    """Resolve a non-Device top-menu entry into (sub_type, sub_cursor, title, items)."""
    sc = 0
    if sub_type == "input":
        indices = [i for i, _ in state.input_names]
        if state.input_index in indices:
            sc = indices.index(state.input_index)
    elif sub_type == "preset":
        vals = [v for v, _ in state.preset_names]
        if state.preset in vals:
            sc = vals.index(state.preset)
    return sub_type, sc, label.upper(), _build_sub_items(sub_type, state)


def _enter_dev_sub(item, state, ui, sound_mod):
    """Open the leaf submenu for a Device menu item. Returns (sub_type, sub_cursor)."""
    if item == "Brightness":
        dial_ui.render_gauge_bg(ui, state.volume_db, state.muted)
        dial_ui.draw_menu(ui, "BRIGHTNESS", _build_sub_items("brightness", state), 0)
        return "brightness", 0
    elif item == "Sound":
        sc = -1   # no pre-selection; user taps or rotates to choose
        dial_ui.draw_menu(ui, "SOUND", _build_sub_items("sound", state), sc, clear_bg=True)
        return "sound", sc
    return None, 0


def _confirm_sub(sub_type, sub_cursor, state, ui):
    """Apply sub-menu selection (side-effects only)."""
    if sub_type == "input":
        index, _ = state.input_names[sub_cursor]
        try:
            driver.set_input(index)
            state.input_index = index
        except Exception as e:
            print("set_input:", e)
    elif sub_type == "preset":
        val, _ = state.preset_names[sub_cursor]
        try:
            # Some backends (confirmed on MiniDSP: ~4-10s) block for real
            # seconds applying a preset change -- paint a static "please
            # wait" frame first since the loop can't animate meanwhile.
            dial_ui.draw_busy(ui, state)
            driver.set_preset(val)
            # Optimistic, like volume/mute elsewhere -- some backends (Denon)
            # take several seconds to actually apply a preset change, so an
            # immediate re-query would just read back stale state.
            state.preset = val
            # Only flip to enabled if this backend's set_preset() actually
            # does that itself (see CAPS["preset_select_enables"] in
            # driver.py) -- on MiniDSP, preset and Dirac on/off are
            # independent, and a slot may deliberately want Dirac left off
            # (e.g. a headphone config), so switching slots must leave
            # state.preset_enabled exactly as it was.
            if driver.CAPS["preset_select_enables"]:
                state.preset_enabled = True
        except Exception as e:
            print("set_preset:", e)
    elif sub_type == "brightness":
        _save_brightness(state.brightness)
    elif sub_type == "sound":
        sound.enabled = (sub_cursor == 0)   # 0=On, 1=Off
        _save_sound(sound.enabled)


def _scroll_for(cursor, scroll, num_items):
    """Return updated scroll offset so cursor stays in the visible window."""
    vis = dial_ui.MENU_VISIBLE
    if cursor < scroll:
        scroll = cursor
    elif cursor >= scroll + vis:
        scroll = cursor - vis + 1
    return max(0, min(scroll, max(0, num_items - vis)))


def _menu_item_at_y(y):
    """Return visible item index [0..MENU_VISIBLE-1] for a touch at y, or -1."""
    half = dial_ui.MENU_ITEM_DY // 2 - 2
    for i in range(dial_ui.MENU_VISIBLE):
        if abs(y - (dial_ui.MENU_ITEM_Y0 + i * dial_ui.MENU_ITEM_DY)) <= half:
            return i
    return -1


# ---------------------------------------------------------------------------
# Main-loop state
# ---------------------------------------------------------------------------

class _Loop:
    """Mutable state carried between iterations of the main loop.

    A plain attribute bag, not a state machine — MODE_* dispatch stays in the
    handler functions below. This just gives them something to read from and
    write to without relying on `nonlocal`.
    """

    def __init__(self):
        self.last_pos         = 0   # overwritten with encoder.position before the loop starts
        self.last_btn_pressed = False

        self.enc_last_move = 0.0
        self.enc_last_tick = 0.0

        self.touch_start       = 0.0
        self.touch_x           = 0   # coordinates captured at touch-down
        self.touch_y           = 0
        self.touch_x_start     = 0   # x at touch-down (for swipe detection)
        self.touch_power_fired = False
        self.last_touch_poll   = 0.0   # throttles touch reads -- see TOUCH_FRAME_S

        self.last_poll   = time.monotonic() + 2.0
        self.error_count = 0
        self.first_poll  = True

        self.mode        = MODE_MAIN
        self.menu_cursor = 0
        self.dev_cursor  = 0    # cursor within Device submenu
        self.sub_cursor  = 0
        self.sub_scroll  = 0    # first visible item in sub-menu (for long lists)
        self.sub_type    = None    # "input", "preset", "brightness", "sound"
        self.menu_idle   = 0.0     # monotonic time of last menu interaction

        self.mute_phase_origin  = 0.0     # monotonic time the current pulse cycle started
        self.mute_trough_polled = False   # guards against polling every tick while under threshold
        self.pulse_last_frame   = 0.0

        self.power_fade_start = 0.0   # monotonic time the power-off fade began; 0.0 = not fading
        self.power_fade_from  = dial_ui.BRIGHTNESS_ON   # display brightness when the fade began


# ---------------------------------------------------------------------------
# Encoder button -- open/navigate/confirm menu
# ---------------------------------------------------------------------------

def _handle_encoder_button(loop, ui, state, btn, now):
    btn_pressed = not btn.value
    fired = btn_pressed and not loop.last_btn_pressed and state.power == "ON"
    loop.last_btn_pressed = btn_pressed
    if not fired:
        return

    loop.menu_idle = now

    if loop.mode == MODE_MAIN:
        # Enter top-level menu
        loop.mode = MODE_MENU_TOP
        loop.menu_cursor = -1   # no item pre-selected; rotary or tap to choose
        dial_ui.draw_menu(ui, "", _build_top_menu(), loop.menu_cursor, clear_bg=True)
        return

    if loop.mode == MODE_MENU_TOP:
        if loop.menu_cursor < 0:
            return   # nothing highlighted yet; ignore button until rotary selects
        sub_type, label = _top_menu_entries()[loop.menu_cursor]
        if sub_type == "device":
            loop.dev_cursor = -1
            loop.mode = MODE_MENU_DEV
            dial_ui.draw_menu(ui, "DEVICE", _build_dev_menu(), loop.dev_cursor, clear_bg=True)
        else:
            loop.sub_type, loop.sub_cursor, title, items = _open_submenu(sub_type, label, state)
            loop.sub_scroll = _scroll_for(loop.sub_cursor, 0, len(items))
            loop.mode = MODE_MENU_SUB
            vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
            dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True)
        return

    if loop.mode == MODE_MENU_DEV:
        if loop.dev_cursor < 0:
            return   # nothing highlighted yet
        item = _build_dev_menu()[loop.dev_cursor]
        if item == "Restart":
            microcontroller.reset()
        else:
            loop.sub_type, loop.sub_cursor = _enter_dev_sub(item, state, ui, sound)
            loop.sub_scroll = 0
            loop.mode = MODE_MENU_SUB
        return

    if loop.mode == MODE_MENU_SUB:
        if loop.sub_cursor >= 0:
            _confirm_sub(loop.sub_type, loop.sub_cursor, state, ui)
        # sub_cursor < 0: no item selected yet — button press closes without applying
        loop.mode = MODE_MAIN
        dial_ui.exit_menu(ui)
        dial_ui.draw_main(ui, state)


# ---------------------------------------------------------------------------
# Encoder rotation -- volume in MAIN, navigation in MENU
# ---------------------------------------------------------------------------

def _handle_encoder_rotation(loop, ui, state, encoder, now):
    pos = encoder.position
    if pos == loop.last_pos:
        return
    delta = pos - loop.last_pos
    loop.last_pos = pos

    if state.power != "ON":
        return   # ignore encoder when standby

    loop.menu_idle = now

    if loop.mode == MODE_MAIN:
        # Accelerated volume control
        elapsed_ms = (now - loop.enc_last_tick) * 1000 if loop.enc_last_tick > 0.0 else 999
        loop.enc_last_tick = now
        # Divide by |delta| to get ms-per-tick: when spinning fast,
        # multiple ticks accumulate per loop iteration so elapsed_ms
        # alone is always too large to trigger the threshold.
        ms_per_tick = elapsed_ms / abs(delta) if abs(delta) > 1 else elapsed_ms
        fast = ms_per_tick < config.ACCEL_THRESHOLD_MS
        step = config.VOLUME_STEP_FAST if fast else config.VOLUME_STEP

        state.apply_volume_delta(-delta, step=step)

        dial_ui.draw_volume(ui, state)
        loop.enc_last_move = now
        return

    if loop.mode == MODE_MENU_TOP:
        items = _build_top_menu()
        loop.menu_cursor = 0 if loop.menu_cursor < 0 else (loop.menu_cursor - delta) % len(items)
        dial_ui.draw_menu(ui, "", items, loop.menu_cursor, clear_bg=True)
        return

    if loop.mode == MODE_MENU_DEV:
        dev_items = _build_dev_menu()
        loop.dev_cursor = 0 if loop.dev_cursor < 0 else (loop.dev_cursor - delta) % len(dev_items)
        dial_ui.draw_menu(ui, "DEVICE", dev_items, loop.dev_cursor, clear_bg=True)
        return

    if loop.mode == MODE_MENU_SUB:
        if loop.sub_type == "brightness":
            # Adjust brightness live; clamp to [0.05, 1.0] in 0.05 steps
            state.brightness = max(0.05, min(1.0, state.brightness - delta * 0.05))
            ui["display"].brightness = state.brightness
            dial_ui.draw_menu(ui, "BRIGHTNESS", _build_sub_items("brightness", state), 0)
            return

        items = _build_sub_items(loop.sub_type, state)
        loop.sub_cursor = 0 if loop.sub_cursor < 0 else (loop.sub_cursor - delta) % len(items)
        loop.sub_scroll = _scroll_for(loop.sub_cursor, loop.sub_scroll, len(items))
        if loop.sub_type == "input":     title = driver.LABELS["input_select"].upper()
        elif loop.sub_type == "preset":  title = driver.LABELS["presets"].upper()
        elif loop.sub_type == "sound":   title = "SOUND"
        else:                            title = loop.sub_type.upper()
        vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
        dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True)


# ---------------------------------------------------------------------------
# Volume debounce send (MAIN mode only)
# ---------------------------------------------------------------------------

def _send_volume_debounced(loop, ui, state, now):
    """After the encoder stops moving, push the settled volume to the AVR.

    If the whole turn happened while muted, this is also the reveal moment:
    send the unmute, then do the one full redraw that swaps the display
    back from blue to the normal gradient and colors.
    """
    if loop.mode != MODE_MAIN or loop.enc_last_move <= 0.0:
        return
    if (now - loop.enc_last_move) < ENC_DEBOUNCE_S:
        return

    loop.enc_last_move = 0.0
    was_muted = state.muted
    try:
        if was_muted:
            driver.mute_off()
            state.muted = False
        driver.set_volume(state.volume_db)
        loop.error_count = 0
    except Exception as e:
        print("volume debounce send:", e)
        loop.error_count += 1
    if was_muted and not state.muted:
        dial_ui.draw_main(ui, state)
    loop.last_poll = time.monotonic()


# ---------------------------------------------------------------------------
# Menu auto-close after idle timeout (not in error state)
# ---------------------------------------------------------------------------

def _auto_close_menu(loop, ui, state, now):
    if loop.mode in (MODE_MAIN, MODE_ERROR):
        return
    if loop.menu_idle <= 0.0 or (now - loop.menu_idle) < MENU_TIMEOUT:
        return

    loop.mode = MODE_MAIN
    dial_ui.exit_menu(ui)
    dial_ui.draw_main(ui, state)


# ---------------------------------------------------------------------------
# Touch -- mute/power in MAIN, tap to navigate in MENU
# ---------------------------------------------------------------------------

def _read_touch_point(touch):
    """Return (x, y) of the first active touch point, or None."""
    try:
        pts = touch.touches
    except Exception:
        return None
    if not pts:
        return None
    return pts[0]['x'], pts[0]['y']


def _handle_touch_down(loop, ui, state, touch, now):
    """Touch held down: track position, fire long-press power toggle."""
    if loop.touch_start == 0.0:
        loop.touch_start = now
        pt = _read_touch_point(touch)
        if pt:
            loop.touch_x, loop.touch_y = pt
            loop.touch_x_start = loop.touch_x
    else:
        # Track current position so release has the final x for swipe detection
        pt = _read_touch_point(touch)
        if pt:
            loop.touch_x = pt[0]

    held = now - loop.touch_start
    if not driver.CAPS["power"] or loop.touch_power_fired or held < TOUCH_POWER_S:
        return

    # Power toggle must be checked first – it works in any power state
    # (this is the only way to wake the AVR from standby).
    loop.touch_power_fired = True
    sound.click_heavy()
    try:
        if state.power == "ON":
            driver.power_standby()
            state.power = "STANDBY"
            # Ease brightness down instead of an instant cut; _fade_power_off
            # swaps in the power icon once dark and hands off to its pulse.
            loop.power_fade_from  = state.brightness
            loop.power_fade_start = now
        else:
            driver.power_on()
            state.power = "ON"
            # Clear stale mute/volume so the display is clean
            # while the AVR boots. Poll will fill in real values.
            state.muted = False
            state.volume_db = config.VOLUME_MIN
            dial_ui.flash_power_on(ui)
            dial_ui.draw_main(ui, state)
        loop.last_poll = time.monotonic() - 4.0  # poll in ~1s (5s interval - 4s)
        loop.error_count = 0
    except Exception as e:
        print("power:", e)
        loop.error_count += 1


def _swipe_back(loop, ui, state, now):
    """Swipe left -> go back one menu level."""
    sound.click()
    loop.menu_idle = now
    if loop.mode == MODE_MENU_SUB:
        if loop.sub_type in ("brightness", "sound"):
            loop.mode = MODE_MENU_DEV
            dial_ui.draw_menu(ui, "DEVICE", _build_dev_menu(), loop.dev_cursor, clear_bg=True)
        else:
            loop.mode = MODE_MENU_TOP
            dial_ui.draw_menu(ui, "", _build_top_menu(), loop.menu_cursor, clear_bg=True)
    elif loop.mode == MODE_MENU_DEV:
        loop.mode = MODE_MENU_TOP
        dial_ui.draw_menu(ui, "", _build_top_menu(), loop.menu_cursor, clear_bg=True)
    else:  # MODE_MENU_TOP
        loop.mode = MODE_MAIN
        dial_ui.exit_menu(ui)
        dial_ui.draw_main(ui, state)


def _tap_menu_top(loop, ui, state, now):
    tapped = _menu_item_at_y(loop.touch_y)
    top_items = _build_top_menu()
    if not (0 <= tapped < len(top_items)):
        sound.click()
        loop.mode = MODE_MAIN
        dial_ui.exit_menu(ui)
        dial_ui.draw_main(ui, state)
        return

    sound.click()
    loop.menu_idle = now
    loop.menu_cursor = tapped
    sub_type, label = _top_menu_entries()[loop.menu_cursor]
    if sub_type == "device":
        loop.dev_cursor = -1
        loop.mode = MODE_MENU_DEV
        dial_ui.draw_menu(ui, "DEVICE", _build_dev_menu(), loop.dev_cursor, clear_bg=True)
    else:
        loop.sub_type, loop.sub_cursor, title, items = _open_submenu(sub_type, label, state)
        loop.sub_scroll = _scroll_for(loop.sub_cursor, 0, len(items))
        loop.mode = MODE_MENU_SUB
        vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
        dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True)


def _tap_menu_dev(loop, ui, state, now):
    tapped = _menu_item_at_y(loop.touch_y)
    dev_items = _build_dev_menu()
    if not (0 <= tapped < len(dev_items)):
        sound.click()
        loop.mode = MODE_MENU_TOP
        dial_ui.draw_menu(ui, "", _build_top_menu(), loop.menu_cursor, clear_bg=True)
        return

    sound.click()
    loop.menu_idle = now
    loop.dev_cursor = tapped
    item = dev_items[loop.dev_cursor]
    if item == "Restart":
        microcontroller.reset()
    else:
        loop.sub_type, loop.sub_cursor = _enter_dev_sub(item, state, ui, sound)
        loop.sub_scroll = 0
        loop.mode = MODE_MENU_SUB


def _tap_menu_sub(loop, ui, state, now):
    tapped = _menu_item_at_y(loop.touch_y)
    items = _build_sub_items(loop.sub_type, state)
    vis_count = min(dial_ui.MENU_VISIBLE, len(items) - loop.sub_scroll)
    if 0 <= tapped < vis_count:
        # Tap a visible item -> select + confirm it
        sound.click()
        loop.menu_idle = now
        loop.sub_cursor = loop.sub_scroll + tapped
        _confirm_sub(loop.sub_type, loop.sub_cursor, state, ui)
    else:
        sound.click()
    loop.mode = MODE_MAIN
    dial_ui.exit_menu(ui)
    dial_ui.draw_main(ui, state)


def _tap_open_menu(loop, ui, now):
    """Tap on MENU area -> open top-level menu."""
    sound.click()
    loop.menu_idle = now
    loop.mode = MODE_MENU_TOP
    loop.menu_cursor = -1
    dial_ui.draw_menu(ui, "", _build_top_menu(), loop.menu_cursor, clear_bg=True)


def _start_mute_pulse(loop, now):
    loop.mute_phase_origin  = now
    loop.mute_trough_polled = False


def _tap_toggle_mute(loop, ui, state, now):
    """Tap anywhere else -> toggle mute."""
    sound.click()
    try:
        if state.muted:
            driver.mute_off()
            state.muted = False
        else:
            driver.mute_on()
            state.muted = True
            _start_mute_pulse(loop, now)
        dial_ui.draw_main(ui, state)
        loop.last_poll = time.monotonic()
        loop.error_count = 0
    except Exception as e:
        print("mute:", e)
        loop.error_count += 1


def _tap_toggle_playback(loop, ui, state, now):
    """Tap the Playing/Paused status text -> toggle HA media playback.

    Only ever reachable when state.media_state is "playing"/"paused" (see
    _tap_main_screen) -- backends that never set media_state (denon,
    minidsp) never populate that text or its tap zone in the first place.
    """
    sound.click()
    try:
        if state.media_state == "playing":
            driver.media_pause()
            state.media_state = "paused"
        else:
            driver.media_play()
            state.media_state = "playing"
        dial_ui.draw_main(ui, state)
        loop.last_poll = time.monotonic()
        loop.error_count = 0
    except Exception as e:
        print("media play/pause:", e)
        loop.error_count += 1


def _tap_main_screen(loop, ui, state, now):
    """Tap above the preset name line -> toggle mute. At or below that line,
    only the quick-select buttons respond -- everything else there is a
    no-op, so the button row doesn't accidentally toggle mute too. The
    play/pause status row (only populated when state.media_state is
    "playing"/"paused") is checked first since it overlaps the top of that
    same "above the line" mute zone."""
    if state.media_state in ("playing", "paused") and dial_ui.media_status_tap(loop.touch_x, loop.touch_y):
        _tap_toggle_playback(loop, ui, state, now)
        return

    if loop.touch_y < dial_ui.PRESET_NAME_Y:
        _tap_toggle_mute(loop, ui, state, now)
        return

    idx = dial_ui.preset_button_at(loop.touch_x, loop.touch_y, len(state.preset_names))
    if idx == -1:
        return

    vals = [v for v, _ in state.preset_names]
    selected_idx = vals.index(state.preset) if state.preset in vals else -1

    if idx == selected_idx and not driver.CAPS["preset_enable"]:
        return   # nothing to toggle -- this slot has no on/off concept

    sound.click()
    try:
        # Some backends (confirmed on MiniDSP: ~4-10s) block for real
        # seconds applying a preset change -- paint a static "please wait"
        # frame first since the loop can't animate meanwhile.
        dial_ui.draw_busy(ui, state)
        if idx == selected_idx:
            # Tapping the already-active slot toggles it in place instead of
            # switching slots -- keeps "which config is loaded" visible (see
            # dial_ui.py's button coloring) even while disabled.
            new_enabled = not state.preset_enabled
            driver.set_preset_enabled(new_enabled)
            state.preset_enabled = new_enabled
        else:
            val, _name = state.preset_names[idx]
            driver.set_preset(val)
            state.preset = val
            # Only flip to enabled if this backend's set_preset() actually
            # does that itself (see CAPS["preset_select_enables"] in
            # driver.py) -- on MiniDSP, preset and Dirac on/off are
            # independent, and a slot may deliberately want Dirac left off
            # (e.g. a headphone config), so switching slots must leave
            # state.preset_enabled exactly as it was.
            if driver.CAPS["preset_select_enables"]:
                state.preset_enabled = True
        # Optimistic, like volume/mute elsewhere -- some backends (Denon)
        # take several seconds to actually apply a preset change, so an
        # immediate re-query would just read back stale state.
        dial_ui.draw_main(ui, state)
        loop.last_poll = time.monotonic()
        loop.error_count = 0
    except Exception as e:
        print("set_preset (quick):", e)
        loop.error_count += 1
        dial_ui.draw_main(ui, state)   # clear the busy frame even on failure


def _dispatch_tap(loop, ui, state, now):
    """Route a completed tap by current mode and touch zone."""
    if loop.mode == MODE_ERROR:
        # Error screen: any tap restarts the device
        microcontroller.reset()
        return

    if state.power != "ON":
        return   # standby taps handled elsewhere (power long-press only)

    if (loop.touch_x - loop.touch_x_start < -SWIPE_THRESHOLD
            and loop.mode in (MODE_MENU_TOP, MODE_MENU_DEV, MODE_MENU_SUB)):
        _swipe_back(loop, ui, state, now)
        return

    # Clean intentional tap – route by zone and mode
    if loop.mode == MODE_MENU_TOP:
        _tap_menu_top(loop, ui, state, now)
    elif loop.mode == MODE_MENU_DEV:
        _tap_menu_dev(loop, ui, state, now)
    elif loop.mode == MODE_MENU_SUB:
        _tap_menu_sub(loop, ui, state, now)
    elif loop.mode == MODE_MAIN and loop.touch_y >= MENU_TAP_Y:
        _tap_open_menu(loop, ui, now)
    elif loop.mode == MODE_MAIN:
        _tap_main_screen(loop, ui, state, now)


def _handle_touch_released(loop, ui, state, now):
    held = (now - loop.touch_start) if loop.touch_start > 0.0 else 0.0
    if held >= TOUCH_MIN_S and not loop.touch_power_fired:
        _dispatch_tap(loop, ui, state, now)

    loop.touch_start       = 0.0
    loop.touch_x           = 0
    loop.touch_y           = 0
    loop.touch_x_start     = 0
    loop.touch_power_fired = False


def _handle_touch(loop, ui, state, touch, touch_ok, now):
    if (now - loop.last_touch_poll) < TOUCH_FRAME_S:
        return
    loop.last_touch_poll = now

    try:
        is_touched = touch_ok and touch.touched > 0
    except Exception:
        is_touched = False

    if is_touched:
        _handle_touch_down(loop, ui, state, touch, now)
    else:
        _handle_touch_released(loop, ui, state, now)


# ---------------------------------------------------------------------------
# Periodic AVR poll
# ---------------------------------------------------------------------------

def _poll_now(loop, ui, state, now):
    """Fetch AVR ground truth and reconcile local state.

    Shared by the adaptive timer poll (_poll_avr) and the mute-pulse trough
    poll (_pulse_mute) -- only the trigger condition differs between them.
    Returns True if the AVR was just detected powering on externally, so a
    timer-driven caller can reschedule its next poll sooner.
    """
    powered_on_externally = False
    try:
        was_standby = state.power != "ON"
        was_on      = state.power == "ON"
        was_muted   = state.muted
        status = driver.get_status()
        changed = state.apply_status(status)

        if was_standby and state.power == "ON" and not loop.first_poll:
            # AVR just powered on externally – show splash then normal UI.
            state.volume_db = config.VOLUME_MIN
            changed = True
            dial_ui.flash_power_on(ui)
            powered_on_externally = True

        if was_on and state.power != "ON" and not loop.first_poll:
            # AVR went to standby from its own remote/panel -- same fade as
            # the local long-press, so the transition feels the same either way.
            loop.power_fade_from  = state.brightness
            loop.power_fade_start = now
            changed = False   # _fade_power_off draws the final frame once it settles

        if state.muted and not was_muted:
            # Muted from the AVR's own remote/app -- start the pulse fresh.
            _start_mute_pulse(loop, now)

        if loop.mode == MODE_ERROR:
            # Recovered after a run of failed polls -- leave the error
            # screen and force a redraw even if nothing else changed.
            loop.mode = MODE_MAIN
            changed = True

        loop.first_poll = False
        if changed:
            dial_ui.draw_main(ui, state)
        loop.error_count = 0
    except Exception as e:
        print("poll:", e)
        loop.error_count += 1
        if loop.error_count >= _MAX_ERRORS and loop.mode != MODE_ERROR:
            loop.mode = MODE_ERROR
            dial_ui.draw_error(ui, "No AVR")
    return powered_on_externally


def _poll_avr(loop, ui, state, now):
    """Ground-truth sync against the AVR; adaptive interval.

    Interval logic:
      - AVR is in STANDBY: always 5s -- detect power-on quickly
      - Recently active (encoder/touch in last 30s): POLL_INTERVAL_S (30s)
        -- prevents polls chaining immediately after commands
      - Truly idle: 5s -- catch remote/app changes promptly
    Always skipped while the encoder is moving, or while muted in MAIN with
    config.MUTE_PULSE on (_pulse_mute polls at the pulse trough instead --
    with the pulse off there's no trough to hide a poll's pause in, so this
    just runs on the normal schedule like any other time).
    """
    if loop.mode == MODE_MAIN and state.power == "ON" and state.muted and config.MUTE_PULSE:
        return

    in_standby = state.power != "ON"
    last_action = max(loop.enc_last_move, loop.enc_last_tick)
    recently_active = (now - last_action) < config.POLL_INTERVAL_S
    poll_interval = 5.0 if (in_standby or not recently_active) else config.POLL_INTERVAL_S

    encoder_idle = (loop.enc_last_move == 0.0)
    if not encoder_idle or (now - loop.last_poll) < poll_interval:
        return

    loop.last_poll = now
    # Same insurance as the boot-time fetches: reclaim garbage from whatever
    # ran since the last poll before asking for a fresh response buffer. The
    # device runs unattended for long stretches, and MicroPython's GC doesn't
    # compact, so this is a cheap hedge against the kind of heap fragmentation
    # that broke the Dirac filter fetch at boot.
    gc.collect()
    if _poll_now(loop, ui, state, now):
        loop.last_poll = now - poll_interval + 1.0


def _pulse_mute(loop, ui, state, now):
    """While muted and resting in MAIN, breathe the volume number and use
    the pulse's natural pause at the bottom of each cycle to sneak in an
    AVR poll. Suspended while the encoder is actively turning -- see
    _handle_encoder_rotation and _send_volume_debounced for that path.

    A no-op entirely when config.MUTE_PULSE is off -- the volume number
    just stays the static muted color _set_vol_labels() already set, and
    _poll_avr() falls back to its normal adaptive schedule instead of
    relying on the trough poll this function would otherwise provide.
    """
    if not config.MUTE_PULSE:
        return
    if loop.mode != MODE_MAIN or state.power != "ON" or not state.muted:
        return
    if loop.enc_last_move > 0.0:
        return   # actively spinning -- draw_volume owns the display for now
    if (now - loop.pulse_last_frame) < PULSE_FRAME_S:
        return
    loop.pulse_last_frame = now

    elapsed = now - loop.mute_phase_origin
    if dial_ui.mute_pulse_at_trough(elapsed):
        if not loop.mute_trough_polled:
            loop.mute_trough_polled = True
            _poll_now(loop, ui, state, now)   # may redraw; the render below always wins
    else:
        loop.mute_trough_polled = False

    # Render last: guarantees this frame's color is what's actually on
    # screen even if _poll_now() just ran a full draw_main().
    dial_ui.pulse_mute(ui, now - loop.mute_phase_origin)


def _fade_power_off(loop, ui, state, now):
    """One-shot brightness ease after powering off, toward the static
    standby brightness draw_main() uses -- hides that redraw at the least
    visible moment, same trick as the mute-pulse trough poll."""
    if loop.power_fade_start <= 0.0:
        return
    elapsed = now - loop.power_fade_start
    done = dial_ui.fade_power_off(ui, elapsed, loop.power_fade_from, state)
    if done:
        loop.power_fade_start = 0.0
        dial_ui.draw_main(ui, state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # ── Startup splash ────────────────────────────────────────────────────
    _splash_t = time.monotonic()
    dial_ui.show_splash(board.DISPLAY)

    # --- Hardware inputs ---
    encoder = rotaryio.IncrementalEncoder(board.ENC_A, board.ENC_B)

    btn = digitalio.DigitalInOut(board.KNOB_BUTTON)
    btn.direction = digitalio.Direction.INPUT
    btn.pull = digitalio.Pull.UP

    i2c = board.I2C()
    try:
        touch = Adafruit_FocalTouch(i2c, address=0x38)
        touch_ok = True
    except Exception as e:
        print("touch init failed:", e)
        touch = None
        touch_ok = False

    # --- UI (replaces the splash group) ---
    ui = dial_ui.init()

    # Enforce 2-second minimum splash display time
    _remaining = 2.0 - (time.monotonic() - _splash_t)
    if _remaining > 0.0:
        time.sleep(_remaining)

    # --- WiFi ---
    try:
        _connect_wifi(ui)
    except Exception as e:
        dial_ui.draw_error(ui, "WiFi fail")
        print("WiFi error:", e)
        while True:
            try:
                if touch_ok and touch.touched > 0:
                    microcontroller.reset()
            except Exception:
                pass

    # --- HTTP session (device backend selected by config.DEVICE_DRIVER) ---
    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool)
    driver.init(session)

    # Fetch input friendly names and source list (for menu) once at boot.
    # No-ops for drivers that don't support input selection (driver.CAPS).
    try:
        driver.load_input_names()
    except Exception as e:
        print("load_input_names:", e)

    try:
        driver.load_source_list()
    except Exception as e:
        print("load_source_list:", e)

    # --- device state ---
    state = AVRState()
    state.brightness = _load_brightness()
    sound.enabled    = _load_sound()
    ui["display"].brightness = state.brightness

    # gc.collect() before each boot-time fetch: reclaims fragmented memory
    # from the previous request/response before asking for the next buffer.
    # Whichever fetch runs last in this sequence is the one most exposed to
    # cumulative heap fragmentation, so this matters most right here.
    gc.collect()
    try:
        state.input_index, state.input_names = driver.get_inputs()
    except Exception as e:
        print("get_inputs:", e, "| free mem:", gc.mem_free())

    gc.collect()
    try:
        state.preset, state.preset_names = driver.get_presets()
        state.preset_enabled = driver.get_preset_enabled()
    except Exception as e:
        print("get_presets:", e, "| free mem:", gc.mem_free())

    loop = _Loop()
    loop.last_pos = encoder.position

    # --- Main loop ---
    while True:
        now = time.monotonic()

        _handle_encoder_button(loop, ui, state, btn, now)
        _handle_encoder_rotation(loop, ui, state, encoder, now)
        _send_volume_debounced(loop, ui, state, now)
        _auto_close_menu(loop, ui, state, now)
        _handle_touch(loop, ui, state, touch, touch_ok, now)
        _pulse_mute(loop, ui, state, now)
        _fade_power_off(loop, ui, state, now)
        _poll_avr(loop, ui, state, now)


main()
