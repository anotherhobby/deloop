# ota_regression_check.py -- on-device regression check for src/ota.py's
# real install sequence (latest.json fetch -> manifest fetch -> every real
# asset downloaded+verified).
#
# This is NOT a host-side test (see tools/probe_ota.py for that -- it only
# checks the CI-side GitHub Release's response *shapes* from a Mac, never
# touches this project's actual Fetcher/session code). This script runs ON
# THE DEVICE, using the real config.WIFI_SSID/PASS and the real
# ota.py/ota_boot.py already deployed there, exercising the exact same
# call sequence ota.apply() uses -- latest-version fetch, manifest fetch,
# then every real asset in that release downloaded for real -- but stops
# short of ota.apply()'s final os.rename() commit step, so a full pass
# never overwrites the files actually running on the device.
#
# Usage:
#   make shell   # or otherwise get a REPL, then Ctrl-] to leave it and:
#   ./.venv/bin/python -m mpremote connect auto run tools/ota_regression_check.py
#
# Run this after any change to ota.py/ota_boot.py's Fetcher, session, or
# apply()/check_latest_version() internals -- a clean pass here is not a
# substitute for testing the real Check Now / Install Update menu items
# end to end at least once, but it's far faster to iterate on than a full
# manual Install Update cycle (including the device UI/reload).

import gc
import os

import wifi
import storage
import config
import ota
import ota_boot


def _log(msg):
    print("[ota_regression_check]", msg)


def main():
    if not config.WIFI_SSID:
        _log("FAIL: no WIFI_SSID configured -- can't run against real S3")
        return False

    if not wifi.radio.connected:
        wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)

    gc.collect()
    _log("starting, free mem: {}".format(gc.mem_free()))

    fetcher = ota._Fetcher(ota_boot._new_session)

    latest = ota._latest_version(fetcher)
    _log("latest.json fetched, version {}, free mem: {}".format(latest, gc.mem_free()))

    manifest_url = ota._s3_url(latest, "manifest.json")
    manifest = fetcher.get_json(manifest_url, config.OTA_CHECK_TIMEOUT_MS)
    files = manifest.get("files") or []
    if not files:
        _log("FAIL: manifest.json lists no files")
        return False
    _log("manifest fetched, {} files, free mem: {}".format(len(files), gc.mem_free()))

    # Real Install Update always runs inside a remount("/", readonly=False)
    # window (ota_boot.run() does this itself before calling apply()) --
    # this script calls the same pieces directly, so it has to do the same.
    storage.remount("/", readonly=False)
    try:
        ota._clear_staging()

        for i, entry in enumerate(files):
            path = entry["path"]
            want_sha = entry["sha256"]
            want_size = entry["size"]

            gc.collect()
            asset_url = ota._s3_url(latest, path)
            dest = ota._STAGING_DIR + "/" + path
            got_sha = fetcher.download(asset_url, dest, config.OTA_INSTALL_TIMEOUT_MS)
            got_size = os.stat(dest)[6]
            if got_sha != want_sha or got_size != want_size:
                _log("FAIL: verify mismatch for {}: got sha256={} size={} (want {} / {})".format(
                    path, got_sha, got_size, want_sha, want_size))
                return False
            _log("{}/{} {} OK, free mem: {}".format(i + 1, len(files), path, gc.mem_free()))

        ota._clear_staging()   # never committed -- see module docstring
        _log("PASS: all {} files downloaded and verified".format(len(files)))
        return True
    finally:
        storage.remount("/", readonly=True)


if __name__ == "__main__":
    ok = main()
    _log("RESULT: {}".format("PASS" if ok else "FAIL"))
