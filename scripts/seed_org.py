#!/usr/bin/env python3
"""Load an organisation hierarchy (Region -> City -> Branch) into the API.

    .venv/bin/python scripts/seed_org.py                                   # config/org.wareed.yaml
    .venv/bin/python scripts/seed_org.py --config config/org.wareed.yaml
    .venv/bin/python scripts/seed_org.py --api-url http://10.0.0.5:8000 --key <api key>
    .venv/bin/python scripts/seed_org.py --dry-run                         # validate, print, write nothing

Goes through POST /api/v1/org/import rather than the database directly, so it
works against a remote deployment and the service's validation is the only
validation. The import upserts by id and deletes nothing: re-running after an
edit updates names and moves branches, and a branch removed from the file stays
in the database until it is deleted on the Network page (a branch that still
owns cameras refuses to go, which is the point).

Exits non-zero on a validation error, with the server's message.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from finblade.org import flatten_import                  # noqa: E402


def load(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.join(REPO, "config", "org.wareed.yaml"))
    ap.add_argument("--api-url", default=os.environ.get("FINBLADE_API_URL",
                                                        "http://127.0.0.1:8000"))
    ap.add_argument("--key", default=os.environ.get("FINBLADE_API_KEY"),
                    help="API key when the deployment has one set")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc = load(args.config)
    regions, cities, branches, errors = flatten_import(doc)
    tenant = doc.get("tenant") or {}
    print(f"{args.config}: {len(regions)} regions, {len(cities)} cities, "
          f"{len(branches)} branches; tenant {tenant.get('name') or '-'}")
    if errors:
        for e in errors:
            print("  error:", e)
        return 2
    for r in regions:
        print(f"  {r['region_id']:10s} {r['name']}")
        for c in [c for c in cities if c['region_id'] == r['region_id']]:
            print(f"    {c['city_id']:8s} {c['name']}")
            for b in [b for b in branches if b['city_id'] == c['city_id']]:
                print(f"      {b['branch_id']:8s} {b['branch_type']:10s} {b['name']}")
    if args.dry_run:
        return 0

    req = urllib.request.Request(
        args.api_url.rstrip("/") + "/api/v1/org/import",
        data=json.dumps(doc).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    if args.key:
        req.add_header("Authorization", f"Bearer {args.key}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("import refused:", e.code, e.read().decode("utf-8", "replace"))
        return 1
    except urllib.error.URLError as e:
        print("cannot reach the API:", e.reason)
        return 1
    print("imported:", body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
