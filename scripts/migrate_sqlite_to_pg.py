#!/usr/bin/env python3
"""Copy a SQLite database into Postgres, table by table.

    # look, change nothing
    .venv/bin/python scripts/migrate_sqlite_to_pg.py --dsn "$DATABASE_URL"

    # configuration only — cameras, zones, areas. Start history fresh.
    .venv/bin/python scripts/migrate_sqlite_to_pg.py --dsn "$DATABASE_URL" \
        --config-only --yes

    # everything, including millions of telemetry rows
    .venv/bin/python scripts/migrate_sqlite_to_pg.py --dsn "$DATABASE_URL" --yes

RUN WITH THE STACK STOPPED. Rows are read from SQLite while the API may still be
writing to it, so a live migration copies a moving target: the last few seconds
arrive in Postgres or they do not, depending on timing. Worse, both databases
are then authoritative for a while and whichever one the API restarts against
silently wins. Stop, migrate, switch, start.

WHICH TABLES ARE WORTH MOVING. Configuration always: re-adding cameras and
redrawing zone polygons is real work and cannot be recovered from anywhere else.
Telemetry is a judgement call — on the dev box that is 4.5 million rows of
history whose main use is a demo that has already happened. --config-only exists
because starting Postgres with a clean history and correct configuration is
usually what people actually want, and it takes seconds rather than an hour.

IDEMPOTENT WHERE IT CAN BE. Tables with a primary key upsert, so re-running
after a partial failure resumes rather than duplicating. zone_state_ts,
area_state_ts and events-without-ids are append-only by nature; --truncate
clears the destination table first so a re-run does not double them.

NEVER TOUCHES THE SOURCE. SQLite is opened read-only. If the migration goes
wrong the original is exactly as it was and you can start the stack back up on
it while working out why.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

# Ordered: configuration first, so a --config-only run leaves a usable system,
# and so that anything referencing a camera or zone arrives after it exists.
CONFIG_TABLES = ["cameras", "zones", "physical_areas"]
TELEMETRY_TABLES = ["events", "zone_state_ts", "zone_live", "area_state_ts",
                    "alerts", "reports", "facility_presence", "facility_doors",
                    "facility_meta", "forwarder_cursors"]

# Primary keys, for ON CONFLICT. Tables absent here are append-only.
CONFLICT_KEY = {
    "cameras": "camera_id",
    "physical_areas": "area_id",
    "events": "event_id",
    "zone_live": "camera_id,zone_id",
    "facility_presence": "ref",
    "facility_doors": "door_zone_id",
    "facility_meta": "key",
    "forwarder_cursors": "name",
}

BATCH = 5000


def sqlite_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def pg_columns(pg, table):
    with pg.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s", (table,))
        return {r[0] for r in cur.fetchall()}


def copy_table(sq, pg, table, truncate=False, echo=print):
    """Copy one table. Returns rows written."""
    src_cols = sqlite_columns(sq, table)
    if not src_cols:
        echo(f"  {table:<20} not in source, skipped")
        return 0
    dest_cols = pg_columns(pg, table)
    if not dest_cols:
        echo(f"  {table:<20} NOT IN POSTGRES — run the DDL first")
        return 0

    # Only columns both sides have. A column the destination lacks means the
    # schema is behind; say so rather than failing halfway through the copy.
    cols = [c for c in src_cols if c in dest_cols]
    dropped = [c for c in src_cols if c not in dest_cols]
    if dropped:
        echo(f"  {table:<20} WARNING: destination lacks {', '.join(dropped)}"
             f" — those values will not be migrated")

    total = sq.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if not total:
        echo(f"  {table:<20} empty")
        return 0

    with pg.cursor() as cur:
        if truncate:
            cur.execute(f"TRUNCATE TABLE {table}")

        collist = ",".join(cols)
        holders = ",".join(["%s"] * len(cols))
        key = CONFLICT_KEY.get(table)
        if key:
            updates = ",".join(f"{c}=excluded.{c}" for c in cols
                               if c not in key.split(","))
            sql = (f"INSERT INTO {table} ({collist}) VALUES ({holders}) "
                   f"ON CONFLICT ({key}) DO UPDATE SET {updates}"
                   if updates else
                   f"INSERT INTO {table} ({collist}) VALUES ({holders}) "
                   f"ON CONFLICT ({key}) DO NOTHING")
        else:
            sql = f"INSERT INTO {table} ({collist}) VALUES ({holders})"

        written = 0
        cursor = sq.execute(f"SELECT {collist} FROM {table}")
        while True:
            rows = cursor.fetchmany(BATCH)
            if not rows:
                break
            cur.executemany(sql, [tuple(r) for r in rows])
            written += len(rows)
            # Carriage-return progress only on a terminal. Redirected to a log
            # it produces one long unreadable line instead of a record.
            if total > BATCH and sys.stdout.isatty():
                print(f"  {table:<20} {written:>10,} / {total:,}",
                      end="\r", flush=True)
    if total > BATCH and sys.stdout.isatty():
        print("")
    echo(f"  {table:<20} {written:>10,} rows")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", default=os.path.join(REPO, "data", "finblade.db"))
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"),
                    help="destination Postgres DSN (or set DATABASE_URL)")
    ap.add_argument("--yes", action="store_true", help="actually copy")
    ap.add_argument("--config-only", action="store_true",
                    help="cameras, zones and areas only — no history")
    ap.add_argument("--truncate", action="store_true",
                    help="empty each destination table first; use when re-running "
                         "after a partial copy of append-only tables")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if not args.dsn:
        print("no --dsn and no DATABASE_URL")
        return 2
    if not os.path.exists(args.sqlite):
        print(f"no SQLite database at {args.sqlite}")
        return 2

    if args.yes:
        import socket
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", args.port)) == 0:
                print(f"REFUSING: something is listening on :{args.port}.")
                print()
                print("Migrating from a database that is still being written to")
                print("copies a moving target, and leaves two authoritative")
                print("copies with the API silently picking one on restart.")
                print()
                print("  cd scripts && bash stop_all.sh")
                return 2

    tables = CONFIG_TABLES + ([] if args.config_only else TELEMETRY_TABLES)

    sq = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    print(f"source : {args.sqlite} ({os.path.getsize(args.sqlite) / 1e6:,.1f} MB)")
    print(f"dest   : {args.dsn.split('@')[-1]}")
    print(f"scope  : {'configuration only' if args.config_only else 'everything'}")
    print()

    if not args.yes:
        print("WOULD COPY")
        grand = 0
        for t in tables:
            try:
                n = sq.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                print(f"  {t:<20} not in source")
                continue
            grand += n
            print(f"  {t:<20} {n:>12,} rows")
        print()
        print(f"Dry run — nothing written. {grand:,} rows total.")
        print("Re-run with --yes.")
        return 0

    import psycopg
    started = time.time()
    with psycopg.connect(args.dsn, autocommit=True) as pg:
        # Make sure the destination schema exists and is current before copying
        # into it — otherwise a missing column is discovered mid-table.
        from services.api.postgres_store import DDL_PATH
        with open(DDL_PATH) as fh:
            pg.execute(fh.read())
        print("schema applied (idempotent)")
        print()
        print("COPYING")
        grand = 0
        for t in tables:
            try:
                grand += copy_table(sq, pg, t, truncate=args.truncate)
            except sqlite3.OperationalError as exc:
                print(f"  {t:<20} not in source ({exc})")

    print()
    print(f"copied {grand:,} rows in {time.time() - started:,.1f}s")
    print()
    print("The SQLite file is untouched. To switch over:")
    print("  1. set DATABASE_URL in ~/finblade-cctv/.env")
    print("  2. start the stack")
    print("  3. .venv/bin/python scripts/db_schema_check.py --dsn \"$DATABASE_URL\"")
    print("  4. .venv/bin/python scripts/pg_apply.py \"$DATABASE_URL\"   # analytics views")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
