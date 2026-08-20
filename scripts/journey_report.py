#!/usr/bin/env python3
"""How much of a person's path can be reconstructed, and why not more.

    .venv/bin/python scripts/journey_report.py
    .venv/bin/python scripts/journey_report.py --since 2026-08-18T06:00
    .venv/bin/python scripts/journey_report.py --dsn "$DATABASE_URL" --top 20

Reads v_journey_fragments / _links / _traces and reports what they recovered.
Read-only; safe against a live database.

WHAT THIS IS FOR. Tracing works by declining when anything is ambiguous, so
"found nothing" is a normal answer and not an error. The useful question is
therefore never "did it work" but "what stopped it", and every section below
exists to answer that:

  fragments   how chopped up the sightings are. Short single-event fragments
              are the upstream problem no amount of stitching fixes.
  density     how busy the building was. Confidence here is a function of how
              many other people could have been each hop, so a busy window
              genuinely cannot be traced and should say so.
  windows     how wide the surveyed transit windows are. Wide windows admit
              more candidates, and candidates are what destroy uniqueness.
  what-if     unique links against a tightened cap, so the trade between
              window width and traceability is a number rather than an
              argument.

A run with no journeys and a full diagnosis is a SUCCESSFUL run. Silence would
be the failure.
"""

import argparse
import datetime as dt
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

CAPS = (30, 45, 60, 90, 120, 180, 240)


def dsn_from_env() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    path = os.path.join(REPO, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def hhmm(x):
    return dt.datetime.utcfromtimestamp(x).strftime("%H:%M")


def stamp(x):
    return dt.datetime.utcfromtimestamp(x).strftime("%Y-%m-%d %H:%M:%S")


def parse_when(text):
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).replace(
                tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"cannot parse a time from {text!r} — use 2026-08-18T06:00")


def h(title):
    print()
    print(title)
    print("-" * len(title))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--since", default=None, help="2026-08-18T06:00 (UTC)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--top", type=int, default=10,
                    help="longest journeys to list")
    args = ap.parse_args()

    d = args.dsn or dsn_from_env()
    if not d:
        print("no --dsn, no DATABASE_URL, and no DATABASE_URL line in .env")
        return 2

    lo = parse_when(args.since) or 0.0
    hi = parse_when(args.until) or 4_102_444_800.0     # year 2100
    win = (lo, hi)

    import psycopg
    conn = psycopg.connect(d, autocommit=True)

    # Scope the journey views to exactly this window, as TEMP views.
    #
    # The deployed views carry a rolling 24h bound because v_journey_links
    # self-joins fragments and cannot be filtered from outside - its window
    # functions are optimisation fences, so a WHERE on the outer query still
    # builds the whole thing first. Asking for an older window therefore means
    # rebuilding the views around it rather than filtering them.
    #
    # TEMP is what makes that safe against a live database: the views live in
    # this session's pg_temp, unqualified names resolve there first, and the
    # real ones are neither dropped nor touched. The DROPs below are explicitly
    # qualified to pg_temp for the same reason - an unqualified DROP VIEW in a
    # session with no temp view yet would find and remove the production one.
    if args.since or args.until:
        from services.api.analytics_views import view_definitions
        for name, sql in view_definitions(journey_since=lo, journey_until=hi,
                                          temp=True):
            if not name.startswith("v_journey"):
                continue
            conn.execute("DROP VIEW IF EXISTS pg_temp.%s CASCADE" % name)
            conn.execute(sql)
        print("journey views scoped to the requested window (temp, session-only)")

    q = lambda sql, p=win: conn.execute(sql, p).fetchall()

    span = q("""SELECT MIN(ts), MAX(ts), COUNT(*) FROM events
                WHERE ts BETWEEN %s AND %s
                  AND event_type IN ('ZONE_ENTRY','ZONE_EXIT','ZONE_TRANSITION')
             """)[0]
    if not span[0]:
        print("no person-bearing events in that window.")
        return 1

    print("=" * 68)
    print("JOURNEY RECONSTRUCTION REPORT")
    print("=" * 68)
    print("window   %s -> %s UTC" % (stamp(span[0]), stamp(span[1])))
    print("events   %d person-bearing" % span[2])

    # ---------------------------------------------------------------- ReID --
    h("Cross-camera identity, as it stands")
    r = q("""SELECT COUNT(DISTINCT global_ref),
                    COUNT(DISTINCT camera_id||':'||person_ref),
                    SUM(CASE WHEN global_ref IS NULL THEN 1 ELSE 0 END),
                    COUNT(*)
             FROM events WHERE person_ref IS NOT NULL
               AND ts BETWEEN %s AND %s""")[0]
    crossed = q("""SELECT COUNT(*) FROM (
        SELECT global_ref FROM events
        WHERE global_ref IS NOT NULL AND ts BETWEEN %s AND %s
        GROUP BY global_ref HAVING COUNT(DISTINCT camera_id) > 1) x""")[0][0]
    print("  distinct global_ref          %5d" % r[0])
    print("  ...of those, on >1 camera    %5d  (%.0f%%)"
          % (crossed, 100.0 * crossed / max(1, r[0])))
    print("  distinct tracker fragments   %5d" % r[1])
    print("  events ReID never resolved   %5d of %d  (%.0f%%)"
          % (r[2], r[3], 100.0 * r[2] / max(1, r[3])))

    # ----------------------------------------------------------- fragments --
    h("Fragments (one unbroken appearance, one camera)")
    tot = q("""SELECT COUNT(*), COUNT(DISTINCT person_key),
                      SUM(CASE WHEN duration_seconds < 3 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN event_count = 1 THEN 1 ELSE 0 END)
               FROM v_journey_fragments WHERE appeared BETWEEN %s AND %s""")[0]
    print("  %d fragments from %d identities" % (tot[0], tot[1]))
    print("  shorter than 3s              %5d  (%.0f%%)"
          % (tot[2], 100.0 * tot[2] / max(1, tot[0])))
    print("  a single event               %5d  (%.0f%%)"
          % (tot[3], 100.0 * tot[3] / max(1, tot[0])))
    if tot[0] and tot[3] / tot[0] > 0.4:
        print("  ^ A fragment's span comes from its EVENTS, so a track that dies")
        print("    without a ZONE_EXIT collapses to a point and its 'vanished' is")
        print("    really its arrival. That inflates every gap measured from it.")
    for row in q("""SELECT camera_id, COUNT(*),
                           ROUND(AVG(duration_seconds)::numeric,1)
                    FROM v_journey_fragments WHERE appeared BETWEEN %s AND %s
                    GROUP BY camera_id ORDER BY camera_id"""):
        print("    %-10s %4d fragments   avg %6ss" % row)

    # ------------------------------------------------------------- density --
    h("How busy (fragments beginning per 10 minutes)")
    for row in q("""SELECT FLOOR(appeared/600)*600, COUNT(*),
                           COUNT(DISTINCT camera_id)
                    FROM v_journey_fragments WHERE appeared BETWEEN %s AND %s
                    GROUP BY 1 ORDER BY 1"""):
        print("  %s  %4d  %d cam  %s"
              % (hhmm(row[0]), row[1], row[2], "#" * min(50, row[1] // 2)))

    # --------------------------------------------------------------- links --
    h("Candidate hops, and how much competition each had")
    lk = q("""SELECT COUNT(*), SUM(CASE WHEN is_unique THEN 1 ELSE 0 END),
                     SUM(CASE WHEN predecessor_options = 1 THEN 1 ELSE 0 END)
              FROM v_journey_links WHERE left_at BETWEEN %s AND %s""")[0]
    print("  %d candidate links after transitive reduction" % lk[0])
    print("  mutually unique (usable)     %5d" % (lk[1] or 0))
    print("  arrival unique only          %5d" % (lk[2] or 0))
    for row in q("""SELECT predecessor_options, COUNT(*)
                    FROM v_journey_links WHERE left_at BETWEEN %s AND %s
                    GROUP BY 1 ORDER BY 1 LIMIT 8"""):
        print("    %2d candidate(s) per arrival  %5d link(s)" % row)

    # ------------------------------------------------------------- windows --
    h("Surveyed transit windows (wider = more candidates = less traceable)")
    for row in q("""SELECT pair_kind, MIN(min_seconds), MAX(max_seconds),
                           COUNT(*) FROM camera_transits GROUP BY 1 ORDER BY 1""",
                 ()):
        print("  %-12s min %6.0fs   max %6.0fs   %d pair(s)" % row)

    h("What a tighter cap on max_seconds would recover")
    print("  cap    candidates   mutually unique")
    for cap in CAPS:
        r = conn.execute("""
            WITH cand AS (
              SELECT a.fragment_id fa, b.fragment_id fb
              FROM v_journey_fragments a
              JOIN camera_transits t ON t.from_camera = a.camera_id
              JOIN v_journey_fragments b ON b.camera_id = t.to_camera
              WHERE b.fragment_id <> a.fragment_id
                AND t.pair_kind <> 'overlapping'
                AND a.appeared BETWEEN %s AND %s
                AND (b.appeared - a.vanished)
                    BETWEEN t.min_seconds AND LEAST(t.max_seconds, %s)),
            w AS (SELECT COUNT(*) OVER (PARTITION BY fa) s,
                         COUNT(*) OVER (PARTITION BY fb) p FROM cand)
            SELECT COUNT(*), SUM(CASE WHEN s=1 AND p=1 THEN 1 ELSE 0 END)
            FROM w""", (lo, hi, cap)).fetchone()
        print("  %4ds  %10d   %15s" % (cap, r[0], r[1] or 0))
    print("  (no transitive reduction in this what-if, so candidates read high)")

    # -------------------------------------------------------------- traces --
    h("Journeys recovered")
    dist = q("""SELECT journey_hops, COUNT(DISTINCT journey_id)
                FROM v_journey_traces WHERE journey_start BETWEEN %s AND %s
                GROUP BY 1 ORDER BY 1""")
    for hops, n in dist:
        label = "isolated sighting" if hops == 1 else "hop journey"
        print("  %2d-%-18s %5d" % (hops, label, n))

    multi = q("""SELECT journey_id, journey_hops, journey_start, journey_end,
                        COUNT(DISTINCT person_key)
                 FROM v_journey_traces WHERE journey_start BETWEEN %s AND %s
                 GROUP BY 1,2,3,4 HAVING journey_hops > 1
                 ORDER BY journey_hops DESC, journey_end - journey_start DESC
                 LIMIT %s""", (lo, hi, args.top))
    if not multi:
        print()
        print("  NO MULTI-HOP JOURNEY WAS RECOVERABLE.")
        print("  Not an error — every hop had competition, so the views declined")
        print("  rather than guessing. See the sections above for which cause")
        print("  dominates: fragment quality, density, or window width.")
    else:
        h("Longest reconstructed journeys")
        for jid, hops, s, e, ids in multi:
            print("  %d hops  %s -> %s  (%3.0fs)  %d global identities inside"
                  % (hops, hhmm(s), hhmm(e), e - s, ids))
            for row in conn.execute(
                    "SELECT hop_no, camera_id, entry_zone, exit_zone, person_key "
                    "FROM v_journey_traces WHERE journey_id = %s ORDER BY hop_no",
                    (jid,)).fetchall():
                print("      %d  %-9s %-16s -> %-16s  %s" % row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
