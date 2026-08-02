# ota.py -- deloop self-update: check for and apply a new app-file release
# from GitHub Releases (config.OTA_REPO). Never touches CircuitPython
# firmware -- only the app files this repo's own Makefile deploys.
#
# Pure network+filesystem module -- no microcontroller.nvm / reset() or
# supervisor.reload() calls anywhere in here. ota_boot.py owns the entire
# NVM-action/reload lifecycle (see its config.NVM_OTA_ACTION and run());
# this module only ever does "check" or "apply" and either returns a
# result or raises. (Moved out of app.py 2026-08-01 -- see ota_boot.py's
# module docstring for why: app.py's own top-level imports alone left too
# little free memory for a reliable TLS handshake.)
#
# TLS: confirmed live (2026-07-31, real M5 Dial, CircuitPython 10.2.1)
# that a plain ssl.create_default_context() -- no embedded CA -- completes
# a real handshake against both api.github.com and the release-asset CDN
# host. Unlike wiim.py's self-signed-cert case, GitHub's chain is trusted
# by this board's default store, so ota.py uses the same bare
# adafruit_requests.Session() every plain-HTTP backend already shares --
# just constructed with an ssl_context this time so the scheme can be
# https. Root cause of an earlier, much stranger-looking failure (a full
# TLS handshake succeeding but the first post-handshake send() failing)
# turned out to be nothing to do with TLS itself -- see ota_boot.py's
# run()/_reload_to_normal() docstrings: it only ever happened following a
# genuine hardware reset, never a soft reload, which is why this module is
# only ever called from a codepath that guarantees the latter.
#
# Hashing: this build's native `hashlib` module has no sha256 at all
# (confirmed live: AttributeError('module' object has no attribute
# 'sha256')) -- not a version gap, just genuinely absent on this port. Uses
# adafruit_hashlib instead (installed via `make install-libs`), which
# falls back to a pure-Python sha256 implementation and was confirmed live
# to produce byte-identical digests to CPython's hashlib.sha256, including
# its incremental .update() path (used here since files are streamed in
# chunks, never loaded whole into RAM).
#
# Manifest-driven, not a hardcoded file list -- ota.py has no opinion about
# which files a release contains. tools/build_release_manifest.py (run in
# CI) is the one place that enumerates them.

import os
import gc
import time
import adafruit_hashlib as hashlib
import adafruit_connection_manager

import config
import version

_API_LATEST = "https://api.github.com/repos/{}/releases/latest"
_STAGING_DIR = "/ota_staging"
_CHUNK_SIZE = 512
# See _Fetcher._retry_wait()'s docstring for why this is raised from the
# original 2 -- a real recovery was observed on attempt 2 of exactly this
# kind of failure, and 2 total attempts is a thin margin against something
# that may be a residual, lower-probability version of the same hiccup.
# 10 chosen explicitly for headroom while gathering real failure-rate data
# (see the diagnostic loop in docs/ota.md) rather than guessing further --
# revisit once that data says what it's actually worth being.
_MAX_ATTEMPTS = 10

# Root-caused live (2026-08-01): every OTA request through adafruit_requests
# was taking 12+ seconds and eventually failing with ETIMEDOUT, while an
# identical raw-socket request (explicit "Connection: close", no keep-alive)
# completed in under 3 seconds every time -- DNS/TLS/GitHub were never the
# problem. Confirmed live that adding these two headers to a plain
# adafruit_requests.Session.get() alone fixes it (2.5s headers + 0.6s body,
# clean 200). Without "Connection: close", the library appears to wait on a
# keep-alive response in a way that eventually times out rather than
# detecting the body's actual end -- consistent with "the connect succeeded;
# the *body read* is what timed out" from the reliability investigation
# below, which stopped one step short of asking why the body read itself
# was slow. GitHub's API also documents User-Agent as effectively required.
_HEADERS = {"User-Agent": "deloop", "Connection": "close"}


def _host_of(url):
    """Mirrors adafruit_requests.Session.request()'s own url parsing
    (proto, dummy, host, path = url.split("/", 3)) so the host comparison
    below is judging by the exact same notion of "host" the library itself
    uses -- just the OUTER url's host, before any redirect. See
    _Fetcher._reset_connections()'s docstring for why that's the right
    granularity, not "did any redirect ever fire"."""
    try:
        _, _, host, _ = url.split("/", 3)
    except ValueError:
        _, _, host = url.split("/", 2)
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


class _Fetcher:
    """Owns one Session, rebuilt from scratch (fresh wifi connect, fresh
    socket pool, fresh TLS context -- see ota_boot.py's _new_session())
    on a retry, or whenever the target host changes -- see
    _maybe_reset()/_reset_connections()'s docstrings for why: reusing an
    ssl_context across requests to different hosts within one apply() call
    produced a real, confirmed OSError -12288, but rebuilding unconditionally
    before every request (tried first) traded that for its own MemoryError
    from repeated back-to-back rebuilds.

    Adafruit_CircuitPython_Requests#191 documents ESP32 TCP connections
    that keep timing out after a prior failure when a retry reuses the
    same pooled socket/session -- rebuilding everything before a retry,
    instead of calling .get() again on the session that just failed, is
    the safe-by-construction fix for that class of issue too. new_session
    is a zero-arg callable (ota_boot.py's _new_session), not a pre-built
    Session, specifically so this class can call it again mid-operation.
    """

    def __init__(self, new_session):
        self._new_session = new_session
        # Deliberately NOT built here -- self._last_host starting as None
        # guarantees _maybe_reset()'s first call (for whatever the first
        # request's host is) already triggers a build via
        # _reset_connections(). Building one here too, eagerly, meant
        # every apply()/check_latest_version() call paid for TWO full
        # session constructions (two wifi radio disable/enable cycles,
        # see ota_boot.py's _new_session()) before the first real request
        # even ran -- confirmed live 2026-08-01 as one contributor to
        # -12288 recurring: rapid-fire radio resets with no settling time
        # between them, not just one.
        self.session = None
        self._last_host = None   # see _maybe_reset()

    def _reset_connections(self):
        """Rebuild the ENTIRE session -- fresh wifi connect, fresh
        socketpool, fresh ssl_context -- not just closed sockets.
        Root-caused live (2026-08-01) in three steps:

        1. This Fetcher's session is reused across MULTIPLE requests to
           DIFFERENT hosts within one apply() call (api.github.com for
           release info, then github.com -> a redirect to
           release-assets.githubusercontent.com for the manifest and every
           file asset) -- confirmed by isolating that the exact same
           request, issued alone through a brand-new session that had
           never touched any other host, always succeeds, while the
           identical request through this Fetcher's shared, already-used
           session intermittently failed with OSError -12288 (a low-level
           TLS/connection error) or downstream MemoryErrors.
        2. First fix attempt only force-closed pooled sockets
           (`self.session._connection_manager._free_sockets(force=True)`,
           the same trick denon.py's own _reset_connections() uses) --
           confirmed live this did NOT fix it; -12288 recurred identically.
           That ruled out socket-pool reuse as the actual mechanism: the
           one thing that step still left untouched was `self._ssl_context`
           itself (the underlying mbedtls context object), reused across
           every request in the Fetcher's lifetime regardless of how many
           sockets got closed. Every test that ever succeeded used a
           freshly-constructed ssl.create_default_context() for that one
           request alone -- nothing that succeeded ever reused an
           ssl_context object across more than one wrap_socket() call.
        3. Rebuilding the whole session fixed -12288, but confirmed live
           to fail differently instead: a bare MemoryError after just the
           SECOND rebuild (2-3 total session constructions, not "many").
           Root cause, found by reading adafruit_connection_manager.py's
           source directly: `get_connection_manager(pool)` caches one
           ConnectionManager per socket pool in a MODULE-LEVEL GLOBAL dict
           (`_global_connection_managers`), keyed by the pool object
           itself. Every `_new_session()` call creates a brand-new
           SocketPool, which permanently registers a new entry in that
           global cache -- setting `self.session = None` and calling
           `gc.collect()` only drops OUR reference, it can never free the
           pool/ConnectionManager, because the global dict still holds a
           strong reference to it as a key. This is a real, unbounded leak
           across repeated rebuilds, not a "too many rebuilds" pressure
           problem -- confirmed by failing at rebuild #2-3, not #18.
           `adafruit_connection_manager.connection_manager_close_all(
           release_references=True)` is the library's OWN documented way to
           release those global references -- tried first, and confirmed
           live to raise `KeyError` immediately (caught, so harmless, but
           it also meant the actual fix never ran): that function assumes
           pools were created via the library's own `get_socketpool()`/
           `get_ssl_context()` factories, which register them in a
           SEPARATE tracking dict (`_global_key_by_socketpool`) that this
           project's pools -- constructed directly via
           `socketpool.SocketPool(wifi.radio)`, never through those
           factories -- were never added to. `release_references=True`
           tries to `.pop()` our pool from that dict with no default,
           raising `KeyError` before it ever reaches the
           `_global_connection_managers.pop(pool, None)` call that
           actually matters for the leak.

           Fix: skip that incompatible helper and pop our own pool
           directly out of `_global_connection_managers` -- the only dict
           our pools were ever actually registered in (by
           `get_connection_manager()`, called internally by
           `adafruit_requests.Session.__init__`), and the only one that
           matters for this leak.

        See _maybe_reset() for when this actually gets called -- rebuilding
        unconditionally before every single request was tried first and,
        before finding the leak above, looked like it just needed to
        happen less often. Kept host-gated for that reason (fewer rebuilds
        means fewer wifi reconnects) -- but see _get_final()'s docstring
        (2026-08-01) for the real remaining piece this section didn't
        cover: a HOST CHANGE isn't the only time two TLS connections can
        end up open at once. Any redirect -- including the one
        adafruit_requests used to follow internally inside a single
        .get() call -- does the same thing, since Response.close() never
        actually closes a socket, only frees it for reuse. _get_final()
        now calls this method directly for that inner hop too, rather
        than leaving it to whatever _maybe_reset() happens to decide."""
        if self.session is not None:   # None on the very first call -- nothing to clean up yet
            try:
                pool = self.session._connection_manager._socket_pool
                self.session._connection_manager._free_sockets(force=True)
                adafruit_connection_manager._global_connection_managers.pop(pool, None)
            except Exception as e:
                print("ota: connection cleanup failed:", type(e), e)
        self.session = None
        gc.collect()
        self.session = self._new_session()

    def _maybe_reset(self, url, attempt):
        """Only rebuild when it's actually needed: on any retry (attempt
        > 1 -- same safety net _retry_wait() used to provide directly), or
        when the target host has genuinely changed since the last request
        this Fetcher made.

        Superseded (2026-08-01): this docstring used to claim a single
        .get() call following its own internal github.com -> CDN redirect
        was "already confirmed safe" and that only OUTER host changes
        (api.github.com -> github.com) needed a reset. That was wrong --
        see _get_final()'s docstring for the real root cause (two TLS
        connections held open at once during ANY redirect, inner or
        outer). _get_final() now does its own _reset_connections() for
        the inner hop and updates self._last_host to the CDN host it
        lands on, so this method's plain outer-host comparison still does
        the right thing for the *next* call for free: every file's outer
        url is `github.com`, which now never matches self._last_host
        (left pointing at the previous file's CDN host by _get_final()),
        so every single file triggers a reset here too. In practice that
        means every file download costs two full session rebuilds, not
        one -- real overhead, but each rebuild is what actually prevents
        two live TLS sessions from ever coexisting, which is the entire
        fix."""
        host = _host_of(url)
        if attempt > 1 or host != self._last_host:
            self._reset_connections()
        self._last_host = host

    def _retry_wait(self, attempt, t0, url, e):
        """Common tail of a failed attempt: log, decide whether to retry.
        Raises the original exception once attempts are exhausted.

        Used to also rebuild the session itself here -- removed 2026-08-01
        once _reset_connections() started running at the top of every loop
        iteration in get_json()/download(), which makes rebuilding here
        too just a redundant extra rebuild right before another one. The
        double-rebuild wasn't wrong, just wasteful; the memory-safety
        lesson that motivated it (drop the reference and gc.collect()
        BEFORE building the replacement, never hold two full sessions live
        at once) still applies and now lives in _reset_connections().

        Attempt cap raised from 2 to _MAX_ATTEMPTS (4) 2026-08-01, after
        calling the REAL _Fetcher/_new_session directly from a REPL
        (not a hand-typed replica) showed attempt 1 fail with OSError
        -12288 and attempt 2 (the retry) succeed cleanly -- proof the
        rebuild-and-retry mechanism genuinely recovers, at least
        sometimes. Every real Install Update failure seen this session
        hit -12288 on BOTH of only 2 total attempts before giving up; if
        this is a real but now much less frequent intermittent TLS hiccup
        (plausible after today's session/ssl_context/connection-manager
        fixes actually reduced how often it happens, even if not to zero),
        a thin 2-attempt margin is exactly what would still occasionally
        exhaust itself against something probabilistic. More attempts is
        a direct, well-evidenced response to that -- not a guess at a new
        root cause when the existing one seems to be a residual, lower
        probability of the exact same failure."""
        print("ota: GET failed after {:.2f}s ({}):".format(
            time.monotonic() - t0, url), type(e), e)
        if attempt >= _MAX_ATTEMPTS:
            raise e
        time.sleep(1.0)

    def _raw_fetch_location(self, url, timeout_ms):
        """Resolve url's redirect Location via a RAW socket, bypassing
        adafruit_requests's Response/_parse_headers entirely for this one
        hop. Returns the Location header's value; raises if the response
        isn't a 3xx with one.

        Root-caused live (2026-08-01) by capturing the ACTUAL bytes GitHub
        sends back for a release-asset `browser_download_url` (of the form
        `https://github.com/<repo>/releases/download/vN/<file>`, which
        ALWAYS 302-redirects to a signed, per-download
        `release-assets.githubusercontent.com` URL): the response carries
        a `Content-Security-Policy` header measured at **3625 bytes** --
        one single header LINE, not the whole response (whose total
        header block is ~5.1KB across ~16 headers; the `Location` header
        itself is a comparatively modest ~915 bytes). Confirmed live via a
        second probe that the CDN response on the OTHER end of that
        redirect has a completely normal ~874-byte header block, longest
        line 65 bytes -- this is exclusively a github.com problem, not a
        redirect problem, not a two-connections problem, not a
        session/ssl_context-reuse problem, and has nothing to do with
        which host the request lands on afterward.

        adafruit_requests's Response._parse_headers()/_readto() reads one
        line at a time by growing a SINGLE bytearray 32 bytes at a time,
        copying the ENTIRE buffer-so-far on every growth step, until it
        finds `\\r\\n`. For the 3625-byte CSP line alone, that's ~113
        sequential allocate-and-copy cycles (32, 64, 96, ... 3625 bytes),
        each leaving the previous, smaller buffer as garbage on this
        device's non-compacting allocator. Confirmed live this reliably
        produces either a bare MemoryError (observed: "allocating 3602
        bytes", within one 32-byte step of the CSP header's actual length)
        or, depending on the heap's exact fragmentation state at the time,
        the same OSError -12288 chased earlier in this investigation as an
        "intermittent TLS hiccup" -- both are almost certainly two
        different-looking symptoms of the identical underlying cause. Free
        memory reading fine throughout every prior test (80-90KB+) was
        never contradictory: fragmentation, not total free space, is what
        a ~113-step allocate-and-copy churn actually threatens.

        Fix: never ask adafruit_requests to parse a github.com response at
        all. We only need the Location header's ~915 bytes, not the
        3625-byte CSP header sitting next to it in the same response --
        read the headers ourselves in fixed 512-byte chunks (no per-byte
        regrowth) and search for what we need, discarding the rest
        unparsed. Uses a brand-new ssl.create_default_context() rather
        than self.session's own -- reusing one ssl_context object across
        different hosts was independently confirmed unsafe earlier in
        this investigation (see _reset_connections()'s docstring), and
        this hop's host (github.com) is never the same as self.session's
        (api.github.com, or a previous file's CDN host)."""
        proto, _, host, path = url.split("/", 3)
        if ":" in host:
            host = host.split(":", 1)[0]

        # Force-close (not just free) whatever self.session has sitting
        # idle -- cheap, no radio bounce, and avoids this raw socket
        # opening alongside a dangling, merely-freed connection to a
        # different host. gc.collect() before touching anything else --
        # confirmed live (2026-08-01) that skipping this and relying on
        # the caller to have already collected is not safe to assume: the
        # caller's own last response/JSON-parse garbage was still fully
        # alive the one time this was tested without it, and this method
        # already asks for one of the larger single allocations (the
        # final header buffer, up to several KB) anywhere in this file.
        gc.collect()
        try:
            self.session._connection_manager._free_sockets(force=True)
        except Exception as e:
            print("ota: pre-probe socket cleanup failed:", type(e), e)

        pool = self.session._connection_manager._socket_pool
        import ssl
        ssl_context = ssl.create_default_context()
        raw = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        sock = ssl_context.wrap_socket(raw, server_hostname=host)
        sock.settimeout(timeout_ms / 1000)
        try:
            sock.connect((host, 443))
            req = "GET /{} HTTP/1.1\r\nHost: {}\r\n".format(path, host)
            for k, v in _HEADERS.items():
                req += "{}: {}\r\n".format(k, v)
            req += "\r\n"
            sock.send(req.encode("utf-8"))

            # Even a single final b"".join() over every accumulated chunk
            # (tried first, after fixing the per-iteration rejoin above)
            # still needs ONE contiguous allocation the size of the WHOLE
            # header block -- confirmed live (2026-08-01) failing with
            # "MemoryError: ... allocating 5165 bytes", matching the full
            # ~5.1KB header block almost exactly. That's a smaller-scale
            # version of the identical problem this method exists to
            # solve: this device's non-compacting heap can't always find
            # one contiguous block that size, total-free numbers
            # notwithstanding. There is no number of bytes we can safely
            # assume we'll always be able to allocate in one piece.
            #
            # Fix: never hold more than one chunk (plus a small carry-over
            # for a line split across a chunk boundary) at a time. Scan
            # for complete lines as each chunk arrives; only the status
            # line and a `location:` line are ever kept -- the 3625-byte
            # CSP line (or anything else) is inspected a fragment at a
            # time and immediately discarded, never fully reassembled.
            # 1024-byte chunks (up from 512) mean even the CSP line only
            # ever needs ~4 carry-concat steps to fully pass through, not
            # the ~7 512-byte chunks or ~113 32-byte steps of earlier
            # approaches.
            status_code = None
            location = None
            carry = b""
            buf = bytearray(1024)
            total = 0
            headers_done = False
            while not headers_done and total < 16384:
                n = sock.recv_into(buf)
                if n == 0:
                    break
                total += n
                carry += bytes(buf[:n])
                while True:
                    i = carry.find(b"\r\n")
                    if i < 0:
                        break   # incomplete line -- wait for more data
                    line = carry[:i]
                    carry = carry[i + 2:]
                    if status_code is None:
                        status_code = int(line.decode("utf-8").split(" ")[1])
                    elif not line:
                        headers_done = True   # blank line -- end of headers
                        break
                    elif line.lower().startswith(b"location:"):
                        location = line.split(b":", 1)[1].strip().decode("utf-8")
        finally:
            sock.close()

        if status_code is None:
            raise RuntimeError("empty response reading {}".format(url))
        if not (300 <= status_code < 400) or not location:
            raise RuntimeError(
                "expected a redirect from {}, got status {} location {!r}".format(
                    url, status_code, location))
        return location

    def resolve_download_url(self, url, timeout_ms):
        """Retry-wrapped entry point for _raw_fetch_location() -- same
        whole-operation retry treatment as get_json()/download(), since
        this is just as real a network call as either of those.

        Also now calls _maybe_reset(url, attempt) first, exactly like
        get_json()/download() do -- confirmed live (2026-08-01) this
        matters, not just cosmetic consistency. This method's raw socket
        used to skip _reset_connections() entirely (it only force-closed
        idle sockets, never did a full session/radio rebuild), meaning
        github.com -- a genuinely new host every time this is called --
        never got its own radio reset the way api.github.com and the CDN
        host both do via get_json()/download()'s _maybe_reset() calls.
        The "3rd distinct hostname" issue documented in
        ota_boot._new_session() reappeared specifically in that shape: a
        3-unique-host sequence where every host got its own
        _reset_connections() call succeeded every time tested, while the
        same 3 hosts failed when the middle one (github.com, reached only
        through this method) skipped it. Whatever cache is scoped per
        hostname, it isn't just about a count of resets happening
        somewhere in the process -- it's about every genuinely new host
        getting one of its own."""
        attempt = 0
        while True:
            attempt += 1
            self._maybe_reset(url, attempt)   # every new host gets its own radio reset -- see docstring
            _t0 = time.monotonic()
            try:
                location = self._raw_fetch_location(url, timeout_ms)
                print("ota: resolved redirect in {:.2f}s: {}".format(
                    time.monotonic() - _t0, url))
                return location
            except OSError as e:
                self._retry_wait(attempt, _t0, url, e)

    def get_json(self, url, timeout_ms):
        # The retry wraps the *entire* GET-and-parse -- not just
        # session.get() -- because live testing (2026-07-31) showed
        # session.get() itself returning cleanly (headers received) while
        # the ETIMEDOUT actually happened inside resp.json()'s body read,
        # a moment later. A retry loop that only covered session.get()
        # would silently never retry that failure at all.
        #
        # url is always either api.github.com's own endpoint or an
        # already-resolved CDN url (see resolve_download_url() -- ota.py's
        # module-level functions resolve every browser_download_url
        # before ever handing it to this method) -- never a github.com
        # release-download url directly. See _raw_fetch_location()'s
        # docstring for why that hop is handled separately.
        attempt = 0
        while True:
            attempt += 1
            self._maybe_reset(url, attempt)   # see docstring -- only rebuilds on host change/retry
            _t0 = time.monotonic()
            resp = None
            try:
                resp = self.session.get(url, headers=_HEADERS, timeout=timeout_ms / 1000)
                if resp.status_code != 200:
                    raise RuntimeError("GET {} -> {}".format(url, resp.status_code))
                result = resp.json()
                print("ota: GET+parse took {:.2f}s: {}".format(time.monotonic() - _t0, url))
                return result
            except OSError as e:
                self._retry_wait(attempt, _t0, url, e)
            finally:
                if resp is not None:
                    resp.close()

    def download(self, url, dest_path, timeout_ms):
        """Stream url to dest_path, returning its sha256 hexdigest. Never
        holds the full body in RAM -- some .mpy files run tens of KB, and
        this device's heap is precious enough (see CLAUDE.md's boot-memory
        guardrails) that .content/.text are not an option here.

        Same whole-operation retry reasoning as get_json() -- a stall
        mid-stream is just as plausible on a large file as during a small
        JSON body's read, maybe more so. url is always an already-resolved
        CDN url -- see get_json()'s docstring."""
        attempt = 0
        while True:
            attempt += 1
            self._maybe_reset(url, attempt)   # see docstring -- only rebuilds on host change/retry
            _t0 = time.monotonic()
            resp = None
            try:
                resp = self.session.get(url, headers=_HEADERS, timeout=timeout_ms / 1000)
                if resp.status_code != 200:
                    raise RuntimeError("GET {} -> {}".format(url, resp.status_code))
                digest = hashlib.sha256()
                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                        f.write(chunk)
                        digest.update(chunk)
                print("ota: GET+download took {:.2f}s: {}".format(time.monotonic() - _t0, url))
                return digest.hexdigest()
            except OSError as e:
                self._retry_wait(attempt, _t0, url, e)
            finally:
                if resp is not None:
                    resp.close()


def _latest_release(fetcher):
    return fetcher.get_json(_API_LATEST.format(config.OTA_REPO), config.OTA_CHECK_TIMEOUT_MS)


def _parse_tag_version(tag_name):
    """"v3" -> 3. Raises clearly if the tag isn't a bare incrementing int,
    protecting the on-device int() compare from a release tagged some
    other way (a stray semver tag, a draft, etc)."""
    s = (tag_name or "").strip()
    if s[:1] == "v":
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        raise ValueError("release tag {!r} is not a bare integer (vN)".format(tag_name))


def _asset_url_map(release):
    """Extract only {name: browser_download_url} from a release's asset
    list, discarding everything else GitHub's API includes per asset
    (uploader info, timestamps, labels, ids, content_type, download
    counts, ...). apply() never reads any of that.

    Confirmed live (2026-08-02), quantified via gc.mem_free() at each
    step: the full parsed release object for this project's ~17-asset
    release holds ~53KB alive -- and apply() used to keep the WHOLE
    thing referenced for the entire file-download loop (needed
    _find_asset() to work), not just the ~17 name/url pairs actually
    used. That 53KB was never reclaimed by gc.collect() while the
    reference was still live -- not a leak, just an unnecessarily large
    object kept around far longer than needed. Letting apply() drop the
    full release object immediately after this extraction, right before
    the memory-sensitive manifest/file-download sequence begins, hands
    that headroom back for the part of the process that was actually
    running out of it."""
    return {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}


def check_latest_version(new_session):
    """Return (latest, current) version integers. Read-only -- makes one
    API call, downloads/installs nothing. new_session is a zero-arg
    callable (ota_boot.py's _new_session), not a pre-built Session -- see
    _Fetcher."""
    fetcher = _Fetcher(new_session)
    release = _latest_release(fetcher)
    latest = _parse_tag_version(release.get("tag_name"))
    return latest, version.CURRENT_VERSION


def _ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass  # already exists -- fine


def _clear_staging():
    _ensure_dir(_STAGING_DIR)
    for name in os.listdir(_STAGING_DIR):
        os.remove(_STAGING_DIR + "/" + name)


def apply(new_session):
    """Check, download, verify, and install the latest release.

    Only ever called from ota_boot.py's run(), after it has already
    called storage.remount("/", readonly=False) itself (a *live* remount,
    no reboot involved -- see that function's docstring for why this
    project moved away from a boot.py-gated write-access scheme entirely).
    Raises on any failure; nothing under _STAGING_DIR is
    ever renamed into place unless every manifest entry downloaded and
    verified clean first -- that verify-then-commit split is the entire
    safety story here, not a general rollback mechanism (acceptable given
    the accepted "worst case is USB recovery" risk tolerance for this
    project). Returns the newly-installed version integer on success.

    new_session is a zero-arg callable (ota_boot.py's _new_session), not a
    pre-built Session -- see _Fetcher, which rebuilds its session from
    scratch on any retry across the whole check+manifest+N-file-download
    sequence below, not just once at the start.
    """
    fetcher = _Fetcher(new_session)
    release = _latest_release(fetcher)
    latest = _parse_tag_version(release.get("tag_name"))
    asset_urls = _asset_url_map(release)
    release = None   # see _asset_url_map()'s docstring -- ~53KB, confirmed live, no longer needed past this point
    gc.collect()

    manifest_browser_url = asset_urls.get("manifest.json")
    if manifest_browser_url is None:
        raise RuntimeError("release {} has no manifest.json asset".format(latest))
    manifest_url = fetcher.resolve_download_url(manifest_browser_url, config.OTA_CHECK_TIMEOUT_MS)
    manifest = fetcher.get_json(manifest_url, config.OTA_CHECK_TIMEOUT_MS)

    files = manifest.get("files") or []
    if not files:
        raise RuntimeError("manifest.json lists no files")

    _clear_staging()

    staged = []
    for i, entry in enumerate(files):
        path = entry["path"]
        want_sha = entry["sha256"]
        want_size = entry["size"]

        browser_url = asset_urls.get(path)
        if browser_url is None:
            raise RuntimeError("release is missing an asset for manifest path {!r}".format(path))

        # Per-file instrumentation + an extra gc.collect() added 2026-08-01
        # while chasing a MemoryError partway through this loop -- 82KB
        # free overall shortly after the failure didn't explain a
        # 3626-byte allocation failing, which pointed at fragmentation
        # building up across the ~17-file sequential download rather than
        # a hard ceiling. The existing gc.collect() only ran at the END of
        # each iteration; collecting again immediately before the next
        # download (same "gc.collect() right before a specific
        # allocation" discipline used everywhere else in this project)
        # costs nothing when memory is fine and may help when it isn't.
        # The print is the cheapest way to find out which file and how
        # much was free right before whichever allocation actually fails,
        # instead of guessing from a single post-failure number.
        gc.collect()
        print("ota: downloading {}/{} {} (free mem: {})".format(
            i + 1, len(files), path, gc.mem_free()))

        dest = _STAGING_DIR + "/" + path
        asset_url = fetcher.resolve_download_url(browser_url, config.OTA_INSTALL_TIMEOUT_MS)
        got_sha = fetcher.download(asset_url, dest, config.OTA_INSTALL_TIMEOUT_MS)
        got_size = os.stat(dest)[6]
        if got_sha != want_sha or got_size != want_size:
            raise RuntimeError(
                "verify failed for {}: got sha256={} size={} (want {} / {})".format(
                    path, got_sha, got_size, want_sha, want_size))

        staged.append((dest, "/" + path))
        gc.collect()

    # Every file downloaded and verified clean -- commit. Each rename is
    # itself atomic on this filesystem, but the *set* of renames is not a
    # single transaction; a crash mid-loop would leave some files updated
    # and others not. Every file that did get renamed is independently
    # known-good, and per this feature's accepted risk tolerance, worst
    # case is a USB re-copy -- not attempting anything fancier than that.
    for dest, live_path in staged:
        os.rename(dest, live_path)

    return latest
