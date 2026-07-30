"""
probe_camilladsp.py — Host-side CamillaDSP WebSocket API discovery tool.

Run this on your Mac (not the device) against a live CamillaDSP process to
confirm the exact command/reply shapes deloop's src/camilladsp.py driver
expects, before flashing firmware.

Usage:
    pip install websocket-client   (or: pip install -e ".[dev]" from repo root)
    python tools/probe_camilladsp.py --host 192.168.x.x
    python tools/probe_camilladsp.py --host 192.168.x.x --port 1234 --skip-write

This uses the `websocket-client` package (a known-good WebSocket
implementation) rather than src/camilladsp.py's own hand-rolled client, so a
failure here means "check CAMILLADSP_HOST/PORT/that CamillaDSP is running",
not "check the CircuitPython driver's WS framing" -- this script does not
exercise that hand-rolled client at all. If this script works but the device
doesn't, the bug is almost certainly in camilladsp.py's WS handshake/framing,
not in CamillaDSP itself or your network setup. (Both were confirmed working
together on real M5 Dial hardware on 2026-07-30 -- this script is still the
right first step against any *new* CamillaDSP instance, since it isolates
"is the process/network/API reachable at all" from "does the device's own
client work," the same way it did the first time.)

The command/reply shapes below were originally confirmed from
local/pycamilladsp's test fixtures (the vendor's own recorded server
responses), since reconfirmed live against a real instance.

By default it only reads state; pass without --skip-write to also nudge
volume/mute (restored afterward) and, with --preset-path, test a config
file switch (also restored afterward, but note this is a full pipeline
reload -- expect audio to briefly interrupt).
"""

import argparse
import json
import sys

try:
    import websocket
except ImportError:
    print("ERROR: 'websocket-client' not installed. Run: make bootstrap")
    sys.exit(1)


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def query(ws, command: str, arg=None):
    """Send one command and return its parsed "value" (per the confirmed
    {command: {"result": ..., "value": ...}} envelope), raising on an
    "Error" result."""
    msg = json.dumps({command: arg}) if arg is not None else json.dumps(command)
    print(f"  -> {msg}")
    ws.send(msg)
    raw = ws.recv()
    print(f"  <- {raw}")
    reply = json.loads(raw)
    body = reply.get(command)
    if body is None:
        raise RuntimeError(f"unexpected reply shape: {raw!r}")
    result = body.get("result")
    if result != "Ok":
        raise RuntimeError(f"{command} failed: result={result!r} value={body.get('value')!r}")
    return body.get("value")


def probe_version(ws) -> None:
    section("GetVersion — liveness check")
    print(f"  version: {query(ws, 'GetVersion')!r}")


def probe_status(ws) -> dict:
    section("GetVolume / GetMute / GetConfigFilePath — what src/camilladsp.py's get_status()/get_presets() read")
    volume = query(ws, "GetVolume")
    mute = query(ws, "GetMute")
    config_path = query(ws, "GetConfigFilePath")
    print(f"\n  volume      : {volume!r}  (float dB)")
    print(f"  mute        : {mute!r}  (bool)")
    print(f"  config path : {config_path!r}")
    return {"volume": volume, "mute": mute, "config_path": config_path}


def probe_volume(ws, original: float, target: float) -> None:
    section(f"SET VOLUME — to {target} dB, then back to {original} dB")
    query(ws, "SetVolume", target)
    print(f"  readback: {query(ws, 'GetVolume')!r}")
    query(ws, "SetVolume", original)


def probe_mute(ws, original: bool) -> None:
    section("MUTE ON / OFF")
    query(ws, "SetMute", True)
    print(f"  readback: {query(ws, 'GetMute')!r}")
    query(ws, "SetMute", False)
    print(f"  readback: {query(ws, 'GetMute')!r}")
    if original:
        query(ws, "SetMute", True)


def probe_preset(ws, path: str, original_path: str) -> None:
    section(f"SET CONFIG FILE PATH — to {path!r}, then Reload")
    print("  Note: this triggers a full filter-pipeline reload -- expect")
    print("  audio to briefly interrupt.\n")
    query(ws, "SetConfigFilePath", path)
    query(ws, "Reload")
    print(f"  readback path: {query(ws, 'GetConfigFilePath')!r}")
    if original_path and original_path != path:
        print(f"\n  restoring original config: {original_path!r}")
        query(ws, "SetConfigFilePath", original_path)
        query(ws, "Reload")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a CamillaDSP WebSocket API")
    parser.add_argument("--host", required=True, help="CamillaDSP host/IP")
    parser.add_argument("--port", type=int, default=1234, help="CamillaDSP websocket port (default 1234)")
    parser.add_argument("--target-db", type=float, default=-30.0,
                         help="target volume (dB) for the volume test (default -30.0)")
    parser.add_argument("--preset-path", default=None,
                         help="if set, also test switching to this config file path "
                              "(must be resolvable by the CamillaDSP process, not this machine)")
    parser.add_argument("--skip-write", action="store_true",
                         help="skip volume/mute/preset changes (read-only mode)")
    args = parser.parse_args()

    url = f"ws://{args.host}:{args.port}"
    print(f"\nProbing CamillaDSP at {url}")

    try:
        ws = websocket.create_connection(url, timeout=5.0)
    except Exception as exc:
        print(f"\nFAIL: Could not connect to {url}")
        print(f"  {exc}")
        print("\nCheck: is CamillaDSP running? Was it started with -p matching --port?")
        print("By default CamillaDSP's websocket server binds to 127.0.0.1 only -- it")
        print("needs -a 0.0.0.0 (or similar) to be reachable from this machine at all.")
        sys.exit(1)

    try:
        probe_version(ws)
        status = probe_status(ws)

        if not args.skip_write:
            probe_volume(ws, status["volume"], args.target_db)
            probe_mute(ws, status["mute"])
            if args.preset_path:
                probe_preset(ws, args.preset_path, status["config_path"])
    except RuntimeError as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)
    finally:
        ws.close()

    print("\n\nDone. Review the output above, then set DEVICE_DRIVER = \"camilladsp\" and")
    print("the CAMILLADSP_* keys in src/settings.toml before deploying.")
    print("\nNote: this only confirmed the API shapes against your instance, not")
    print("src/camilladsp.py's own hand-rolled WS client against your device --")
    print("that combination was confirmed working on different hardware, not")
    print("necessarily yours. Watch `make shell` (REPL) on first boot regardless.")


if __name__ == "__main__":
    main()
