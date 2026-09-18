#!/usr/bin/env python3
"""Propose config/topology.<site>.yaml from what the cameras have already seen.

    .venv/bin/python scripts/propose_topology.py --hours 72
    .venv/bin/python scripts/propose_topology.py --site RUH-HQ --out config/topology.ruh-hq.yaml
    .venv/bin/python scripts/propose_topology.py --all-sites --out-dir config/

Reads the events table (read-only) through DATABASE_URL — the Postgres the
API runs on; .env is read for it — groups sightings by global_ref, and
classifies each camera pair from the gaps between consecutive sightings.
See finblade/topology_survey.py for the rules and their limits.

The output is a DRAFT. It marks every pair it could not settle, and those are
the ones worth walking. Review it against the floor plan before pointing
FINBLADE_TOPOLOGY at it: a pair the data calls "overlapping" because two
cameras see the same corridor from either end is right; a pair it calls
"unsurveyed" because nobody walked it in the window is not evidence of a
wall.
"""

import argparse
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from finblade.topology_survey import propose, to_yaml   # noqa: E402


def _env():
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def load_sightings(store, t0, t1, site=None):
    """(global_ref, camera_id, ts) for every event that resolved to a person."""
    q = ("SELECT global_ref, camera_id, ts FROM events "
         "WHERE ts BETWEEN %s AND %s AND global_ref IS NOT NULL "
         "AND global_ref <> '' AND camera_id IS NOT NULL")
    p = [float(t0), float(t1)]
    if site:
        q += " AND site_id = %s"
        p.append(site)
    return [(r["global_ref"], r["camera_id"], float(r["ts"])) for r in store._q(q, p)]


def load_cameras(store, site=None):
    q = "SELECT camera_id FROM cameras"
    p = []
    if site:
        q += " WHERE site_id = %s"
        p.append(site)
    return [r["camera_id"] for r in store._q(q, p) if r.get("camera_id")]


def load_sites(store):
    return [r["site_id"] for r in store._q(
        "SELECT DISTINCT site_id FROM cameras WHERE site_id IS NOT NULL")]


def main():
    _env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24.0,
                    help="how far back to look (default 24)")
    ap.add_argument("--site", help="restrict to one site_id")
    ap.add_argument("--all-sites", action="store_true",
                    help="write one proposal per site")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--out-dir", default="config",
                    help="directory for --all-sites (default config/)")
    ap.add_argument("--overlap-max-dt", type=float, default=2.0,
                    help="gap under which a handover counts as simultaneous")
    ap.add_argument("--min-samples", type=int, default=5,
                    help="handovers needed before a pair is classified")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set (env or .env) — this reads the API's Postgres")
    from services.api.postgres_store import PostgresStore
    store = PostgresStore(dsn, apply_schema=False)
    t1 = time.time()
    t0 = t1 - args.hours * 3600.0

    sites = load_sites(store) if args.all_sites else [args.site]
    if args.all_sites and not sites:
        sys.exit("no sites found in the cameras table")

    for site in sites:
        sightings = load_sightings(store, t0, t1, site)
        cameras = load_cameras(store, site)
        label = site or "all cameras"

        if not sightings:
            # Silence here is the commonest result and the most misread: it
            # almost always means ReID never ran, not that nobody moved.
            print(f"[!] {label}: no events carrying a global_ref in the last "
                  f"{args.hours}h.", file=sys.stderr)
            print("    Cross-camera identity produces these. Check that the "
                  "OSNet weights loaded and that ReID is enabled on the zones "
                  "you care about, then let it run with people moving about.",
                  file=sys.stderr)
            if not args.all_sites:
                sys.exit(1)
            continue

        proposal = propose(sightings, overlap_max_dt=args.overlap_max_dt,
                           min_samples=args.min_samples, cameras=cameras)
        text = to_yaml(proposal, site=site)

        n_ov = len(proposal["overlapping_pairs"])
        n_tr = len(proposal["transits"])
        n_un = len(proposal["unsurveyed"])
        print(f"[*] {label}: {len(sightings)} sightings, {len(proposal['cameras'])} "
              f"cameras -> {n_ov} overlapping, {n_tr} transits, "
              f"{n_un} still unsurveyed", file=sys.stderr)

        if args.all_sites:
            path = os.path.join(args.out_dir,
                                f"topology.{str(site).lower()}.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"    wrote {path}", file=sys.stderr)
        elif args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"    wrote {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
