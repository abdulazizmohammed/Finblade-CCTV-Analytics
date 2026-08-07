"""Is our store relational? Join it, view it, constrain it, roll it back.

Written to answer that as a demonstration rather than an assertion. Operates on
a COPY — CREATE VIEW is a write, and this proves a point, it does not change
production.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

src = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(src):
    print(f"no database at {src}")
    raise SystemExit(0)

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "copy.db")
shutil.copy(src, db)
conn = sqlite3.connect(db)


def show(title, sql, params=()):
    print(f"\n== {title}")
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    if cols:
        width = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
                 for i, c in enumerate(cols)]
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, width)))
        print("  " + "  ".join("-" * w for w in width))
        for r in rows:
            print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, width)))
    return rows


show("indexes the schema declares",
     "SELECT tbl_name AS \"table\", name AS \"index\" FROM sqlite_master "
     "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name, name")

show("a three-table join: zones x latest state x alerts", """
  SELECT z.camera_id, z.zone_id, z.zone_name,
         l.occupancy, l.status, COUNT(a.alert_id) AS alerts
  FROM zones z
  LEFT JOIN zone_live l ON l.zone_id = z.zone_id AND l.camera_id = z.camera_id
  LEFT JOIN alerts    a ON a.zone_id = z.zone_id AND a.camera_id = z.camera_id
  GROUP BY z.camera_id, z.zone_id
  ORDER BY z.camera_id, z.zone_id""")

# The view FinBlade would want, written in SQLite's own SQL. Window functions
# and all — this is the "hold each reading forward" rule expressed as a view.
conn.execute("""
  CREATE VIEW zone_occupancy_changes AS
  SELECT camera_id, zone_id, ts, occupancy, status,
         LAG(occupancy) OVER w AS previous,
         LEAD(ts)       OVER w AS held_until
  FROM zone_state_ts
  WINDOW w AS (PARTITION BY camera_id, zone_id ORDER BY ts)""")
show("CREATE VIEW with a window function — the hold-forward rule as SQL", """
  SELECT camera_id, zone_id, previous, occupancy,
         ROUND(held_until - ts, 1) AS held_seconds
  FROM zone_occupancy_changes
  WHERE previous IS NOT NULL AND occupancy <> previous
  ORDER BY ts DESC LIMIT 5""")

print("\n== transactions really roll back")
before = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
conn.execute("BEGIN")
conn.execute("DELETE FROM alerts")
during = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
conn.rollback()
after = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
print(f"  before={before}  inside txn={during}  after rollback={after}")

print("\n== what it does NOT have")
print("  A listening socket. There is no host:port for psql, Metabase, Power BI")
print("  or a chatbot to connect to — the database IS this file. That, and")
print("  nothing about being relational, is why direct access is awkward.")
print(f"  file: {os.path.getsize(src) / 1e9:.2f} GB at {src}")

conn.close()
shutil.rmtree(tmp, ignore_errors=True)
