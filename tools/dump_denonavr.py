"""
dump_denonavr.py — Use the python-denonavr library to connect to the AVR
and dump all discovered state, endpoints, and capabilities.

This is a one-time diagnostic tool. Its output tells us:
  - Which API version the receiver uses
  - The exact endpoints and XML format in play
  - Current volume, mute, power, input values

Usage:
    ./.venv/bin/pip install denonavr
    ./.venv/bin/python tools/dump_denonavr.py --host 192.168.x.x
"""

import argparse
import asyncio
import json
import sys

try:
    import denonavr
except ImportError:
    print("ERROR: denonavr not installed. Run: ./.venv/bin/pip install denonavr")
    sys.exit(1)


async def dump(host: str) -> None:
    print(f"\nConnecting to AVR at {host} …\n")

    # Intercept httpx to log every request and response denonavr makes
    import httpx
    _orig_send = httpx.AsyncClient.send
    async def _logging_send(self, request, **kwargs):
        body = request.content.decode("utf-8", errors="replace") if request.content else ""
        print(f"  [REQ]  {request.method} {request.url}")
        if body:
            print(f"         {body[:300]}")
        resp = await _orig_send(self, request, **kwargs)
        await resp.aread()
        text = resp.text[:500] if resp.text else ""
        print(f"  [RESP] {resp.status_code}  {text}")
        print()
        return resp
    httpx.AsyncClient.send = _logging_send

    avr = denonavr.DenonAVR(host)

    try:
        await avr.async_setup()
    except Exception as e:
        print(f"async_setup failed: {e}")
        sys.exit(1)

    try:
        await avr.async_update()
    except Exception as e:
        print(f"async_update failed: {e}")

    print("=== Identity ===")
    print(f"  Name:         {avr.name}")
    print(f"  Model:        {avr.model_name}")
    print(f"  Serial:       {avr.serial_number}")
    print(f"  API type:     {avr.receiver_type}")
    print(f"  Support URL:  {getattr(avr, 'support_url', 'n/a')}")

    print("\n=== Current State ===")
    print(f"  Power:        {avr.power}")
    print(f"  Volume:       {avr.volume} dB")
    print(f"  Muted:        {avr.muted}")
    print(f"  Input:        {avr.input_func}")
    try:
        print(f"  Sound mode:   {avr.sound_mode}")
    except Exception:
        pass

    print("\n=== Available Inputs ===")
    input_list = getattr(avr, "input_func_list", None) or list(
        (getattr(avr, "input_func_map", None) or {}).keys()
    )
    for name in (input_list or []):
        print(f"  {name!r}")

    print("\n=== Available Sound Modes ===")
    for m in (avr.sound_mode_list or []):
        print(f"  {m!r}")

    print("\n=== Internal API details ===")
    # Dump any attributes that reveal the API base URL / port
    for attr in ["_host", "_port", "_urls", "_receiver", "api_host",
                 "support_url", "_update_audyssey"]:
        val = getattr(avr, attr, "<not present>")
        if val != "<not present>":
            print(f"  avr.{attr} = {val!r}")

    # Show the URLs denonavr actually uses (varies by receiver type)
    print("\n=== URLs used by denonavr ===")
    receiver = getattr(avr, "_receiver", None)
    if receiver:
        for attr in dir(receiver):
            if "url" in attr.lower() or "port" in attr.lower() or "path" in attr.lower():
                try:
                    val = getattr(receiver, attr)
                    if not callable(val):
                        print(f"  receiver.{attr} = {val!r}")
                except Exception:
                    pass

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump denonavr state")
    parser.add_argument("--host", required=True, help="AVR IP address")
    args = parser.parse_args()
    asyncio.run(dump(args.host))


if __name__ == "__main__":
    main()
