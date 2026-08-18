#!/usr/bin/env python3
"""Project config/topology.yaml into the camera_transits table.

    .venv/bin/python scripts/sync_topology.py                    # apply
    .venv/bin/python scripts/sync_topology.py --dry-run          # show only
    .venv/bin/python scripts/sync_topology.py --topology config/topology.yaml

RUN IT WHENEVER THE TOPOLOGY OR THE CAMERA LIST CHANGES. The journey views join
against this table, and an absent row means "no route" — so a camera added
without a re-sync silently produces a person who appears from nowhere and whose
journey never links to anything. That failure is quiet, which is why this
prints the row count and the surveyed share every time.

WHY A TABLE AND NOT A LOOKUP. finblade/topology.py answers "is this one hop
feasible?" one pair at a time, in Python, on the live path. The journey views
ask the opposite question — "of every fragment in this window, which pairs are
feasible?" — and that is a join. Postgres cannot read a YAML file, so the YAML
is projected here. The YAML stays the authority; this table is a cache of it
and is safe to drop and rebuild.

WHAT GETS WRITTEN. Every ORDERED pair of cameras in the cameras table, resolved
through CameraTopology so the fallback rules are applied once, here, rather than
re-implemented in SQL:

    same_camera   (0, default_max)  re-acquisition after a tracking dropout
    overlapping   (-tolerance, hi)  simultaneous is expected; see the DDL note
    surveyed      (min, max)        paced walk times from the survey
    default       (min, max)        nobody has surveyed this pair yet

With allow_unknown_pairs: false, unsurveyed pairs are written as NO ROW at all,
because that is what the topology means by it — and a missing row is exactly how
the join expresses "unreachable".

CAMERAS COME FROM THE DATABASE, not the YAML. The YAML describes pairs; the
database knows which cameras exist. A topology file that covers a camera nobody
deployed should not create rows, and a deployed camera the topology forgot
should still get default rows so its fragments can link at all.
"""

import argparse
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from finblade.topology import CameraTopology            # noqa: E402


def dsn_from_env() -> str:
    """DATABASE_URL, falling back to the .env line start_stack.sh reads."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    path = os.path.join(REPO, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def rows_for(topo: CameraTopology, cameras, now: float):
    """Resolve every ordered camera pair. Pure — no connection, so it tests.

    Returns [(from_camera, to_camera, min_seconds, max_seconds, pair_kind, ts)].
    """
    out = []
    for a in cameras:
        for b in cameras:
            if a == b:
                # Re-acquisition on one camera after a dropout or an ID break.
                # topology.feasible() calls this always-possible and leaves the
                # bound to the caller; offline we need an actual number, and the
                # default window is the least surprising one to borrow.
                out.append((a, b, 0.0, topo.default_transit[1],
                            "same_camera", now))
                continue

            lo, hi = topo.transit_window(a, b)

            if topo.is_overlapping(a, b):
                # Negative floor, so one BETWEEN covers clock skew between two
                # independent camera processes. See the DDL comment.
                out.append((a, b, -topo.overlap_tolerance_s, hi,
                            "overlapping", now))
                continue

            if topo.is_known_pair(a, b):
                out.append((a, b, lo, hi, "surveyed", now))
                continue

            if not topo.allow_unknown_pairs:
                # No row: the join will find nothing and the hop is dropped.
                continue
            out.append((a, b, lo, hi, "default", now))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--topology",
                    default=os.environ.get(
                        "FINBLADE_TOPOLOGY",
                        os.path.join(REPO, "config", "topology.yaml")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.topology):
        print("no topology file at %s" % args.topology)
        return 2

    dsn = args.dsn or dsn_from_env()
    if not dsn:
        print("no --dsn, no DATABASE_URL, and no DATABASE_URL line in .env")
        return 2

    topo = CameraTopology.load(args.topology)
    now = time.time()

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        cameras = [r[0] for r in conn.execute(
            "SELECT camera_id FROM cameras ORDER BY camera_id").fetchall()]
        if not cameras:
            print("no cameras in the database — nothing to pair. Start the "
                  "stack once so the cameras register, then re-run.")
            return 2

        rows = rows_for(topo, cameras, now)
        kinds = {}
        for r in rows:
            kinds[r[4]] = kinds.get(r[4], 0) + 1

        print("topology : %s" % args.topology)
        print("cameras  : %d (%s)" % (len(cameras), ", ".join(cameras)))
        print("pairs    : %d of %d possible" % (len(rows), len(cameras) ** 2))
        for kind in sorted(kinds):
            print("  %-12s %4d" % (kind, kinds[kind]))

        surveyed = kinds.get("surveyed", 0) + kinds.get("overlapping", 0)
        movement = len(rows) - kinds.get("same_camera", 0)
        if movement:
            pct = 100.0 * surveyed / movement
            print("surveyed : %.0f%% of movement pairs" % pct)
            if pct < 100.0:
                print("  Unsurveyed pairs fall back to a guessed window, and "
                      "v_journey_links reports them as pair_kind='default'.")

        if args.dry_run:
            print()
            print("Dry run — nothing written.")
            return 0

        # Replace wholesale rather than upsert. The camera list shrinks (a
        # camera is removed in the UI) as well as grows, and a stale pair
        # pointing at a camera that no longer exists would keep licensing hops
        # that cannot happen.
        with conn.transaction():
            conn.execute("DELETE FROM camera_transits")
            conn.cursor().executemany(
                "INSERT INTO camera_transits(from_camera, to_camera, "
                "min_seconds, max_seconds, pair_kind, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)", rows)

    print()
    print("wrote %d row(s) to camera_transits" % len(rows))
    print("The journey views read this on every query — no restart needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
