# app.py -- deloop main app (the real logic; code.py is a thin entry point
# that just does `import app; app.main()`). CircuitPython requires code.py
# itself to stay uncompiled source, but a module it imports doesn't -- this
# split lets everything in this file be precompiled to app.mpy (see
# Makefile's `mpy` target), which matters: this is comfortably the largest
# single file in the project, and CircuitPython compiles a module's entire
# bytecode before running any of it, so its size directly costs heap
# fragmentation at boot whether or not most of it has run yet (see
# local/agent/project-context.md's 2026-07-29 WiFi outage incident, which
# turned out not to be about WiFi at all).
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
import version
from state import AVRState

_MAX_ERRORS = 5

# Bound to the denon module only for a DEVICE_DRIVER=="denon" build (set in
# main(), see its init_transport branch) -- lets _poll_avr/_pulse_mute route
# the background status poll through denon.py's non-blocking engine instead
# of _poll_now()'s blocking driver.get_status(), without every other backend
# paying to import denon.py at all. None for every other backend.
_denon = None

# How often to retry driver.get_presets() after it failed at boot (AVR
# unreachable, timeout, etc) -- much shorter than the normal 5s/30s adaptive
# poll interval, since a missing preset/Dirac list is a visibly broken UI
# element (buttons never appear) rather than just slightly-stale state, and
# is worth reconnecting for aggressively until the first success. Stops
# retrying permanently the moment it succeeds once -- see _retry_presets().
_PRESET_RETRY_INTERVAL_S = 10.0

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
# OTA NVM state (bytes 2-4) is defined once in config.py (NVM_OTA_ACTION/
# RESULT/VERSION) since ota_boot.py needs it too, not just app.py -- see
# that module's docstring for why the actual check/install work now lives
# there instead of here. This file only ever reads OTA_RESULT/OTA_VERSION
# (to show a check/install's outcome) and writes OTA_ACTION (to request
# one via _confirm_sub's "update" branch, right before a reload hands off
# to ota_boot.py). Never calls microcontroller.reset() anywhere in that
# handoff -- see docs/ota.md for why a hard reset breaks TLS for the rest
# of that power cycle, root-caused live 2026-07-31.

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

# --- TEMPORARY: touch-drop investigation (see docs/architecture.md's
# "Still broken, confirmed by real extended use" note) -----------------
# The FocalTouch chip has no queue -- it only reports whatever is touched
# *right now*. The main loop has no sleep (see TOUCH_FRAME_S's comment
# above), so if any single iteration runs long (a blocking AVR/network
# poll is the prime suspect), every touch-poll frame that would have
# landed during that stall is simply never taken -- a tap that begins and
# ends entirely inside the gap leaves no trace anywhere, in state or in
# the FocalTouch chip itself. This block prints a breakdown of any
# iteration slower than STALL_THRESHOLD_S so a stall can be lined up
# against a tap the user reports as "didn't register" with nothing else
# to explain it. Remove once root-caused.
STALL_THRESHOLD_S = 0.03   # ~2 frames at 60fps


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


def _set_ota_action(val):
    """Requests a check/install on the next boot -- see _confirm_sub's
    "update" branch, which calls this then reloads. Consuming this flag
    (reading it, doing the real work, clearing it) is entirely
    ota_boot.py's job now -- this file only ever writes it."""
    gc.collect()   # nvm writes need a real buffer -- see _set_ota_result
    try:
        microcontroller.nvm[config.NVM_OTA_ACTION] = val
    except Exception as e:
        print("set_ota_action:", type(e), e)


def _ota_result():
    try:
        return microcontroller.nvm[config.NVM_OTA_RESULT]
    except Exception:
        return 0


def _set_ota_result(val):
    # Confirmed live (2026-07-31): an nvm[] write itself needs to allocate
    # a real buffer (observed ~8KB -- likely a flash-sector-sized read-
    # modify-write, not just the one byte being set), which can fail with
    # a MemoryError of its own right after a network-heavy operation.
    # gc.collect() first, same discipline used everywhere else in this
    # codebase before a specific allocation. The only caller left in this
    # file is main()'s "clear the result after displaying it" step.
    gc.collect()
    try:
        microcontroller.nvm[config.NVM_OTA_RESULT] = val
    except Exception as e:
        print("set_ota_result:", type(e), e)


def _ota_latest_version():
    try:
        v = microcontroller.nvm[config.NVM_OTA_VERSION]
        return None if v == 255 else v   # 255 = erased/uninitialized flash
    except Exception:
        return None


def _connect_wifi(ui):
    if not config.WIFI_SSID:
        dial_ui.draw_error(ui, "No WiFi cfg")
        raise RuntimeError("WIFI_SSID not set in settings.toml")
    dial_ui.draw_status(ui, "WiFi...")
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("power_management NONE failed:", type(e), e)
    wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)


def _top_menu_entries():
    """(sub_type, label) pairs for the top-level menu, filtered by what the
    active driver actually supports -- see driver.CAPS / driver.LABELS.

    Read fresh every call, not cached -- this is what lets CAPS actually
    change at runtime (see driver.py's CAPS["player_select"] note) show up
    in the menu immediately, with no extra plumbing needed here.
    """
    entries = []
    if driver.CAPS["input_select"]:
        entries.append(("input", driver.LABELS["input_select"]))
    if driver.CAPS["presets"]:
        entries.append(("preset", driver.LABELS["presets"]))
    if driver.CAPS["player_select"]:
        entries.append(("player", driver.LABELS["player_select"]))
    if config.OTA_ENABLED:
        entries.append(("update", "Update"))
    entries.append(("device", "Device"))
    return entries


def _build_top_menu():
    return [label for _, label in _top_menu_entries()]


def _build_dev_menu():
    return ["Brightness", "Sound", "Restart"]


def _ota_menu_title():
    """Submenu title for sub_type == "update"."""
    return "UPDATE"


def _ota_version_text():
    """Current version string for the Update submenu's persistent status
    row (dial_ui.draw_menu's version_text param) -- visible the moment the
    submenu opens, no tap required, styled to match the main screen's
    "input" label (dim gray) rather than a bright title. Separate from
    _ota_menu_title() because draw_menu()'s title parameter is never
    actually rendered (see its own comment: "no title; context comes from
    the items themselves") -- version_text is a real, distinct label."""
    return "v{}".format(version.CURRENT_VERSION)


def _ota_update_items(loop):
    """Status-dependent display strings for the Update submenu -- always a
    1- or 2-item action list, never a real list of choices the way input/
    preset/player are. The current version is shown in the submenu's
    title instead (see _open_submenu/_handle_encoder_rotation), not
    repeated in every one of these. See _confirm_sub's "update" branch for
    what each label triggers. loop.ota_status is surfaced from NVM once,
    on the first normal boot after a check/install reload -- see main()."""
    if loop.ota_status == "available":
        return ["Install Update (v{})".format(loop.ota_latest_version), "Check Again"]
    if loop.ota_status == "pending_power_cycle":
        return ["Power cycle now\nto install v{}".format(loop.ota_latest_version), "Cancel"]
    if loop.ota_status == "up_to_date":
        return ["Up to date", "Check Again"]
    if loop.ota_status == "error":
        return ["Check failed", "Check Again"]
    if loop.ota_status == "eject_needed":
        return ["Eject drive first", "Check Again"]
    if loop.ota_status == "just_installed":
        return ["Update installed", "Check Now"]
    if loop.ota_status == "just_failed":
        return ["Install failed", "Check Now"]
    return ["Check Now"]


def _build_sub_items(sub_type, state, loop):
    """Return list of display strings for a submenu."""
    if sub_type == "input":
        return [name for _, name in state.input_names]
    if sub_type == "preset":
        return [name for _, name in state.preset_names]
    if sub_type == "player":
        return [name for _, name in state.player_names]
    if sub_type == "brightness":
        pct = int(round(state.brightness * 100))
        return ["            {} %\n(rotate to adjust)".format(pct)]
    if sub_type == "sound":
        return ["On", "Off"]
    if sub_type == "update":
        return _ota_update_items(loop)
    return []


def _open_submenu(sub_type, label, state, loop):
    """Resolve a non-Device top-menu entry into (sub_type, sub_cursor, title, version_text, items)."""
    sc = 0
    if sub_type == "input":
        indices = [i for i, _ in state.input_names]
        if state.input_index in indices:
            sc = indices.index(state.input_index)
    elif sub_type == "preset":
        vals = [v for v, _ in state.preset_names]
        if state.preset in vals:
            sc = vals.index(state.preset)
    elif sub_type == "player":
        ids = [i for i, _ in state.player_names]
        if state.player_id in ids:
            sc = ids.index(state.player_id)
    title = _ota_menu_title() if sub_type == "update" else label.upper()
    ver = _ota_version_text() if sub_type == "update" else ""
    return sub_type, sc, title, ver, _build_sub_items(sub_type, state, loop)


def _enter_dev_sub(item, state, ui, sound_mod, loop):
    """Open the leaf submenu for a Device menu item. Returns (sub_type, sub_cursor)."""
    if item == "Brightness":
        dial_ui.render_gauge_bg(ui, state.volume_db, state.muted)
        dial_ui.draw_menu(ui, "BRIGHTNESS", _build_sub_items("brightness", state, loop), 0)
        return "brightness", 0
    elif item == "Sound":
        sc = -1   # no pre-selection; user taps or rotates to choose
        dial_ui.draw_menu(ui, "SOUND", _build_sub_items("sound", state, loop), sc, clear_bg=True)
        return "sound", sc
    return None, 0


def _confirm_sub(sub_type, sub_cursor, state, ui, loop, now):
    """Apply sub-menu selection (side-effects only).

    Returns a truthy value to mean "stay open, refresh items" instead of
    the usual "close back to MODE_MAIN" -- every branch below except
    "update"'s Install/Cancel actions implicitly returns None (falsy), so
    existing behavior for every other sub_type is unchanged. "Check Now"/
    "Check Again" reload immediately and never return at all; "Install"
    sets the pending action and stays open to show "power cycle now"
    instead (see that branch's comment for why); "Cancel" clears the
    pending action and closes normally.
    """
    if sub_type == "input":
        index, _ = state.input_names[sub_cursor]
        try:
            _denon.async_set_input(index, now) if _denon is not None else driver.set_input(index)
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
            _denon.async_set_preset(val, now) if _denon is not None else driver.set_preset(val)
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
    elif sub_type == "player":
        entity_id, _ = state.player_names[sub_cursor]
        try:
            # Switching targets is a few sequential HTTP calls (set target,
            # re-derive capabilities/source list, fetch status) -- same
            # "please wait" treatment as a slow preset switch above.
            dial_ui.draw_busy(ui, state)
            driver.set_player(entity_id)
            state.player_id = entity_id
            # Refresh everything that's cached per-target rather than
            # re-fetched live -- driver.set_player() already re-derived
            # driver.CAPS in place (see driver.py's contract note), so the
            # menu itself picks that up next time it's opened with no
            # further action needed here.
            state.input_index, state.input_names = driver.get_inputs()
            state.apply_status(driver.get_status())
        except Exception as e:
            print("set_player:", e)
    elif sub_type == "brightness":
        _save_brightness(state.brightness)
    elif sub_type == "sound":
        sound.enabled = (sub_cursor == 0)   # 0=On, 1=Off
        _save_sound(sound.enabled)
    elif sub_type == "update":
        items = _ota_update_items(loop)
        label = items[sub_cursor] if 0 <= sub_cursor < len(items) else ""
        if label.startswith("Install"):
            # Deliberately does NOT reload itself -- see docs/ota.md's
            # "wifi_inited is power-cycle-scoped" finding. The ESP-IDF WiFi
            # stack's real init (esp_wifi_init(), netif/event-handler setup)
            # only ever runs once per power cycle; every soft reload after
            # that just re-enables the same already-initialized radio. A
            # supervisor.reload() here would give ota_boot.run() a fresh
            # Python VM but the SAME wifi state every other reload this
            # power cycle has been reusing, which is less reliable than a
            # genuine power cycle's first-ever use of a freshly
            # re-initialized radio. code.py already reads NVM_OTA_ACTION
            # before importing app.py/ota_boot, so simply leaving action=2
            # set is enough -- the next real power-on picks it up
            # automatically.
            _set_ota_action(2)
            loop.ota_status = "pending_power_cycle"
            return True   # stay open, refresh items to show the new status
        elif label.startswith("Check"):
            _set_ota_action(1)
            dial_ui.show_message(ui["display"], "Checking...")
            import supervisor
            supervisor.reload()   # never returns
        elif label == "Cancel":
            _set_ota_action(0)
            loop.ota_status = "idle"
            # falls through, returns None -> closes back to MODE_MAIN
        # else: tapped/confirmed an inert informational row -- no-op, closes normally


def _scroll_for(cursor, scroll, num_items):
    """Return updated scroll offset so cursor stays in the visible window."""
    vis = dial_ui.MENU_VISIBLE
    if cursor < scroll:
        scroll = cursor
    elif cursor >= scroll + vis:
        scroll = cursor - vis + 1
    return max(0, min(scroll, max(0, num_items - vis)))


def _menu_item_at_y(y, max_items):
    """Return visible item index [0..max_items-1] for a touch at y, or -1.

    max_items is required, not defaulted to MENU_VISIBLE -- found live
    (2026-08-01) that scanning all MENU_VISIBLE slot positions regardless
    of how many items a menu actually has meant a menu with fewer than
    MENU_VISIBLE items (the Update submenu's single "Check Now" row is the
    extreme case) had "phantom" hit zones below its real content: a tap
    landing near one of those unused slot positions returned a real index
    that then failed the caller's `tapped < actual count` check and closed
    the menu entirely, misread as "tapped outside every item." Every
    caller must pass its own real, currently-visible item count."""
    half = dial_ui.MENU_ITEM_DY // 2 - 2
    for i in range(max_items):
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
        # Set by _poll_avr right before kicking off an async poll (denon
        # only -- see _denon/_pump_denon_poll), so _apply_poll_result can
        # still do the "AVR powered on externally, reschedule sooner"
        # rescheduling once the poll resolves, several ticks later, the
        # same way _poll_now's synchronous return value lets it do today
        # for every other backend.
        self.pending_poll_interval = None

        # 0.0, not now() -- fires on the very first main-loop pass if
        # get_presets() failed at boot, rather than waiting a full interval.
        self.last_preset_retry = 0.0

        self.mode        = MODE_MAIN
        self.menu_cursor = 0
        self.dev_cursor  = 0    # cursor within Device submenu
        self.sub_cursor  = 0
        self.sub_scroll  = 0    # first visible item in sub-menu (for long lists)
        self.sub_type    = None    # "input", "preset", "player", "brightness", "sound", "update"
        self.menu_idle   = 0.0     # monotonic time of last menu interaction

        self.mute_phase_origin  = 0.0     # monotonic time the current pulse cycle started
        self.mute_trough_polled = False   # guards against polling every tick while under threshold
        self.pulse_last_frame   = 0.0

        self.power_fade_start = 0.0   # monotonic time the power-off fade began; 0.0 = not fading
        self.power_fade_from  = dial_ui.BRIGHTNESS_ON   # display brightness when the fade began

        # OTA session status -- ephemeral UI state, same category as mode/
        # sub_type above: reset fresh every boot, surfaced once from NVM
        # (see main()) right after a check/install reload. Deliberately
        # not on AVRState -- that's the amp/DSP model, this is not.
        self.ota_status          = "idle"   # idle|available|pending_power_cycle|up_to_date|error|eject_needed|just_installed|just_failed
        self.ota_latest_version  = None


# ---------------------------------------------------------------------------
# Encoder button -- open/navigate/confirm menu
# ---------------------------------------------------------------------------

def _handle_encoder_button(loop, ui, state, btn, now):
    btn_pressed = not btn.value
    fired = btn_pressed and not loop.last_btn_pressed
    loop.last_btn_pressed = btn_pressed
    if not fired:
        return

    # Standby, resting on the power-off screen: only backends that can
    # switch to a different target (CAPS["player_select"], e.g. HA's Media
    # Player list) can open the menu from here -- see dial_ui.draw_main's
    # power-off branch, which shows a MENU hint only for those backends.
    # Once inside a menu (loop.mode != MODE_MAIN), the button works
    # regardless of power state, same as touch does in _dispatch_tap.
    if state.power != "ON" and loop.mode == MODE_MAIN and not driver.CAPS["player_select"]:
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
            loop.sub_type, loop.sub_cursor, title, ver, items = _open_submenu(sub_type, label, state, loop)
            loop.sub_scroll = _scroll_for(loop.sub_cursor, 0, len(items))
            loop.mode = MODE_MENU_SUB
            vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
            dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True, version_text=ver)
        return

    if loop.mode == MODE_MENU_DEV:
        if loop.dev_cursor < 0:
            return   # nothing highlighted yet
        item = _build_dev_menu()[loop.dev_cursor]
        if item == "Restart":
            microcontroller.reset()
        else:
            loop.sub_type, loop.sub_cursor = _enter_dev_sub(item, state, ui, sound, loop)
            loop.sub_scroll = 0
            loop.mode = MODE_MENU_SUB
        return

    if loop.mode == MODE_MENU_SUB:
        stay_open = False
        if loop.sub_cursor >= 0:
            stay_open = _confirm_sub(loop.sub_type, loop.sub_cursor, state, ui, loop, now)
        # sub_cursor < 0: no item selected yet — button press closes without applying
        if stay_open:
            items = _build_sub_items(loop.sub_type, state, loop)
            loop.sub_cursor = 0
            loop.sub_scroll = 0
            title = _ota_menu_title() if loop.sub_type == "update" else loop.sub_type.upper()
            ver = _ota_version_text() if loop.sub_type == "update" else ""
            dial_ui.draw_menu(ui, title, items[:dial_ui.MENU_VISIBLE], 0, clear_bg=True, version_text=ver)
        else:
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

    # Ignore the encoder while resting on the power-off screen -- there's no
    # volume to adjust there, regardless of backend. Once a menu is open
    # (see _handle_encoder_button/_dispatch_tap), rotation navigates it
    # normally even in standby.
    if state.power != "ON" and loop.mode == MODE_MAIN:
        return

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
            dial_ui.draw_menu(ui, "BRIGHTNESS", _build_sub_items("brightness", state, loop), 0)
            return

        items = _build_sub_items(loop.sub_type, state, loop)
        loop.sub_cursor = 0 if loop.sub_cursor < 0 else (loop.sub_cursor - delta) % len(items)
        loop.sub_scroll = _scroll_for(loop.sub_cursor, loop.sub_scroll, len(items))
        if loop.sub_type == "input":     title = driver.LABELS["input_select"].upper()
        elif loop.sub_type == "preset":  title = driver.LABELS["presets"].upper()
        elif loop.sub_type == "player":  title = driver.LABELS["player_select"].upper()
        elif loop.sub_type == "sound":   title = "SOUND"
        elif loop.sub_type == "update":  title = _ota_menu_title()
        else:                            title = loop.sub_type.upper()
        ver = _ota_version_text() if loop.sub_type == "update" else ""
        vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
        dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True, version_text=ver)


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
    if _denon is not None:
        # Async kickoff never raises for a routine network hiccup (only a
        # local problem like socket allocation would) -- the real
        # success/failure lands a tick or two later via
        # _pump_denon_command, same error_count bookkeeping either way.
        if was_muted:
            _denon.async_mute_off(now)
            state.muted = False
        _denon.async_set_volume(state.volume_db, now)
    else:
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
    """Touch held down: track position, fire long-press power toggle.

    Found + fixed 2026-08-01: touch_y used to be set only once, on the
    very first frame of a touch-down, while touch_x kept updating on every
    later frame (for swipe detection). If that first frame's read came
    back phantom/empty (the FocalTouch driver can report touched>0 with
    an empty touches list after filtering invalid points -- confirmed
    reachable), touch_y stayed stuck at its reset value of 0 for the
    entire gesture, even once a later frame got a real reading. Since
    every real menu item sits near vertical screen center, a stuck
    touch_y=0 never matches any item's hit band regardless of how many
    items exist -- indistinguishable from "tapped outside everything,"
    closing the menu on release. touch_x_start (the swipe-detection
    baseline) had the same latent gap: only ever set on the literal first
    frame, so a phantom first read left it stuck at 0 too. Both are now
    set on whichever frame first produces a real point, not necessarily
    the first frame, and touch_x/touch_y both keep updating on every
    later frame a valid point comes back."""
    first_frame = loop.touch_start == 0.0
    if first_frame:
        loop.touch_start = now
    pt = _read_touch_point(touch)
    if pt:
        got_first_point = loop.touch_x_start == 0 and loop.touch_y == 0
        loop.touch_x, loop.touch_y = pt
        if first_frame or got_first_point:
            loop.touch_x_start = loop.touch_x

    # TEMPORARY: touch-drop investigation -- see STALL_THRESHOLD_S above.
    print("TOUCH down t=%.3f first=%d pt=%r xy=(%d,%d) mode=%d" % (
        now, first_frame, pt, loop.touch_x, loop.touch_y, loop.mode))

    held = now - loop.touch_start
    if not driver.CAPS["power"] or loop.touch_power_fired or held < TOUCH_POWER_S:
        return

    # Power toggle must be checked first – it works in any power state
    # (this is the only way to wake the AVR from standby).
    loop.touch_power_fired = True
    sound.click_heavy()
    try:
        if state.power == "ON":
            _denon.async_power_standby(now) if _denon is not None else driver.power_standby()
            state.power = "STANDBY"
            # Ease brightness down instead of an instant cut; _fade_power_off
            # swaps in the power icon once dark and hands off to its pulse.
            loop.power_fade_from  = state.brightness
            loop.power_fade_start = now
        else:
            _denon.async_power_on(now) if _denon is not None else driver.power_on()
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
    top_items = _build_top_menu()
    tapped = _menu_item_at_y(loop.touch_y, min(dial_ui.MENU_VISIBLE, len(top_items)))
    # TEMPORARY: touch-drop investigation -- see STALL_THRESHOLD_S above.
    print("TAP menu_top y=%d n_items=%d -> tapped=%d" % (loop.touch_y, len(top_items), tapped))
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
        loop.sub_type, loop.sub_cursor, title, ver, items = _open_submenu(sub_type, label, state, loop)
        loop.sub_scroll = _scroll_for(loop.sub_cursor, 0, len(items))
        loop.mode = MODE_MENU_SUB
        vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
        dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True, version_text=ver)


def _tap_menu_dev(loop, ui, state, now):
    dev_items = _build_dev_menu()
    tapped = _menu_item_at_y(loop.touch_y, min(dial_ui.MENU_VISIBLE, len(dev_items)))
    # TEMPORARY: touch-drop investigation -- see STALL_THRESHOLD_S above.
    print("TAP menu_dev y=%d n_items=%d -> tapped=%d" % (loop.touch_y, len(dev_items), tapped))
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
        loop.sub_type, loop.sub_cursor = _enter_dev_sub(item, state, ui, sound, loop)
        loop.sub_scroll = 0
        loop.mode = MODE_MENU_SUB


def _tap_menu_sub(loop, ui, state, now):
    items = _build_sub_items(loop.sub_type, state, loop)
    vis_count = min(dial_ui.MENU_VISIBLE, len(items) - loop.sub_scroll)
    tapped = _menu_item_at_y(loop.touch_y, vis_count)
    # TEMPORARY: touch-drop investigation -- see STALL_THRESHOLD_S above.
    print("TAP menu_sub y=%d vis_count=%d -> tapped=%d" % (loop.touch_y, vis_count, tapped))
    stay_open = False
    if 0 <= tapped < vis_count:
        # Tap a visible item -> select + confirm it
        sound.click()
        loop.menu_idle = now
        loop.sub_cursor = loop.sub_scroll + tapped
        stay_open = _confirm_sub(loop.sub_type, loop.sub_cursor, state, ui, loop, now)
    else:
        sound.click()
    if stay_open:
        items = _build_sub_items(loop.sub_type, state, loop)
        loop.sub_cursor = 0
        loop.sub_scroll = 0
        title = _ota_menu_title() if loop.sub_type == "update" else loop.sub_type.upper()
        ver = _ota_version_text() if loop.sub_type == "update" else ""
        dial_ui.draw_menu(ui, title, items[:dial_ui.MENU_VISIBLE], 0, clear_bg=True, version_text=ver)
    else:
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


def _try_action(loop, label, action):
    """Run `action()` (a no-arg callable -- usually a driver call, or a
    closure wrapping one plus a local state flip), then reset the poll
    timer + error count on success or print+count on failure. Shared tail
    of every simple single-action tap handler below -- fires exactly once
    per tap, no multi-step logic. Returns True on success so a caller with
    something to redraw (a flipped state field) can branch on it; a caller
    with nothing to redraw (media prev/next -- no local field changes)
    can just ignore the return value.

    Not used by every tap handler in this file -- the preset quick-select
    and power-toggle handlers each have their own extra steps (a busy
    screen, a non-default poll-timer offset, redrawing on failure too)
    that don't fit this shared shape, and forcing them into it would cost
    more in indirection than the duplication it would remove.
    """
    try:
        action()
        loop.last_poll = time.monotonic()
        loop.error_count = 0
        return True
    except Exception as e:
        print(label + ":", e)
        loop.error_count += 1
        return False


def _tap_toggle_mute(loop, ui, state, now):
    """Tap anywhere else -> toggle mute."""
    sound.click()

    def action():
        if state.muted:
            _denon.async_mute_off(now) if _denon is not None else driver.mute_off()
            state.muted = False
        else:
            _denon.async_mute_on(now) if _denon is not None else driver.mute_on()
            state.muted = True
            _start_mute_pulse(loop, now)

    if _try_action(loop, "mute", action):
        dial_ui.draw_main(ui, state)


def _tap_toggle_playback(loop, ui, state, now):
    """Tap the play/pause status row -> toggle media playback.

    Only ever reachable when state.media_state is "playing"/"paused" (see
    _tap_main_screen) -- backends that never set media_state (denon,
    minidsp) never populate that row or its tap zone in the first place.
    """
    sound.click()

    def action():
        if state.media_state == "playing":
            driver.media_pause()
            state.media_state = "paused"
        else:
            driver.media_play()
            state.media_state = "playing"

    if _try_action(loop, "media play/pause", action):
        dial_ui.draw_main(ui, state)


def _tap_media_prev(loop, ui, state, now):
    """Tap the '<' beside the Playing/Paused status text -> previous track.
    No local state to flip -- nothing to redraw either way, unlike the
    toggle handlers above."""
    sound.click()
    _try_action(loop, "media previous", driver.media_previous)


def _tap_media_next(loop, ui, state, now):
    """Tap the '>' beside the Playing/Paused status text -> next track."""
    sound.click()
    _try_action(loop, "media next", driver.media_next)


def _tap_open_preset_menu(loop, ui, state, now):
    """Tap the preset/status text row -> jump straight into the Preset
    submenu, same as selecting "Preset" from the top menu and pressing the
    encoder button. Only reachable for a backend with presets but no
    main-screen quick-select buttons (see _tap_main_screen) -- e.g. wiim,
    where the row is otherwise just a static name/placeholder with nothing
    else to tap."""
    sound.click()
    loop.menu_idle = now
    label = driver.LABELS["presets"]
    loop.sub_type, loop.sub_cursor, title, ver, items = _open_submenu("preset", label, state, loop)
    loop.sub_scroll = _scroll_for(loop.sub_cursor, 0, len(items))
    loop.mode = MODE_MENU_SUB
    vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
    dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True, version_text=ver)


def _tap_main_screen(loop, ui, state, now):
    """Tap above the preset name line -> toggle mute. The play/pause status
    row (only ever populated via a UI extension -- see driver.py's
    contract -- currently at _PLAYER_NAME_Y, one row below the preset name)
    is checked first since a backend with both presets and playback (e.g.
    wiim) needs both rows tappable independently, not one sharing a slot
    with the other. The narrow prev/next zones flanking it are checked
    before the full-row toggle zone, so they take priority without
    shrinking that zone's own generous "anywhere in the row" target.

    At the preset name row itself (PRESET_NAME_Y): quick-select buttons
    respond if the backend has them (CAPS["preset_quickbuttons"]);
    otherwise, for a backend with presets but no quick buttons (e.g. wiim),
    tapping that row instead opens the Preset submenu directly (see
    _tap_open_preset_menu).
    """
    if state.media_state in ("playing", "paused"):
        if dial_ui.media_prev_tap(loop.touch_x, loop.touch_y):
            _tap_media_prev(loop, ui, state, now)
            return
        if dial_ui.media_next_tap(loop.touch_x, loop.touch_y):
            _tap_media_next(loop, ui, state, now)
            return
        if dial_ui.media_status_tap(loop.touch_x, loop.touch_y):
            _tap_toggle_playback(loop, ui, state, now)
            return

    if loop.touch_y < dial_ui.PRESET_NAME_Y:
        _tap_toggle_mute(loop, ui, state, now)
        return

    if not driver.CAPS["preset_quickbuttons"]:
        # No fixed lower bound needed here (e.g. matching wherever a UI
        # extension draws its own play/pause row) -- _dispatch_tap already
        # only calls this function for touch_y < MENU_TAP_Y (below that is
        # the bottom MENU-tap zone), and the media-tap checks above already
        # ran first and would have claimed their own zone whenever
        # state.media_state qualifies -- this only fires for whatever's left.
        if driver.CAPS["presets"]:
            _tap_open_preset_menu(loop, ui, state, now)
        return   # otherwise: presets (if any) are only reachable via the top menu

    idx = dial_ui.preset_button_at(loop.touch_x, loop.touch_y, len(state.preset_quick_names))
    if idx == -1:
        return

    vals = [v for v, _ in state.preset_quick_names]
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
            if _denon is not None:
                _denon.async_set_preset_enabled(new_enabled, now)
            else:
                driver.set_preset_enabled(new_enabled)
            state.preset_enabled = new_enabled
        else:
            val, _name = state.preset_quick_names[idx]
            if _denon is not None:
                _denon.async_set_preset(val, now)
            else:
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

    if state.power != "ON" and loop.mode == MODE_MAIN:
        # Standby, resting on the power-off screen: nothing responds to a
        # short tap except the centered MENU zone (the power ring's hollow
        # middle -- see dial_ui.draw_main's power-off branch, which moves
        # the MENU hint there instead of its normal bottom position), and
        # only for backends that can switch to a different target
        # (CAPS["player_select"], e.g. HA's Media Player list). Power
        # itself is long-press only, handled in _handle_touch_down, not
        # here.
        if driver.CAPS["player_select"] and dial_ui.menu_standby_tap(loop.touch_x, loop.touch_y):
            _tap_open_menu(loop, ui, now)
        return

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
    # TEMPORARY: touch-drop investigation -- see STALL_THRESHOLD_S above.
    if loop.touch_start > 0.0:
        print("TOUCH up   t=%.3f held=%.3f xy=(%d,%d) mode=%d dispatch=%d" % (
            now, held, loop.touch_x, loop.touch_y, loop.mode,
            held >= TOUCH_MIN_S and not loop.touch_power_fired))
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

def _apply_poll_result(loop, ui, state, now, status):
    """Reconcile local state against a fresh AVR status dict, however it
    was obtained -- _poll_now()'s blocking driver.get_status() for every
    backend except denon, or denon.py's non-blocking poll engine (see
    _pump_denon_poll()) for denon. Returns True if the AVR was just
    detected powering on externally, so a timer-driven caller can
    reschedule its next poll sooner.

    Wraps the whole diffing/render body in the same try/except the
    original inline _poll_now() used to have around this same code (not
    just around the network call) -- state.apply_status()/dial_ui calls
    are unlikely to raise given a well-formed status dict, but preserving
    the original protection here means both callers (_poll_now and
    _pump_denon_poll) get it for free rather than one of them silently
    losing it.
    """
    try:
        powered_on_externally = False
        was_standby = state.power != "ON"
        was_on      = state.power == "ON"
        was_muted   = state.muted
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

        # Async-poll callers (denon) stash the interval that was in effect
        # at kickoff time here, since they can't use this function's
        # return value synchronously the way _poll_now()'s callers do --
        # see _poll_avr.
        if powered_on_externally and loop.pending_poll_interval is not None:
            loop.last_poll = now - loop.pending_poll_interval + 1.0
        loop.pending_poll_interval = None

        return powered_on_externally
    except Exception as e:
        _apply_poll_error(loop, ui, state, e)
        return False


def _apply_poll_error(loop, ui, state, e):
    """Failure half of _apply_poll_result -- same error_count/MODE_ERROR
    bookkeeping regardless of whether the poll came from _poll_now()'s
    blocking call or denon.py's non-blocking engine."""
    print("poll:", e)
    loop.error_count += 1
    if loop.error_count >= _MAX_ERRORS and loop.mode != MODE_ERROR:
        loop.mode = MODE_ERROR
        dial_ui.draw_error(ui, "Reconnecting...")
    loop.pending_poll_interval = None


def _poll_now(loop, ui, state, now):
    """Fetch AVR ground truth and reconcile local state via a blocking
    driver.get_status() call -- used directly by every backend except
    denon (which routes the background poll through denon.py's non-
    blocking engine instead, see _pump_denon_poll()).

    Shared by the adaptive timer poll (_poll_avr) and the mute-pulse trough
    poll (_pulse_mute) -- only the trigger condition differs between them.
    Returns True if the AVR was just detected powering on externally, so a
    timer-driven caller can reschedule its next poll sooner.
    """
    try:
        status = driver.get_status()
    except Exception as e:
        _apply_poll_error(loop, ui, state, e)
        return False
    return _apply_poll_result(loop, ui, state, now, status)


def _pump_denon_poll(loop, ui, state, now):
    """Advance denon.py's non-blocking status-poll engine by one tick.
    No-op unless denon is the active backend and a poll is in flight --
    see _denon/start_status_poll()/_poll_avr()/_pulse_mute()."""
    if _denon is None:
        return
    kind, payload = _denon.pump_status_poll(now)
    if kind == "done":
        _apply_poll_result(loop, ui, state, now, payload)
    elif kind == "error":
        _apply_poll_error(loop, ui, state, payload)


def _pump_denon_command(loop, ui, state, now):
    """Advance denon.py's non-blocking control-command engine (volume/
    mute/power/input/preset) by one tick. No-op unless denon is the active
    backend and a command is in flight -- see _denon/start_command() and
    each hot-path tap/encoder handler's _denon-is-not-None branch.

    Unlike _pump_denon_poll, success doesn't carry a status dict to
    reconcile -- just the same error_count/last_poll reset every hot-path
    call site already did inline on success before this conversion.
    Failure reuses _apply_poll_error() so a broken connection drives the
    same error_count/MODE_ERROR bookkeeping regardless of whether the
    poll or a control command noticed it first."""
    if _denon is None:
        return
    kind, payload = _denon.pump_command(now)
    if kind == "done":
        loop.error_count = 0
        loop.last_poll = now
    elif kind == "error":
        _apply_poll_error(loop, ui, state, payload)


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

    if _denon is not None:
        # Kick off the non-blocking poll and return immediately -- the
        # result lands later, over however many ticks it takes, via
        # _pump_denon_poll() (called every main-loop iteration). Stash the
        # interval here since _apply_poll_result can't get it from a
        # synchronous return value the way _poll_now()'s callers do.
        loop.pending_poll_interval = poll_interval
        _denon.start_status_poll(now)
    elif _poll_now(loop, ui, state, now):
        loop.last_poll = now - poll_interval + 1.0


def _retry_presets(loop, ui, state, now):
    """Catch-up retry for driver.get_presets() after it failed at boot.

    main() fetches presets/Dirac filters exactly once, at boot -- if the
    backend isn't reachable yet (AVR still warming up, transient network
    issue), state.preset_names stays permanently empty and the quick-select
    buttons never appear, with nothing to ever retry it again. Runs on its
    own short, fixed interval (_PRESET_RETRY_INTERVAL_S) independent of the
    normal adaptive status poll, and stops for good the moment it succeeds
    once -- this is a boot-catch-up mechanism, not an ongoing poll (a preset
    switch already refreshes the list itself, same as at boot).
    """
    if state.preset_names or not driver.CAPS.get("presets"):
        return
    if loop.enc_last_move != 0.0 or (now - loop.last_preset_retry) < _PRESET_RETRY_INTERVAL_S:
        return
    loop.last_preset_retry = now
    gc.collect()
    try:
        state.preset, state.preset_names = driver.get_presets()
        state.preset_enabled = driver.get_preset_enabled()
        state.preset_quick_names = driver.get_quick_presets()
        # Found + fixed 2026-08-01: this used to render unconditionally
        # once preset_names arrived, with no check on loop.first_poll.
        # state.power defaults to "STANDBY" (see AVRState.__init__) until
        # the first real status poll lands, and this retry's own interval
        # can genuinely fire before that first poll does -- rendering here
        # painted the power-off screen using that fabricated default,
        # which then got silently corrected a few seconds later once the
        # real poll came in. User-reported symptom: "it goes to the power
        # off screen before the menu screen... it's not powered off."
        # Guarding on first_poll means this only ever renders once
        # state.power reflects a real reading.
        if state.preset_names and loop.mode == MODE_MAIN and not loop.first_poll:
            dial_ui.draw_main(ui, state)   # quick-select buttons now have names to show
    except Exception as e:
        print("preset retry:", e, "| free mem:", gc.mem_free())


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
            if _denon is not None:
                # Kicked off, not awaited -- resolves later via
                # _pump_denon_poll(), which runs earlier in the main-loop
                # step order than this function, so the same "render below
                # always wins" guarantee still holds on whichever tick it
                # actually completes on.
                _denon.start_status_poll(now)
            else:
                _poll_now(loop, ui, state, now)   # may redraw; the render below always wins
    else:
        loop.mute_trough_polled = False

    # Render last: guarantees this frame's color is what's actually on
    # screen even if _poll_now()/_pump_denon_poll() just ran a full
    # draw_main().
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
    print("free mem at top of main:", gc.mem_free(), "t:", _splash_t)
    dial_ui.show_splash(board.DISPLAY)

    # Note: there is no OTA-pending check here anymore. code.py itself
    # reads the pending-action nvm byte and imports ota_boot.py instead of
    # app.py entirely when one is set -- main() is never even called in
    # that case. See ota_boot.py's module docstring for why that split
    # exists (importing app.py alone was pulling in driver.py/dial_ui.py/
    # the backend module regardless, leaving too little free memory for a
    # reliable TLS handshake).

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
    # gc.collect() immediately before the ~28KB gauge bitmap allocation --
    # CircuitPython's allocator is non-compacting, so anything freed by an
    # earlier import/compile step just leaves scattered holes rather than
    # one clean contiguous region; collecting right before a big allocation
    # is Adafruit's own documented mitigation (see "Memory-saving tips for
    # CircuitPython" -> "Reducing memory fragmentation").
    gc.collect()
    print("free mem right before dial_ui.init:", gc.mem_free())
    ui = dial_ui.init()

    # Enforce 2-second minimum splash display time
    _remaining = 2.0 - (time.monotonic() - _splash_t)
    if _remaining > 0.0:
        time.sleep(_remaining)

    # dial_ui.init() just allocated the ~28KB gauge bitmap plus every label
    # object. Same insurance as the gc.collect() calls before each
    # boot-time fetch further down: reclaim fragmented memory before the
    # WiFi radio needs to allocate its own buffers to associate/DHCP --
    # this is the very first network operation, so it's the most exposed
    # to whatever init() just left behind. Free memory logged once here to
    # make any future low-memory WiFi failure easy to confirm from the
    # serial log instead of having to guess (see project-context.md's
    # 2026-07-29 WiFi incident -- this print didn't find the actual bug
    # that day, a bloated font file did, but it's cheap enough to leave in
    # for next time).
    gc.collect()
    print("free mem before wifi connect:", gc.mem_free())

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
    if config.DEVICE_DRIVER == "wiim":
        # WiiM's httpapi.asp is HTTPS-only with a self-signed cert whose CN
        # (www.linkplay.com) never matches WIIM_HOST (an IP) -- and
        # adafruit_requests has no way to use a different SNI/verification
        # hostname than the request URL's literal host, so wiim.py can't use
        # the shared Session above at all. It gets its own raw-socket
        # transport instead, wired up directly here rather than through the
        # generic driver.init(session) contract -- see wiim.py's module
        # docstring for the full story (confirmed live on real hardware).
        # Imported here, not at module level, so a denon/minidsp/ha build
        # never pays to import or compile either module (see the heap/boot-
        # memory guardrails in .claude/CLAUDE.md).
        import ssl
        import wiim
        ssl_context = ssl.create_default_context()
        ssl_context.load_verify_locations(cadata=wiim.LINKPLAY_CA_PEM)
        wiim.init_transport(pool, ssl_context)
    elif config.DEVICE_DRIVER == "camilladsp":
        # CamillaDSP's control protocol is WebSocket-only -- adafruit_requests
        # doesn't speak that at all, so this backend needs the raw socket pool
        # directly, same reason (different protocol) as wiim.py above. No TLS
        # here (plain ws://), so no ssl_context needed. See camilladsp.py's
        # module docstring for the transport details -- confirmed working on
        # real hardware, same as wiim's.
        import camilladsp
        camilladsp.init_transport(pool)
    elif config.DEVICE_DRIVER == "denon":
        # Unlike wiim/camilladsp above, denon.py does NOT replace the
        # shared session -- control commands and boot-time loaders still
        # go through it via the normal driver.init(session) contract
        # below. Only the background status poll (the one call site
        # confirmed to freeze the touch-polling main loop for 1-3s on a
        # slow/failed request) gets its own raw, non-blocking socket path
        # -- see denon.py's "Non-blocking status poll" section.
        global _denon
        import denon as _denon
        _denon.init_transport(pool)
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

    # Discover every controllable target this backend can see (currently
    # only ha.py -- every media_player entity in the Home Assistant
    # instance). No-op for drivers that don't support it (driver.CAPS).
    try:
        driver.load_players()
    except Exception as e:
        print("load_players:", e)

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
        state.preset_quick_names = driver.get_quick_presets()
    except Exception as e:
        print("get_presets:", e, "| free mem:", gc.mem_free())

    gc.collect()
    try:
        state.player_id, state.player_names = driver.get_players()
    except Exception as e:
        print("get_players:", e, "| free mem:", gc.mem_free())

    loop = _Loop()
    loop.last_pos = encoder.position

    # Surface the result of a check/install exactly once, on the first
    # normal boot after the reload ota_boot.run() ends with -- see
    # _confirm_sub's "update" branch and ota_boot.py's run().
    _result = _ota_result()
    if _result != 0:
        loop.ota_status = {
            1: "available",
            2: "up_to_date",
            3: "error",
            4: "just_installed",
            5: "just_failed",
            6: "eject_needed",
        }.get(_result, "idle")
        if _result in (1, 5):
            # 1 (available): "Install Update (vN)". 5 (just_failed): "Install
            # failed" -- tapping either starts with "Install" in
            # _confirm_sub, which now shows "Power cycle now to install vN"
            # (see that branch's comment), so both need the version number
            # populated, not just the first-check-ever case.
            loop.ota_latest_version = _ota_latest_version()
        _set_ota_result(0)

        # Land straight back in the Update submenu with the result already
        # showing, instead of the plain volume screen -- a check/install
        # only ever reloads because the user asked for one, so "here's what
        # we found, confirm to proceed" should be the very next thing they
        # see, not something they have to re-navigate into Update to find.
        # Same _open_submenu() a manual tap on "Update" already uses --
        # sub_cursor lands on index 0 either way, which for this sub_type is
        # always the primary action ("Install Update (vN)"/"Check Again"/
        # etc, see _ota_update_items), so a single knob press or tap
        # confirms it exactly like opening Update by hand always has.
        loop.sub_type, loop.sub_cursor, title, ver, items = _open_submenu(
            "update", "Update", state, loop)
        loop.sub_scroll = _scroll_for(loop.sub_cursor, 0, len(items))
        loop.mode = MODE_MENU_SUB
        vis = items[loop.sub_scroll : loop.sub_scroll + dial_ui.MENU_VISIBLE]
        dial_ui.draw_menu(ui, title, vis, loop.sub_cursor - loop.sub_scroll, clear_bg=True, version_text=ver)

    # --- Main loop ---
    while True:
        t0 = time.monotonic()
        now = t0

        _handle_encoder_button(loop, ui, state, btn, now)
        t1 = time.monotonic()
        _handle_encoder_rotation(loop, ui, state, encoder, now)
        t2 = time.monotonic()
        _send_volume_debounced(loop, ui, state, now)
        t3 = time.monotonic()
        _auto_close_menu(loop, ui, state, now)
        t4 = time.monotonic()
        _handle_touch(loop, ui, state, touch, touch_ok, now)
        t5 = time.monotonic()
        # Placed before _pulse_mute (not next to _poll_avr, which kicks the
        # async poll off but never resolves it) so that if a poll resolves
        # on this tick, _pulse_mute's render afterward still "wins" the
        # frame -- see its own comment for why that ordering matters.
        _pump_denon_poll(loop, ui, state, now)
        t5a = time.monotonic()
        _pump_denon_command(loop, ui, state, now)
        t5b = time.monotonic()
        _pulse_mute(loop, ui, state, now)
        t6 = time.monotonic()
        _fade_power_off(loop, ui, state, now)
        t7 = time.monotonic()
        _poll_avr(loop, ui, state, now)
        t8 = time.monotonic()
        _retry_presets(loop, ui, state, now)
        t9 = time.monotonic()

        # TEMPORARY: touch-drop investigation -- see STALL_THRESHOLD_S above.
        if (t9 - t0) >= STALL_THRESHOLD_S:
            print(
                "STALL %dms enc_btn=%d enc_rot=%d vol=%d autoclose=%d "
                "touch=%d denon=%d cmd=%d mute=%d fade=%d avr=%d presets=%d" % (
                    (t9 - t0) * 1000,
                    (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000,
                    (t4 - t3) * 1000, (t5 - t4) * 1000, (t5a - t5) * 1000,
                    (t5b - t5a) * 1000,
                    (t6 - t5b) * 1000, (t7 - t6) * 1000, (t8 - t7) * 1000,
                    (t9 - t8) * 1000,
                )
            )
