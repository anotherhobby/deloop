# denon.py -- Denon AVR-X4800H HTTP client
#
# Status polling uses POST /goform/AppCommand.xml on port 8080 (plain HTTP).
# The AVR requires the XML body to have a newline after the declaration and
# no Content-Type header -- this is how the denonavr library sends it.
# Max 5 <cmd> elements per <tx> block; multiple <tx> blocks are supported.
#
# Control commands use GET /goform/formiPhoneAppDirect.xml?CMD on port 8080.
#
# Confirmed working endpoints (port 8080, HTTP):
#   Status poll:  POST /goform/AppCommand.xml  (see _STATUS_BODY)
#   Volume up:    GET  /goform/formiPhoneAppDirect.xml?MVUP
#   Volume down:  GET  /goform/formiPhoneAppDirect.xml?MVDOWN
#   Set volume:   GET  /goform/formiPhoneAppVolume.xml?1+{db:.1f}
#   Mute on:      GET  /goform/formiPhoneAppDirect.xml?MUON
#   Mute off:     GET  /goform/formiPhoneAppDirect.xml?MUOFF
#   Power on:     GET  /goform/formiPhoneAppDirect.xml?PWON
#   Power off:    GET  /goform/formiPhoneAppDirect.xml?PWSTANDBY

import config

# Capability flags + menu labels -- see driver.py for the full contract.
# preset_select_enables=True: the AVR's DiracLive value IS the filter
# selection -- there's no way to pick a filter without engaging it, so
# set_preset() always implies enabled.
CAPS = {"power": True, "input_select": True, "presets": True,
        "preset_enable": True, "preset_select_enables": True}
LABELS = {"input_select": "Input", "presets": "Dirac Live"}

_session = None
_BASE    = "{}://{}:{}".format(config.AVR_SCHEME, config.AVR_HOST, config.AVR_PORT)
_BASE_UI = "http://{}:{}".format(config.AVR_HOST, config.AVR_PORT_UI)
_TIMEOUT = config.AVR_TIMEOUT_MS // 1000

# AppCommand.xml body -- newline after XML declaration is required by the AVR.
# No Content-Type header should be set on this POST request.
_STATUS_BODY = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    "<tx>"
    '<cmd id="1">GetAllZonePowerStatus</cmd>'
    '<cmd id="1">GetAllZoneSource</cmd>'
    '<cmd id="1">GetAllZoneVolume</cmd>'
    '<cmd id="1">GetAllZoneMuteStatus</cmd>'
    "</tx>"
)


def init(session):
    """Supply the HTTP session. Must be called before any other function."""
    global _session
    _session = session


def _reset_connections():
    """Force-close every socket this session has pooled, rather than the
    normal resp.close() (which just *frees a socket for reuse*, never
    actually closes it -- confirmed by reading adafruit_requests'
    Response.close() source: it calls connection_manager.free_socket(),
    not close_socket()). Needed because a body-read failure (confirmed,
    same investigation as ota.py's _Fetcher, to have zero retry
    protection anywhere in adafruit_requests itself -- only the earlier
    connect+send phase gets that) otherwise leaves a broken socket
    sitting in the pool, ready to be silently handed back to the very
    next request against the same host/port."""
    try:
        _session._connection_manager._free_sockets(force=True)
    except Exception as e:
        print("denon: socket reset failed:", type(e), e)


def _request_text(method, url, **kwargs):
    """GET/POST via _session, returning the decoded response body text.

    Retries once on a body-read failure, resetting the whole connection
    pool first (see _reset_connections()) so the retry can't be handed
    back the same broken socket. Connect/send failures are already
    retried internally by adafruit_requests itself (its own
    "Repeated socket failures" mechanism, confirmed by reading its
    source); this covers the gap after that -- reading the body once a
    request has already been sent."""
    attempt = 0
    while True:
        attempt += 1
        resp = getattr(_session, method)(url, **kwargs)
        try:
            text = resp.text
            resp.close()
            return text
        except OSError as e:
            print("denon: body read failed ({}, attempt {}):".format(url, attempt), type(e), e)
            _reset_connections()   # already closes resp's socket along with every other pooled one
            if attempt >= 2:
                raise


# ---------------------------------------------------------------------------
# Input name mapping
# ---------------------------------------------------------------------------

# Populated by load_input_names(); maps normalized_raw -> friendly name.
_input_names = {}

_RENAME_BODY = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    '<tx><cmd id="1">GetRenameSource</cmd></tx>'
)

# GetRenameSource keys some inputs by a descriptive name that differs from
# the short protocol code GetAllZoneSource actually reports in status polls
# (e.g. rename table says "Media Player", status poll says "MPLAY"). Fixed
# Denon/Marantz protocol quirk, not user-configurable -- same table
# python-denonavr (the Home Assistant integration) hardcodes as SOURCE_MAPPING.
# Only ever applied to the rename table's own name field (see load_input_names)
# -- never to a raw status value, so it can't misfire on a real raw code that
# happens to share text with one of these keys.
_SOURCE_ALIAS = {
    "TV AUDIO":      "TV",
    "IPOD/USB":      "USB/IPOD",
    "BLUETOOTH":     "BT",
    "BLU-RAY":       "BD",
    "NETWORK":       "NET",
    "MEDIA PLAYER":  "MPLAY",
    "AUX":           "AUX1",
    "FM":            "TUNER",
    "SPOTIFYCONNECT": "SPOTIFY CONNECT",
}


def _norm_source(s):
    """Normalize a source name for lookup: uppercase, sort slash-parts.

    CBL/SAT and SAT/CBL both normalize to CBL/SAT so the lookup is
    independent of which ordering the AVR happens to use.
    """
    return "/".join(sorted(s.strip().upper().split("/")))


def load_input_names():
    """Fetch and cache the AVR's source rename map. Call once at boot.

    Builds _input_names: {normalized_raw -> friendly_name}.
    Silently does nothing on network failure (raw names will be shown).
    """
    global _input_names
    url = _BASE + "/goform/AppCommand.xml"
    xml = _request_text("post", url, data=_RENAME_BODY, timeout=_TIMEOUT)

    result = {}
    pos = 0
    while True:
        start = xml.find("<list>", pos)
        if start == -1:
            break
        end = xml.find("</list>", start)
        if end == -1:
            break
        block = xml[start:end]
        name   = _tag(block, "name")
        rename = _tag(block, "rename")
        if name and rename:
            raw = _SOURCE_ALIAS.get(name.strip().upper(), name)
            result[_norm_source(raw)] = rename.strip()
        pos = end + 7
    _input_names = result


def friendly_input(raw_name):
    """Return the user-visible name for a raw AVR source string."""
    if not raw_name:
        return ""
    return _input_names.get(_norm_source(raw_name), raw_name)





# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _post_status():
    """POST AppCommand status request; returns XML text."""
    url = _BASE + "/goform/AppCommand.xml"
    # No Content-Type header -- the AVR rejects requests that include one.
    return _request_text("post", url, data=_STATUS_BODY, timeout=_TIMEOUT)


def _send(path):
    """HTTP GET for control commands where the response body is not needed."""
    url = _BASE + path
    resp = _session.get(url, timeout=_TIMEOUT)
    resp.close()


def _tag(xml, tag):
    """Extract the text content of the first occurrence of <tag>...</tag>."""
    open_t  = "<{}>".format(tag)
    close_t = "</{}>".format(tag)
    start = xml.find(open_t)
    if start == -1:
        return None
    start += len(open_t)
    end = xml.find(close_t, start)
    if end == -1:
        return None
    return xml[start:end].strip()


def _nth_cmd_block(xml, n):
    """Return the text of the nth <cmd>...</cmd> block (0-indexed)."""
    pos = 0
    for _ in range(n + 1):
        start = xml.find("<cmd>", pos)
        if start == -1:
            return ""
        end = xml.find("</cmd>", start)
        if end == -1:
            return ""
        pos = end + 6
    return xml[start + 5 : end]  # content between <cmd> and </cmd>


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_status():
    """Poll the AVR for current state (Main Zone only).

    Returns a dict:
        {
            "volume_db": float,   # e.g. -40.0
            "muted":     bool,
            "power":     str,     # "ON" or "STANDBY"
            "input":     str,     # raw source name e.g. "SAT/CBL"
        }

    Raises OSError on network failure.
    """
    xml = _post_status()

    # Response <cmd> blocks are in the same order as the request commands:
    #   0: GetAllZonePowerStatus  -> <zone1>ON</zone1>
    #   1: GetAllZoneSource       -> <zone1><source>SAT/CBL</source></zone1>
    #   2: GetAllZoneVolume       -> <zone1><volume>-40.0</volume>...</zone1>
    #   3: GetAllZoneMuteStatus   -> <zone1>off</zone1>
    power_block  = _nth_cmd_block(xml, 0)
    source_block = _nth_cmd_block(xml, 1)
    volume_block = _nth_cmd_block(xml, 2)
    mute_block   = _nth_cmd_block(xml, 3)

    raw_power  = _tag(power_block,  "zone1")
    raw_source = _tag(_tag(source_block, "zone1") or "", "source") or ""
    raw_volume = _tag(_tag(volume_block, "zone1") or "", "volume")
    raw_mute   = _tag(mute_block,   "zone1") or "off"

    volume_db = float(raw_volume) if raw_volume is not None else -80.0
    muted     = raw_mute.strip().lower() == "on"
    # Found + fixed 2026-08-01: this used to be `raw_power or "STANDBY"`,
    # then "STANDBY" again if the result wasn't "ON"/"OFF" -- silently
    # converting ANY unparseable power reading (missing <zone1> tag, a
    # truncated/malformed response) into a real, definite-looking
    # "STANDBY" rather than treating it as the parse failure it actually
    # is. Boot is this codebase's most heap-fragmented, most
    # truncation-prone moment (see docs/architecture.md's boot-memory
    # guardrails) -- exactly where a short/partial read is most likely,
    # and exactly what was producing a real, reproducible "power off
    # screen flashes at boot, then corrects itself" bug once app.py
    # rendered from this fabricated value before a clean poll landed.
    # Raising here instead routes a genuine parse failure through the
    # same retry/error-count path _poll_now() already has for network
    # errors, rather than smoothing it into a wrong-but-plausible answer.
    power = (raw_power or "").strip().upper()
    if power not in ("ON", "OFF"):
        raise RuntimeError(
            "denon: unexpected power value {!r} (missing/truncated response?)".format(raw_power))

    return {
        "volume_db": volume_db,
        "muted":     muted,
        "power":     power,
        "input":     raw_source.strip(),
    }


def volume_up():
    _send("/goform/formiPhoneAppDirect.xml?MVUP")


def volume_down():
    _send("/goform/formiPhoneAppDirect.xml?MVDOWN")


def set_volume(db):
    """Set AVR master volume to an absolute dB value."""
    db = max(config.VOLUME_MIN, min(config.VOLUME_MAX, float(db)))
    _send("/goform/formiPhoneAppVolume.xml?1+{:.1f}".format(db))


def mute_on():
    _send("/goform/formiPhoneAppDirect.xml?MUON")


def mute_off():
    _send("/goform/formiPhoneAppDirect.xml?MUOFF")


def power_on():
    _send("/goform/formiPhoneAppDirect.xml?PWON")


def power_standby():
    _send("/goform/formiPhoneAppDirect.xml?PWSTANDBY")


# ---------------------------------------------------------------------------
# Shared web UI helpers (port 11080) -- used by the source list and Dirac Live
# ---------------------------------------------------------------------------

def _attr(tag, name):
    """Extract an XML attribute value from a tag string like <Name display="3">."""
    pattern = '{}="'.format(name)
    start = tag.find(pattern)
    if start == -1:
        return None
    start += len(pattern)
    end = tag.find('"', start)
    return tag[start:end] if end != -1 else None


def _xml_encode(s):
    """Percent-encode the XML special characters needed in a query string."""
    return (s.replace("&", "%26").replace(" ", "%20").replace('"', "%22")
             .replace("<", "%3C").replace(">", "%3E").replace("/", "%2F"))


def _ui_get(path, params=""):
    """GET from the web UI port (11080). Returns text."""
    url = _BASE_UI + path + (("?" + params) if params else "")
    return _request_text("get", url, timeout=_TIMEOUT)


# ---------------------------------------------------------------------------
# Source (input) list -- port 11080 web UI
# ---------------------------------------------------------------------------

_source_list = []           # list of (index_str, friendly_name) for Zone 1
_current_source_index = ""  # Zone 1 current source index as string


def load_source_list():
    """Fetch Zone 1 source list and current source index from port 11080."""
    global _source_list, _current_source_index
    xml = _ui_get("/ajax/globals/get_config", "type=7")

    # Find Zone 1 tag and read its current index attribute
    z1_start = xml.find('<Zone zone="1"')
    if z1_start == -1:
        return
    z1_tag_end = xml.find(">", z1_start)
    idx = _attr(xml[z1_start:z1_tag_end + 1], "index")
    if idx:
        _current_source_index = idx.strip()

    # Parse sources within Zone 1
    z1_end = xml.find("</Zone>", z1_start)
    if z1_end == -1:
        z1_end = len(xml)
    z1_block = xml[z1_start:z1_end]

    result = []
    pos = 0
    while True:
        src_start = z1_block.find("<Source ", pos)
        if src_start == -1:
            break
        tag_end = z1_block.find(">", src_start)
        src_end = z1_block.find("</Source>", src_start)
        if tag_end == -1 or src_end == -1:
            break
        src_index = _attr(z1_block[src_start:tag_end + 1], "index")
        name = _tag(z1_block[src_start:src_end + 9], "Name")
        if src_index and name:
            result.append((src_index.strip(), name.strip()))
        pos = src_end + 9
    _source_list = result


def get_inputs():
    """Return (current_index, [(index, friendly_name), ...]) for Zone 1."""
    return _current_source_index, list(_source_list)


def set_input(index):
    """Switch Zone 1 to the source with the given index string."""
    xml = '<Source zone="1" index="{}"></Source>'.format(index)
    qs  = "type=7&data={}".format(_xml_encode(xml))
    url = _BASE_UI + "/ajax/globals/set_config?" + qs
    resp = _session.get(url, timeout=_TIMEOUT)
    resp.close()


# Two different endpoints are involved here, confirmed against a real
# AVR-X4800H:
#
#   Read (GET /ajax/audio/set_config?type=14, web UI port): reliable for
#   polling. "Value" uses index+2 (index=0 -> "2", index=1 -> "3", ...);
#   "1" is reported when Off, but there's only one dimension on the wire --
#   Off is just another value in the same enumeration as the real filters,
#   not an independent bit, and the AVR forgets which filter was active
#   once you leave this state.
#
#   Write: that same ajax endpoint (POST-via-GET /ajax/audio/set_config)
#   silently ignores "0" and "1" -- there is no way to reach Off through
#   it. Turning Dirac off (or back on) actually requires the legacy
#   control API on port 8080 (the same command family already used for
#   volume/mute/power elsewhere in this file): GET
#   /goform/formiPhoneAppDirect.xml?PSDIRAC%20{arg}, where {arg} is "OFF"
#   or a *1-indexed* slot number ("1","2","3",...) -- one less than the
#   ajax Value for the same filter. This matches how the denonavr library
#   (python-denonavr, used by Home Assistant) controls Dirac Live.
#   Confirmed working, but the AVR takes several seconds (~5s observed) to
#   actually apply a Dirac change -- code.py updates state optimistically
#   rather than re-querying right after a set, same as volume/mute.
#
# get_presets()/set_preset() hide the Off value entirely and deal only in
# real filters (ajax-value encoded); get_preset_enabled()/
# set_preset_enabled() reconstruct an independent enabled/disabled
# dimension on top, remembering the last real filter locally (in
# _last_active) since the AVR itself forgets which one was selected once
# you set it to Off.
_OFF_VALUE = "1"
_last_active = None   # last real (non-Off) filter value seen or set, ajax-encoded


def _set_dirac_control(control_arg):
    """GET /goform/formiPhoneAppDirect.xml?PSDIRAC%20{control_arg} (port 8080)."""
    _send("/goform/formiPhoneAppDirect.xml?PSDIRAC%20{}".format(control_arg))


def _ajax_value_to_control_slot(value):
    """Ajax Value ('2','3',...) -> control API's 1-indexed slot ('1','2',...)."""
    return str(int(value) - 1)


def _get_dirac_value():
    """Raw current DiracLive value straight off the AVR ("1" if Off)."""
    xml = _ui_get("/ajax/audio/get_config", "type=14")
    return (_tag(xml, "Value") or _OFF_VALUE).strip()


def get_presets():
    """Return (current_value, [(value, name), ...]) -- real Dirac filters
    only, no synthetic "Off" entry. This is the Denon driver's "presets"
    capability -- see driver.py for the generic contract other drivers
    implement differently.
    """
    global _last_active
    xml = _ui_get("/ajax/audio/get_config", "type=14")
    current = (_tag(xml, "Value") or _OFF_VALUE).strip()

    names = []
    pos = 0
    while True:
        start = xml.find("<Name ", pos)
        if start == -1:
            break
        close = xml.find(">", start)
        end   = xml.find("</Name>", close)
        if close == -1 or end == -1:
            break
        tag_str  = xml[start:close + 1]
        display  = _attr(tag_str, "display")
        index    = _attr(tag_str, "index")
        name_str = xml[close + 1:end].strip()
        if display == "3" and name_str and index is not None:
            value = str(int(index) + 2)  # index=0->value=2, index=1->value=3, etc.
            names.append((value, name_str))
        pos = end + 7

    if current != _OFF_VALUE:
        _last_active = current
    elif _last_active is None and names:
        # Booting while Off: the AVR has no memory of which filter was last
        # active, so this is a best guess (first filter) rather than a fact.
        _last_active = names[0][0]

    return (_last_active if _last_active is not None else current), names


def set_preset(value):
    """Select and enable a Dirac filter. value: '2'=first filter, '3'=second,
    etc. (the ajax-Value encoding get_presets() returns)."""
    global _last_active
    _last_active = value
    _set_dirac_control(_ajax_value_to_control_slot(value))


def get_preset_enabled():
    """True unless Dirac Live is currently set to Off."""
    return _get_dirac_value() != _OFF_VALUE


def set_preset_enabled(enabled):
    """Toggle Dirac Live on/off without forgetting which filter is selected."""
    if enabled:
        if _last_active is None:
            return   # nothing known to re-enable yet
        _set_dirac_control(_ajax_value_to_control_slot(_last_active))
    else:
        _set_dirac_control("OFF")
