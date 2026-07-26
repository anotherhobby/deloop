"""
probe_input.py -- Discover and test input switching on the Denon AVR-X4800H.

Fetches the input list from GetRenameSource, shows current input, then
attempts to switch using three candidate URL formats so we can see which one
the receiver actually accepts.

Usage:
    python tools/probe_input.py --host 10.0.0.75 [--target "SAT/CBL"]

The --target value is the raw <name> from GetRenameSource for the input you
want to switch TO.  If omitted the script switches to the first input in the
list and then switches back so the receiver is left unchanged.
"""

import argparse
import sys
import time

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("ERROR: install requests:  pip install requests")
    sys.exit(1)

PORT = 8080


def get(host, path):
    url = f"http://{host}:{PORT}{path}"
    print(f"  GET {url}")
    r = requests.get(url, timeout=5)
    print(f"  -> {r.status_code}  ({len(r.text)} bytes)")
    return r


def post(host, path, body):
    url = f"http://{host}:{PORT}{path}"
    print(f"  POST {url}")
    r = requests.post(url, data=body, timeout=5)
    print(f"  -> {r.status_code}  ({len(r.text)} bytes)")
    return r


def tag(xml, t):
    o, c = f"<{t}>", f"</{t}>"
    s = xml.find(o)
    if s == -1:
        return ""
    s += len(o)
    e = xml.find(c, s)
    return xml[s:e].strip() if e != -1 else ""


def current_input(host):
    body = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<tx><cmd id='1'>GetAllZoneSource</cmd></tx>"
    )
    r = post(host, "/goform/AppCommand.xml", body)
    zone1 = tag(r.text, "zone1")
    return tag(zone1, "source")


def fetch_input_list(host):
    body = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<tx><cmd id="1">GetRenameSource</cmd></tx>'
    )
    r = post(host, "/goform/AppCommand.xml", body)
    print(f"\n  Raw GetRenameSource response:\n{r.text[:2000]}\n")

    inputs = []
    xml = r.text
    pos = 0
    while True:
        s = xml.find("<list>", pos)
        if s == -1:
            break
        e = xml.find("</list>", s)
        if e == -1:
            break
        block = xml[s:e]
        name   = tag(block, "name")
        rename = tag(block, "rename")
        if name:
            inputs.append((name, rename))
        pos = e + 7
    return inputs


def try_switch(host, raw_name):
    """Try three URL formats for input switching; return which succeeded."""
    candidates = [
        ("formiPhoneAppDirect SI",     f"/goform/formiPhoneAppDirect.xml?SI{raw_name}"),
        ("formiPhoneAppDirect SI%2F",  f"/goform/formiPhoneAppDirect.xml?SI{raw_name.replace('/', '%2F')}"),
        ("formiPhoneAppSelector 1+",   f"/goform/formiPhoneAppSelector.xml?1+{raw_name}"),
    ]

    before = current_input(host)
    print(f"\n  Input before switch: {before!r}")

    for label, path in candidates:
        print(f"\n  --- trying [{label}] ---")
        get(host, path)
        time.sleep(1.5)
        after = current_input(host)
        print(f"  Input after: {after!r}")
        if after.upper().replace("/", "").replace(" ", "") == raw_name.upper().replace("/", "").replace(" ", ""):
            print(f"  *** SUCCESS with [{label}] ***")
            return label, path
        else:
            print(f"  No change (still {after!r})")

    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--target", default=None, help="raw input name to switch to")
    args = ap.parse_args()

    host = args.host

    print("\n=== Fetching input list from GetRenameSource ===")
    inputs = fetch_input_list(host)
    print(f"\n  Found {len(inputs)} inputs:")
    for raw, friendly in inputs:
        print(f"    raw={raw!r}  friendly={friendly!r}")

    print("\n=== Current input ===")
    cur = current_input(host)
    print(f"  {cur!r}")

    if not inputs:
        print("No inputs found -- cannot test switching")
        sys.exit(1)

    if args.target:
        target_raw = args.target
    else:
        # Pick an input that isn't the current one
        target_raw = next((r for r, _ in inputs if r.upper() != cur.upper()), inputs[0][0])
        print(f"\n  No --target given; will try switching to {target_raw!r} then back to {cur!r}")

    print(f"\n=== Testing switch to {target_raw!r} ===")
    label, path = try_switch(host, target_raw)

    if label:
        print(f"\n  Working format: {label}")
        print(f"  URL path: {path}")
        if not args.target:
            print(f"\n  Switching back to original input {cur!r}")
            get(host, path.replace(target_raw, cur).replace(target_raw.replace("/", "%2F"), cur))
    else:
        print("\n  None of the candidates worked.")
        print("  Next step: run dump_denonavr.py with input switching to see what denonavr sends.")


if __name__ == "__main__":
    main()
