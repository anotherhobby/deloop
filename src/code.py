# code.py -- deloop main app (CircuitPython entry point)
#
# Input model:
#   - Rotate encoder     -> volume (in MAIN mode) or navigate menu (in MENU mode)
#   - Encoder press      -> open menu / select item / confirm
#   - Touch 0.5 s hold   -> mute toggle (MAIN mode only)
#   - Touch 1.5 s hold   -> power toggle (MAIN mode only)
#   - Touch tap (<0.5 s) -> close menu (MENU mode)
#   - Poll adaptive      -> AVR ground-truth sync

import time
import board
import rotaryio
import digitalio
import microcontroller
import wifi
import socketpool
import adafruit_requests
from adafruit_focaltouch import Adafruit_FocalTouch

import config
import denon
import dial_ui
import sound
from state import AVRState

_MAX_ERRORS = 5

# Menu mode constants
MODE_MAIN     = 0
MODE_MENU_TOP = 1   # top-level menu
MODE_MENU_DEV = 2   # Device submenu (Brightness, Sound, Restart)
MODE_MENU_SUB = 3   # leaf submenu (inputs, Dirac, brightness adjuster, sound picker)
MODE_ERROR    = 4   # network/AVR error – tap anywhere to restart

# NVM slot for persisting brightness (byte 0 = brightness * 100)
_NVM_BRIGHTNESS = 0
# NVM slot for persisting sound on/off (byte 1: 0=off, any other=on)
_NVM_SOUND      = 1

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


def _build_top_menu():
    return ["Input", "Dirac Live", "Device"]


def _build_dev_menu():
    return ["Brightness", "Sound", "Restart"]


def _build_sub_items(sub_type, state):
    """Return list of display strings for a submenu."""
    if sub_type == "input":
        return [name for _, name in state.input_names]
    if sub_type == "dirac":
        return [name for _, name in state.dirac_names]
    if sub_type == "brightness":
        pct = int(round(state.brightness * 100))
        return ["            {} %\n(rotate to adjust)".format(pct)]
    if sub_type == "sound":
        return ["On", "Off"]
    return []


def _open_submenu(menu_cursor, state):
    """Resolve a non-Device top-menu item into (sub_type, sub_cursor, title, items)."""
    item = _build_top_menu()[menu_cursor]
    if item == "Input":
        sub_type = "input"
        sc = 0
        indices = [i for i, _ in state.input_names]
        if state.input_index in indices:
            sc = indices.index(state.input_index)
    elif item == "Dirac Live":
        sub_type = "dirac"
        sc = 0
        vals = [v for v, _ in state.dirac_names]
        if state.dirac_filter in vals:
            sc = vals.index(state.dirac_filter)
    else:
        sub_type = "input"  # fallback
        sc = 0
    return sub_type, sc, item.upper(), _build_sub_items(sub_type, state)


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


def _confirm_sub(sub_type, sub_cursor, state):
    """Apply sub-menu selection (side-effects only)."""
    if sub_type == "input":
        index, _ = state.input_names[sub_cursor]
        try:
            denon.set_input(index)
            state.input_index = index
        except Exception as e:
            print("set_input:", e)
    elif sub_type == "dirac":
        val, _ = state.dirac_names[sub_cursor]
        try:
            denon.set_dirac_filter(val)
            state.dirac_filter = val
        except Exception as e:
            print("set_dirac:", e)
    elif sub_type == "speaker":
        preset = str(sub_cursor + 1)
        try:
            denon.set_speaker_preset(preset)
            state.speaker_preset = preset
            try:
                state.dirac_filter, state.dirac_names = denon.get_dirac_filters()
            except Exception as e:
                print("reload_dirac:", e)
        except Exception as e:
            print("set_speaker:", e)
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


def main():
    # ── Startup splash ───────────────────────────────────────────────────────────────────
    _splash_t = time.monotonic()
    dial_ui.show_splash(board.DISPLAY)

    # --- Hardware inputs ---
    encoder = rotaryio.IncrementalEncoder(board.ENC_A, board.ENC_B)
    last_pos = encoder.position

    btn = digitalio.DigitalInOut(board.KNOB_BUTTON)
    btn.direction = digitalio.Direction.INPUT
    btn.pull = digitalio.Pull.UP
    last_btn_pressed = False

    i2c = board.I2C()
    try:
        touch = Adafruit_FocalTouch(i2c, address=0x38)
        _touch_ok = True
    except Exception as e:
        print("touch init failed:", e)
        touch = None
        _touch_ok = False

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
                if _touch_ok and touch.touched > 0:
                    microcontroller.reset()
            except Exception:
                pass

    # --- HTTP session (plain HTTP, port 8080 -- AVR-X4800H control API) ---
    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool)
    denon.init(session)

    # Fetch input friendly names and source list (for menu) once at boot
    try:
        denon.load_input_names()
    except Exception as e:
        print("load_input_names:", e)

    try:
        denon.load_source_list()
    except Exception as e:
        print("load_source_list:", e)

    # --- AVR state ---
    state = AVRState()
    state.brightness = _load_brightness()
    sound.enabled    = _load_sound()
    ui["display"].brightness = state.brightness

    try:
        state.speaker_preset = denon.get_speaker_preset()
    except Exception as e:
        print("get_speaker_preset:", e)

    try:
        state.input_index, state.input_names = denon.get_inputs()
    except Exception as e:
        print("get_inputs:", e)

    try:
        state.dirac_filter, state.dirac_names = denon.get_dirac_filters()
    except Exception as e:
        print("get_dirac_filters:", e)

    last_poll = time.monotonic() + 2.0
    error_count = 0
    _first_poll = True

    # Encoder debounce (volume mode only)
    enc_last_move  = 0.0
    ENC_DEBOUNCE_S = 0.15
    enc_last_tick  = 0.0

    # Touch long-press thresholds
    touch_start       = 0.0
    touch_x = 0; touch_y = 0   # coordinates captured at touch-down
    touch_mute_fired  = False
    touch_power_fired = False
    TOUCH_MUTE_S  = 0.5
    TOUCH_POWER_S = 1.5
    TOUCH_MIN_S   = 0.05  # minimum tap duration; filters electrical noise
    MENU_TAP_Y    = 195  # pixels from top: taps below this open the menu
    SWIPE_THRESHOLD = 50  # pixels; horizontal delta to register a swipe
    touch_x_start = 0    # x at touch-down (for swipe detection)

    # Menu state
    mode         = MODE_MAIN
    menu_cursor  = 0
    dev_cursor   = 0    # cursor within Device submenu
    sub_cursor   = 0
    sub_scroll   = 0    # first visible item in sub-menu (for long lists)
    sub_type     = None    # "dirac", "speaker", "brightness"
    menu_idle    = 0.0     # monotonic time of last menu interaction
    MENU_TIMEOUT = 8.0     # auto-close menu after this many seconds idle

    # --- Main loop ---
    while True:
        now = time.monotonic()

        # ---------------------------------------------------------------
        # Encoder button -- open/navigate/confirm menu
        # ---------------------------------------------------------------
        btn_pressed = not btn.value
        if btn_pressed and not last_btn_pressed and state.power == "ON":
            menu_idle = now
            if mode == MODE_MAIN:
                # Enter top-level menu
                mode = MODE_MENU_TOP
                menu_cursor = -1   # no item pre-selected; rotary or tap to choose
                dial_ui.draw_menu(ui, "", _build_top_menu(), menu_cursor, clear_bg=True)
            elif mode == MODE_MENU_TOP:
                if menu_cursor < 0:
                    pass   # nothing highlighted yet; ignore button until rotary selects
                else:
                    item = _build_top_menu()[menu_cursor]
                    if item == "Device":
                        dev_cursor = -1
                        mode = MODE_MENU_DEV
                        dial_ui.draw_menu(ui, "DEVICE", _build_dev_menu(), dev_cursor, clear_bg=True)
                    else:
                        sub_type, sub_cursor, title, items = _open_submenu(menu_cursor, state)
                        sub_scroll = _scroll_for(sub_cursor, 0, len(items))
                        mode = MODE_MENU_SUB
                        vis = items[sub_scroll : sub_scroll + dial_ui.MENU_VISIBLE]
                        dial_ui.draw_menu(ui, title, vis, sub_cursor - sub_scroll, clear_bg=True)
            elif mode == MODE_MENU_DEV:
                if dev_cursor < 0:
                    pass   # nothing highlighted yet
                else:
                    item = _build_dev_menu()[dev_cursor]
                    if item == "Restart":
                        microcontroller.reset()
                    else:
                        sub_type, sub_cursor = _enter_dev_sub(item, state, ui, sound)
                        sub_scroll = 0
                        mode = MODE_MENU_SUB
            elif mode == MODE_MENU_SUB:
                if sub_cursor < 0:
                    # No item selected yet — button press closes without applying
                    mode = MODE_MAIN
                    dial_ui.exit_menu(ui)
                    dial_ui.draw_main(ui, state)
                else:
                    _confirm_sub(sub_type, sub_cursor, state)
                    mode = MODE_MAIN
                    dial_ui.exit_menu(ui)
                    dial_ui.draw_main(ui, state)
        last_btn_pressed = btn_pressed

        # ---------------------------------------------------------------
        # Encoder rotation -- volume in MAIN, navigation in MENU
        # ---------------------------------------------------------------
        pos = encoder.position
        if pos != last_pos:
            delta    = pos - last_pos
            last_pos = pos

            if state.power != "ON":
                pass   # ignore encoder when standby
            elif mode == MODE_MAIN:
                menu_idle = now
                # Accelerated volume control
                elapsed_ms = (now - enc_last_tick) * 1000 if enc_last_tick > 0.0 else 999
                enc_last_tick = now
                # Divide by |delta| to get ms-per-tick: when spinning fast,
                # multiple ticks accumulate per loop iteration so elapsed_ms
                # alone is always too large to trigger the threshold.
                ms_per_tick = elapsed_ms / abs(delta) if abs(delta) > 1 else elapsed_ms
                fast = ms_per_tick < config.ACCEL_THRESHOLD_MS
                step = config.VOLUME_STEP_FAST if fast else config.VOLUME_STEP

                state.apply_volume_delta(-delta, step=step)

                dial_ui.draw_volume(ui, state)
                enc_last_move = now

            elif mode == MODE_MENU_TOP:
                menu_idle = now
                items = _build_top_menu()
                if menu_cursor < 0:
                    menu_cursor = 0
                else:
                    menu_cursor = (menu_cursor - delta) % len(items)
                dial_ui.draw_menu(ui, "", items, menu_cursor, clear_bg=True)

            elif mode == MODE_MENU_DEV:
                menu_idle = now
                dev_items = _build_dev_menu()
                if dev_cursor < 0:
                    dev_cursor = 0
                else:
                    dev_cursor = (dev_cursor - delta) % len(dev_items)
                dial_ui.draw_menu(ui, "DEVICE", dev_items, dev_cursor, clear_bg=True)

            elif mode == MODE_MENU_SUB:
                menu_idle = now
                if sub_type == "brightness":
                    # Adjust brightness live; clamp to [0.05, 1.0] in 0.05 steps
                    state.brightness = max(0.05, min(1.0,
                        state.brightness - delta * 0.05))
                    ui["display"].brightness = state.brightness
                    dial_ui.draw_menu(ui, "BRIGHTNESS",
                                      _build_sub_items("brightness", state), 0)
                else:
                    items = _build_sub_items(sub_type, state)
                    if sub_cursor < 0:
                        sub_cursor = 0
                    else:
                        sub_cursor = (sub_cursor - delta) % len(items)
                    sub_scroll = _scroll_for(sub_cursor, sub_scroll, len(items))
                    if sub_type == "input":   title = "INPUT"
                    elif sub_type == "dirac": title = "DIRAC LIVE"
                    elif sub_type == "sound": title = "SOUND"
                    else:                     title = sub_type.upper()
                    vis = items[sub_scroll : sub_scroll + dial_ui.MENU_VISIBLE]
                    dial_ui.draw_menu(ui, title, vis, sub_cursor - sub_scroll, clear_bg=True)

        # Volume debounce send (MAIN mode only)
        if mode == MODE_MAIN and enc_last_move > 0.0 and (now - enc_last_move) >= ENC_DEBOUNCE_S:
            enc_last_move = 0.0
            try:
                denon.set_volume(state.volume_db)
                error_count = 0
            except Exception as e:
                print("set_volume:", e)
                error_count += 1
            last_poll = time.monotonic()

        # Menu auto-close after idle timeout (not in error state)
        if mode not in (MODE_MAIN, MODE_ERROR) and menu_idle > 0.0 and (now - menu_idle) >= MENU_TIMEOUT:
            mode = MODE_MAIN
            dial_ui.exit_menu(ui)
            dial_ui.draw_main(ui, state)

        # ---------------------------------------------------------------
        # Touch -- mute/power in MAIN, tap to close menu in MENU
        # ---------------------------------------------------------------
        try:
            is_touched = _touch_ok and touch.touched > 0
        except Exception:
            is_touched = False

        if is_touched:
            if touch_start == 0.0:
                touch_start = now
                try:
                    pts = touch.touches
                    if pts:
                        touch_x = pts[0]['x']
                        touch_y = pts[0]['y']
                        touch_x_start = touch_x
                except Exception:
                    pass
            else:
                # Track current position so release has the final x for swipe detection
                try:
                    pts = touch.touches
                    if pts:
                        touch_x = pts[0]['x']
                except Exception:
                    pass
            held = now - touch_start

            # Power toggle must be checked first – it works in any power state
            # (this is the only way to wake the AVR from standby).
            if not touch_power_fired and held >= TOUCH_POWER_S:
                touch_power_fired = True
                touch_mute_fired  = True  # suppress tap-mute when power fires
                sound.click_heavy()
                try:
                    if state.power == "ON":
                        denon.power_standby()
                        state.power = "STANDBY"
                    else:
                        denon.power_on()
                        state.power = "ON"
                        # Clear stale mute/volume so the display is clean
                        # while the AVR boots. Poll will fill in real values.
                        state.muted = False
                        state.volume_db = -80.0
                        dial_ui.flash_power_on(ui)
                    dial_ui.draw_main(ui, state)
                    last_poll = time.monotonic() - 4.0  # poll in ~1s (5s interval - 4s)
                    error_count = 0
                except Exception as e:
                    print("power:", e)
                    error_count += 1
            elif state.power != "ON":
                pass   # standby: only power long-press (handled above) is active
            elif mode != MODE_MAIN:
                pass   # in menu: let release handler decide
        else:
            # Touch released
            held = (now - touch_start) if touch_start > 0.0 else 0.0
            if held >= TOUCH_MIN_S and not touch_mute_fired and not touch_power_fired:
                # Error screen: any tap restarts the device
                if mode == MODE_ERROR:
                    microcontroller.reset()
                elif state.power != "ON":
                    pass   # standby taps handled above (power long-press only)
                # Swipe left → go back one menu level
                elif touch_x - touch_x_start < -SWIPE_THRESHOLD \
                        and mode in (MODE_MENU_TOP, MODE_MENU_DEV, MODE_MENU_SUB):
                    sound.click()
                    menu_idle = now
                    if mode == MODE_MENU_SUB:
                        if sub_type in ("brightness", "sound"):
                            mode = MODE_MENU_DEV
                            dial_ui.draw_menu(ui, "DEVICE", _build_dev_menu(), dev_cursor, clear_bg=True)
                        else:
                            mode = MODE_MENU_TOP
                            dial_ui.draw_menu(ui, "", _build_top_menu(), menu_cursor, clear_bg=True)
                    elif mode == MODE_MENU_DEV:
                        mode = MODE_MENU_TOP
                        dial_ui.draw_menu(ui, "", _build_top_menu(), menu_cursor, clear_bg=True)
                    else:  # MODE_MENU_TOP
                        mode = MODE_MAIN
                        dial_ui.exit_menu(ui)
                        dial_ui.draw_main(ui, state)
                # Clean intentional tap – route by zone and mode
                elif mode == MODE_MENU_TOP:
                    tapped = _menu_item_at_y(touch_y)
                    top_items = _build_top_menu()
                    if 0 <= tapped < len(top_items):
                        sound.click()
                        menu_idle = now
                        menu_cursor = tapped
                        item = top_items[menu_cursor]
                        if item == "Device":
                            dev_cursor = -1
                            mode = MODE_MENU_DEV
                            dial_ui.draw_menu(ui, "DEVICE", _build_dev_menu(), dev_cursor, clear_bg=True)
                        else:
                            sub_type, sub_cursor, title, items = _open_submenu(menu_cursor, state)
                            sub_scroll = _scroll_for(sub_cursor, 0, len(items))
                            mode = MODE_MENU_SUB
                            vis = items[sub_scroll : sub_scroll + dial_ui.MENU_VISIBLE]
                            dial_ui.draw_menu(ui, title, vis, sub_cursor - sub_scroll, clear_bg=True)
                    else:
                        sound.click()
                        mode = MODE_MAIN
                        dial_ui.exit_menu(ui)
                        dial_ui.draw_main(ui, state)
                elif mode == MODE_MENU_DEV:
                    tapped = _menu_item_at_y(touch_y)
                    dev_items = _build_dev_menu()
                    if 0 <= tapped < len(dev_items):
                        sound.click()
                        menu_idle = now
                        dev_cursor = tapped
                        item = dev_items[dev_cursor]
                        if item == "Restart":
                            microcontroller.reset()
                        else:
                            sub_type, sub_cursor = _enter_dev_sub(item, state, ui, sound)
                            sub_scroll = 0
                            mode = MODE_MENU_SUB
                    else:
                        sound.click()
                        mode = MODE_MENU_TOP
                        dial_ui.draw_menu(ui, "", _build_top_menu(), menu_cursor, clear_bg=True)
                elif mode == MODE_MENU_SUB:
                    tapped = _menu_item_at_y(touch_y)
                    items = _build_sub_items(sub_type, state)
                    vis_count = min(dial_ui.MENU_VISIBLE, len(items) - sub_scroll)
                    if 0 <= tapped < vis_count:
                        # Tap a visible item → select + confirm it
                        sound.click()
                        menu_idle = now
                        sub_cursor = sub_scroll + tapped
                        _confirm_sub(sub_type, sub_cursor, state)
                        mode = MODE_MAIN
                        dial_ui.exit_menu(ui)
                        dial_ui.draw_main(ui, state)
                    else:
                        sound.click()
                        mode = MODE_MAIN
                        dial_ui.exit_menu(ui)
                        dial_ui.draw_main(ui, state)
                elif mode == MODE_MAIN and touch_y >= MENU_TAP_Y:
                    # Tap on MENU area → open top-level menu
                    sound.click()
                    menu_idle = now
                    mode = MODE_MENU_TOP
                    menu_cursor = -1
                    dial_ui.draw_menu(ui, "", _build_top_menu(), menu_cursor, clear_bg=True)
                elif mode == MODE_MAIN:
                    # Tap anywhere else → toggle mute
                    sound.click()
                    try:
                        if state.muted:
                            denon.mute_off()
                            state.muted = False
                        else:
                            denon.mute_on()
                            state.muted = True
                        dial_ui.draw_main(ui, state)
                        last_poll = time.monotonic()
                        error_count = 0
                    except Exception as e:
                        print("mute:", e)
                        error_count += 1
            touch_start       = 0.0
            touch_x = 0; touch_y = 0; touch_x_start = 0
            touch_mute_fired  = False
            touch_power_fired = False

        # Periodic AVR poll.
        # Interval logic:
        #   - AVR is in STANDBY: always 5s -- detect power-on quickly
        #   - Recently active (encoder/touch in last 30s): POLL_INTERVAL_S (30s)
        #     -- prevents polls chaining immediately after commands
        #   - Truly idle: 5s -- catch remote/app changes promptly
        # Always skipped while encoder is moving.
        in_standby = state.power != "ON"
        last_action = max(enc_last_move, enc_last_tick)
        recently_active = (now - last_action) < config.POLL_INTERVAL_S
        if in_standby or not recently_active:
            poll_interval = 5.0
        else:
            poll_interval = config.POLL_INTERVAL_S

        encoder_idle = (enc_last_move == 0.0)
        if encoder_idle and now - last_poll >= poll_interval:
            last_poll = now
            try:
                was_standby = state.power != "ON"
                status = denon.get_status()
                changed = state.apply_status(status)

                if was_standby and state.power == "ON" and not _first_poll:
                    # AVR just powered on externally – show splash then normal UI.
                    state.volume_db = -80.0
                    last_poll = now - poll_interval + 1.0
                    changed = True
                    dial_ui.flash_power_on(ui)

                _first_poll = False
                if changed:
                    dial_ui.draw_main(ui, state)
                error_count = 0
            except Exception as e:
                print("poll:", e)
                error_count += 1
                if error_count >= _MAX_ERRORS and mode != MODE_ERROR:
                    mode = MODE_ERROR
                    dial_ui.draw_error(ui, "No AVR")


main()
