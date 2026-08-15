#!/usr/bin/env python3
"""Record the instant each camera path of a set starts publishing.

Polls the MediaMTX API and prints, per path, how many milliseconds after the
first camera it became ready. Used by test_set.sh to measure how closely the
cameras of one scenario actually start together.

usage: watch_ready.py <setnum> <expected_count> [timeout_sec]
"""

import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:9997/v3/paths/list"


def main():
    setnum = sys.argv[1]
    expected = int(sys.argv[2])
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
    prefix = "set_%s/cam_" % setnum

    first_ready = {}
    deadline = time.time() + timeout

    while time.time() < deadline and len(first_ready) < expected:
        try:
            with urllib.request.urlopen(API, timeout=2) as fh:
                items = json.load(fh).get("items", [])
        except Exception:
            time.sleep(0.05)
            continue

        now = time.time()
        for it in items:
            name = it.get("name", "")
            if name.startswith(prefix) and it.get("ready") and name not in first_ready:
                first_ready[name] = now
        time.sleep(0.02)

    if not first_ready:
        print("NO_PATHS_READY")
        return 1

    base = min(first_ready.values())
    for name in sorted(first_ready):
        print("%s\t%.0f" % (name, (first_ready[name] - base) * 1000.0))
    print("SKEW_MS\t%.0f" % ((max(first_ready.values()) - base) * 1000.0))
    print("COUNT\t%d" % len(first_ready))
    return 0


if __name__ == "__main__":
    sys.exit(main())
