# reset_tls_check.py -- does microcontroller.reset() still break the first TLS send?
#
# docs/ota.md carries a hard rule: never call microcontroller.reset() anywhere
# in the OTA flow, because a genuine hardware reset breaks the first
# post-handshake TLS send() on the very next boot's first network attempt.
#
# That rule was established while a poorly-placed second AP overlapped the
# primary -- the same RF environment now believed to be behind the long-running
# cold-boot networking fault. A first TLS send failing right after a reset is
# entirely consistent with a marginal link rather than with anything
# reset-specific, so the finding is worth re-running in the clean environment.
#
# This does ONE TLS request against the real OTA endpoint and reports exactly
# where it fails if it does: handshake, first send, or body read. Denon is
# plain HTTP, so the app's own traffic never gets there first -- whatever this
# does is genuinely the device's first TLS of the boot.
#
# Usage -- the sequence matters:
#   1. ./.venv/bin/python -m mpremote connect auto run tools/reset_tls_check.py
#      (control: no preceding hard reset)
#   2. ./.venv/bin/python -m mpremote connect auto exec \
#        "import microcontroller; microcontroller.reset()"
#   3. wait ~15s for the app to boot and associate, then run this again
#      (the actual test: first TLS after a hard reset)

import gc
import time

import wifi
import socketpool
import ssl
import adafruit_requests

import config

URL = config.OTA_S3_BASE + "/latest.json"


def main():
    print("\nreset/TLS check")
    print("  url: %s" % URL)

    if not wifi.radio.connected:
        print("  associating...")
        wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)
    print("  ip: %s  rssi: %s" % (
        wifi.radio.ipv4_address,
        wifi.radio.ap_info.rssi if wifi.radio.ap_info else "?"))

    gc.collect()
    pool = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool, ssl.create_default_context())

    t0 = time.monotonic()
    resp = None
    try:
        # adafruit_requests does connect + handshake + send inside get(), so a
        # failure here IS the thing the rule is about. The error type tells
        # them apart: OSError/timeout on send vs an ssl error on handshake.
        resp = session.get(URL, timeout=20)
        body = resp.text
        dt = (time.monotonic() - t0) * 1000
        print("  OK  %d  %s  (%.0f ms)" % (resp.status_code, body.strip(), dt))
        print("\n  RESULT: first TLS request succeeded.")
    except Exception as e:
        dt = (time.monotonic() - t0) * 1000
        print("  FAIL %s: %s  (%.0f ms)" % (type(e).__name__, e, dt))
        print("\n  RESULT: first TLS request failed -- the rule still holds.")
    finally:
        # Hard rule: every adafruit_requests response must be closed, the
        # socket pool is only ~4 deep.
        if resp is not None:
            resp.close()


main()
