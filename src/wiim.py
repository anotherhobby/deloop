# wiim.py -- WiiM / LinkPlay streamer HTTP client
#
# Talks straight to a WiiM device's own `httpapi.asp` API over the LAN, same
# "direct to device" shape as denon.py (no intermediate daemon/service like
# minidsp.py or ha.py). Confirmed live against a WiiM Pro (firmware
# Linkplay.4.8.814756) at the IP in WIIM_HOST -- see tools/probe_wiim.py.
#
# ---------------------------------------------------------------------------
# HTTPS only, self-signed cert -- the first backend in this codebase to need
# TLS at all, and the first to need a raw-socket transport instead of the
# shared adafruit_requests.Session every other backend uses
# ---------------------------------------------------------------------------
# Confirmed live: plain HTTP (port 80) gives no response whatsoever; every
# command must go over HTTPS on port 443. The cert is self-signed
# (CN=www.linkplay.com, issued 2018-11-14, expires 2028-11-11) -- confirmed
# via `openssl s_client` that this is LinkPlay's fixed firmware cert, not
# generated per-device, so it's safe to embed once as LINKPLAY_CA_PEM below
# and pin it for every WiiM/LinkPlay unit rather than requiring each user to
# extract their own.
#
# HARD LESSON, confirmed live against a real M5 Dial (2026-07-29):
# CircuitPython's ssl.SSLContext.check_hostname does NOT do what its name/the
# official docs imply. Setting it False and connecting by IP still fails
# with OSError(-9984) (mbedtls's MBEDTLS_ERR_X509_CERT_VERIFY_FAILED) --
# confirmed by wrapping a raw socket manually and testing both ways: the
# exact same handshake, with server_hostname="10.0.1.75", fails every time;
# with server_hostname="www.linkplay.com" (matching the pinned cert's real
# CN), it succeeds. So check_hostname isn't actually suppressing the CN
# check -- the underlying mbedtls binding verifies the peer cert's CN
# against whatever server_hostname was passed to wrap_socket() regardless.
#
# Worse: adafruit_requests / adafruit_connection_manager (confirmed by
# reading adafruit_connection_manager.py's _get_connected_socket) always
# calls `ssl_context.wrap_socket(socket, server_hostname=host)` where `host`
# is the literal hostname from the request URL -- there is no parameter
# anywhere in Session.get()/get_socket() to use a different string for
# TLS/SNI purposes than the actual connection target. Since this backend
# necessarily connects by IP (WIIM_HOST) but the cert's CN will never match
# an IP, the shared Session can never work here no matter how ssl_context is
# configured.
#
# The fix: this backend bypasses adafruit_requests entirely and speaks raw
# HTTP/1.0 over a manually TLS-wrapped socket (see init_transport()/
# _request() below), forcing server_hostname=_SNI_HOSTNAME
# ("www.linkplay.com") independently of WIIM_HOST. This still performs real
# certificate verification (LINKPLAY_CA_PEM must still validate the peer
# cert's chain/signature) -- it's not a "give up on verification" workaround,
# just decoupling "which name the cert is checked against" from "which IP we
# actually connect to". Confirmed live end-to-end: full HTTP/1.0 response
# (status line, headers including Content-Length, JSON body) received
# correctly over this path against a real WiiM Pro.
#
# app.py calls wiim.init_transport(pool, ssl_context) directly (not through
# the generic driver.init(session) contract) from its DEVICE_DRIVER=="wiim"
# block, which is also where ssl_context itself is built -- see app.py's
# main(). init(session) below still exists to satisfy the driver contract's
# call signature but does nothing.
#
# ---------------------------------------------------------------------------
# Confirmed API shapes (live)
# ---------------------------------------------------------------------------
#   GET https://<host>/httpapi.asp?command=getPlayerStatus
#     -> {"vol": "0"-"100" (plain int, NOT a 0.0-1.0 fraction like ha.py),
#         "mute": "0"/"1", "status": "play"/"pause"/"stop", "mode": "<code>"}
#   GET .../httpapi.asp?command=setPlayerCmd:vol:<0-100>
#   GET .../httpapi.asp?command=setPlayerCmd:mute:<0|1>
#   GET .../httpapi.asp?command=setPlayerCmd:resume|pause|prev|next
#   GET .../httpapi.asp?command=setPlayerCmd:switchmode:<source-key>
#   GET .../httpapi.asp?command=getPresetInfo
#     -> {"preset_num": N, "preset_list": [{...}, ...]}
#   GET .../httpapi.asp?command=MCUKeyShortClick:<1-based-preset-number>
#     (note: no "setPlayerCmd:" prefix on this one -- confirmed against the
#     local/uc-intg-wiim reference integration's client.py)
# All of the above return the bare text "OK" (or JSON for the two GETs that
# return status), confirmed live; responses are discarded for the fire-and-
# forget commands, same pattern as minidsp.py's _post_master.
#
# Track metadata (getMetaInfo) comes back hex-encoded (e.g. "Title":
# "556E6B6E6F776E" = hex for "Unknown") -- confirmed live, not used by this
# driver (no title/artist display in scope), noted here so a future session
# touching metadata isn't caught out by it.
#
# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
#   - No power/standby concept found anywhere in the API (only a hard
#     `reboot` command) -- CAPS["power"] is False, like minidsp.py.
#   - Input list is NOT auto-detected -- there's no capability-bitmask
#     endpoint worth reverse-engineering. Empirically confirmed live on this
#     specific WiiM Pro by cycling every source key the reference
#     integration knows about via setPlayerCmd:switchmode and reading back
#     the resulting `mode`: "wifi" (mode 10), "bluetooth" (41), "line-in"
#     (40, "AUX-In"), "optical" (43) all really switch; "co-axial", "udisk",
#     "PCUSB", "HDMI", "phono" all left mode at 0/status "none" -- not
#     present on this unit. Other WiiM/LinkPlay models (Amp, Ultra, Pro
#     Plus) may expose a different physical set -- config.WIIM_INPUTS is a
#     configurable override for exactly that, same escape hatch
#     MINIDSP_PRESET_NAMES already uses for a similarly per-unit list.
#   - CAPS["presets"] is len(config.WIIM_PRESET_NAMES) > 0 -- NOT auto-discovered.
#     HARD LESSON, confirmed live: getPresetInfo's preset_list stays
#     genuinely empty ({"preset_num": 0, "preset_list": []}) even after
#     configuring real Favorites in the WiiM app and rebooting the Dial --
#     this isn't a timing/caching issue, the plain HTTP API just doesn't
#     reliably report this particular feature's contents. Confirmed via
#     WiiM's own forum (forum.wiimhome.com, "Recall Presets using the Wiim
#     API"): retrieving real preset names requires the UPnP/SOAP interface
#     (GetKeyMapping via a PlayQueueSCPD.xml service) -- a materially
#     heavier protocol than every other GET-and-parse-JSON call this whole
#     project uses, and not implemented here (explicit choice, see below).
#     MCUKeyShortClick:<1-12> (set_preset() below) is confirmed to still
#     work for *activating* a preset regardless of this -- it's only listing
#     them back that the plain API can't do.
#   - Given that, this backend falls back to the same "API can't report
#     names, so config does" pattern minidsp.py already established for its
#     own unrelated gap (config.MINIDSP_PRESET_NAMES): list the Favorites
#     you've actually configured (1-12) in WIIM_PRESET_NAMES, in order --
#     that list IS the preset set, its length is the count. This was a
#     deliberate choice over implementing UPnP/SOAP
#     just for this (see the user-facing tradeoff this was chosen over:
#     real names with zero settings.toml upkeep, at the cost of a new
#     protocol/complexity class on a memory-constrained device, untested
#     against this unit's actual UPnP service layout).
#   - CAPS["preset_quickbuttons"] is config-driven, via WIIM_PRESET_BUTTONS.
#     A WiiM unit can have far more presets than the main-screen row holds
#     (this one reports "preset_key": "12" in getStatusEx, i.e. 12 favorite
#     slots, confirmed live) -- which is an argument for showing a *subset*,
#     not for showing none. WIIM_PRESET_BUTTONS picks that subset by name;
#     the full list stays reachable through the scrollable Preset submenu.
#     See driver.py's _CAPS_DEFAULTS for the flag, config.py for parsing.
#     NOTE this setting also chooses the whole lower-screen layout: buttons
#     and the play/pause + skip row want the same pixels and cannot coexist,
#     so unset means media controls (the default) and naming any preset means
#     buttons instead. wiim_ui.py's _media_row_active() is the predicate.
#   - No separate "preset enabled" dimension -- MCUKeyShortClick just jumps
#     to a favorite, there's no on/off toggle independent of which one was
#     last activated. CAPS["preset_enable"]/["preset_select_enables"] stay
#     at their False defaults.
#   - CAPS["player_select"] is False -- WiiM only ever controls itself, no
#     device discovery/switching like ha.py.

import json

import config

CAPS = {"power": False, "input_select": True,
        "presets": len(config.WIIM_PRESET_NAMES) > 0,
        "preset_enable": False, "preset_select_enables": False,
        "player_select": False,
        "preset_quickbuttons": len(config.WIIM_PRESET_BUTTONS) > 0}
LABELS = {"input_select": "Source", "presets": "Preset"}

# LinkPlay's fixed self-signed firmware cert (CN=www.linkplay.com) -- the
# same cert across all LinkPlay/WiiM units, not per-device. See the TLS note
# above; app.py loads this via ssl_context.load_verify_locations(cadata=...)
# only when config.DEVICE_DRIVER == "wiim".
LINKPLAY_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIEATCCAumgAwIBAgIJAIis2nWA+bRbMA0GCSqGSIb3DQEBCwUAMIGWMQswCQYD
VQQGEwJDTjERMA8GA1UECAwIU2hhbmdoYWkxETAPBgNVBAcMCFNoYW5naGFpMREw
DwYDVQQKDAhsaW5rcGxheTERMA8GA1UECwwIbGlua3BsYXkxGTAXBgNVBAMMEHd3
dy5saW5rcGxheS5jb20xIDAeBgkqhkiG9w0BCQEWEW1haWxAbGlua3BsYXkuY29t
MB4XDTE4MTExNDEyMjQxOFoXDTI4MTExMTEyMjQxOFowgZYxCzAJBgNVBAYTAkNO
MREwDwYDVQQIDAhTaGFuZ2hhaTERMA8GA1UEBwwIU2hhbmdoYWkxETAPBgNVBAoM
CGxpbmtwbGF5MREwDwYDVQQLDAhsaW5rcGxheTEZMBcGA1UEAwwQd3d3Lmxpbmtw
bGF5LmNvbTEgMB4GCSqGSIb3DQEJARYRbWFpbEBsaW5rcGxheS5jb20wggEiMA0G
CSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCvHA4cinJAj3gkUcna4kdDpKwccNxW
gO44VHAKe8DjfOkvjTEx8lgS+jNp+OMk4KP80Koi+bX7CYlOOqcFdkh+Dr95CGou
hnjM0tR++8vkQWDHY+bLqgogJ7OBhxMPMA0rsUOUEPT79peY0fMhNHHVG2U2zJNY
DpWJ0dwl+l0AQoYaImeau1uR2k/5Qc5dNsMAUhrEFsbniIL+3dpNL4UNlSR6pPwj
ibK1uvap3mXs+35BcbUrccrdIC7RQ1YIP04kbM62MjrJ/dRrS+iivlef8/CtNGNr
lhY1iU2xhu/wsG5VCBzhO6SvZ0O+cfTOQwUbVjMmr66bQ8ADKl0x5KufAgMBAAGj
UDBOMB0GA1UdDgQWBBSIHFMV2yDQ7LubyjX+62oSUnQw7TAfBgNVHSMEGDAWgBSI
HFMV2yDQ7LubyjX+62oSUnQw7TAMBgNVHRMEBTADAQH/MA0GCSqGSIb3DQEBCwUA
A4IBAQBNKB648BV9NN0lr8PCIJBPZIETSds6/itsVVOuoW6hEGhzmHT533vMZ8hA
If4F3M8SHyXe8SKpdSHbnKoVMdjq/hyRJ9xcuzTJghQUfZeq4q6OQn9ehRekmXjU
XoEDbqyRfmqLaN3dwO5ODiFDbHl+sT1GQK70ILx6rI52cDWz6jeenQZ2KtToiATx
1DIMc8Rh6Dh+aIre6XYVbrOXbMqPeMrldDTAoW4El6Tcqq8Mwrif79bVNc1QDOoi
qLJgrk4gu5WEuFWv55MaV6/1pLzqkbYau6XKUV9zo8f8sG0NDih3wYeWgJvzQtRF
6k4Qk0aDsTJD1n7GOwMZwgpE8FHN
-----END CERTIFICATE-----
"""

# SNI hostname forced for every TLS handshake -- see the module docstring's
# TLS section. Every LinkPlay unit's self-signed cert has this exact CN
# regardless of the device's real IP; CircuitPython's mbedtls binding
# verifies the peer cert's CN against whatever string is passed here, not
# against WIIM_HOST, so this must stay hardcoded to match LINKPLAY_CA_PEM's
# subject rather than ever being set to config.WIIM_HOST.
_SNI_HOSTNAME = "www.linkplay.com"

_pool = None
_ssl_context = None
_TIMEOUT = config.WIIM_TIMEOUT_MS // 1000


def init(session):
    """Required by the driver contract's call signature, but unused -- see
    init_transport() and the module docstring's TLS section for why this
    backend needs a raw socketpool + ssl_context instead."""
    pass


def init_transport(pool, ssl_context):
    """Supply the raw socket pool + TLS context. Called directly by app.py
    (not through the generic driver contract) since this backend's
    transport needs differ from every other backend's -- see module
    docstring. Must be called before any other function."""
    global _pool, _ssl_context
    _pool = pool
    _ssl_context = ssl_context


# ---------------------------------------------------------------------------
# Internal helpers -- raw HTTP/1.0 over a manually TLS-wrapped socket. See
# module docstring for why: adafruit_requests has no way to use a different
# SNI/verification hostname than the literal connection target, and this
# backend's real host (WIIM_HOST, an IP) will never match the pinned cert's
# CN.
# ---------------------------------------------------------------------------

def _request(cmd):
    """GET /httpapi.asp?command=<cmd> and return the response body as str.
    Raises on network/TLS failure. Confirmed live end-to-end (status line,
    headers, Content-Length, JSON body) against a real WiiM Pro.

    Closes its socket on every path, including a failed connect/handshake --
    CircuitPython's socket pool is small (~4), so a leaked socket on every
    failed poll would exhaust it after a handful of retries (same concern
    that requires every adafruit_requests response elsewhere in this project
    to be .close()d -- see .claude/CLAUDE.md's "Socket management" note).
    """
    sock = _pool.socket(_pool.AF_INET, _pool.SOCK_STREAM)
    sock.settimeout(_TIMEOUT)
    try:
        sock.connect((config.WIIM_HOST, 443))
        wrapped = _ssl_context.wrap_socket(sock, server_hostname=_SNI_HOSTNAME)
    except Exception:
        sock.close()
        raise
    try:
        request = "GET /httpapi.asp?command={} HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n".format(
            cmd, config.WIIM_HOST)
        wrapped.send(request.encode("utf-8"))
        data = b""
        buf = bytearray(512)
        while True:
            n = wrapped.recv_into(buf, 512)
            if n == 0:
                break
            data += bytes(buf[:n])
    finally:
        wrapped.close()   # closes the underlying socket too -- do not also close sock
    _, _, body = data.partition(b"\r\n\r\n")
    return body.decode("utf-8")


def _get_json(cmd):
    return json.loads(_request(cmd))


def _send(cmd):
    """Fire-and-forget GET -- response already fully read (and discarded)
    by _request() itself. Optimistic UI already covers the round trip, same
    as minidsp.py's _post_master."""
    _request(cmd)


def _get_player_status():
    return _get_json("getPlayerStatus")


# ---------------------------------------------------------------------------
# Public API -- volume / mute / status
# ---------------------------------------------------------------------------

_PLAY_STATE = {"play": "playing", "pause": "paused"}


def get_status():
    """Poll the device for current state.

    Returns a dict shaped like denon.get_status() -- power is always "ON"
    since LinkPlay streamers have no power/standby concept (see module
    docstring). "input" is the raw mode code; friendly_input() below turns
    it into a display string. Raises on network failure.
    """
    status = _get_player_status()
    return {
        "volume_db":   float(status.get("vol", 0)),
        "muted":       status.get("mute") == "1",
        "power":       "ON",
        "input":       status.get("mode", ""),
        "media_state": _PLAY_STATE.get(status.get("status", ""), ""),
    }


def set_volume(pct):
    pct = int(max(config.VOLUME_MIN, min(config.VOLUME_MAX, float(pct))))
    _send("setPlayerCmd:vol:{}".format(pct))


def mute_on():
    _send("setPlayerCmd:mute:1")


def mute_off():
    _send("setPlayerCmd:mute:0")


# ---------------------------------------------------------------------------
# Media playback (play/pause/skip) -- see get_status()'s "media_state" key.
# Confirmed live: resume/pause/prev/next all return "OK".
# ---------------------------------------------------------------------------

def media_play():
    _send("setPlayerCmd:resume")


def media_pause():
    _send("setPlayerCmd:pause")


def media_previous():
    _send("setPlayerCmd:prev")


def media_next():
    _send("setPlayerCmd:next")


# ---------------------------------------------------------------------------
# Input (source) selection -- see module docstring for how this list was
# empirically confirmed rather than assumed from the reference integration.
# ---------------------------------------------------------------------------

_INPUT_FRIENDLY = {
    "wifi": "WiFi", "bluetooth": "Bluetooth", "line-in": "Line In",
    "optical": "Optical", "co-axial": "Coax", "udisk": "USB",
    "hdmi": "HDMI", "phono": "Phono", "pcusb": "PC USB",
}
_input_list = [(key, _INPUT_FRIENDLY.get(key, key)) for key in config.WIIM_INPUTS]

# Reverse map from a live getPlayerStatus "mode" code to one of the switch-
# mode keys above, for highlighting the current selection in the Source
# menu -- confirmed live (see module docstring). Any mode not listed here
# (streaming-service codes like Spotify/TIDAL Connect, AirPlay, DLNA, etc.)
# falls back to "wifi" since they're all network-origin playback.
_MODE_TO_INPUT_KEY = {"10": "wifi", "40": "line-in", "41": "bluetooth", "43": "optical"}

# Broad mode-code -> descriptive name table for get_status()'s "input" field
# (friendly_input() below) -- distinct from the Source menu above, since
# this shows *what's actually playing* (e.g. "TIDAL"), not just which
# physical input is selected. Ported from local/uc-intg-wiim's
# PLAYBACK_MODE_MAP; "10"/"40"/"41"/"43" confirmed live, the rest are from
# the reference and not individually re-verified.
#
# These are DISPLAY strings only -- nothing on the wire uses them -- and they
# must fit dial_ui.py's input label, which sits at y=62 on a round 240px
# screen in Inter_Medium_20. That row clears the gauge arc (inner radius 90)
# for only ~122px, about 12 characters of typical mixed-case text; past that
# the name draws under the arc band and then gets clipped by the display
# circle. So three of the reference's names are deliberately shortened here:
# "TIDAL Connect" (144px), "Spotify Connect" (156px) and "External Storage"
# (158px) all overflowed. Measure with tools/font_fit.py before adding or
# renaming an entry -- it is a pixel budget, not a character count (Inter's
# advances run 5px for "i" to 20px for "W").
_MODE_NAME = {
    "-1": "Idle", "0": "Idle", "1": "AirPlay", "2": "DLNA", "10": "WiiM",
    "11": "USB Disk", "16": "TF Card", "31": "Spotify",
    "32": "TIDAL", "40": "AUX-In", "41": "Bluetooth",
    "42": "Storage", "43": "Optical-In", "50": "Mirror",
    "60": "Voice Mail", "99": "Slave",
}


def get_inputs():
    """Return (current_key, [(key, friendly_name), ...]) for the Source menu."""
    try:
        mode = _get_player_status().get("mode", "")
    except Exception:
        mode = ""
    current = _MODE_TO_INPUT_KEY.get(mode, "wifi")
    return current, list(_input_list)


def set_input(key):
    _send("setPlayerCmd:switchmode:{}".format(key))


def friendly_input(raw_mode):
    """Return the user-visible "what's active" string for a raw mode code."""
    if not raw_mode:
        return ""
    return _MODE_NAME.get(raw_mode, "Unknown")


# ---------------------------------------------------------------------------
# Presets -- WiiM-app Favorites, activated via MCUKeyShortClick. See module
# docstring's CAPS["presets"] note for why this is config-driven
# (WIIM_PRESET_NAMES/_BUTTONS) rather than auto-discovered -- getPresetInfo's
# preset_list does not reliably reflect this feature's contents, confirmed
# live even after configuring real Favorites and rebooting. No on/off
# dimension either (CAPS["preset_enable"] stays False) -- MCUKeyShortClick
# just jumps to a favorite, there's nothing to toggle independent of that.
# ---------------------------------------------------------------------------

def get_presets():
    """Return (current_value, [(value, name), ...]) for every WIIM_PRESET_NAMES entry.

    `value` is the 1-based preset number MCUKeyShortClick expects, which is
    just the name's position in the list. There's no API field reporting
    which preset (if any) is currently active, so current_value is always
    "" -- the Preset submenu opens with no slot pre-highlighted.

    The list length is the preset count, full stop -- see config.py for why
    there is no separate count setting. WIIM_PRESET_BUTTONS answers the
    different question of how many get main-screen buttons; see
    get_quick_presets().
    """
    names = [(str(i), name)
             for i, name in enumerate(config.WIIM_PRESET_NAMES, 1)]
    return "", names


def get_quick_presets():
    """Return the subset of get_presets() that gets main-screen buttons.

    config.WIIM_PRESET_BUTTONS is configured by name but already resolved to
    1-based positions there, along with all the validation (unknown or
    repeated names dropped with a boot print, capped at what fits) -- so this
    is a pure lookup. Order is the configured one, since "Night,Movie" is a
    request to draw Night first.
    """
    presets = config.WIIM_PRESET_NAMES
    return [(str(pos), presets[pos - 1]) for pos in config.WIIM_PRESET_BUTTONS]


def set_preset(value):
    """Activate WiiM-app Favorite `value` (1-based). Note: no "setPlayerCmd:"
    prefix on this command -- confirmed against the reference integration
    and live against a real unit."""
    _send("MCUKeyShortClick:{}".format(value))
