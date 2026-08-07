"""Read-only: replay the real zone_state_ts history through the gate.

Answers the only question that matters about step 4 — how many of the rows
already on disk would it have kept? The unit tests use synthetic traffic; this
uses nine days of what the cameras actually posted.

Changes nothing. Opens the database read-only.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from finblade.emission import StateWriteGate                # noqa: E402

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
keepalive = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
total = conn.execute("SELECT COUNT(*) FROM zone_state_ts").fetchone()[0]
if not total:
    print("no zone state in this database")
    raise SystemExit(0)

gate = StateWriteGate(keepalive_s=keepalive)
per_zone = {}
for camera_id, zone_id, ts, occ, status in conn.execute(
        "SELECT camera_id, zone_id, ts, occupancy, status FROM zone_state_ts "
        "ORDER BY ts"):
    kept = gate.should_write(camera_id, zone_id, occ, status, ts)
    slot = per_zone.setdefault((camera_id, zone_id), [0, 0])
    slot[0] += 1
    slot[1] += kept

page = conn.execute("PRAGMA page_size").fetchone()[0]
bytes_per_row = os.path.getsize(db) / total          # crude, includes indexes

print(f"database        : {db}  ({os.path.getsize(db) / 1e9:.2f} GB)")
print(f"keepalive       : {keepalive:.0f}s")
print(f"page size       : {page}")
print()
print(f"{'camera / zone':<26} {'rows':>10} {'kept':>9} {'dropped':>8}")
print("-" * 58)
for (cam, zone), (rows, kept) in sorted(per_zone.items(),
                                        key=lambda kv: -kv[1][0]):
    pct = 100.0 * (rows - kept) / rows if rows else 0
    print(f"{str(cam) + ' / ' + str(zone):<26} {rows:>10,} {kept:>9,} {pct:>7.1f}%")

kept = gate.written
print("-" * 58)
print(f"{'TOTAL':<26} {total:>10,} {kept:>9,} "
      f"{100.0 * (total - kept) / total:>7.1f}%")
print()
print(f"projected size  : {kept * bytes_per_row / 1e6:.0f} MB, "
      f"down from {total * bytes_per_row / 1e6:.0f} MB")
print()
print("Rows are only dropped where occupancy and status both repeat, so every")
print("transition survives. What is lost is the resolution of avg_occupancy,")
print("trend and the rolling flow rates between transitions — up to one")
print("keepalive interval stale in the history, exact in the live reading.")
