"""
probe_wiim.py — Host-side WiiM / LinkPlay HTTP API discovery tool.

Run this on your Mac (not the device) against a live WiiM streamer to
confirm the exact response shapes deloop's src/wiim.py driver expects,
before flashing firmware.

Usage:
    pip install requests   (or: pip install -e ".[dev]" from repo root)
    python tools/probe_wiim.py --host 10.0.1.75
    python tools/probe_wiim.py --host 10.0.1.75 --skip-write

The API is HTTPS-only (plain HTTP on port 80 returns nothing at all) and
serves a self-signed certificate, so this script passes verify=False and
silences the resulting urllib3 warning. Note that the device driver does
NOT do the equivalent: CircuitPython's ssl module has no way to skip
verification, so src/wiim.py pins LinkPlay's certificate instead -- see its
module docstring. That difference is deliberate, not an oversight.

The script prints each response so you can compare it against the shapes
documented at the top of src/wiim.py. By default it only reads state; pass
without --skip-write to also nudge volume/mute/source and confirm the
device actually reacts (each write is restored afterwards).
"""

import argparse
import json
import sys
import time

try:
    import requests
    import urllib3
except ImportError:
    print("ERROR: 'requests' not installed. Run: make bootstrap")
    sys.exit(1)

# The streamer's cert is self-signed -- expected, not a misconfiguration.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Mode codes worth calling out in the status dump -- the four confirmed
# switchable physical/network sources on a WiiM Pro. Anything else is a
# streaming service or idle state; see _MODE_NAME in src/wiim.py.
_MODE_TO_INPUT = {"10": "wifi", "40": "line-in", "41": "bluetooth", "43": "optical"}


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def command(base: str, cmd: str, timeout: float = 5.0) -> str:
    """Send one httpapi.asp command and return the raw response text."""
    url = f"{base}/httpapi.asp?command={cmd}"
    print(f"  GET {url}")
    resp = requests.get(url, timeout=timeout, verify=False)
    print(f"  HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.text


def command_json(base: str, cmd: str) -> dict:
    text = command(base, cmd)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  (non-JSON response: {text[:200]!r})")
        return {}


def probe_device_info(base: str) -> None:
    section("getStatusEx — device identity")
    info = command_json(base, "getStatusEx")
    for key in ("DeviceName", "project", "firmware", "hardware", "MAC", "eth0", "preset_key"):
        print(f"  {key:12s}: {info.get(key)!r}")


def probe_player_status(base: str) -> dict:
    section("getPlayerStatus — what src/wiim.py's get_status() reads")
    status = command_json(base, "getPlayerStatus")
    mode = status.get("mode")
    print(f"  vol    : {status.get('vol')!r}  (plain 0-100 int, not a fraction)")
    print(f"  mute   : {status.get('mute')!r}")
    print(f"  status : {status.get('status')!r}  -> media_state "
          f"{ {'play': 'playing', 'pause': 'paused'}.get(status.get('status'), '') !r}")
    print(f"  mode   : {mode!r}  -> source key {_MODE_TO_INPUT.get(mode, '(streaming/idle)')!r}")
    return status


def probe_presets(base: str) -> None:
    section("getPresetInfo — WiiM-app favorites")
    info = command_json(base, "getPresetInfo")
    preset_list = info.get("preset_list") or []
    print(f"  preset_num  : {info.get('preset_num')!r}")
    print(f"  preset_list : {len(preset_list)} entries")
    if not preset_list:
        print("  -> empty: wiim.py leaves CAPS['presets'] False and the Preset")
        print("     menu entry never appears. Add favorites in the WiiM app to test.")
        return
    for i, preset in enumerate(preset_list, start=1):
        print(f"    [{i}] {preset!r}")
    print("  -> confirm the name/source field names above match what")
    print("     src/wiim.py's get_presets() reads (they were unverified at")
    print("     the time it was written -- no favorites existed yet).")


def probe_volume(base: str, original: str, target: int) -> None:
    section(f"SET VOLUME — to {target}, then back to {original}")
    print(f"  -> {command(base, f'setPlayerCmd:vol:{target}')!r}")
    time.sleep(1.0)
    print(f"  readback vol: {command_json(base, 'getPlayerStatus').get('vol')!r}")
    print(f"  -> {command(base, f'setPlayerCmd:vol:{original}')!r}")


def probe_mute(base: str, original: str) -> None:
    section("MUTE ON / OFF")
    for value in ("1", "0"):
        print(f"  -> {command(base, f'setPlayerCmd:mute:{value}')!r}")
        time.sleep(1.0)
        print(f"  readback mute: {command_json(base, 'getPlayerStatus').get('mute')!r}")
    if original != "0":
        print(f"  restoring original mute state: {command(base, f'setPlayerCmd:mute:{original}')!r}")


def probe_source(base: str, source: str, original_mode: str) -> None:
    section(f"SWITCH SOURCE — {source!r}")
    print(f"  -> {command(base, f'setPlayerCmd:switchmode:{source}')!r}")
    time.sleep(1.5)
    status = command_json(base, "getPlayerStatus")
    mode = status.get("mode")
    print(f"  resulting mode: {mode!r}  status: {status.get('status')!r}")
    if mode in ("0", None) or status.get("status") == "none":
        print("  -> this source is NOT supported on this unit (mode stayed 0).")
        print("     Leave it out of WIIM_INPUTS in settings.toml.")
    back = _MODE_TO_INPUT.get(original_mode)
    if back:
        print(f"  restoring original source {back!r}: "
              f"{command(base, f'setPlayerCmd:switchmode:{back}')!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a WiiM/LinkPlay streamer")
    parser.add_argument("--host", required=True, help="Streamer IP/hostname (no scheme)")
    parser.add_argument("--target-vol", type=int, default=30,
                        help="volume to set during the write test (default 30)")
    parser.add_argument("--source", default=None,
                        help="if set, also test switching to this source key "
                             "(wifi, bluetooth, line-in, optical, ...)")
    parser.add_argument("--skip-write", action="store_true",
                        help="skip volume/mute/source changes (read-only mode)")
    args = parser.parse_args()

    base = f"https://{args.host}"
    print(f"\nProbing WiiM at {base} (TLS verification disabled -- self-signed cert)")

    try:
        probe_device_info(base)
        status = probe_player_status(base)
        probe_presets(base)

        if not args.skip_write:
            probe_volume(base, status.get("vol", "30"), args.target_vol)
            probe_mute(base, status.get("mute", "0"))
            if args.source:
                probe_source(base, args.source, status.get("mode", ""))

    except requests.exceptions.SSLError as exc:
        print(f"\nFAIL: TLS error talking to {base}")
        print(f"  {exc}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as exc:
        print(f"\nFAIL: Could not connect to {base}")
        print("  The API is HTTPS-only -- port 80 returns nothing at all.")
        print(f"  {exc}")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"\nFAIL: Request timed out to {base}")
        sys.exit(1)
    except requests.exceptions.HTTPError as exc:
        print(f"\nFAIL: HTTP error from {base}")
        print(f"  {exc}")
        sys.exit(1)

    print("\n\nDone. Review the output above, then set DEVICE_DRIVER = \"wiim\" and")
    print("WIIM_HOST (plus WIIM_INPUTS, if your unit's sources differ from the")
    print("default) in src/settings.toml before deploying.")


if __name__ == "__main__":
    main()
