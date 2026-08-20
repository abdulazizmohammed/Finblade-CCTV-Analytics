#!/usr/bin/env python3
"""Did one global identity survive the whole walk?

    .venv/bin/python scripts/walk_score.py                 # last 10 minutes
    .venv/bin/python scripts/walk_score.py --minutes 20
    .venv/bin/python scripts/walk_score.py --route CAM-01,CAM-02,CAM-03

The measurement the cross-camera work has been missing. Everything else reports
counts; this reports whether the thing actually worked, against a route somebody
walked on purpose.

WHY IT REPORTS OBSERVED GAPS TOO. Transit windows in the topology are the gate
that runs BEFORE appearance is scored, so a window that does not match reality
rejects true matches silently and no counter blames it. The gaps printed here
are what the cameras actually saw, and they are what the topology should be
built from — measured, not paced and not guessed.
"""

import argparse
import datetime as dt
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

DEFAULT_ROUTE = ["CAM-01", "CAM-02", "CAM-03", "CAM-04", "CAM-05"]
PERSON_EVENTS = ("ZONE_ENTRY", "ZONE_EXIT", "ZONE_TRANSITION")


def dsn_from_env():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "postgresql://postgres@127.0.0.1:5432/finblade"


def hms(x):
    return dt.datetime.utcfromtimestamp(float(x)).strftime("%H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--route", default=",".join(DEFAULT_ROUTE))
    ap.add_argument("--quiet", action="store_true", help="one summary line only")
    args = ap.parse_args()

    route = [c.strip() for c in args.route.split(",") if c.strip()]
    import psycopg
    c = psycopg.connect(args.dsn or dsn_from_env(), autocommit=True)
    now = float(c.execute("SELECT EXTRACT(EPOCH FROM now())").fetchone()[0])
    lo = now - args.minutes * 60.0

    seen = c.execute(
        "SELECT camera_id, COUNT(*), MIN(ts), MAX(ts) FROM events "
        "WHERE ts >= %s AND event_type = ANY(%s) GROUP BY 1 ORDER BY 1",
        (lo, list(PERSON_EVENTS))).fetchall()

    spans = c.execute("""
        SELECT global_ref, COUNT(DISTINCT camera_id) ncam, COUNT(*) n,
               MIN(ts) lo, MAX(ts) hi
        FROM events
        WHERE global_ref IS NOT NULL AND ts >= %s AND event_type = ANY(%s)
          AND camera_id = ANY(%s)
        GROUP BY 1 ORDER BY ncam DESC, n DESC""",
        (lo, list(PERSON_EVENTS), route)).fetchall()

    best_n, best = 0, None
    for gref, ncam, n, a, b in spans:
        cams = [r[0] for r in c.execute(
            "SELECT DISTINCT camera_id FROM events WHERE global_ref=%s AND ts >= %s",
            (gref, lo)).fetchall()]
        hit = len([x for x in route if x in cams])
        if hit > best_n:
            best_n, best = hit, (gref, cams, a, b, n)

    if args.quiet:
        print("SCORE %d/%d  best=%s" % (best_n, len(route),
                                        best[0] if best else "-"))
        return 0 if best_n == len(route) else 1

    print("=" * 62)
    print("WALK SCORE   route: %s" % " -> ".join(route))
    print("window: last %.0f min, to %s UTC" % (args.minutes, hms(now)))
    print("=" * 62)

    print("\ncameras reporting people")
    if not seen:
        print("  none — nothing is being detected")
    for cam, n, a, b in seen:
        mark = "" if cam in route else "   (not on the route)"
        print("  %-10s %4d events   %s -> %s%s" % (cam, n, hms(a), hms(b), mark))

    tot, res = c.execute(
        "SELECT COUNT(*), SUM(CASE WHEN global_ref IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM events WHERE ts >= %s AND person_ref IS NOT NULL", (lo,)).fetchone()
    print("\n  person events %s, ReID resolved %s (%d%%)"
          % (tot, res or 0, round(100.0 * (res or 0) / max(1, tot))))

    print("\nidentities spanning more than one route camera")
    multi = 0
    for gref, ncam, n, a, b in spans:
        if ncam < 2:
            continue
        multi += 1
        hops = c.execute("""
            SELECT camera_id, MIN(ts) FROM events
            WHERE global_ref=%s AND ts >= %s AND event_type = ANY(%s)
            GROUP BY 1 ORDER BY 2""", (gref, lo, list(PERSON_EVENTS))).fetchall()
        order = [h[0] for h in hops]
        hit = len([x for x in route if x in order])
        print("  %s  %d/%d cams  %s -> %s   %s"
              % (gref, hit, len(route), hms(a), hms(b), " ".join(order)))
    if not multi:
        print("  none — every identity stayed on one camera")

    # What the cameras actually saw between consecutive route cameras. This is
    # the number the topology should be built from.
    print("\nobserved gaps between consecutive route cameras")
    print("  (last sighting on A -> first sighting on B, per pass)")
    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        rows = c.execute("""
            WITH x AS (SELECT ts FROM events WHERE camera_id=%s AND ts >= %s
                         AND event_type = ANY(%s)),
                 y AS (SELECT ts FROM events WHERE camera_id=%s AND ts >= %s
                         AND event_type = ANY(%s))
            SELECT MIN(y.ts - x.ts) FROM x, y WHERE y.ts > x.ts""",
            (a, lo, list(PERSON_EVENTS), b, lo, list(PERSON_EVENTS))).fetchone()
        gap = rows[0] if rows and rows[0] is not None else None
        print("  %-8s -> %-8s  %s" % (a, b,
              "%6.1fs (closest pairing)" % gap if gap is not None else "no pairing"))

    print("\n" + "=" * 62)
    print("SCORE: %d of %d route cameras under one global identity" % (best_n, len(route)))
    if best:
        print("  best: %s  (%d events, %s -> %s)"
              % (best[0], best[4], hms(best[2]), hms(best[3])))
        missing = [x for x in route if x not in best[1]]
        if missing:
            print("  missing:", ", ".join(missing))
    print("=" * 62)
    return 0 if best_n == len(route) else 1


if __name__ == "__main__":
    raise SystemExit(main())
