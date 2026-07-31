"""
probe_ota.py -- Host-side GitHub Releases discovery tool for deloop's OTA
self-update feature (src/ota.py).

Run this on your Mac (not the device) against the real repo to confirm the
exact response shapes ota.py expects before trusting them on-device: the
releases/latest JSON, the manifest.json asset, and one representative
release asset's download. No device or daemon needed -- just network
access to github.com.

Usage:
    python tools/probe_ota.py --repo anotherhobby/deloop
    python tools/probe_ota.py --repo anotherhobby/deloop --tag v4

Unauthenticated GitHub API calls are capped at 60/hr per IP -- this script
prints the rate-limit headers each run so that's easy to keep an eye on;
fine for occasional manual use, but don't loop this in a script.
"""

import argparse
import json
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: make bootstrap")
    sys.exit(1)


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def probe_release(repo: str, tag: str) -> dict:
    if tag:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
    section(f"GET {url}")
    resp = requests.get(url, timeout=10)
    print(f"  HTTP {resp.status_code}")
    print(f"  X-RateLimit-Remaining: {resp.headers.get('X-RateLimit-Remaining')}"
          f" / {resp.headers.get('X-RateLimit-Limit')}")
    resp.raise_for_status()
    release = resp.json()
    print(f"  tag_name: {release.get('tag_name')!r}")
    print(f"  assets  : {[a.get('name') for a in release.get('assets', [])]}")
    return release


def probe_manifest(release: dict) -> dict:
    section("manifest.json asset")
    asset = next((a for a in release.get("assets", []) if a.get("name") == "manifest.json"), None)
    if asset is None:
        print("  -> no manifest.json asset on this release. ota.py's apply() would "
              "fail here -- has the release workflow actually run for this tag?")
        return {}
    url = asset["browser_download_url"]
    print(f"  GET {url}")
    t0 = time.monotonic()
    resp = requests.get(url, timeout=10)
    elapsed = time.monotonic() - t0
    print(f"  HTTP {resp.status_code}  Content-Type: {resp.headers.get('Content-Type')!r}"
          f"  ({elapsed:.2f}s)")
    resp.raise_for_status()
    manifest = resp.json()
    print(f"  version: {manifest.get('version')!r}")
    files = manifest.get("files", [])
    print(f"  files  : {len(files)} entries")
    for entry in files[:5]:
        print(f"    {entry.get('path')!r}  size={entry.get('size')}  sha256={entry.get('sha256')}")
    if len(files) > 5:
        print(f"    ... and {len(files) - 5} more")
    return manifest


def probe_one_asset(release: dict, manifest: dict) -> None:
    files = manifest.get("files", [])
    if not files:
        return
    path = files[0]["path"]
    section(f"Representative asset download -- {path!r}")
    asset = next((a for a in release.get("assets", []) if a.get("name") == path), None)
    if asset is None:
        print(f"  -> release is missing an asset matching manifest path {path!r}")
        return
    url = asset["browser_download_url"]
    print(f"  GET {url}")
    t0 = time.monotonic()
    resp = requests.get(url, timeout=30)
    elapsed = time.monotonic() - t0
    size = len(resp.content)
    print(f"  HTTP {resp.status_code}  Content-Type: {resp.headers.get('Content-Type')!r}")
    print(f"  {size} bytes in {elapsed:.2f}s  (manifest says size={files[0]['size']},"
          f" {'MATCH' if size == files[0]['size'] else 'MISMATCH'})")
    print("  -> feeds OTA_INSTALL_TIMEOUT_MS sizing: measure real per-file latency,")
    print("     don't guess (this is one file over a wired/Wi-Fi host connection,")
    print("     not the device's own Wi-Fi -- a rough floor, not the real number).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe deloop's GitHub Releases OTA source")
    parser.add_argument("--repo", required=True, help="owner/name, e.g. anotherhobby/deloop")
    parser.add_argument("--tag", default=None,
                        help="specific tag to probe (default: whatever /releases/latest returns)")
    args = parser.parse_args()

    try:
        release = probe_release(args.repo, args.tag)
        manifest = probe_manifest(release)
        probe_one_asset(release, manifest)
    except requests.exceptions.HTTPError as exc:
        print(f"\nFAIL: HTTP error -- {exc}")
        sys.exit(1)
    except requests.exceptions.RequestException as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)

    print("\n\nDone. Compare the shapes above against src/ota.py's assumptions.")


if __name__ == "__main__":
    main()
