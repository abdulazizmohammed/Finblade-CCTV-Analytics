"""Read-only: how long does the zone_live backfill take on the real database?

The migration runs once, on the first open after upgrading, and holds the
connection while it does. On a 1.6M-row zone_state_ts that is worth measuring
before it happens on a live box rather than after.

Opens with mode=ro — this script cannot modify anything.
"""
import os
import sqlite3
import sys
import time

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
size_mb = os.path.getsize(db) / 1e6
rows = conn.execute("SELECT COUNT(*) FROM zone_state_ts").fetchone()[0]
print(f"database   : {db}  ({size_mb:.0f} MB)")
print(f"zone_state_ts rows: {rows:,}")

print("\n== the backfill SELECT (what _migrate runs once) ==")
t0 = time.monotonic()
picked = conn.execute(
    "SELECT COUNT(*) FROM zone_state_ts WHERE id IN "
    "(SELECT MAX(id) FROM zone_state_ts GROUP BY zone_id, camera_id)"
).fetchone()[0]
print(f"  {time.monotonic() - t0:.2f}s, selects {picked} row(s) — one per zone")

print("\n== the OLD live-state query, run on every dashboard poll ==")
t0 = time.monotonic()
conn.execute(
    "SELECT zone_id,camera_id,ts,occupancy FROM zone_state_ts WHERE id IN "
    "(SELECT MAX(id) FROM zone_state_ts GROUP BY zone_id, camera_id)"
).fetchall()
old = time.monotonic() - t0
print(f"  {old:.3f}s")

print("\n== the NEW query, against a table of that size ==")
conn.execute("CREATE TEMP TABLE zone_live_probe AS "
             "SELECT zone_id,camera_id,ts,occupancy FROM zone_state_ts WHERE id IN "
             "(SELECT MAX(id) FROM zone_state_ts GROUP BY zone_id, camera_id)")
t0 = time.monotonic()
conn.execute("SELECT zone_id,camera_id,ts,occupancy FROM zone_live_probe").fetchall()
new = time.monotonic() - t0
print(f"  {new:.4f}s")
if new > 0:
    print(f"\n  {old / new:.0f}x faster on this data "
          f"(and it stops growing with history)")
