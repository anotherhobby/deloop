# ota.py -- deloop self-update: check for and apply a new app-file release
# from a flat S3 layout (config.OTA_S3_BASE -- see _s3_url()). Never touches
# CircuitPython firmware -- only the app files this repo's own Makefile
# deploys. CI publishes a GitHub Release alongside each version too, for
# changelog/history -- see release.yml -- but the device only ever talks
# to S3.
#
# Pure network+filesystem module -- no microcontroller.nvm / reset() or
# supervisor.reload() calls anywhere in here. ota_boot.py owns the entire
# NVM-action/reload lifecycle (see its config.NVM_OTA_ACTION and run());
# this module only ever does "check" or "apply" and either returns a
# result or raises: app.py's own top-level imports alone would leave too
# little free memory for a reliable TLS handshake, so this only ever runs
# from ota_boot.py's lean import path -- see that module's docstring.
#
# TLS: a plain ssl.create_default_context() -- no embedded CA -- completes
# a real handshake against S3; this board's default cert store trusts it,
# so ota.py uses the same bare adafruit_requests.Session() every
# plain-HTTP backend already shares, just constructed with an ssl_context
# this time so the scheme can be https. This module is only ever called
# from a codepath that guarantees a soft reload rather than a hard reset
# before it runs -- see ota_boot.py's run()/_reload_to_normal() docstrings
# for why that distinction matters for TLS specifically.
#
# Hashing: this build's native `hashlib` module has no sha256 at all --
# not a version gap, just genuinely absent on this port. Uses
# adafruit_hashlib instead (installed via `make install-libs`), which
# falls back to a pure-Python sha256 implementation, confirmed
# byte-identical to CPython's hashlib.sha256 including its incremental
# .update() path (used here since files are streamed in chunks, never
# loaded whole into RAM).
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

_STAGING_DIR = "/ota_staging"
_CHUNK_SIZE = 512
# Generous headroom against a transient network hiccup during a real
# Check/Install -- each attempt is a full session rebuild (see
# _Fetcher._reset_connections()), so this is cheap insurance, not a tight
# loop.
_MAX_ATTEMPTS = 10

# Explicit "Connection: close" -- without it, adafruit_requests waits on a
# keep-alive response in a way that eventually times out (12+ seconds,
# ETIMEDOUT) rather than detecting the body's actual end. With it, the
# same request completes in under 3 seconds every time.
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
    socket pool, fresh TLS context -- see ota_boot.py's _new_session()) on
    a retry, or whenever the target host changes -- see
    _maybe_reset()/_reset_connections()'s docstrings for why: reusing an
    ssl_context across requests to different hosts produces a real
    OSError -12288, while rebuilding unconditionally before every single
    request costs its own MemoryError from repeated back-to-back rebuilds
    -- host-gating is the balance between the two.

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
        # _reset_connections(). Building one here too, eagerly, would mean
        # every apply()/check_latest_version() call pays for TWO full
        # session constructions (two wifi radio disable/enable cycles, see
        # ota_boot.py's _new_session()) before the first real request even
        # runs -- rapid-fire radio resets with no settling time between
        # them are a real source of -12288.
        self.session = None
        self._last_host = None   # see _maybe_reset()

    def _reset_connections(self):
        """Rebuild the ENTIRE session -- fresh wifi connect, fresh
        socketpool, fresh ssl_context -- not just closed sockets. Reusing
        an ssl_context (the underlying mbedtls context object) across
        requests to different hosts produces a real OSError -12288;
        force-closing pooled sockets alone
        (`self.session._connection_manager._free_sockets(force=True)`,
        the same trick denon.py's own _reset_connections() uses) doesn't
        fix it, since the ssl_context itself is what needs to be fresh.

        Rebuilding the whole session on every call isn't free either:
        adafruit_connection_manager caches one ConnectionManager per
        socket pool in a MODULE-LEVEL GLOBAL dict
        (`_global_connection_managers`), keyed by the pool object itself.
        Every `_new_session()` call creates a brand-new SocketPool, which
        permanently registers a new entry in that global cache -- setting
        `self.session = None` and calling `gc.collect()` only drops OUR
        reference, since the global dict still holds a strong reference to
        the pool as a key. Left alone, that's an unbounded leak of one
        ConnectionManager per rebuild. The library's own documented
        cleanup, `adafruit_connection_manager.connection_manager_close_all(
        release_references=True)`, doesn't work for pools built directly
        via `socketpool.SocketPool(wifi.radio)` (as this project's are,
        rather than through the library's own `get_socketpool()`/
        `get_ssl_context()` factories) -- it raises `KeyError` trying to
        pop from a separate tracking dict (`_global_key_by_socketpool`)
        our pools were never registered in, before it ever reaches the
        `_global_connection_managers.pop(pool, None)` call that actually
        matters. Popping our own pool directly out of
        `_global_connection_managers` -- the only dict our pools were ever
        actually registered in (by `get_connection_manager()`, called
        internally by `adafruit_requests.Session.__init__`) -- is what
        actually releases it.

        See _maybe_reset() for when this actually gets called -- host- and
        retry-gated rather than unconditional, so a real Check/Install
        doesn't pay for more radio reconnects than it needs to."""
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
        this Fetcher made. Every URL apply()/check_latest_version() ever
        requests shares one host (config.OTA_S3_BASE), so in practice this
        never rebuilds mid-install -- the reset path exists for retries,
        not something the happy path exercises."""
        host = _host_of(url)
        if attempt > 1 or host != self._last_host:
            self._reset_connections()
        self._last_host = host

    def _retry_wait(self, attempt, t0, url, e):
        """Common tail of a failed attempt: log, decide whether to retry.
        Raises the original exception once attempts are exhausted. Doesn't
        rebuild the session itself -- _reset_connections() already runs at
        the top of every loop iteration in get_json()/download(), so
        rebuilding here too would just be a redundant extra rebuild right
        before another one."""
        print("ota: GET failed after {:.2f}s ({}):".format(
            time.monotonic() - t0, url), type(e), e)
        if attempt >= _MAX_ATTEMPTS:
            raise e
        time.sleep(1.0)

    def get_json(self, url, timeout_ms):
        # The retry wraps the *entire* GET-and-parse -- not just
        # session.get() -- because live testing (2026-07-31) showed
        # session.get() itself returning cleanly (headers received) while
        # the ETIMEDOUT actually happened inside resp.json()'s body read,
        # a moment later. A retry loop that only covered session.get()
        # would silently never retry that failure at all.
        #
        # url is always a direct S3 object URL (config.OTA_S3_BASE's
        # latest.json, or a versioned manifest.json/asset -- see
        # _s3_url()) -- no redirect ever involved.
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
        JSON body's read, maybe more so. url is always a direct S3 object
        URL -- see get_json()'s docstring."""
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


def _s3_url(version_int, path):
    """Every asset for a given release lives flat under one versioned
    prefix -- e.g. config.OTA_S3_BASE + "/v9/app.mpy" -- direct object
    GETs, no redirect ever involved (see module docstring for why that
    matters)."""
    return "{}/v{}/{}".format(config.OTA_S3_BASE, version_int, path)


def _latest_version(fetcher):
    """Fetch latest.json ({"version": N}) from the bucket root. Written
    LAST by the release workflow, only after every versioned asset for
    that version has already been uploaded -- so a device can never
    observe "latest" pointing at a partially-uploaded version."""
    data = fetcher.get_json(config.OTA_S3_BASE + "/latest.json", config.OTA_CHECK_TIMEOUT_MS)
    return int(data["version"])


def check_latest_version(new_session):
    """Return (latest, current) version integers. Read-only -- makes one
    request, downloads/installs nothing. new_session is a zero-arg
    callable (ota_boot.py's _new_session), not a pre-built Session -- see
    _Fetcher."""
    fetcher = _Fetcher(new_session)
    latest = _latest_version(fetcher)
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


def apply(new_session, on_progress=None):
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

    on_progress, if given, is called (current, total) once per file right
    BEFORE that file's own download starts -- ota_boot.py's own UI hook, so
    a stalled/crashed install shows the file it actually died on ("stuck
    at X of N"), not the last one that already finished. Optional and
    unused by tools/ota_regression_check.py, which replicates this loop
    directly rather than calling apply().
    """
    fetcher = _Fetcher(new_session)
    latest = _latest_version(fetcher)

    manifest_url = _s3_url(latest, "manifest.json")
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
        # Fires before this file's own network work starts, not after --
        # so a hang or crash mid-file shows "stuck at X of N" on-screen
        # (the file it died on), not the last one that already finished.
        if on_progress is not None:
            on_progress(i + 1, len(files))

        dest = _STAGING_DIR + "/" + path
        asset_url = _s3_url(latest, path)
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
