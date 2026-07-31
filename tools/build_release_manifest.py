"""
build_release_manifest.py -- build a deloop OTA release manifest.json from
a directory of already-built app files: the exact set `make deploy`/
`deploy-mpy` ships (precompiled .mpy for everything in the Makefile's
MPY_MODULES, plus code.py, which stays uncompiled).

Run locally against local/build/ (whatever `make deploy` last compiled
there) via `make build-manifest`, or in CI (.github/workflows/release.yml)
against a clean dist/ directory built fresh for the release.

The file list below is an explicit allowlist, not a glob over the
directory -- kept in sync by hand with the Makefile's MPY_MODULES list
(same "explicit is more reliable than clever" choice made elsewhere in
this project). Deliberately does NOT include settings.toml (user
credentials, never OTA-managed), fonts, splash_logo.bmp, or sounds/ --
out of scope for this pass, see CLAUDE.md's OTA section.

Usage:
    python tools/build_release_manifest.py --dir local/build \\
        --out local/build/manifest.json --version 5
"""

import argparse
import hashlib
import json
import os
import sys

# Kept in sync by hand with Makefile's MPY_MODULES.
_MPY_MODULES = [
    "config", "driver", "denon", "minidsp", "camilladsp", "ha", "ha_ui",
    "wiim", "wiim_ui", "state", "dial_ui", "sound", "app", "ota", "version",
]
# Never .mpy-compiled -- code.py is CircuitPython's boot entry point,
# required as plain source.
_UNCOMPILED_FILES = ["code.py"]


def _manifest_files(build_dir):
    names = [mod + ".mpy" for mod in _MPY_MODULES] + _UNCOMPILED_FILES
    files = []
    for name in names:
        path = os.path.join(build_dir, name)
        if not os.path.isfile(path):
            print("ERROR: expected file not found: {}".format(path), file=sys.stderr)
            sys.exit(1)
        with open(path, "rb") as f:
            data = f.read()
        files.append({
            "path": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True, help="directory containing the built files")
    ap.add_argument("--out", required=True, help="path to write manifest.json to")
    ap.add_argument("--version", required=True, type=int,
                    help="release version -- bare int, matches the vN git tag")
    args = ap.parse_args()

    manifest = {
        "version": args.version,
        "files": _manifest_files(args.dir),
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f)

    print("wrote {} ({} files, version {})".format(
        args.out, len(manifest["files"]), args.version))


if __name__ == "__main__":
    main()
