"""
probe_ha.py — Host-side Home Assistant REST API discovery tool.

Run this on your Mac (not the device) against a live Home Assistant
instance to confirm the exact JSON shapes deloop's src/ha.py driver
expects, before flashing firmware.

Usage:
    pip install requests   (or: pip install -e ".[dev]" from repo root)
    python tools/probe_ha.py --host hobbyhub.cabin.hobbysprawl.com \
        --token "$(cat local/agent/deloop-ha-token.txt)" \
        --entity media_player.office

The script prints each response as raw JSON so you can compare it against
the shapes documented at the top of src/ha.py. By default it only reads
state; pass without --skip-write to also nudge volume/mute/source and
confirm the entity actually reacts.
"""

import argparse
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: make bootstrap")
    sys.exit(1)


# media_player domain supported_features bits (stable across HA versions).
_SUPPORT_BITS = {
    4:      "SUPPORT_VOLUME_SET",
    8:      "SUPPORT_VOLUME_MUTE",
    128:    "SUPPORT_TURN_ON",
    256:    "SUPPORT_TURN_OFF",
    1024:   "SUPPORT_VOLUME_STEP",
    2048:   "SUPPORT_SELECT_SOURCE",
    65536:  "SUPPORT_SELECT_SOUND_MODE",
}


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def get_state(base: str, headers: dict, entity: str) -> dict:
    section(f"GET /api/states/{entity}")
    url = f"{base}/api/states/{entity}"
    print(f"  GET {url}")
    resp = requests.get(url, headers=headers, timeout=5.0)
    print(f"  HTTP {resp.status_code}")
    resp.raise_for_status()
    body = resp.json()
    attrs = body.get("attributes", {})
    print(f"  state              : {body.get('state')!r}")
    print(f"  volume_level       : {attrs.get('volume_level')!r}  (0.0-1.0 fraction)")
    print(f"  is_volume_muted    : {attrs.get('is_volume_muted')!r}")
    print(f"  source             : {attrs.get('source')!r}")
    print(f"  source_list        : {attrs.get('source_list')!r}")
    features = attrs.get("supported_features") or 0
    have = [name for bit, name in _SUPPORT_BITS.items() if features & bit]
    print(f"  supported_features : {features} -> {have}")
    return body


def call_service(base: str, headers: dict, entity: str, domain: str, service: str, data: dict) -> None:
    url = f"{base}/api/services/{domain}/{service}"
    body = {"entity_id": entity, **data}
    print(f"  POST {url}  body={body!r}")
    resp = requests.post(url, headers=headers, json=body, timeout=5.0)
    print(f"  HTTP {resp.status_code}  body: {resp.text[:200]!r}")
    resp.raise_for_status()


def probe_volume(base: str, headers: dict, entity: str, target_pct: float) -> None:
    section(f"SET VOLUME — absolute to {target_pct}%")
    call_service(base, headers, entity, "media_player", "volume_set",
                 {"volume_level": target_pct / 100.0})
    get_state(base, headers, entity)


def probe_mute(base: str, headers: dict, entity: str) -> None:
    section("MUTE ON / OFF")
    call_service(base, headers, entity, "media_player", "volume_mute", {"is_volume_muted": True})
    get_state(base, headers, entity)
    call_service(base, headers, entity, "media_player", "volume_mute", {"is_volume_muted": False})
    get_state(base, headers, entity)


def probe_source(base: str, headers: dict, entity: str, source: str) -> None:
    section(f"SELECT SOURCE — {source!r}")
    call_service(base, headers, entity, "media_player", "select_source", {"source": source})
    get_state(base, headers, entity)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a Home Assistant media_player entity")
    parser.add_argument("--host", required=True, help="Home Assistant host/IP (no scheme)")
    parser.add_argument("--port", type=int, default=8123, help="Home Assistant port (default 8123)")
    parser.add_argument("--token", required=True, help="Long-lived access token")
    parser.add_argument("--entity", default="media_player.office", help="Entity ID to probe")
    parser.add_argument("--target-pct", type=float, default=50.0,
                         help="target volume (percent) for the volume test (default 50.0)")
    parser.add_argument("--source", default=None,
                         help="if set, also test switching to this source name")
    parser.add_argument("--skip-write", action="store_true",
                         help="skip volume/mute/source changes (read-only mode)")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    print(f"\nProbing Home Assistant at {base} entity={args.entity}")

    try:
        get_state(base, headers, args.entity)

        if not args.skip_write:
            probe_volume(base, headers, args.entity, args.target_pct)
            probe_mute(base, headers, args.entity)
            if args.source:
                probe_source(base, headers, args.entity, args.source)

    except requests.exceptions.ConnectionError as exc:
        print(f"\nFAIL: Could not connect to {base}")
        print(f"  {exc}")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"\nFAIL: Request timed out to {base}")
        sys.exit(1)
    except requests.exceptions.HTTPError as exc:
        print(f"\nFAIL: HTTP error from {base}")
        print(f"  {exc}")
        sys.exit(1)

    print("\n\nDone. Review the output above, then set DEVICE_DRIVER = \"ha\" and")
    print("the HA_* keys in src/settings.toml before deploying.")


if __name__ == "__main__":
    main()
