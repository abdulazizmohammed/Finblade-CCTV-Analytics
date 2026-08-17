#!/usr/bin/env python3
"""Clear telemetry — alerts, history, occupancy — and keep the configuration.

    .venv/bin/python scripts/reset_data.py                 # dry run, counts only
    .venv/bin/python scripts/reset_data.py --yes           # actually delete
    .venv/bin/python scripts/reset_data.py --yes --vacuum  # ...and shrink the file

WHAT GOES, AND WHAT STAYS. The split is "things that were observed" versus
"things somebody configured", because after a demo run you want the first gone
and the second exactly as you left it:

    CLEARED   events, zone_state_ts, zone_live, area_state_ts, alerts (and
              their JPEGs), reports, facility_presence / _doors / _meta,
              forwarder_cursors
    KEPT      cameras, zones, physical_areas

Re-adding cameras and redrawing zone polygons is the expensive part and there is
no reason to lose it. If you want a genuinely blank slate, stop the stack and
delete data/finblade.db — the schema is rebuilt on the next API start.

WHY IT REFUSES TO RUN WHILE THE API IS UP. The facility roster lives in memory
in the API process and is flushed to disk periodically. Clearing
facility_presence underneath a running API achieves nothing: the next flush
writes the in-memory roster straight back over it, and the count you thought you
reset reappears. Stop the API first. The same goes for zone_live, which every
reporting camera rewrites within seconds.

Alert snapshots are removed the same way the API does it — refs are read BEFORE
the rows are deleted, because once they are gone nothing knows which JPEGs they
owned, and each path is resolved and confined to evidence/bookmarks so a
malformed ref cannot reach outside it.
"""

import argparse
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOOKMARKS = os.path.join(REPO, "evidence", "bookmarks")

# Observed data. Order matters only for readability; there are no FK cascades.
TELEMETRY = ["events", "zone_state_ts", "zone_live", "area_state_ts",
             "alerts", "reports", "facility_presence", "facility_doors",
             "facility_meta", "forwarder_cursors"]
CONFIG = ["cameras", "zones", "physical_areas"]


def api_is_up(port: int = 8000) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def counts(conn, tables):
    out = {}
    for t in tables:
        if table_exists(conn, t):
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def frame_refs(conn):
    """Snapshot paths owned by alerts, read before anything is deleted."""
    if not table_exists(conn, "alerts"):
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "frame" not in cols:
        return []
    return [r[0] for r in conn.execute(
        "SELECT frame FROM alerts WHERE frame IS NOT NULL AND frame != ''")]


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
    ap.add_argument("--db", default=os.path.join(REPO, "data", "finblade.db"))
    ap.add_argument("--yes", action="store_true",
                    help="actually delete; without it this is a dry run")
    ap.add_argument("--keep-frames", action="store_true",
                    help="delete alert rows but leave their JPEGs on disk")
    ap.add_argument("--vacuum", action="store_true",
                    help="rewrite the file to reclaim disk. Takes an exclusive "
                         "lock and rewrites everything — minutes on a large "
                         "database. Without it the file does not shrink.")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no database at {args.db}")
        return 2

    if args.yes and api_is_up(args.port):
        print(f"REFUSING: something is listening on :{args.port}.")
        print()
        print("The facility roster is held in memory by the API and flushed to")
        print("disk periodically, so clearing it underneath a running process")
        print("just gets overwritten on the next flush. zone_live is rewritten")
        print("by every reporting camera within seconds for the same reason.")
        print()
        print("  cd scripts && bash stop_all.sh")
        print("  .venv/bin/python scripts/reset_data.py --yes")
        print("  cd scripts && ./start_stack.sh")
        return 2

    conn = sqlite3.connect(args.db)
    before = counts(conn, TELEMETRY)
    kept = counts(conn, CONFIG)
    refs = frame_refs(conn)

    size_mb = os.path.getsize(args.db) / 1e6
    print(f"database : {args.db}  ({size_mb:,.1f} MB)")
    print()
    print("WILL CLEAR")
    total = 0
    for t, n in before.items():
        total += n
        print(f"  {t:<20} {n:>12,} rows")
    print(f"  {'alert snapshots':<20} {len(refs):>12,} files")
    print()
    print("WILL KEEP")
    for t, n in kept.items():
        print(f"  {t:<20} {n:>12,} rows")

    if not args.yes:
        print()
        print(f"Dry run — nothing deleted. {total:,} rows would go.")
        print("Re-run with --yes to do it.")
        conn.close()
        return 0

    removed = skipped = 0
    if not args.keep_frames and refs:
        removed, skipped = delete_frames(refs)

    for t in before:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()

    print()
    print(f"cleared {total:,} rows across {len(before)} tables")
    if not args.keep_frames:
        print(f"deleted {removed:,} snapshot file(s)"
              + (f", {skipped} skipped" if skipped else ""))

    if args.vacuum:
        print("vacuuming (exclusive lock, rewrites the whole file)...")
        conn.execute("VACUUM")
        conn.commit()
        print(f"file now {os.path.getsize(args.db) / 1e6:,.1f} MB")
    else:
        print("file size unchanged — SQLite keeps freed pages. Add --vacuum to "
              "reclaim the disk.")
    conn.close()

    print()
    print("Cameras and zones are untouched. Start the stack and they report "
          "into an empty history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
