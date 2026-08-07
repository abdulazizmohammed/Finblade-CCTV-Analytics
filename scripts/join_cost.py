"""Read-only: what does "one view with all the data" actually produce?

Before designing it, measure it. The three tables a chatbot wants are a time
series, a stream of discrete events, and a set of lifecycle records — joining
them on (camera, zone) multiplies rather than combines.
"""
import os
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def one(sql):
    return conn.execute(sql).fetchone()[0]


states = one("SELECT COUNT(*) FROM zone_state_ts")
events = one("SELECT COUNT(*) FROM events")
alerts = one("SELECT COUNT(*) FROM alerts")
print(f"zone_state_ts {states:>12,}")
print(f"events        {events:>12,}")
print(f"alerts        {alerts:>12,}")
print(f"sum           {states + events + alerts:>12,}   (a UNION view)")

print("\n-- rows produced by joining them on (camera_id, zone_id) --")
per_zone = conn.execute("""
  SELECT s.camera_id, s.zone_id, COUNT(*) AS states,
         (SELECT COUNT(*) FROM events e
           WHERE e.camera_id = s.camera_id AND e.zone_id = s.zone_id) AS events,
         (SELECT COUNT(*) FROM alerts a
           WHERE a.camera_id = s.camera_id AND a.zone_id = s.zone_id) AS alerts
  FROM zone_state_ts s GROUP BY s.camera_id, s.zone_id""").fetchall()

total = 0
print(f"{'camera / zone':<24} {'states':>10} {'events':>10} {'alerts':>7} {'joined rows':>16}")
print("-" * 72)
for cam, zone, ns, ne, na in per_zone:
    product = ns * max(ne, 1) * max(na, 1)
    total += product
    print(f"{cam + ' / ' + zone:<24} {ns:>10,} {ne:>10,} {na:>7,} {product:>16,}")
print("-" * 72)
print(f"{'TOTAL':<24} {states:>10,} {events:>10,} {alerts:>7,} {total:>16,}")

bytes_per_row = os.path.getsize(db) / max(states + events, 1)
print(f"\nA flat three-way join is {total:,} rows — roughly "
      f"{total * bytes_per_row / 1e12:.1f} TB at this table's bytes-per-row.")
print(f"The source database is {os.path.getsize(db) / 1e9:.2f} GB.")
print("\nThat is the arithmetic behind 'one wide view of everything': a time")
print("series joined to discrete events multiplies, it does not combine.")
