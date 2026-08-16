#!/usr/bin/env python3
"""Propose config/topology.<site>.yaml from what the cameras have already seen.

    .venv/bin/python scripts/propose_topology.py --hours 24
    .venv/bin/python scripts/propose_topology.py --site SITE-B --out config/topology.site-b.yaml
    .venv/bin/python scripts/propose_topology.py --all-sites --out-dir config/

Reads the events table directly (read-only), groups sightings by global_ref,
and classifies each camera pair from the gaps between consecutive sightings.
See finblade/topology_survey.py for the rules and their limits.

The output is a DRAFT. It marks every pair it could not settle, and those are
the ones worth walking.
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade.topology_survey import propose, to_yaml   # noqa: E402


def load_sightings(db, t0, t1, site=None):
    """(global_ref, camera_id, ts) for every event that resolved to a person."""
    q = ("SELECT global_ref, camera_id, ts FROM events "
         "WHERE ts BETWEEN ? AND ? AND global_ref IS NOT NULL "
         "AND global_ref != '' AND camera_id IS NOT NULL")
    p = [t0, t1]
    if site:
        q += " AND site_id = ?"
        p.append(site)
    return [(r[0], r[1], r[2]) for r in db.execute(q, p)]


def load_cameras(db, site=None):
    q = "SELECT camera_id FROM cameras"
    p = []
    if site:
        q += " WHERE site_id = ?"
        p.append(site)
    return [r[0] for r in db.execute(q, p) if r[0]]


def load_sites(db):
    return [r[0] for r in db.execute(
        "SELECT DISTINCT site_id FROM cameras WHERE site_id IS NOT NULL")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/finblade.db")
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

    if not os.path.exists(args.db):
        sys.exit(f"no database at {args.db}")

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    t1 = time.time()
    t0 = t1 - args.hours * 3600.0

    sites = load_sites(db) if args.all_sites else [args.site]
    if args.all_sites and not sites:
        sys.exit("no sites found in the cameras table")

    for site in sites:
        sightings = load_sightings(db, t0, t1, site)
        cameras = load_cameras(db, site)
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
            print(text)


if __name__ == "__main__":
    main()
