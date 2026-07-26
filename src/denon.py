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


# ---------------------------------------------------------------------------
# Input name mapping
# ---------------------------------------------------------------------------

# Populated by load_input_names(); maps normalized_raw -> friendly name.
_input_names = {}

_RENAME_BODY = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    '<tx><cmd id="1">GetRenameSource</cmd></tx>'
)


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
    resp = _session.post(url, data=_RENAME_BODY, timeout=_TIMEOUT)
    try:
        xml = resp.text
    finally:
        resp.close()

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
            result[_norm_source(name)] = rename.strip()
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
    """POST AppCommand status request; returns XML text, closes socket."""
    url = _BASE + "/goform/AppCommand.xml"
    # No Content-Type header -- the AVR rejects requests that include one.
    resp = _session.post(url, data=_STATUS_BODY, timeout=_TIMEOUT)
    try:
        return resp.text
    finally:
        resp.close()


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

    raw_power  = _tag(power_block,  "zone1") or "STANDBY"
    raw_source = _tag(_tag(source_block, "zone1") or "", "source") or ""
    raw_volume = _tag(_tag(volume_block, "zone1") or "", "volume")
    raw_mute   = _tag(mute_block,   "zone1") or "off"

    volume_db = float(raw_volume) if raw_volume is not None else -80.0
    muted     = raw_mute.strip().lower() == "on"
    power     = raw_power.strip().upper()
    if power not in ("ON", "OFF"):
        power = "STANDBY"

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
# Speaker preset and Dirac Live (port 11080 web UI API)
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
    """GET from the web UI port (11080). Returns text, closes socket."""
    url = _BASE_UI + path + (("?" + params) if params else "")
    resp = _session.get(url, timeout=_TIMEOUT)
    try:
        return resp.text
    finally:
        resp.close()


def _ui_set(path, type_id, element, value):
    """Send a set_config command via GET with URL-encoded XML data.

    The web UI sends: GET /ajax/.../set_config?type=N&data=<Element>value</Element>
    where the XML is percent-encoded.
    """
    xml  = "<{0}>{1}</{0}>".format(element, value)
    qs   = "type={}&data={}".format(type_id, _xml_encode(xml))
    url  = _BASE_UI + path + "?" + qs
    resp = _session.get(url, timeout=_TIMEOUT)
    resp.close()


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


def get_speaker_preset():
    """Return current speaker preset as '1' or '2'."""
    xml = _ui_get("/ajax/speakers/get_config", "type=11")
    return (_tag(xml, "SpeakerPreset") or "1").strip()


def set_speaker_preset(preset):
    """Set speaker preset. preset: '1' or '2'."""
    _ui_set("/ajax/speakers/set_config", "11", "SpeakerPreset", preset)


def get_dirac_filters():
    """Return (current_value, names) for Dirac Live filters.

    Confirmed value encoding on AVR-X4800H:
      "1"          = Off (no filter applied)
      "2"          = first filter  (Name index="0")
      "3"          = second filter (Name index="1")
      str(index+2) = any filter
    Off is always listed last in the menu.
    """
    xml = _ui_get("/ajax/audio/get_config", "type=14")
    current = (_tag(xml, "Value") or "1").strip()

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

    names.append(("1", "Off"))  # Off = value "1", always last
    return current, names


def set_dirac_filter(value):
    """Set Dirac Live filter. value: '0'=Off, '1'=first filter, '2'=second."""
    _ui_set("/ajax/audio/set_config", "14", "DiracLive", value)
