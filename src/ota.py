# ota.py -- deloop self-update: check for and apply a new app-file release
# from GitHub Releases (config.OTA_REPO). Never touches CircuitPython
# firmware -- only the app files this repo's own Makefile deploys.
#
# Pure network+filesystem module -- no microcontroller.nvm / reset() or
# supervisor.reload() calls anywhere in here. app.py owns the entire NVM-
# action/reload lifecycle (see its _NVM_OTA_ACTION and _ota_lean_mode());
# this module only ever does "check" or "apply" and either returns a
# result or raises.
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
# turned out to be nothing to do with TLS itself -- see _ota_lean_mode()'s
# docstring in app.py: it only ever happened following a genuine hardware
# reset, never a soft reload, which is why this module is only ever
# called from a codepath that guarantees the latter.
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

import config
import version

_API_LATEST = "https://api.github.com/repos/{}/releases/latest"
_STAGING_DIR = "/ota_staging"
_CHUNK_SIZE = 512


def _fetch_json(session, url, timeout_ms):
    # One retry with a short pause -- cheap insurance against an ordinary
    # transient network hiccup. Not standing in for anything deeper: the
    # one real reliability issue this project hit (a full TLS handshake
    # succeeding but the first post-handshake send() failing, 100% of the
    # time) turned out to be tied to a genuine hardware reset having
    # happened in the current power cycle, not something a request-level
    # retry could paper over -- see app.py's _ota_lean_mode() docstring.
    # This module is only ever reached from a codepath that avoids that
    # condition entirely, so this retry is just normal defensive practice.
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = session.get(url, timeout=timeout_ms / 1000)
            break
        except OSError as e:
            if attempt >= 2:
                raise
            print("ota: request failed ({}), retrying once:".format(url), type(e), e)
            time.sleep(1.0)
    try:
        if resp.status_code != 200:
            raise RuntimeError("GET {} -> {}".format(url, resp.status_code))
        return resp.json()
    finally:
        resp.close()


def _latest_release(session):
    return _fetch_json(session, _API_LATEST.format(config.OTA_REPO), config.OTA_CHECK_TIMEOUT_MS)


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


def _find_asset(release, filename):
    for asset in release.get("assets", []):
        if asset.get("name") == filename:
            return asset
    return None


def check_latest_version(session):
    """Return (latest, current) version integers. Read-only -- makes one
    API call, downloads/installs nothing."""
    release = _latest_release(session)
    latest = _parse_tag_version(release.get("tag_name"))
    return latest, version.CURRENT_VERSION


def _download_to(session, url, dest_path, timeout_ms):
    """Stream url to dest_path, returning its sha256 hexdigest. Never
    holds the full body in RAM -- some .mpy files run tens of KB, and this
    device's heap is precious enough (see CLAUDE.md's boot-memory
    guardrails) that .content/.text are not an option here."""
    resp = session.get(url, timeout=timeout_ms / 1000)
    try:
        if resp.status_code != 200:
            raise RuntimeError("GET {} -> {}".format(url, resp.status_code))
        digest = hashlib.sha256()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                f.write(chunk)
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        resp.close()


def _ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass  # already exists -- fine


def _clear_staging():
    _ensure_dir(_STAGING_DIR)
    for name in os.listdir(_STAGING_DIR):
        os.remove(_STAGING_DIR + "/" + name)


def apply(session):
    """Check, download, verify, and install the latest release.

    Only ever called from app.py's _ota_lean_mode(), after it has already
    called storage.remount("/", readonly=False) itself (a *live* remount,
    no reboot involved -- see that function's docstring for why this
    project moved away from a boot.py-gated write-access scheme entirely).
    Raises on any failure; nothing under _STAGING_DIR is
    ever renamed into place unless every manifest entry downloaded and
    verified clean first -- that verify-then-commit split is the entire
    safety story here, not a general rollback mechanism (acceptable given
    the accepted "worst case is USB recovery" risk tolerance for this
    project). Returns the newly-installed version integer on success.
    """
    release = _latest_release(session)
    latest = _parse_tag_version(release.get("tag_name"))

    manifest_asset = _find_asset(release, "manifest.json")
    if manifest_asset is None:
        raise RuntimeError(
            "release {} has no manifest.json asset".format(release.get("tag_name")))
    manifest = _fetch_json(
        session, manifest_asset["browser_download_url"], config.OTA_CHECK_TIMEOUT_MS)

    files = manifest.get("files") or []
    if not files:
        raise RuntimeError("manifest.json lists no files")

    _clear_staging()

    staged = []
    for entry in files:
        path = entry["path"]
        want_sha = entry["sha256"]
        want_size = entry["size"]

        asset = _find_asset(release, path)
        if asset is None:
            raise RuntimeError("release is missing an asset for manifest path {!r}".format(path))

        dest = _STAGING_DIR + "/" + path
        got_sha = _download_to(
            session, asset["browser_download_url"], dest, config.OTA_INSTALL_TIMEOUT_MS)
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
