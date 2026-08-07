"""Copy the live SQLite database into Postgres, table by table.

Streams with COPY rather than INSERT — 3.3M rows is enough that the difference
is minutes against an hour. Reads SQLite read-only; the source is never
modified, so this can be run against a live system and re-run if it fails.

Idempotent by truncation: each table is emptied before it is filled, so a
partial run leaves no duplicates. That is safe here because the target is a
migration target, not a second live system. It refuses to run if the target
already holds MORE rows than the source, which is the shape of "someone pointed
this at the wrong database".

Usage:
  .venv/bin/python scripts/pg_migrate.py [sqlite_path] [dsn]
"""
import os
import sqlite3
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_conn                                                     # noqa: E402

src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "data", "finblade.db")
dsn = pg_conn.dsn(sys.argv[2] if len(sys.argv) > 2 else None)

if not os.path.exists(src):
    print(f"no database at {src}")
    raise SystemExit(1)

# sqlite_sequence is SQLite's own bookkeeping for AUTOINCREMENT; Postgres has
# sequences of its own and copying it would be meaningless.
SKIP = {"sqlite_sequence", "sqlite_stat1"}

lite = sqlite3.connect(f"file:{os.path.abspath(src)}?mode=ro", uri=True)
tables = [r[0] for r in lite.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    if r[0] not in SKIP]

print(f"source: {src} ({os.path.getsize(src) / 1e9:.2f} GB)")
print(f"target: {dsn.split('?')[0]}")
print()

total_rows = 0
started = time.time()

with pg_conn.connect(dsn) as pg:
    # Column intersection, not "SELECT *". The generated DDL mirrors SQLite, but
    # if the two ever diverge, copying positionally would put values in the
    # wrong columns silently. Naming them makes a divergence an error.
    for table in tables:
        lite_cols = [r[1] for r in lite.execute(f"PRAGMA table_info({table})")]
        pg_cols = [r[0] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,))]
        if not pg_cols:
            print(f"  {table:<20} SKIPPED — no such table in Postgres")
            continue
        cols = [c for c in lite_cols if c in pg_cols]
        missing = [c for c in lite_cols if c not in pg_cols]
        if missing:
            print(f"  {table:<20} WARNING — Postgres is missing {missing}")

        n_src = lite.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_dst = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if n_dst > n_src:
            print(f"  {table:<20} REFUSING — target has {n_dst:,} rows against "
                  f"{n_src:,} in the source; wrong database?")
            raise SystemExit(2)

        t0 = time.time()
        pg.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
        collist = ", ".join(cols)
        copy_sql = f"COPY {table} ({collist}) FROM STDIN"
        rows = 0
        with pg.cursor().copy(copy_sql) as cp:
            for row in lite.execute(f"SELECT {collist} FROM {table}"):
                cp.write_row(row)
                rows += 1
        total_rows += rows
        secs = time.time() - t0
        rate = rows / secs if secs > 0.01 else 0
        print(f"  {table:<20} {rows:>12,} rows  {secs:>6.1f}s  {rate:>10,.0f}/s")

    # BIGSERIAL sequences do not know about rows inserted with an explicit id.
    # Without this the next INSERT collides with an existing primary key — and
    # it would happen on the first alert raised after the migration, not here.
    print("\n== resetting identity sequences")
    for table, col in (("alerts", "alert_id"), ("reports", "report_id")):
        seq = pg.execute("SELECT pg_get_serial_sequence(%s, %s)",
                         (table, col)).fetchone()[0]
        if not seq:
            continue
        pg.execute(
            f"SELECT setval(%s, COALESCE((SELECT MAX({col}) FROM {table}), 1))",
            (seq,))
        nxt = pg.execute(f"SELECT last_value FROM {seq}").fetchone()[0]
        print(f"  {seq} -> {nxt}")

    print("\n== row counts, source vs target")
    mismatch = []
    for table in tables:
        a = lite.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        b = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        flag = "ok" if a == b else "MISMATCH"
        if a != b:
            mismatch.append(table)
        print(f"  {table:<20} sqlite={a:>12,}  postgres={b:>12,}  {flag}")

    print("\n== ANALYZE (the planner has no statistics until this runs)")
    pg.execute("ANALYZE")

print(f"\n{total_rows:,} rows in {time.time() - started:.1f}s")
if mismatch:
    print(f"MISMATCHED TABLES: {mismatch}")
raise SystemExit(1 if mismatch else 0)
