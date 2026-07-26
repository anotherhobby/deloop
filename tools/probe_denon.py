"""
probe_denon.py — Host-side Denon AVR HTTP API discovery tool.

Run this on your Mac (not the device) after Phase 0 firmware setup.
It hits every HTTP endpoint that deloop will use and prints the raw
response so you can confirm the exact format before writing device code.

Usage:
    pip install requests   (or: pip install -e ".[dev]" from repo root)
    python tools/probe_denon.py --host 192.168.x.x

The script will step through each test and print PASS/FAIL with
the raw response body. Review the status XML output carefully —
confirm the volume value is a float like -36.0, not a Denon integer scale.
"""

import argparse
import sys
import time
import socket

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("ERROR: 'requests' not installed. Run: make bootstrap")
    sys.exit(1)


def get(host: str, port: int, path: str, timeout: float = 5.0,
        scheme: str = "https", allow_redirects: bool = True,
        origin: str | None = None) -> requests.Response:
    url = f"{scheme}://{host}:{port}{path}"
    headers = {"Origin": origin} if origin else {}
    print(f"  GET {url}" + (f"  [Origin: {origin}]" if origin else ""))
    return requests.get(url, timeout=timeout, verify=False,
                        headers=headers, allow_redirects=allow_redirects)


def diagnose(host: str) -> None:
    """Try several connection variants to find what the AVR accepts."""
    path = "/goform/formMainZone_MainZoneXmlStatus.xml"
    attempts = [
        ("HTTP port 80, no redirect",        80,   "http",  False, None),
        ("HTTP port 80, follow redirect",     80,   "http",  True,  None),
        ("HTTP port 8080",                    8080, "http",  True,  None),
        ("HTTPS port 443, no Origin",         443,  "https", True,  None),
        ("HTTPS port 443, http Origin",       443,  "https", True,  f"http://{host}"),
        ("HTTPS port 443, https Origin",      443,  "https", True,  f"https://{host}"),
    ]
    print("\n=== DIAGNOSE — trying all connection variants ===")
    for label, port, scheme, redir, origin in attempts:
        print(f"\n  [{label}]")
        try:
            resp = get(host, port, path, scheme=scheme,
                       allow_redirects=redir, origin=origin)
            print(f"  → HTTP {resp.status_code}  ({len(resp.text)} bytes)")
            if resp.status_code in (301, 302):
                print(f"  → Redirect Location: {resp.headers.get('Location', '(none)')}")
            if resp.status_code == 200 and "MasterVolume" in resp.text:
                print("  ✓ GOT VALID XML — use this config")
                vol_start = resp.text.find("<MasterVolume>")
                vol_end   = resp.text.find("</MasterVolume>", vol_start)
                print(f"  MasterVolume block: {resp.text[vol_start:vol_end+15]!r}")
        except Exception as exc:
            print(f"  → FAIL: {exc}")

    # Also try the newer AppCommand.xml API (used by denonavr / Home Assistant)
    print("\n  [HTTPS port 443, AppCommand.xml (newer API)]")
    try:
        body = "<?xml version='1.0' encoding='utf-8'?><tx><cmd id='1'>GetAllZonePowerStatus</cmd></tx>"
        url = f"https://{host}:443/goform/AppCommand.xml"
        print(f"  POST {url}")
        resp = requests.post(url, data=body, verify=False,
                             headers={"Content-Type": "text/xml"},
                             timeout=5.0)
        print(f"  → HTTP {resp.status_code}  ({len(resp.text)} bytes)")
        if resp.status_code == 200:
            print(f"  Body: {resp.text[:300]!r}")
    except Exception as exc:
        print(f"  → FAIL: {exc}")

    # Check root path for auth clues
    print("\n  [Root path — checking for auth/login requirements]")
    for scheme, port in [("http", 80), ("https", 443)]:
        try:
            resp = requests.get(f"{scheme}://{host}:{port}/", verify=False,
                                timeout=5.0, allow_redirects=False)
            loc = resp.headers.get("Location", "")
            www_auth = resp.headers.get("WWW-Authenticate", "")
            ct = resp.headers.get("Content-Type", "")
            print(f"  {scheme}:{port}  → {resp.status_code}  "
                  f"Location={loc!r}  Auth={www_auth!r}  CT={ct!r}")
        except Exception as exc:
            print(f"  {scheme}:{port}  → FAIL: {exc}")
    print("\n  Also try opening https://10.0.0.75 in your browser.")
    print("  If there's a login page, the API needs a session token.\n")


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def probe_status(host: str, port: int) -> None:
    section("STATUS POLL — formMainZone_MainZoneXmlStatus.xml")
    print(
        "This is the primary polling endpoint. Confirm volume is a float like -36.0\n"
    )
    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    print(f"  HTTP {resp.status_code}")
    print(resp.text)


def probe_mainzone(host: str, port: int) -> None:
    section("MAIN ZONE XML — formMainZone_MainZoneXml.xml")
    print("Alternative status endpoint (may have more fields)\n")
    resp = get(host, port, "/goform/formMainZone_MainZoneXml.xml")
    print(f"  HTTP {resp.status_code}")
    print(resp.text)


def probe_volume_commands(host: str, port: int) -> None:
    section("VOLUME UP / DOWN")
    print("Sends MVUP then MVDOWN. You should hear the AVR volume change.\n")

    resp = get(host, port, "/goform/formiPhoneAppDirect.xml?MVUP")
    print(f"  MVUP  → HTTP {resp.status_code}  body: {resp.text!r}")
    time.sleep(1)

    resp = get(host, port, "/goform/formiPhoneAppDirect.xml?MVDOWN")
    print(f"  MVDOWN → HTTP {resp.status_code}  body: {resp.text!r}")
    time.sleep(0.5)

    # Re-poll to confirm volume changed
    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    start = resp.text.find("<MasterVolume>")
    end = resp.text.find("</MasterVolume>", start)
    print(f"  MasterVolume block after change: {resp.text[start:end + 15]!r}")


def probe_set_volume(host: str, port: int, target_db: float) -> None:
    section(f"SET VOLUME — absolute to {target_db} dB")
    print(
        "Uses formiPhoneAppVolume.xml?1+{db}. Confirm AVR jumps to the target volume.\n"
    )
    path = f"/goform/formiPhoneAppVolume.xml?1+{target_db:.1f}"
    resp = get(host, port, path)
    print(f"  HTTP {resp.status_code}  body: {resp.text!r}")
    time.sleep(1)

    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    start = resp.text.find("<MasterVolume>")
    end = resp.text.find("</MasterVolume>", start)
    print(f"  MasterVolume block after set: {resp.text[start:end + 15]!r}")


def probe_mute(host: str, port: int) -> None:
    section("MUTE ON / OFF")
    print("Mutes then unmutes the AVR. Confirm the Mute field changes in status.\n")

    resp = get(host, port, "/goform/formiPhoneAppMute.xml?1+MuteOn")
    print(f"  MuteOn  → HTTP {resp.status_code}  body: {resp.text!r}")
    time.sleep(1)

    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    start = resp.text.find("<Mute>")
    end = resp.text.find("</Mute>", start)
    print(f"  Mute block after MuteOn: {resp.text[start:end + 7]!r}")

    resp = get(host, port, "/goform/formiPhoneAppMute.xml?1+MuteOff")
    print(f"  MuteOff → HTTP {resp.status_code}  body: {resp.text!r}")
    time.sleep(0.5)

    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    start = resp.text.find("<Mute>")
    end = resp.text.find("</Mute>", start)
    print(f"  Mute block after MuteOff: {resp.text[start:end + 7]!r}")


def probe_power(host: str, port: int) -> None:
    section("POWER STATUS (read only — not toggling to avoid disruption)")
    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    start = resp.text.find("<Power>")
    end = resp.text.find("</Power>", start)
    print(f"  Power block: {resp.text[start:end + 8]!r}")
    print()
    print(
        "  Power ON command  (not sent): GET /goform/formiPhoneAppPower.xml?1+PowerOn"
    )
    print(
        "  Power OFF command (not sent): GET /goform/formiPhoneAppPower.xml?1+PowerStandby"
    )


def probe_input(host: str, port: int) -> None:
    section("INPUT STATUS (read only — not changing input)")
    resp = get(host, port, "/goform/formMainZone_MainZoneXmlStatus.xml")
    start = resp.text.find("<InputFuncSelect>")
    end = resp.text.find("</InputFuncSelect>", start)
    print(f"  InputFuncSelect block: {resp.text[start:end + 18]!r}")
    print()
    print("  Input select command: GET /goform/formiPhoneAppDirect.xml?SI<INPUT_NAME>")
    print("  Example: ?SICD, ?SIBD, ?SINET/USB")


def probe_osd(host: str, port: int = 23, listen_seconds: float = 3.0) -> None:
    section("OSD PROBE — Return Onscreen Display Information List (NSA/NSE)")
    print("This attempts a TCP connection to the AVR control port and sends")
    print("the NSA/NSE requests to see what OSD lines the AVR returns.\n")

    try:
        print(f"  Connecting to {host}:{port} (TCP)")
        with socket.create_connection((host, port), timeout=5.0) as s:
            print("  Connected")
            s.settimeout(0.5)

            # Send both ASCII (NSA) and UTF-8 (NSE) variants; many AVRs
            # accept one or the other depending on source type.
            for cmd in ("NSA\r", "NSE\r"):
                try:
                    print(f"  Sending: {cmd.strip()!r}")
                    s.sendall(cmd.encode("utf-8"))
                except Exception as exc:
                    print(f"  → Send failed: {exc}")

            print(f"  Listening for responses for {listen_seconds} seconds...")
            end = time.time() + listen_seconds
            buf = b""
            while time.time() < end:
                try:
                    data = s.recv(4096)
                    if not data:
                        break
                    buf += data
                    # Split on CR which the protocol uses
                    parts = buf.split(b'\r')
                    for part in parts[:-1]:
                        text = part.decode("utf-8", errors="replace")
                        print(f"  RECV: {text!r}")
                    buf = parts[-1]
                except socket.timeout:
                    continue

            if buf:
                rem = buf.decode("utf-8", errors="replace")
                print(f"  RECV (partial): {rem!r}")
            print("  Done listening")
    except Exception as exc:
        print(f"  → FAIL: could not probe OSD on {host}:{port}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Denon AVR HTTP API")
    parser.add_argument("--host", required=True, help="AVR IP address or hostname")
    parser.add_argument("--port", type=int, default=80, help="AVR HTTP port (default 80)")
    parser.add_argument(
        "--target-db",
        type=float,
        default=-40.0,
        help="Target volume (dB) for set-volume test (default -40.0)",
    )
    parser.add_argument(
        "--skip-write",
        action="store_true",
        help="Skip volume change and mute tests (read-only mode)",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Try multiple connection variants to find what the AVR accepts",
    )
    parser.add_argument(
        "--osd",
        action="store_true",
        help="Probe OSD via NSA/NSE on AVR control port (default port 23)",
    )
    parser.add_argument(
        "--osd-port",
        type=int,
        default=23,
        help="TCP port for OSD control commands (default 23)",
    )
    args = parser.parse_args()

    if args.diagnose:
        diagnose(args.host)
        return

    print(f"\nProbing Denon AVR at {args.host}:{args.port}")
    print("Make sure the AVR is powered on and on the network.\n")

    try:
        probe_status(args.host, args.port)
        probe_mainzone(args.host, args.port)

        if not args.skip_write:
            probe_volume_commands(args.host, args.port)
            probe_set_volume(args.host, args.port, args.target_db)
            probe_mute(args.host, args.port)

        probe_power(args.host, args.port)
        probe_input(args.host, args.port)
        if args.osd:
            probe_osd(args.host, args.osd_port)

    except requests.exceptions.ConnectionError as exc:
        print(f"\nFAIL: Could not connect to {args.host}:{args.port}")
        print(f"  {exc}")
        print("\nCheck: Is the AVR powered on? Is the IP correct? Is port 80 open?")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"\nFAIL: Request timed out to {args.host}:{args.port}")
        sys.exit(1)

    print("\n\nDone. Review the output above before writing device code.")
    print("Key things to confirm:")
    print("  1. MasterVolume/value is a float like -36.0 (not a Denon integer)")
    print("  2. Mute/value is 'on' or 'off' (lowercase)")
    print("  3. Power/value is 'ON' or 'OFF' (uppercase)")
    print("  4. HTTP 200 for all command GETs (some return empty body — that's OK)")


if __name__ == "__main__":
    main()
