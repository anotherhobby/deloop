# wifi_pin_check.py -- on-device proof that BSSID pinning works and that the
# access point is the variable behind the "associates fine, passes no
# traffic" failure.
#
# Background (2026-08-04): on a mesh SSID the radio picks a node itself, and
# roughly half of cold boots landed on one from which the device could reach
# nothing at all -- not the target, not even the gateway -- permanently,
# until power-cycled, despite a correct DHCP lease. Successes were always on
# one BSSID and failures always on another. Before building anything that
# learns and remembers a good AP, this answers two prerequisites:
#
#   1. Can we reliably pin association to a chosen BSSID on this stack?
#   2. Does pinning to the good one reliably give working TCP -- and does
#      pinning to the bad one reliably fail? (If the bad AP also works here,
#      the AP is NOT the variable and the whole theory is wrong.)
#
# Each trial forces a genuinely fresh association (radio down, radio up,
# connect) rather than reusing whatever is already up, then does a real TCP
# round trip to the target on both ports it actually uses. Run via
# `mpremote run`, which iterates in seconds instead of a full deploy.
#
# Usage:
#   ./.venv/bin/python -m mpremote connect auto run tools/wifi_pin_check.py

import time
import gc
import wifi
import socketpool

import config

TRIALS = 4          # per AP
TCP_TIMEOUT_S = 3.0

# From the 2026-08-04 boot logs: every success was on ...b8f2, every failure
# on ...b7ce. None = let the radio choose, i.e. current shipping behaviour.
GOOD = b"\x62\xe9\x31\xa3\xb8\xf2"
BAD  = b"\x62\xe9\x31\xa3\xb7\xce"

_STATUS_BODY = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    "<tx>"
    '<cmd id="1">GetAllZonePowerStatus</cmd>'
    "</tx>"
)


def _hex(b):
    return "".join("%02x" % x for x in b) if b else "?"


def _associate(bssid):
    """Force a fresh association, optionally pinned. Returns elapsed seconds."""
    t0 = time.monotonic()
    wifi.radio.enabled = False
    time.sleep(0.5)
    wifi.radio.enabled = True
    try:
        wifi.radio.power_management = wifi.PowerManagement.NONE
    except Exception as e:
        print("      power_management NONE failed:", e)
    if bssid is None:
        wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS)
    else:
        wifi.radio.connect(config.WIFI_SSID, config.WIFI_PASS, bssid=bssid)
    return time.monotonic() - t0


def _tcp_probe(pool, host, port, body=None):
    """One real TCP round trip. Returns (ok, ms, note)."""
    t0 = time.monotonic()
    s = None
    try:
        s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        s.settimeout(TCP_TIMEOUT_S)
        s.connect((host, port))
        if body is None:
            req = ("GET /ajax/globals/get_config?type=7 HTTP/1.0\r\n"
                   "Host: %s:%d\r\nConnection: close\r\n\r\n" % (host, port))
        else:
            req = ("POST /goform/AppCommand.xml HTTP/1.0\r\n"
                   "Host: %s:%d\r\nContent-Length: %d\r\n"
                   "Connection: close\r\n\r\n%s" % (host, port, len(body), body))
        s.send(req.encode())
        buf = bytearray(256)
        n = s.recv_into(buf)
        ms = (time.monotonic() - t0) * 1000
        head = bytes(buf[:min(n, 16)]).decode("utf-8", "ignore") if n else ""
        return (n > 0, ms, head.split("\r")[0])
    except Exception as e:
        return (False, (time.monotonic() - t0) * 1000, "%s %s" % (type(e).__name__, e))
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def _trial(label, bssid):
    gc.collect()
    try:
        assoc_s = _associate(bssid)
    except Exception as e:
        print("    associate FAILED: %s %s" % (type(e).__name__, e))
        return False

    ap = wifi.radio.ap_info
    got = bytes(ap.bssid) if ap is not None else None
    pinned_ok = (bssid is None) or (got == bssid)
    print("    assoc %.1fs  ip=%s  bssid=%s%s  rssi=%s ch=%s" % (
        assoc_s, wifi.radio.ipv4_address, _hex(got),
        "" if pinned_ok else "  <-- NOT THE REQUESTED BSSID",
        ap.rssi if ap else "?", ap.channel if ap else "?"))

    # Gateway reachability: exercises the association without the target.
    gw = wifi.radio.ipv4_gateway
    try:
        gc.collect()
        rtt = wifi.radio.ping(gw, timeout=1.0)
        print("    gateway %s: %s" % (gw, "%.0fms" % (rtt * 1000) if rtt else "NO REPLY"))
    except Exception as e:
        print("    gateway %s: probe unavailable (%s)" % (gw, e))

    pool = socketpool.SocketPool(wifi.radio)
    ok8, ms8, note8 = _tcp_probe(pool, config.AVR_HOST, config.AVR_PORT, _STATUS_BODY)
    print("    tcp %s:%d  %s  %.0fms  %s" % (
        config.AVR_HOST, config.AVR_PORT, "OK " if ok8 else "FAIL", ms8, note8))
    ok11, ms11, note11 = _tcp_probe(pool, config.AVR_HOST, config.AVR_PORT_UI)
    print("    tcp %s:%d %s  %.0fms  %s" % (
        config.AVR_HOST, config.AVR_PORT_UI, "OK " if ok11 else "FAIL", ms11, note11))

    return pinned_ok and ok8 and ok11


def main():
    print("wifi pin check -- ssid=%s target=%s\n" % (config.WIFI_SSID, config.AVR_HOST))
    results = []
    for label, bssid in (("GOOD  (pinned %s)" % _hex(GOOD), GOOD),
                         ("BAD   (pinned %s)" % _hex(BAD),  BAD),
                         ("AUTO  (radio chooses)",          None)):
        print("=== %s ===" % label)
        wins = 0
        for i in range(TRIALS):
            print("  trial %d/%d" % (i + 1, TRIALS))
            if _trial(label, bssid):
                wins += 1
        print("  --> %d/%d fully OK\n" % (wins, TRIALS))
        results.append((label, wins))

    print("SUMMARY")
    for label, wins in results:
        print("  %-34s %d/%d" % (label, wins, TRIALS))
    print("\nIf GOOD is ~%d/%d and BAD is ~0/%d, pinning works and the AP is"
          " the variable." % (TRIALS, TRIALS, TRIALS))


main()
