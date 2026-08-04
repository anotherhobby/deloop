"""
avr_health_check.py -- Host-side AVR latency/reliability check, independent
of the M5 Dial hardware entirely.

Why this exists: across sessions, "the device is acting up" investigations
have repeatedly floated the AVR as a suspect (it's the other end of every
network call deloop makes), but no session has ever actually turned up
evidence the AVR itself is slow or flaky -- every real root cause found so
far (blocking HTTP calls starving the touch-poll loop, a CircuitPython
socket quirk, an over-eager gc.collect(), WiFi-connect-time memory
pressure) has been on the ESP32/deloop side. This script hits the AVR
directly from a Mac -- no ESP32, no CircuitPython, no deloop code at all --
so a bad run here is real evidence the AVR is the problem, and a clean run
is real evidence it isn't, instead of guessing from device-side symptoms.

Exercises the exact two endpoints deloop itself depends on:
  - POST /goform/AppCommand.xml (port AVR_PORT, default 8080) -- the
    status poll body, same request denon.py's start_status_poll() sends.
  - GET /ajax/audio/get_config?type=14 (port AVR_PORT_UI, 11080) -- the
    endpoint denon.py's load_source_list()/load_input_names() use; this
    specific one has shown repeated ETIMEDOUT failures during on-device
    testing, making it the natural first thing to verify independently.

Usage:
    pip install requests   (or: pip install -e ".[dev]" from repo root)
    python tools/avr_health_check.py --host 10.0.0.75
    python tools/avr_health_check.py --host 10.0.0.75 --count 30 --timeout 2
"""

import argparse
import statistics
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: make bootstrap")
    sys.exit(1)


_STATUS_BODY = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    "<tx>"
    '<cmd id="1">GetAllZonePowerStatus</cmd>'
    '<cmd id="1">GetAllZoneSource</cmd>'
    '<cmd id="1">GetAllZoneVolume</cmd>'
    '<cmd id="1">GetAllZoneMuteStatus</cmd>'
    "</tx>"
)


def _run(label, request_fn, count, timeout):
    print(f"\n=== {label} ===")
    latencies_ms = []
    failures = []
    for i in range(count):
        t0 = time.monotonic()
        try:
            resp = request_fn(timeout)
            elapsed_ms = (time.monotonic() - t0) * 1000
            ok = resp.status_code == 200 and len(resp.text) > 0
            latencies_ms.append(elapsed_ms)
            mark = "." if ok else "F"
            if not ok:
                failures.append(f"#{i}: HTTP {resp.status_code}, {len(resp.text)} bytes")
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            mark = "F"
            failures.append(f"#{i}: {type(e).__name__}: {e} (after {elapsed_ms:.0f}ms)")
        print(mark, end="", flush=True)
    print()

    n_ok = len(latencies_ms)
    print(f"  {n_ok}/{count} succeeded")
    if latencies_ms:
        latencies_ms.sort()
        p95_idx = min(len(latencies_ms) - 1, int(len(latencies_ms) * 0.95))
        print(f"  latency ms: min={latencies_ms[0]:.0f} "
              f"avg={statistics.mean(latencies_ms):.0f} "
              f"p95={latencies_ms[p95_idx]:.0f} "
              f"max={latencies_ms[-1]:.0f}")
    for f in failures:
        print(f"  FAIL {f}")
    return n_ok, count


def main() -> None:
    parser = argparse.ArgumentParser(description="AVR latency/reliability check (host-side, no device involved)")
    parser.add_argument("--host", required=True, help="AVR IP address or hostname")
    parser.add_argument("--port", type=int, default=8080, help="AVR status-poll port (default 8080, matches config.AVR_PORT)")
    parser.add_argument("--port-ui", type=int, default=11080, help="AVR web UI port (default 11080, matches config.AVR_PORT_UI)")
    parser.add_argument("--count", type=int, default=20, help="requests per endpoint (default 20)")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout, seconds (default 5)")
    args = parser.parse_args()

    print(f"AVR health check against {args.host} (status port {args.port}, UI port {args.port_ui})")
    print(f"{args.count} requests per endpoint, {args.timeout}s timeout")

    status_url = f"http://{args.host}:{args.port}/goform/AppCommand.xml"
    config_url = f"http://{args.host}:{args.port_ui}/ajax/audio/get_config?type=14"

    results = []
    results.append(_run(
        f"Status poll: POST {status_url}",
        lambda timeout: requests.post(status_url, data=_STATUS_BODY, timeout=timeout),
        args.count, args.timeout))
    results.append(_run(
        f"get_config (the historically flaky one): GET {config_url}",
        lambda timeout: requests.get(config_url, timeout=args.timeout),
        args.count, args.timeout))

    total_ok = sum(ok for ok, _ in results)
    total = sum(n for _, n in results)
    print(f"\n=== Verdict ===")
    if total_ok == total:
        print(f"{total_ok}/{total} requests succeeded -- AVR is responding reliably. "
              "If deloop is still acting up, look at the ESP32/WiFi/deloop side, not the AVR.")
        sys.exit(0)
    else:
        print(f"{total_ok}/{total} requests succeeded -- the AVR itself failed or was slow to "
              "respond here, independent of any deloop/ESP32 code. This is real evidence "
              "pointing at the AVR/network path, not deloop.")
        sys.exit(1)


if __name__ == "__main__":
    main()
