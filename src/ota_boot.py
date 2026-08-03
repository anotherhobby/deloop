# ota_boot.py -- the entire reason this module exists, separate from app.py:
# code.py imports THIS instead of app.py whenever an OTA action is pending,
# so a Check Now / Install Update never pays the cost of importing
# driver.py, dial_ui.py (and its 3 loaded PCF fonts), sound.py, state.py,
# or whichever backend module config.DEVICE_DRIVER selects.
#
# `import app` alone -- regardless of which branch main() takes afterward
# -- unconditionally runs app.py's own top-level `import driver` /
# `import dial_ui` / `import sound` / `from state import AVRState`, which
# transitively pulls in the entire device-driver stack. With all of that
# resident, free memory drops to ~30KB, and a bare TLS handshake (no
# adafruit_requests involved at all -- a raw socket + ssl.wrap_socket())
# fails outright with a MemoryError at that level; the same handshake
# succeeds in under 3 seconds every time with ~100KB+ free. It's a hard
# memory-budget problem, not a library bug or a missing-header issue --
# the fix is not importing the driver stack at all when the boot's only
# job is a network call.
#
# This module's only display dependency is terminalio's built-in font (no
# PCF file load, unlike dial_ui.py's three loaded fonts) plus vectorio for
# a plain background rect -- deliberately not dial_ui.py, even just for a
# one-line status message.
#
# NVM byte layout (config.NVM_OTA_ACTION/RESULT/VERSION) is shared with
# app.py, which still owns reading OTA_RESULT/OTA_VERSION for its own
# normal-mode UI (the Update submenu's status line and "Install Update
# (vN)" label) and writing OTA_ACTION to request a check/install. This
# module owns everything else: consuming the pending action, doing the
# real network/filesystem work, and writing the result.
#
# Never calls microcontroller.reset() -- see the "why a soft reload, never
# a hard reset" reasoning in docs/ota.md; that finding is independent of
# this module split and still holds.

import gc
import time
import board
import microcontroller
import wifi
import socketpool
import adafruit_requests
import displayio
import terminalio
from adafruit_display_text import label

import config
import version
import ota

_W = _H = 240
_CX = _CY = 120

def _ota_action():
    try:
        return microcontroller.nvm[config.NVM_OTA_ACTION]
    except Exception:
        return 0


def _set_ota_action(val):
    gc.collect()   # nvm writes need a real contiguous buffer -- see _set_ota_result
    try:
        microcontroller.nvm[config.NVM_OTA_ACTION] = val
    except Exception as e:
        print("set_ota_action:", type(e), e)


def _set_ota_result(val):
    # Confirmed live (2026-07-31): an nvm[] write itself needs to allocate
    # a real buffer (observed ~8KB), which can fail with a MemoryError of
    # its own right after a network-heavy operation. gc.collect() first.
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


def _set_ota_latest_version(val):
    try:
        microcontroller.nvm[config.NVM_OTA_VERSION] = val
    except Exception as e:
        print("set_ota_latest_version:", type(e), e)


def _show_message(text):
    """Minimal single-line status screen -- terminalio's built-in font
    (no PCF file load) and no gauge bitmap. See module docstring for why
    this deliberately doesn't call into dial_ui.py at all."""
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
        terminalio.FONT, text=text, color=0xEEEEEE,
        anchor_point=(0.5, 0.5), anchored_position=(_CX, _CY),
    ))
    board.DISPLAY.auto_refresh = False
    board.DISPLAY.root_group = group
    board.DISPLAY.refresh()


def _reload_to_normal():
    """Clear the pending action and reload back into normal UI mode --
    shared tail for every run() exit path. A *soft* reload, not
    microcontroller.reset() -- see docs/ota.md for why that distinction
    matters here just as much on the way out as on the way in.

    Also disables the WiFi radio before reloading: a soft reload resets
    the Python heap but not whatever the WiFi/TLS stack holds onto after a
    network-heavy attempt, so a harder radio shutdown here forces that
    release."""
    _set_ota_action(0)
    try:
        wifi.radio.enabled = False
    except Exception as e:
        print("ota: wifi.radio.enabled = False failed:", type(e), e)
    gc.collect()
    import supervisor
    supervisor.reload()


def _new_session():
    """Fresh Session(ssl_context) -- factored out so ota.py's _Fetcher can
    call it again mid-operation (see its _reset_connections()) without the
    caller needing to spell any of this out twice.

    Cycles the WiFi radio (stop_station() then start_station(), each
    followed by a settle delay) before reconnecting, rather than just
    calling connect() again on an already-connected radio -- a plain
    enable/disable toggle isn't enough to guarantee a clean radio/TLS
    state for the new session; the stop/start station cycle is.

    Gated on wifi.radio.connected: no prior connection to reset on the
    very first call of a fresh boot, so just connect plainly there
    instead of stopping a station that was never started."""
    if wifi.radio.connected:
        try:
            wifi.radio.stop_station()
            time.sleep(0.5)
            wifi.radio.start_station()
            time.sleep(0.5)
        except Exception as e:
            print("ota: wifi radio stop/start station cycle failed:", type(e), e)
        try:
            wifi.radio.power_management = wifi.PowerManagement.NONE
        except Exception as e:
            print("power_management NONE failed:", type(e), e)
        wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)
    else:
        try:
            wifi.radio.power_management = wifi.PowerManagement.NONE
        except Exception as e:
            print("power_management NONE failed:", type(e), e)
        wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)
    pool = socketpool.SocketPool(wifi.radio)
    import ssl
    ssl_context = ssl.create_default_context()
    return adafruit_requests.Session(pool, ssl_context)


def _do_check():
    """Runs in its own stack frame so pool/ssl_context/session -- the real
    memory hogs -- are collectible before the nvm write that records the
    result. See _set_ota_result's comment on why that matters."""
    return ota.check_latest_version(_new_session)


def _show_install_progress(current, total):
    """ota.apply()'s on_progress hook -- redraws the status message right
    before each file's own download starts, so a stalled or crashed
    install shows the file it actually died on ("stuck at X of N
    packages") instead of a static "Updating..." that gives no sense of
    whether it's still making progress, or the last file that already
    finished. Same show_message() every other status screen in this
    module uses -- no separate display path to keep in sync."""
    _show_message("Updating to v{}...\n{} of {} packages".format(
        _ota_latest_version(), current, total))


def _do_install():
    """Same reasoning as _do_check()."""
    ota.apply(_new_session, on_progress=_show_install_progress)


def run():
    """Entry point -- code.py calls this INSTEAD OF app.main() whenever an
    OTA action is pending. Never returns; every path ends in a reload."""
    action = _ota_action()
    # Consumed immediately, before any network/filesystem work: a failure
    # later in this function (caught or not) can then never leave this
    # flag stuck at "pending" and boot-loop the device. Worst case is a
    # single lost manual check/install attempt, trivially recoverable by
    # pressing the button again.
    _set_ota_action(0)

    # Diagnostic-only escape hatch: code.py routes here whenever
    # NVM_OTA_ACTION is any nonzero value, not just 1/2, so this just
    # returns immediately instead of running a real check/install or
    # reloading. Lands back at the REPL with ONLY this module's own
    # imports resident (wifi/socketpool/adafruit_requests/ota, none of
    # app.py's driver-stack imports) -- the exact same memory footprint
    # check_latest_version()/apply() actually run with, useful for
    # comparing a REPL measurement against real OTA memory conditions.
    # Trigger with `microcontroller.nvm[config.NVM_OTA_ACTION] = 9` then
    # `supervisor.reload()` from REPL.
    if action == 9:
        print("ota_boot: diagnostic mode, free mem:", gc.mem_free())
        return

    if action == 1:
        _show_message("Checking...\ncurrent v{}".format(version.CURRENT_VERSION))
    else:
        _show_message("Updating to v{}...".format(_ota_latest_version()))

    if not config.WIFI_SSID:
        _set_ota_result(3 if action == 1 else 5)
        _reload_to_normal()
        return

    # One-time warm-up: the FIRST wifi.radio.stop_station()/start_station()
    # reconnect cycle after a fresh boot doesn't leave the radio/TLS stack
    # fully ready for a real request. Absorbing that settle cost here, on
    # a throwaway session nothing else uses, means it never lands on the
    # first real request instead.
    try:
        _new_session()
    except Exception as e:
        print("ota: warm-up session build failed:", type(e), e)
    gc.collect()

    if action == 1:   # Check Now
        try:
            latest, current = _do_check()
            gc.collect()   # pool/ssl_context/session are out of scope now
            print("free mem before ota result nvm writes:", gc.mem_free())
            if latest > current:
                _set_ota_latest_version(latest)
                _set_ota_result(1)
            else:
                _set_ota_result(2)
        except Exception as e:
            print("ota check failed:", type(e), e)
            _set_ota_result(3)
        _reload_to_normal()
        return

    # action == 2: Install Update.
    import storage
    try:
        storage.remount("/", readonly=False)
    except Exception as e:
        print("ota install: remount failed:", type(e), e)
        _set_ota_result(6)   # eject_needed
        _reload_to_normal()
        return

    ok = False
    try:
        _do_install()
        ok = True
    except Exception as e:
        print("ota apply failed:", type(e), e)
    gc.collect()   # pool/ssl_context/session are out of scope now
    print("free mem before ota result nvm writes:", gc.mem_free())

    try:
        storage.remount("/", readonly=True)   # restore the normal default
    except Exception as e:
        print("ota install: remount-back failed:", type(e), e)

    _set_ota_result(4 if ok else 5)
    _reload_to_normal()
