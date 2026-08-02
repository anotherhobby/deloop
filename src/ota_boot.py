# ota_boot.py -- the entire reason this module exists, separate from app.py:
# code.py imports THIS instead of app.py whenever an OTA action is pending,
# so a Check Now / Install Update never pays the cost of importing
# driver.py, dial_ui.py (and its 3 loaded PCF fonts), sound.py, state.py,
# or whichever backend module config.DEVICE_DRIVER selects.
#
# Root-caused live (2026-08-01), after headers/timeouts/retry logic all
# turned out to be red herrings: app.py's _ota_lean_mode() was never
# actually lean with respect to *imports* -- only with respect to which
# functions got called (skipping dial_ui.init()'s big bitmap, skipping
# driver/hardware setup). But `import app` alone -- regardless of which
# branch main() takes afterward -- unconditionally runs app.py's own
# top-level `import driver` / `import dial_ui` / `import sound` /
# `from state import AVRState`, which transitively pulls in the entire
# device-driver stack. Confirmed live: with all of that resident, free
# memory drops to ~30KB, and a bare TLS handshake to api.github.com (no
# adafruit_requests involved at all -- a raw socket + ssl.wrap_socket())
# fails outright with a MemoryError at that level. The same request
# succeeds in under 3 seconds every time with ~100KB+ free. This isn't a
# library bug or a missing-header issue (both were tried and ruled out
# first) -- it's a hard memory-budget problem, and the only real fix is
# not importing the driver stack at all when the boot's only job is a
# network call. See docs/ota.md's "Reliability investigation" for the
# full trail that led here.
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

    Also disables the WiFi radio before reloading -- confirmed live
    (2026-08-01) that a soft reload resets the Python heap but not
    whatever the WiFi/TLS stack holds onto after a network-heavy attempt;
    a harder radio shutdown here is an attempt to force that release.
    Unconfirmed whether this alone is sufficient now that the real
    memory-budget fix (this module's entire existence) is in place --
    see docs/ota.md."""
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

    Superseded (2026-08-01): every earlier version of this docstring (the
    disable/enable cycle, the settle delays, the wifi.radio.connected
    gating) was reasoning about a real effect -- something the radio
    reset genuinely helped with -- but never actually identified the
    mechanism, because a SEPARATE bug (adafruit_requests choking on
    github.com's ~3625-byte Content-Security-Policy header -- see
    ota.py's _raw_fetch_location() docstring) was confounding every test
    run at the same time. With that bug independently found and fixed,
    -12288 was STILL 100% reproducible in the real apply() flow, and
    isolating it live (REPL, calling the real _Fetcher/_new_session
    directly, not a hand-typed replica) finally pinned down the actual
    pattern: connecting to a 3rd genuinely NEW hostname within one
    continuous process reliably fails, at the .connect() call itself,
    in ~0.05-0.3s (vs ~2s for a real handshake) -- while connecting
    (even repeatedly, even freshly rebuilding the whole session every
    time) to only 1 or 2 distinct hostnames, in any order, works
    indefinitely. Confirmed with 4 sequential connections to a single
    host, and with 4 connections alternating between only 2 hosts -- both
    all succeeded. The one thing that differs for a 3rd NEW host is
    exactly that: it's new. This is almost certainly some small,
    fixed-capacity (2-entry) DNS-resolver or TLS-session/SNI cache
    internal to this CircuitPython port's ESP32-S3 WiFi/TLS bindings that
    overflows instead of evicting on a 3rd distinct hostname.

    The plain wifi.radio.enabled = False / True toggle this function used
    to do does NOT clear whatever that cache is -- confirmed live, -12288
    recurred identically with it in place. wifi.radio.stop_station()
    followed by start_station() (both with a real settle delay, then a
    fresh connect()) does -- confirmed live: after 2 successful unique
    hosts, a stop_station()/start_station() cycle let a 3rd AND a 4th
    brand-new hostname both connect cleanly afterward, not just one --
    this is a genuine reset of whatever's overflowing, not a one-time
    workaround for a small fixed budget. In this project's real OTA
    flow, only 3 unique hosts are ever actually needed (api.github.com,
    github.com for the redirect resolve, and the CDN host every file's
    signed download URL resolves to) -- so this reset only ever needs to
    matter once per apply()/check_latest_version() call, not once per
    file, but it's applied on every _reset_connections() call
    unconditionally (matching this function's existing host-agnostic
    design) rather than trying to track "is this host new" here too.

    Still gated on wifi.radio.connected, same reasoning as always: no
    prior connection to reset on the very first call of a fresh boot, so
    just connect plainly there instead of stopping a station that was
    never started."""
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


def _do_install():
    """Same reasoning as _do_check()."""
    ota.apply(_new_session)


def run():
    """Entry point -- code.py calls this INSTEAD OF app.main() whenever an
    OTA action is pending. Never returns; every path ends in a reload."""
    action = _ota_action()
    # Consumed immediately, before any network/filesystem work -- confirmed
    # live (2026-08-01) that a MemoryError from a failed retry can leave the
    # *recovery* path's own nvm write also failing (heap still fragmented),
    # which left this flag stuck at "pending" and boot-looped the device
    # forever. Clearing it here means no failure later in this function --
    # caught or not -- can re-trigger OTA on the next boot. Worst case is a
    # single lost manual check/install attempt, trivially recoverable by
    # pressing the button again.
    _set_ota_action(0)

    # Diagnostic-only escape hatch (added 2026-08-01): code.py routes here
    # whenever NVM_OTA_ACTION is any nonzero value, not just 1/2, so this
    # just returns immediately instead of running a real check/install or
    # reloading. Lands back at the REPL with ONLY this module's own
    # imports resident (wifi/socketpool/adafruit_requests/ota, none of
    # app.py's driver-stack imports) -- the exact same memory footprint
    # check_latest_version()/apply() actually run with. Exists because
    # real-hardware debugging of -12288/MemoryError kept getting
    # contaminated by REPL sessions that had inherited app.py's ~30-90KB
    # of driver-stack imports from whatever boot happened to be running
    # before -- there was no clean way to compare a REPL measurement
    # against real OTA memory conditions without this. Trigger with
    # `microcontroller.nvm[config.NVM_OTA_ACTION] = 9` then
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

    # One-time warm-up (2026-08-01): confirmed live that the FIRST
    # wifi.radio.stop_station()/start_station() reconnect cycle after a
    # fresh boot doesn't leave the radio/TLS stack fully ready --
    # connecting to release-assets.githubusercontent.com right after
    # api.github.com failed with OSError -12288 100% of the time (10/10),
    # every time, in a controlled sequence where every host got its own
    # full session rebuild. Isolated the actual variable by testing many
    # reorderings of the same 3 hosts: identity and order of the hosts
    # turned out not to matter at all. What did: adding one single EXTRA,
    # otherwise pointless _new_session() rebuild before touching any real
    # host made the exact same original ordering succeed every time
    # after that. Whatever needs to finish settling only needs to happen
    # once per boot -- absorbing that cost here, on a throwaway session
    # nothing else uses, means it never lands on whatever the first real
    # request happens to be.
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
