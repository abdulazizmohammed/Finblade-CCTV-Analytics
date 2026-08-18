#!/usr/bin/env python3
"""Clear telemetry — alerts, history, occupancy — and keep the configuration.

    .venv/bin/python scripts/reset_data.py            # dry run, counts only
    .venv/bin/python scripts/reset_data.py --yes      # actually delete
    .venv/bin/python scripts/reset_data.py --yes --dsn "$DATABASE_URL"

WHAT GOES, AND WHAT STAYS. The split is "things that were observed" versus
"things somebody configured", because after a demo run you want the first gone
and the second exactly as you left it:

    CLEARED   events, zone_state_ts, zone_live, area_state_ts, alerts (and
              their JPEGs), reports, facility_presence / _doors / _meta,
              forwarder_cursors
    KEPT      cameras, zones, physical_areas

Re-adding cameras and redrawing zone polygons is the expensive part and there is
no reason to lose it. For a genuinely blank slate, stop the stack and run
DROP SCHEMA public CASCADE; CREATE SCHEMA public; — the API rebuilds the schema
from services/api/ddl_pg.sql on the next start.

WHY IT REFUSES TO RUN WHILE THE API IS UP. The facility roster lives in memory
in the API process and is flushed to the database periodically. Clearing
facility_presence underneath a running API achieves nothing: the next flush
writes the in-memory roster straight back over it, and the count you thought you
reset reappears. Stop the API first. The same goes for zone_live, which every
reporting camera rewrites within seconds.

Alert snapshots are removed the same way the API does it — refs are read BEFORE
the rows are deleted, because once they are gone nothing knows which JPEGs they
owned, and each path is resolved and confined to evidence/bookmarks so a
malformed ref cannot reach outside it.

WHY TRUNCATE AND NOT DELETE. This ran against SQLite and needed a --vacuum flag,
because DELETE there leaves the file the same size and the surprise ("I cleared
two million rows and reclaimed nothing") was worth a flag. TRUNCATE returns the
space as it goes, so the flag went with the engine.
"""

import argparse
import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOOKMARKS = os.path.join(REPO, "evidence", "bookmarks")

# Observed data. Truncated together in one statement so no foreign key between
# them can object to the order.
TELEMETRY = ["events", "zone_state_ts", "zone_live", "area_state_ts",
             "alerts", "reports", "facility_presence", "facility_doors",
             "facility_meta", "forwarder_cursors"]
CONFIG = ["cameras", "zones", "physical_areas"]


def dsn_from_env() -> str:
    """DATABASE_URL, falling back to .env — the same file start_stack.sh reads.

    Without this the script and the running API can disagree about which
    database is "the" database, which is exactly the split-brain that cost a day
    when start_stack.sh was not reading .env either.
    """
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    path = os.path.join(REPO, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def api_is_up(port: int = 8000) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def existing_tables(conn, wanted):
    """Which of `wanted` are really there, in the order given."""
    have = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    ).fetchall()}
    return [t for t in wanted if t in have]


def counts(conn, tables):
    out = {}
    for t in tables:
        out[t] = conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
    return out


def frame_refs(conn, tables):
    """Snapshot paths owned by alerts, read before anything is deleted."""
    if "alerts" not in tables:
        return []
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'alerts'"
    ).fetchall()}
    if "frame" not in cols:
        return []
    return [r[0] for r in conn.execute(
        "SELECT frame FROM alerts WHERE frame IS NOT NULL AND frame != ''"
    ).fetchall()]


def delete_frames(refs):
    """Unlink snapshots, refusing any path that resolves outside bookmarks/."""
    root = os.path.abspath(BOOKMARKS)
    removed = skipped = 0
    for ref in refs:
        name = str(ref).split("/")[-1]
        path = os.path.abspath(os.path.join(root, name))
        if not path.startswith(root + os.sep):
            skipped += 1
            continue
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            skipped += 1
    return removed, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None,
                    help="Postgres connection string. Defaults to DATABASE_URL, "
                         "then to the DATABASE_URL line in .env")
    ap.add_argument("--yes", action="store_true",
                    help="actually delete; without it this is a dry run")
    ap.add_argument("--keep-frames", action="store_true",
                    help="delete alert rows but leave their JPEGs on disk")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    dsn = args.dsn or dsn_from_env()
    if not dsn:
        print("no --dsn, no DATABASE_URL, and no DATABASE_URL line in .env")
        print("Postgres is the only backend; there is no file to fall back to.")
        return 2

    if args.yes and api_is_up(args.port):
        print("REFUSING: something is listening on :%d." % args.port)
        print()
        print("The facility roster is held in memory by the API and flushed to")
        print("the database periodically, so clearing it underneath a running")
        print("process just gets overwritten on the next flush. zone_live is")
        print("rewritten by every reporting camera within seconds, for the same")
        print("reason.")
        print()
        print("  cd scripts && bash stop_all.sh")
        print("  .venv/bin/python scripts/reset_data.py --yes")
        print("  cd scripts && ./start_stack.sh")
        return 2

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        telemetry = existing_tables(conn, TELEMETRY)
        config = existing_tables(conn, CONFIG)
        before = counts(conn, telemetry)
        kept = counts(conn, config)
        refs = frame_refs(conn, telemetry)

        size, dbname = conn.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database())), "
            "current_database()").fetchone()

        # Host and database name only. The DSN carries a password and this
        # output gets pasted into chat windows.
        where = dsn.split("@")[-1].split("/")[0]
        print("database : %s on %s  (%s)" % (dbname, where, size))
        print()
        print("WILL CLEAR")
        total = 0
        for t, n in before.items():
            total += n
            print("  %-20s %12s rows" % (t, format(n, ",")))
        absent = [t for t in TELEMETRY if t not in before]
        if absent:
            print("  (no such table: %s)" % ", ".join(absent))
        print("  %-20s %12s files" % ("alert snapshots", format(len(refs), ",")))
        print()
        print("WILL KEEP")
        for t, n in kept.items():
            print("  %-20s %12s rows" % (t, format(n, ",")))

        if not args.yes:
            print()
            print("Dry run — nothing deleted. %s rows would go." % format(total, ","))
            print("Re-run with --yes to do it.")
            return 0

        removed = skipped = 0
        if not args.keep_frames and refs:
            removed, skipped = delete_frames(refs)

        # One statement: TRUNCATE takes an ACCESS EXCLUSIVE lock per table, and
        # taking ten of them one at a time is ten chances to deadlock against
        # anything that woke up mid-run.
        conn.execute("TRUNCATE TABLE "
                     + ", ".join('"%s"' % t for t in telemetry)
                     + " RESTART IDENTITY")

        after = conn.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        ).fetchone()[0]

    print()
    print("cleared %s rows across %d tables" % (format(total, ","), len(telemetry)))
    if not args.keep_frames:
        print("deleted %s snapshot file(s)%s"
              % (format(removed, ","), ", %d skipped" % skipped if skipped else ""))
    print("database now %s" % after)
    print()
    print("Cameras and zones are untouched. Start the stack and they report "
          "into an empty history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
