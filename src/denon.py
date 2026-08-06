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

# Raw SocketPool for the non-blocking status-poll engine only (see
# _AsyncRequest below) -- separate from _session, which still backs every
# control command and boot-time loader.
_pool = None
_CONNECT_TIMEOUT_S  = config.AVR_CONNECT_TIMEOUT_MS / 1000
_POLL_OP_DEADLINE_S = 2.0
_RECV_CHUNK    = 512
_RESP_BUF_SIZE = 2048
_EAGAIN   = 11
_ENOTCONN = 128

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


def init_transport(pool):
    """Supply the raw SocketPool for the non-blocking status-poll engine
    (start_status_poll()/pump_status_poll()). Independent of init(session)
    -- call both; control commands and boot-time loaders keep using the
    adafruit_requests session, only the background status poll uses this
    raw-socket path."""
    global _pool
    _pool = pool


def _reset_connections():
    """Force-close every socket this session has pooled, rather than the
    normal resp.close() (which just *frees a socket for reuse*, never
    actually closes it -- confirmed by reading adafruit_requests'
    Response.close() source: it calls connection_manager.free_socket(),
    not close_socket()). Needed because a body-read failure, or a
    connect/send failure (found 2026-08-03: adafruit_requests' own
    internal connect/send retry does not reliably prevent this -- see
    _request_text()'s docstring), otherwise leaves a broken socket
    sitting in the pool, ready to be silently handed back to the very
    next request against the same host/port."""
    try:
        _session._connection_manager._free_sockets(force=True)
    except Exception as e:
        print("denon: socket reset failed:", type(e), e)


def _request_text(method, url, **kwargs):
    """GET/POST via _session, returning the decoded response body text.

    Retries once on failure, resetting the whole connection pool first
    (see _reset_connections()) so the retry can't be handed back the same
    broken socket. Originally this only wrapped the body-read (resp.text),
    on the theory that adafruit_requests' own internal "Repeated socket
    failures" retry already covered connect/send. Found + fixed
    2026-08-03: it doesn't cover it reliably enough in practice -- live
    testing showed _poll_avr's connect/send call (the line that used to
    sit outside this try) timing out (ETIMEDOUT) on nearly every poll,
    every ~1s, indefinitely. That call raising means adafruit_requests
    gave up internally without leaving the pool clean, so the exact same
    broken socket got handed back to the very next poll -- a self-
    perpetuating timeout loop, never recovering on its own. Now the
    connect/send call is inside the same try/reset/retry as the body
    read, so either failure mode gets the pool force-reset before a
    retry."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = getattr(_session, method)(url, **kwargs)
            text = resp.text
            resp.close()
            return text
        except OSError as e:
            print("denon: request failed ({}, attempt {}):".format(url, attempt), type(e), e)
            _reset_connections()   # force-closes resp's socket (if any) along with every other pooled one
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
# Non-blocking status poll -- see docs/architecture.md's touch-drop
# investigation (2026-08-03). app.py's main loop has no sleep and polls
# touch every tick; a blocking status-poll request (the old _post_status()
# via adafruit_requests) could freeze that loop for 1-3s on a slow/failed
# request, and the FocalTouch controller has no queue, so any tap whose
# entire gesture happened during that freeze was silently, permanently
# lost. _AsyncRequest drives one HTTP/1.0 request/response over a raw,
# non-blocking socket across many pump(now) calls instead, so the status
# poll (the one call site that was actually measured causing these
# freezes) never blocks the loop for more than a bounded slice.
#
# Control commands (_send(), set_input(), the boot-time loaders below)
# deliberately still use the blocking _session/_request_text() path --
# out of scope for this pass, see the plan this was built from.
# ---------------------------------------------------------------------------

_PHASE_SENDING   = 0
_PHASE_RECEIVING = 1
_PHASE_DONE      = 2
_PHASE_ERROR     = 3


def _build_request(method, path, body, port):
    """Build a raw HTTP/1.0 request (status line + Host/Connection/
    Content-Length headers + body) -- denon.py talks to a raw socket for
    this path, not adafruit_requests, so it has to frame the request
    itself. Same "Connection: close, read until the peer closes" shape as
    wiim.py's _request(), which is confirmed working end-to-end on real
    hardware.

    port: the status poll and most control commands hit config.AVR_PORT
    (8080); set_input and the Dirac/preset web-UI endpoints hit
    config.AVR_PORT_UI (11080) -- see async_set_input()."""
    body_bytes = body.encode("utf-8") if body else b""
    header = (
        "{} {} HTTP/1.0\r\n"
        "Host: {}:{}\r\n"
        "Connection: close\r\n"
    ).format(method, path, config.AVR_HOST, port)
    if body_bytes:
        header += "Content-Length: {}\r\n".format(len(body_bytes))
    header += "\r\n"
    return header.encode("utf-8") + body_bytes


def _is_eagain(e):
    """True if e is a would-block OSError (EAGAIN/EWOULDBLOCK) rather than
    a genuine failure. errno 11 confirmed against real hardware -- if this
    stops matching, every would-block gets misclassified as a hard
    failure, so this is the first thing to check if the poll starts
    failing on every attempt instead of quietly retrying next tick."""
    return len(e.args) > 0 and e.args[0] == _EAGAIN


def _is_enotconn(e):
    """True if e is ENOTCONN. errno 128 confirmed against real hardware.

    Found 2026-08-03, live: this build's recv_into(), in fully non-
    blocking mode (timeout_ms==0), does NOT return 0 for a graceful peer
    close the way a normal socket would -- reading CircuitPython's
    socketpool_socket_recv_into() (Socket.c) shows that when lwip_recv()
    returns 0 with timeout_ms==0, it's deliberately remapped to a raised
    OSError(ENOTCONN) instead. Every response here ends with the peer
    closing (we send "Connection: close"), so this is the normal,
    expected end-of-response signal on this build, not a failure -- see
    _AsyncRequest.pump()."""
    return len(e.args) > 0 and e.args[0] == _ENOTCONN


class _AsyncRequest:
    """Drives one HTTP/1.0 request/response across many pump(now) calls.

    Connect uses a short, bounded *blocking* timeout (_CONNECT_TIMEOUT_S),
    not a true non-blocking connect -- confirmed by reading this build's
    socketpool Socket.c that a literal settimeout(0) before connect()
    doesn't give a resumable "check back later" result here (the internal
    poll-loop guard never runs at timeout_ms==0, so it raises ETIMEDOUT
    immediately instead, even though the underlying connect attempt is
    still in progress at the OS level). So connect() gets one short
    bounded slice instead of being pumped -- on the LAN, with AVR_HOST a
    literal IP, a healthy connect returns almost immediately; the full
    budget is only spent when the AVR is genuinely unreachable, which is
    already the case the poll's error-count/MODE_ERROR machinery exists
    to handle.

    send()/recv_into() are genuinely single-attempt/non-blocking once the
    socket's timeout is 0 (also confirmed from the same source: send()
    never internally retries, and recv_into() raises EAGAIN immediately
    on no data) -- so pump() does at most one send() or one recv_into()
    per call, picking back up next tick on EAGAIN.

    One more platform quirk found live 2026-08-03: in this fully non-
    blocking mode, recv_into() reports a graceful peer close as a raised
    ENOTCONN instead of returning 0 -- see _is_enotconn()'s docstring.
    Since the request always sends "Connection: close", that's the
    expected way every response ends, not a failure -- pump() treats it
    as success (matching what n==0 would have meant), but only while
    receiving; the same errno during the send phase is a real failure.
    """

    def __init__(self, method, path, body, now, port=None):
        port = port or config.AVR_PORT
        self.phase     = _PHASE_SENDING
        self.error     = None
        self.result    = None
        self._deadline = now + _POLL_OP_DEADLINE_S
        self._sock     = None
        self._request  = _build_request(method, path, body, port)
        self._sent     = 0
        self._buf      = bytearray(_RESP_BUF_SIZE)
        self._recv_len = 0
        try:
            self._sock = _pool.socket(_pool.AF_INET, _pool.SOCK_STREAM)
            self._sock.settimeout(_CONNECT_TIMEOUT_S)
            self._sock.connect((config.AVR_HOST, port))
            self._sock.settimeout(0)
        except Exception as e:
            self._fail(e)

    def pump(self, now):
        """Advance one step. Call every main-loop tick until self.phase is
        DONE or ERROR."""
        if self.phase in (_PHASE_DONE, _PHASE_ERROR):
            return
        if now >= self._deadline:
            self._fail(OSError("denon: async request timed out"))
            return
        try:
            if self.phase == _PHASE_SENDING:
                self._pump_send()
            elif self.phase == _PHASE_RECEIVING:
                self._pump_recv()
        except OSError as e:
            if _is_eagain(e):
                return   # would block -- try again next tick
            if self.phase == _PHASE_RECEIVING and _is_enotconn(e):
                self._finish()   # graceful peer close, reported oddly -- see class docstring
                return
            self._fail(e)

    def _pump_send(self):
        n = self._sock.send(self._request[self._sent:])
        self._sent += n
        if self._sent >= len(self._request):
            self.phase = _PHASE_RECEIVING

    def _pump_recv(self):
        remaining = len(self._buf) - self._recv_len
        if remaining <= 0:
            self._fail(RuntimeError("denon: async response exceeded buffer"))
            return
        want = min(_RECV_CHUNK, remaining)
        n = self._sock.recv_into(
            memoryview(self._buf)[self._recv_len:self._recv_len + want], want)
        if n == 0:
            self._finish()
            return
        self._recv_len += n

    def _finish(self):
        self._sock.close()
        self._sock = None
        data = bytes(self._buf[:self._recv_len])
        _, _, body = data.partition(b"\r\n\r\n")
        self.result = body.decode("utf-8")
        self.phase = _PHASE_DONE

    def _fail(self, e):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self.error = e
        self.phase = _PHASE_ERROR


_poll_op      = None
_poll_attempt = 0


def start_status_poll(now):
    """Kick off a background status poll if one isn't already in flight.
    Idempotent -- safe to call every tick from _poll_avr's own interval
    gating."""
    global _poll_op, _poll_attempt
    if _poll_op is not None and _poll_op.phase not in (_PHASE_DONE, _PHASE_ERROR):
        return
    _poll_attempt = 1
    _poll_op = _AsyncRequest("POST", "/goform/AppCommand.xml", _STATUS_BODY, now)


def pump_status_poll(now):
    """Advance the in-flight poll (if any) by one tick.

    Returns ("pending", None) while still in flight, ("done", status_dict)
    or ("error", exception) once it resolves. Retries once internally
    (a fresh _AsyncRequest, matching _request_text()'s existing
    "attempt >= 2: raise" policy) before surfacing an error -- there's no
    connection pool in this design, so there's nothing to reset between
    attempts the way _reset_connections() does for the blocking path."""
    global _poll_op, _poll_attempt
    if _poll_op is None:
        return ("pending", None)

    _poll_op.pump(now)

    if _poll_op.phase == _PHASE_DONE:
        result = _poll_op.result
        _poll_op = None
        # Found 2026-08-03, live: gc.collect() here (originally added to
        # reclaim the discarded buffer promptly) is itself a 400-700ms
        # synchronous cost on this heap -- exactly the kind of main-loop
        # stall this whole engine exists to eliminate. Removed; this
        # restores the exact cadence the blocking design already shipped
        # with (_poll_avr's existing gc.collect() before each poll
        # kickoff, and nothing after) rather than adding a second point
        # of collection that turned out not to be free.
        try:
            status = _parse_status_xml(result)
        except Exception as e:
            return ("error", e)
        return ("done", status)

    if _poll_op.phase == _PHASE_ERROR:
        err = _poll_op.error
        if _poll_attempt < 2:
            _poll_attempt += 1
            _poll_op = _AsyncRequest("POST", "/goform/AppCommand.xml", _STATUS_BODY, now)
            return ("pending", None)
        _poll_op = None
        return ("error", err)

    return ("pending", None)


# ---------------------------------------------------------------------------
# Non-blocking control commands (volume/mute/power/input/preset) -- same
# _AsyncRequest engine as the status poll above, but fire-and-forget: every
# one of these is already a write whose response body app.py never reads
# (see each sync function's docstring/_send()'s own docstring), so there's
# no result to parse, just success-or-failure.
#
# Deliberately simpler than the poll in two ways:
#   - No retry-on-failure. Commands fire far more often than the poll (every
#     tap/settled encoder turn, not every 5-30s), so a dropped one
#     self-corrects on the user's next interaction, and the poll's own
#     error-count/MODE_ERROR tracking remains the backstop for sustained AVR
#     unavailability -- retrying here would just add complexity for a rare
#     case already covered elsewhere.
#   - No queue. If a new command is kicked off while one is still in flight,
#     the old one is dropped (socket closed) in favor of the new one --
#     latest call always wins, same principle _send_volume_debounced already
#     applies by collapsing every intermediate encoder tick into just the
#     one final debounced value rather than sending each tick.
# ---------------------------------------------------------------------------

_cmd_op = None


def start_command(path, now, port=None, body=None, method="GET"):
    """Fire-and-forget an async control command. Safe to call every tick
    from a tap/encoder handler -- idempotent-ish in that a still-pending
    older command is simply superseded, never queued."""
    global _cmd_op
    if _cmd_op is not None and _cmd_op.phase not in (_PHASE_DONE, _PHASE_ERROR):
        _cmd_op._fail(RuntimeError("denon: command superseded"))
    _cmd_op = _AsyncRequest(method, path, body, now, port=port)


def pump_command(now):
    """Advance the in-flight command (if any) by one tick.

    Returns ("pending", None) while still in flight, ("done", None) or
    ("error", exception) once it resolves -- no status dict, commands
    don't parse a response."""
    global _cmd_op
    if _cmd_op is None:
        return ("pending", None)

    _cmd_op.pump(now)

    if _cmd_op.phase == _PHASE_DONE:
        _cmd_op = None
        return ("done", None)

    if _cmd_op.phase == _PHASE_ERROR:
        err = _cmd_op.error
        _cmd_op = None
        return ("error", err)

    return ("pending", None)


def async_set_volume(db, now):
    """Mirrors set_volume() below -- same VOLUME_MIN/MAX clamp, same path,
    async kickoff instead of a blocking _send()."""
    db = max(config.VOLUME_MIN, min(config.VOLUME_MAX, float(db)))
    start_command("/goform/formiPhoneAppVolume.xml?1+{:.1f}".format(db), now)


def async_mute_on(now):
    start_command("/goform/formiPhoneAppDirect.xml?MUON", now)


def async_mute_off(now):
    start_command("/goform/formiPhoneAppDirect.xml?MUOFF", now)


def async_power_on(now):
    start_command("/goform/formiPhoneAppDirect.xml?PWON", now)


def async_power_standby(now):
    start_command("/goform/formiPhoneAppDirect.xml?PWSTANDBY", now)


def async_set_input(index, now):
    """Mirrors set_input() below -- same web-UI (port 11080) endpoint and
    XML body, async kickoff instead of a blocking _request_text()."""
    xml = '<Source zone="1" index="{}"></Source>'.format(index)
    qs  = "type=7&data={}".format(_xml_encode(xml))
    start_command("/ajax/globals/set_config?" + qs, now, port=config.AVR_PORT_UI)


def async_set_preset(value, now):
    """Mirrors set_preset() below -- same _last_active bookkeeping (local,
    no network), async kickoff for the actual control send."""
    global _last_active
    _last_active = value
    start_command(
        "/goform/formiPhoneAppDirect.xml?PSDIRAC%20{}".format(
            _ajax_value_to_control_slot(value)),
        now)


def async_set_preset_enabled(enabled, now):
    """Mirrors set_preset_enabled() below -- same OFF/_last_active logic."""
    if enabled:
        if _last_active is None:
            return   # nothing known to re-enable yet
        arg = _ajax_value_to_control_slot(_last_active)
    else:
        arg = "OFF"
    start_command("/goform/formiPhoneAppDirect.xml?PSDIRAC%20{}".format(arg), now)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _post_status():
    """POST AppCommand status request; returns XML text."""
    url = _BASE + "/goform/AppCommand.xml"
    # No Content-Type header -- the AVR rejects requests that include one.
    return _request_text("post", url, data=_STATUS_BODY, timeout=_TIMEOUT)


def _send(path):
    """HTTP GET for control commands where the response body is not needed.

    Found 2026-08-03: this used to call _session.get() directly, bypassing
    _request_text()'s retry/pool-reset -- every control command (volume,
    mute, power, preset/Dirac select) could hang on the same broken-pooled-
    socket bug _post_status() was just fixed for, with nothing to recover
    it. Routed through _request_text() so it gets the same protection; the
    response body is fetched and discarded rather than skipped, since
    _request_text() needs it to detect a body-read failure at all -- for
    these tiny control-command responses that cost is negligible."""
    url = _BASE + path
    _request_text("get", url, timeout=_TIMEOUT)


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

def _parse_status_xml(xml):
    """Parse an AppCommand.xml status response into the driver contract's
    status dict. Shared by the blocking get_status() and the non-blocking
    pump_status_poll() (see above) so the 2026-08-01 "raise on unparseable
    power instead of smoothing into STANDBY" fix below can't drift between
    two copies.

    Returns a dict:
        {
            "volume_db": float,   # e.g. -40.0
            "muted":     bool,
            "power":     str,     # "ON" or "STANDBY"
            "input":     str,     # raw source name e.g. "SAT/CBL"
        }
    """
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


def get_status():
    """Poll the AVR for current state (Main Zone only), via the blocking
    adafruit_requests session. Not used by app.py's background poll any
    more (see pump_status_poll() above) -- kept for any caller that wants
    a synchronous read. Raises OSError on network failure."""
    return _parse_status_xml(_post_status())


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
    # See _send()'s 2026-08-03 note -- same bare-_session.get() bug, same fix.
    _request_text("get", url, timeout=_TIMEOUT)


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
