# camilladsp.py -- CamillaDSP WebSocket client
# (https://github.com/HEnquist/camilladsp)
#
# Like minidsp.py, deloop never talks to hardware directly here -- CamillaDSP
# is a software DSP engine that must already be running on some host (it
# owns the actual audio capture/playback devices via its own config file),
# exposing a control API deloop talks to over the LAN. Unlike minidsp-rs,
# that control API is WebSocket-only -- there is no plain HTTP/REST option
# at all -- so this backend needs its own from-scratch WS client instead of
# the shared adafruit_requests.Session every HTTP backend uses (denon.py/
# minidsp.py/ha.py). See "Transport" below for what that involves and what's
# confirmed vs. still open about it.
#
# ---------------------------------------------------------------------------
# Command/reply shapes -- originally confirmed against the vendor's own
# client library, since reconfirmed live against a real CamillaDSP 4.1.3
# process (2026-07-30, via tools/probe_camilladsp.py and this driver itself)
# ---------------------------------------------------------------------------
# local/pycamilladsp (HEnquist/pycamilladsp, downloaded but gitignored, not
# vendored into this repo) is the official Python WebSocket client. Its
# camillaws.py wraps the `websocket-client` package and cannot run on
# CircuitPython as-is (no `websocket`/`threading`/`typing` there) -- but its
# test suite (tests/test_camillaws.py) records the *exact* request/reply JSON
# shapes the real server produces, which was already a materially stronger
# source than a summarized doc (see .claude/CLAUDE.md's hard lesson #3)
# before the live confirmation below existed:
#
#   Request:  bare `json.dumps(command)` for a no-arg command (e.g.
#             "GetVolume"), or `json.dumps({command: arg})` for one with an
#             argument (e.g. {"SetVolume": -30.0}).
#   Reply:    {"<Command>": {"result": "Ok" | "Error" | {"SomeError": "msg"},
#                             "value": <optional>}}
#             "result": "Ok" commands with nothing to return (SetVolume,
#             SetMute, Reload, ...) omit "value" entirely -- treat it as None.
#
# Commands used here, all present in pycamilladsp's confirmed command set:
#   GetVolume -> float (dB)          SetVolume: <float dB>  (clamped -150..+50
#                                     server-side, confirmed via CamillaDSP's
#                                     own websocket.md docs)
#   GetMute -> bool                  SetMute: <bool>
#   GetConfigFilePath -> str         SetConfigFilePath: <str>  (does not take
#                                     effect until a following "Reload")
#   Reload -> (no value)             reloads whatever SetConfigFilePath last set
#
# ---------------------------------------------------------------------------
# Transport -- hand-rolled WebSocket client. Confirmed working end-to-end on
# real M5 Dial hardware against a real CamillaDSP 4.1.3 process (2026-07-30).
# ---------------------------------------------------------------------------
# CircuitPython has no usable native WebSocket client for this board (the
# community libraries are work-in-progress and target Airlift co-processor
# boards, not the M5 Dial's plain wifi/socketpool stack -- the same finding
# that ruled out a WebSocket-based ha.py design back when that backend was
# built), so this hand-rolled client follows the WebSocket RFC (6455) and the
# confirmed shapes above directly. It was written with no live CamillaDSP
# instance or M5 Dial available, then verified live the same day: volume,
# mute, and touch all confirmed working against a real process. Still
# genuinely unverified: the Ping/no-Pong assumption below under real-world
# conditions beyond what a short test session exercised, and preset-switch
# timing against anything but a trivial test config (see
# CAMILLADSP_PRESET_TIMEOUT_MS's comment in config.py). tools/probe_camilladsp.py
# remains the right first step against any *new* CamillaDSP instance -- it
# exercises the same command set via the real `websocket-client` package (a
# known-good implementation) so a failure there means "check
# CAMILLADSP_HOST/PORT", while a failure only in this file's own handshake/
# framing narrows it to this code.
#
# Deliberate scope-reductions, chosen because deloop's whole architecture is
# poll-per-call with no persistent connections anywhere (see _poll_avr in
# app.py) -- there is no case here that needs a long-lived connection:
#   - Every call opens a fresh TCP connection, does the WS opening handshake,
#     sends one or more command frames, reads their replies, sends a close
#     frame, and closes the socket. No keepalive, no reconnect-on-drop logic.
#   - Sec-WebSocket-Accept (the server's echo of a hash of the client's
#     Sec-WebSocket-Key) is never verified. That check exists to stop a non-
#     WS-aware server from being mistaken for one; it is not a security
#     control on a private LAN. Skipping it avoids needing SHA1, which is
#     not a CircuitPython core module (would otherwise need the
#     adafruit_hashlib bundle library, an extra dependency for a check this
#     project has no real use for).
#   - Only single-frame text replies are handled; the 126-byte extended-
#     length case is handled (a GetConfig dump could plausibly exceed 125
#     bytes) but the 64-bit length case and multi-frame fragmentation are
#     not -- not needed for the short JSON replies every command used here
#     produces.
#   - Ping frames from the server are read and discarded, never ponged.
#     Correct per-spec behavior is to reply with a matching Pong; skipped
#     here because every connection this file opens is closed within
#     milliseconds of being opened, so there should never be a window for
#     the server to expect one. Unverified: if CamillaDSP's server actually
#     expects an immediate Pong before it will let a connection stay open at
#     all, that assumption is wrong and this needs revisiting.
#
# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
#   - No power/standby concept, same reasoning as minidsp.py: CamillaDSP is
#     "on" whenever its host process is running. CAPS["power"] is False.
#   - No input/source concept exposed here either. CamillaDSP's capture
#     device is part of its loaded config file, not a separate switchable
#     axis the way a Denon/MiniDSP/WiiM "input" is -- switching to a config
#     with a different capture device is just a different preset (below),
#     not a distinct menu. CAPS["input_select"] stays False -- but
#     get_status()'s "input" field (dial_ui.py's top status label, otherwise
#     always blank for this backend) is repurposed for DSP processing status
#     instead: "<rate>khz" while GetState is "Running", or the raw GetState
#     word (Paused/Inactive/Starting/Stalled) otherwise. Purely a display
#     choice, not a capability -- see get_status()/_format_status() below.
#   - Presets are CamillaDSP config files, offered as a fixed name/path list
#     from config.CAMILLADSP_PRESETS (see config.py) -- CamillaDSP has no
#     concept of a fixed "slot count" the way MiniDSP's config slots or
#     WiiM's favorites do, so this is a plain list rather than a count +
#     optional-names pair. Unlike minidsp.py/wiim.py, the *current* selection
#     is a real live value (GetConfigFilePath) rather than a guess or a
#     permanently-empty placeholder -- CamillaDSP actually reports which
#     config file is loaded, which neither of those other two APIs do for
#     their own preset-equivalents.
#   - No independent "enabled" dimension for a preset (CAPS["preset_enable"]
#     stays False) -- a config file is either the one loaded or it isn't,
#     there is no separate on/off bit the way Denon/MiniDSP's Dirac Live
#     toggle is independent of which filter/slot is selected.
#   - Main-screen quick-select buttons are a config-driven *subset* of the
#     full preset list, not the full list itself -- CAPS["preset_quickbuttons"]
#     is `len(config.CAMILLADSP_QUICK_PRESETS) > 0`, and get_quick_presets()
#     returns that separate list rather than deferring to driver.py's default
#     (which would just reuse get_presets()' full list, the denon.py/
#     minidsp.py behavior).
#
#     The subset is the point, and both extremes are wrong. Buttons for the
#     whole list (the denon.py/minidsp.py behavior) breaks here because
#     CamillaDSP presets are arbitrary named config files with no ceiling,
#     unlike their physically-fixed slot counts -- a long list overflows the
#     button row and its tap-rect math. But no buttons at all gives up a
#     genuinely useful shortcut just because the *full* list can be long.
#     CAMILLADSP_QUICK_PRESETS (config.py) is the middle: a capped-at-4 list
#     of names that must already appear in CAMILLADSP_PRESETS, so a user
#     picks which few are worth one tap while the complete list stays
#     reachable through the submenu.
#   - CAPS["player_select"] is False -- this backend only ever controls the
#     one CamillaDSP instance at CAMILLADSP_HOST/PORT.

import binascii
import json
import os

import config

CAPS = {"power": False, "input_select": False,
        "presets": len(config.CAMILLADSP_PRESETS) > 0,
        "preset_enable": False, "preset_select_enables": False,
        "player_select": False,
        "preset_quickbuttons": len(config.CAMILLADSP_QUICK_PRESETS) > 0}
LABELS = {"presets": "Preset"}

_pool = None
_TIMEOUT = config.CAMILLADSP_TIMEOUT_MS // 1000
# Switching the active config file reloads CamillaDSP's whole filter
# pipeline -- same "full reconfiguration is slow" reasoning as minidsp.py's
# _PRESET_TIMEOUT. Measured ~1-3ms live against a trivial test config (see
# config.py's CAMILLADSP_PRESET_TIMEOUT_MS comment) -- a lower bound, not a
# general answer, since a real config with large filter files to load could
# be much slower. Left at the same generous headroom minidsp.py needed for
# its own case until a real production config is timed.
_PRESET_TIMEOUT = config.CAMILLADSP_PRESET_TIMEOUT_MS // 1000


def init(session):
    """Required by the driver contract's call signature, but unused -- see
    init_transport() for why this backend needs the raw socket pool instead
    of the shared adafruit_requests.Session (it doesn't speak WebSocket)."""
    pass


def init_transport(pool):
    """Supply the raw socket pool. Called directly by app.py (not through
    the generic driver contract), same reason as wiim.py's init_transport()
    -- this backend's transport needs differ from the shared Session. Must
    be called before any other function."""
    global _pool
    _pool = pool


# ---------------------------------------------------------------------------
# Internal helpers -- raw WebSocket client. See module docstring for what
# this deliberately does and does not implement, and what's still unverified.
# ---------------------------------------------------------------------------

def _recv_more(sock, buf):
    chunk = bytearray(512)
    n = sock.recv_into(chunk, 512)
    if n == 0:
        raise OSError("camilladsp: connection closed unexpectedly")
    return buf + bytes(chunk[:n])


def _ws_handshake(sock):
    """Send the WS opening handshake and confirm a 101 response. Returns any
    bytes already read past the header block -- the start of the first WS
    frame, if the server sent one immediately (it shouldn't, but nothing
    reads past the header boundary on the wire, so there is no way to know
    without checking)."""
    key = binascii.b2a_base64(os.urandom(16), newline=False).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        "Host: {}:{}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).format(config.CAMILLADSP_HOST, config.CAMILLADSP_PORT, key)
    sock.send(request.encode("utf-8"))

    buf = b""
    sep = b""
    while not sep:
        buf = _recv_more(sock, buf)
        head, sep, rest = buf.partition(b"\r\n\r\n")
    status_line, _, _ = head.partition(b"\r\n")
    if b" 101 " not in status_line:
        raise OSError("camilladsp: handshake failed: {!r}".format(status_line))
    return rest


def _mask(payload, key):
    masked = bytearray(len(payload))
    for i in range(len(payload)):
        masked[i] = payload[i] ^ key[i % 4]
    return bytes(masked)


def _send_frame(sock, payload):
    """Send `payload` (bytes) as one masked WS text frame (opcode 0x1).
    Client->server frames must be masked per RFC 6455."""
    length = len(payload)
    mask_key = os.urandom(4)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    elif length < 65536:
        header = bytes([0x81, 0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
    else:
        raise OSError("camilladsp: message too large ({} bytes)".format(length))
    sock.send(header + mask_key + _mask(payload, mask_key))


def _recv_frame(sock, leftover):
    """Read one unmasked WS frame (server->client frames are never masked).
    Returns (opcode, payload, leftover-bytes-for-next-frame). Only the
    <126 and 126 (2-byte extended) length forms are handled -- see module
    docstring."""
    buf = leftover
    while len(buf) < 2:
        buf = _recv_more(sock, buf)
    opcode = buf[0] & 0x0F
    length = buf[1] & 0x7F
    header_len = 2
    if length == 126:
        while len(buf) < 4:
            buf = _recv_more(sock, buf)
        length = (buf[2] << 8) | buf[3]
        header_len = 4
    elif length == 127:
        raise OSError("camilladsp: 64-bit frame length not supported")
    while len(buf) < header_len + length:
        buf = _recv_more(sock, buf)
    payload = buf[header_len:header_len + length]
    rest = buf[header_len + length:]
    return opcode, payload, rest


def _send_close(sock):
    """Best-effort clean WS close (masked, empty-payload close frame) --
    not required for correctness (the socket is closed either way right
    after), just avoids CamillaDSP logging a missing-closing-handshake
    warning -- see local/pycamilladsp's _CamillaWS.__del__ comment, which
    closes the same way for the same reason."""
    try:
        sock.send(bytes([0x88, 0x80]) + os.urandom(4))
    except Exception:
        pass


def _ws_connect(timeout=None):
    sock = _pool.socket(_pool.AF_INET, _pool.SOCK_STREAM)
    sock.settimeout(timeout or _TIMEOUT)
    try:
        sock.connect((config.CAMILLADSP_HOST, config.CAMILLADSP_PORT))
        leftover = _ws_handshake(sock)
    except Exception:
        sock.close()
        raise
    return sock, leftover


def _parse_reply(command, raw):
    """Parse CamillaDSP's {command: {"result": ..., "value": ...}} envelope
    -- see module docstring's "Command/reply shapes" section for how this
    was confirmed. Ported from local/pycamilladsp's _CamillaWS._handle_reply/
    _handle_result without its custom exception hierarchy -- this codebase
    just raises OSError everywhere (see denon.py/minidsp.py) and lets
    app.py's blanket `except Exception` handle it."""
    reply = json.loads(raw)
    body = reply.get(command)
    if body is None:
        raise OSError("camilladsp: unexpected reply {!r}".format(raw))
    result = body.get("result")
    if result == "Ok":
        return body.get("value")
    if isinstance(result, dict):
        _, message = next(iter(result.items()))
    else:
        message = body.get("value") or result
    raise OSError("camilladsp: {} failed: {}".format(command, message))


def _ws_query_many(commands, timeout=None):
    """Open one connection, send each (command, arg) pair in order, and
    return a list of their parsed values -- avoids paying a fresh TCP
    connect + WS handshake per command when several are needed together
    (see get_status(), which always wants GetVolume and GetMute at once, and
    set_preset(), which needs SetConfigFilePath followed by Reload).
    """
    sock, leftover = _ws_connect(timeout)
    values = []
    try:
        for command, arg in commands:
            message = json.dumps({command: arg}) if arg is not None else json.dumps(command)
            _send_frame(sock, message.encode("utf-8"))
            while True:
                opcode, payload, leftover = _recv_frame(sock, leftover)
                if opcode in (0x1, 0x2):
                    values.append(_parse_reply(command, payload))
                    break
                if opcode == 0x8:
                    raise OSError("camilladsp: connection closed during reply")
                # Ping (0x9) or other control frame -- ignore and keep
                # reading rather than reply with a Pong. See module
                # docstring's transport section for why.
        _send_close(sock)
    finally:
        sock.close()
    return values


def _ws_query(command, arg=None, timeout=None):
    return _ws_query_many([(command, arg)], timeout=timeout)[0]


# ---------------------------------------------------------------------------
# Public API -- volume / mute / status
# ---------------------------------------------------------------------------

# CamillaDSP has no input/source concept (see module docstring), so
# get_status()'s "input" field -- dial_ui.py's top status label, otherwise
# always blank for this backend -- is repurposed to show DSP processing
# status instead: "<rate>khz" while actively processing audio, or the raw
# GetState word (Paused/Inactive/Starting/Stalled) otherwise. This is
# display-only -- CAPS["input_select"] stays False, no Input menu,
# friendly_input() is never called since nothing reads state.input_names
# for this backend.
#
# CONFIRMED CAMILLADSP QUIRK (2026-07-30, real hardware + CamillaDSP 4.1.3
# source read directly): a "SignalGenerator" capture device -- which is what
# local/camilladsp/*.yml's throwaway test configs use, specifically to avoid
# needing real audio hardware or a macOS mic-permission prompt -- never
# reports GetState as "Running". Confirmed in CamillaDSP's own source
# (src/generatordevice.rs): its capture loop only ever writes
# ProcessingState::Inactive (on stop); it never writes ::Running at all,
# unlike every other capture backend (CoreAudio/Alsa/Wasapi/Asio/Pulse/
# PipeWire/File all do, confirmed by grepping each). GetCaptureRate also
# stays 0 for the same reason -- there's no real clock to measure on a
# synthetic source. Confirmed live: swapping to a RawFile capture (a short
# generated PCM tone, real channels: field, real measured clock) shows
# GetState reach "Running" within ~1s and GetCaptureRate report real
# (noisily fluctuating, as expected for a measured value) numbers around the
# nominal rate. This is a real CamillaDSP behavior, not a deloop bug -- any
# real capture device (which is what an actual CamillaDSP user has) will
# report "Running" normally. Don't be alarmed if `local/camilladsp/flat.yml`
# et al permanently show "Starting" on deloop's display -- that's expected
# for this specific test setup, not a regression.
_RUNNING = "Running"


def _format_status(state_str, rate):
    """Format the DSP status text for dial_ui.py's top label. Only shows the
    rate while actually "Running" -- GetCaptureRate isn't necessarily
    meaningful yet during "Starting", and doesn't apply at all to
    Paused/Inactive/Stalled -- so every other state just shows the literal
    GetState word, which CamillaDSP's own vocabulary already makes clear to
    anyone familiar with the tool.

    Rate only, never channel count: "<channels>ch - <rate>khz" was built
    and measured visibly too wide on the real display. The channel count is
    available (GetConfigJson's devices.capture.channels) if a row is ever
    found for it."""
    if state_str != _RUNNING or not rate:
        return state_str or ""
    rate_khz = rate / 1000.0
    rate_text = "{:.1f}".format(rate_khz).rstrip("0").rstrip(".")
    return "{}khz".format(rate_text)


def get_status():
    """Poll CamillaDSP for current state.

    Returns a dict shaped like denon.get_status() -- power is always "ON"
    since CamillaDSP has no power/standby concept (see module docstring).
    "input" is repurposed for DSP processing status (see _format_status()),
    not a real input. Raises OSError on network/protocol failure.
    """
    volume_db, muted, state_str, rate = _ws_query_many([
        ("GetVolume", None), ("GetMute", None),
        ("GetState", None), ("GetCaptureRate", None),
    ])
    return {
        "volume_db": float(volume_db) if volume_db is not None else config.VOLUME_MIN,
        "muted": bool(muted),
        "power": "ON",
        "input": _format_status(state_str, rate),
    }


def set_volume(db):
    db = max(config.VOLUME_MIN, min(config.VOLUME_MAX, float(db)))
    _ws_query("SetVolume", db)


def mute_on():
    _ws_query("SetMute", True)


def mute_off():
    _ws_query("SetMute", False)


# ---------------------------------------------------------------------------
# Presets -- CamillaDSP config files. See module docstring's Capabilities
# section for why this is a fixed name/path list rather than a numbered-slot
# scheme like minidsp.py/wiim.py.
# ---------------------------------------------------------------------------

def get_presets():
    """Return (current_path, [(path, name), ...]) from config.CAMILLADSP_PRESETS.

    current_path comes from a live GetConfigFilePath query -- unlike
    minidsp.py/wiim.py, CamillaDSP actually reports which config is loaded,
    so this doesn't need to fall back to a guess or a permanent placeholder.
    Falls back to "" (no slot pre-highlighted) on query failure.
    """
    try:
        current = _ws_query("GetConfigFilePath") or ""
    except Exception:
        current = ""
    return current, list(config.CAMILLADSP_PRESETS)


def get_quick_presets():
    """Return the main-screen quick-button subset -- config.CAMILLADSP_QUICK_PRESETS,
    already resolved/validated/capped at 4 by config.py. See module
    docstring's Capabilities section for why this differs from get_presets()'
    full list here, unlike denon.py/minidsp.py."""
    return list(config.CAMILLADSP_QUICK_PRESETS)


def set_preset(path):
    """Switch to config file `path` and reload it -- a full pipeline
    rebuild, not just a settings change. Uses _PRESET_TIMEOUT; see its
    comment above -- measured near-instant on a trivial test config, still
    unverified on a real production one."""
    _ws_query_many([("SetConfigFilePath", path), ("Reload", None)], timeout=_PRESET_TIMEOUT)
