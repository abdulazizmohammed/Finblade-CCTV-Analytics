"""Read-only: a real DENSITY_UPDATE event next to the zone_state_ts row
written at the same instant, from the live database."""
import json
import os
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

row = conn.execute(
    "SELECT * FROM events WHERE event_type='DENSITY_UPDATE' "
    "ORDER BY ts DESC LIMIT 1").fetchone()
if row is None:
    print("no DENSITY_UPDATE events in this database")
    raise SystemExit(0)

print("=" * 62)
print("ONE DENSITY_UPDATE, as stored in the events table")
print("=" * 62)
evt = {k: row[k] for k in row.keys() if row[k] is not None}
payload = evt.pop("payload", None)
if payload:
    try:
        evt.update(json.loads(payload))
    except Exception:
        pass
print(json.dumps(evt, indent=2))

print()
print("=" * 62)
print("The zone_state_ts row written at the same moment")
print("=" * 62)
state = conn.execute(
    "SELECT zone_id, camera_id, ts, occupancy, density, capacity_pct, status, "
    "peak_occupancy, avg_occupancy, trend, inflow, outflow "
    "FROM zone_state_ts WHERE zone_id=? AND ABS(ts-?) < 0.01 LIMIT 1",
    (row["zone_id"], row["ts"])).fetchone()
if state is None:
    print("  (no zone_state_ts row within 10ms — unusual)")
else:
    print(json.dumps({k: state[k] for k in state.keys()}, indent=2))

print()
print("=" * 62)
print("How many of each, over the whole database")
print("=" * 62)
d = conn.execute(
    "SELECT COUNT(*) FROM events WHERE event_type='DENSITY_UPDATE'").fetchone()[0]
z = conn.execute("SELECT COUNT(*) FROM zone_state_ts").fetchone()[0]
other = conn.execute(
    "SELECT COUNT(*) FROM events WHERE event_type!='DENSITY_UPDATE'").fetchone()[0]
print(f"  DENSITY_UPDATE events   {d:>10,}")
print(f"  zone_state_ts rows      {z:>10,}")
print(f"  every other event type  {other:>10,}")
print(f"\n  difference between the first two: {abs(d - z):,}")
