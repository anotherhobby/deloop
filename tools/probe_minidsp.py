"""
probe_minidsp.py — Host-side minidsp-rs HTTP API discovery tool.

Run this on your Mac (not the device) against a live minidsp-rs daemon
(https://github.com/mrene/minidsp-rs) to confirm the exact JSON shapes
deloop's src/minidsp.py driver expects, before flashing firmware.

Usage:
    pip install requests   (or: pip install -e ".[dev]" from repo root)
    python tools/probe_minidsp.py --host 192.168.x.x

minidsp-rs must already be running on that host with the MiniDSP attached
over USB, and its HTTP server must be reachable on the network -- its own
default (127.0.0.1:5380) only accepts local connections, so its config
needs `bind_address = "0.0.0.0:5380"` (or similar) first.

The script prints each response as raw JSON so you can compare it against
the shapes documented at the top of src/minidsp.py. By default it only
reads state; pass without --skip-write to also nudge volume/mute/preset
and confirm the device actually reacts.
"""

import argparse
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: make bootstrap")
    sys.exit(1)


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def get(base: str, path: str, timeout: float = 5.0) -> requests.Response:
    url = f"{base}{path}"
    print(f"  GET {url}")
    return requests.get(url, timeout=timeout)


def post(base: str, path: str, body: dict, timeout: float = 5.0) -> requests.Response:
    url = f"{base}{path}"
    print(f"  POST {url}  body={body!r}")
    return requests.post(url, json=body, timeout=timeout)


def probe_devices(base: str) -> dict:
    section("GET /devices — device list + hw_id/dsp_version")
    resp = get(base, "/devices")
    print(f"  HTTP {resp.status_code}")
    devices = resp.json()
    print(f"  {devices!r}")
    if not devices:
        print("\n  No devices reported -- is minidsp-rs running with the unit attached?")
        return {}
    info = devices[0].get("version") or {}
    print(f"\n  hw_id={info.get('hw_id')}  dsp_version={info.get('dsp_version')}  "
          f"fw={info.get('fw_major')}.{info.get('fw_minor')}")
    print("  Compare hw_id/dsp_version against minidsp.py's _SOURCE_MAP to confirm")
    print("  the source (input) list deloop will offer matches your unit.")
    return info


def probe_status(base: str, index: int) -> dict:
    section(f"GET /devices/{index} — StatusSummary (master + levels)")
    resp = get(base, f"/devices/{index}")
    print(f"  HTTP {resp.status_code}")
    body = resp.json()
    print(f"  {body!r}")
    master = body.get("master", {})
    print(f"\n  Key things to confirm:")
    print(f"    volume : {master.get('volume')!r}  (float, range [-127, 0])")
    print(f"    mute   : {master.get('mute')!r}  (bool)")
    print(f"    source : {master.get('source')!r}  (lowercase string)")
    print(f"    preset : {master.get('preset')!r}  (int, 0-based config slot)")
    print(f"    dirac  : {master.get('dirac')!r}  (bool if this unit supports Dirac Live, else absent)")
    return master


def probe_volume(base: str, index: int, target_db: float) -> None:
    section(f"SET VOLUME — absolute to {target_db} dB")
    resp = post(base, f"/devices/{index}", {"volume": target_db})
    print(f"  HTTP {resp.status_code}  body: {resp.text!r}")

    resp = get(base, f"/devices/{index}")
    print(f"  Master status after set: {resp.json().get('master')!r}")


def probe_mute(base: str, index: int) -> None:
    section("MUTE ON / OFF")
    resp = post(base, f"/devices/{index}", {"mute": True})
    print(f"  HTTP {resp.status_code}  body: {resp.text!r}")
    resp = get(base, f"/devices/{index}")
    print(f"  Master status after mute=true: {resp.json().get('master')!r}")

    resp = post(base, f"/devices/{index}", {"mute": False})
    print(f"  HTTP {resp.status_code}  body: {resp.text!r}")
    resp = get(base, f"/devices/{index}")
    print(f"  Master status after mute=false: {resp.json().get('master')!r}")


def probe_preset(base: str, index: int, preset: int) -> None:
    section(f"SET PRESET — config slot {preset}")
    print("  Note: this actually switches the DSP's active config, same as pressing")
    print("  the button on a physical remote/app -- expect audio to briefly interrupt.\n")
    resp = post(base, f"/devices/{index}", {"preset": preset})
    print(f"  HTTP {resp.status_code}  body: {resp.text!r}")
    resp = get(base, f"/devices/{index}")
    print(f"  Master status after set: {resp.json().get('master')!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a minidsp-rs HTTP API")
    parser.add_argument("--host", required=True, help="minidsp-rs daemon host/IP")
    parser.add_argument("--port", type=int, default=5380, help="minidsp-rs HTTP port (default 5380)")
    parser.add_argument("--device-index", type=int, default=0, help="device index (default 0)")
    parser.add_argument("--target-db", type=float, default=-40.0,
                         help="target volume (dB) for the volume test (default -40.0)")
    parser.add_argument("--preset", type=int, default=None,
                         help="if set, also test switching to this config slot (0-based)")
    parser.add_argument("--skip-write", action="store_true",
                         help="skip volume/mute/preset changes (read-only mode)")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    print(f"\nProbing minidsp-rs at {base}")

    try:
        probe_devices(base)
        master = probe_status(base, args.device_index)

        if not args.skip_write:
            probe_volume(base, args.device_index, args.target_db)
            probe_mute(base, args.device_index)
            if args.preset is not None:
                probe_preset(base, args.device_index, args.preset)

        # Note: this checks the live "dirac" field, not dsp_version -- the
        # upstream DeviceInfo::supports_dirac() version check predates the
        # Flex family and doesn't cover it. The live field is what
        # src/minidsp.py's get_presets() actually keys off of.
        if master.get("dirac") is not None:
            print(f"\n  This unit reports Dirac Live support (dirac={master['dirac']!r}).")
            print("  deloop's minidsp.py will use it as the Presets menu instead of")
            print("  config-slot switching -- see get_presets() there.")
        else:
            print("\n  This unit's status has no \"dirac\" field -- deloop's Presets menu")
            print("  will fall back to config-slot switching (MINIDSP_PRESET_COUNT).")

    except requests.exceptions.ConnectionError as exc:
        print(f"\nFAIL: Could not connect to {base}")
        print(f"  {exc}")
        print("\nCheck: is minidsp-rs running? Is bind_address set to something LAN-reachable")
        print("(its default 127.0.0.1:5380 only accepts local connections)?")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"\nFAIL: Request timed out to {base}")
        sys.exit(1)

    print("\n\nDone. Review the output above, then set DEVICE_DRIVER = \"minidsp\" and")
    print("the MINIDSP_* keys in src/settings.toml before deploying.")


if __name__ == "__main__":
    main()
