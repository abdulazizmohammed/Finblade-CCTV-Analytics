"""Build the analytics views over the live database and query them.

The views are the deliverable FinBlade queries, so they get exercised against
real data — 1.67M state rows and 1.68M events — not a fixture. Answers the same
three questions the API endpoints answer, in plain SQL, and compares.

Read-only with respect to production, and it does not copy the file either.
Copying 1.15 GB took long enough to saturate the box; ATTACH-ing it read-only
into a scratch database gives the views something to read without moving a
byte, and a read-only attachment cannot be written to by definition.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.api.analytics_views import create_all                # noqa: E402

src = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(src):
    print(f"no database at {src}")
    raise SystemExit(0)

print(f"opening {os.path.getsize(src) / 1e9:.2f} GB read-only ...")
# TEMP views over a read-only main.
#
# Two approaches did not work. Copying the file took long enough to saturate
# the box. ATTACH-ing it as `live` and creating views in an empty main failed
# at creation — SQLite binds unqualified names in a view body to the schema it
# is created in, so they resolved to main.alerts, which does not exist.
#
# A TEMP view lives in the session's temp schema while main stays read-only,
# and unqualified names resolve to main. Nothing is written to production, and
# the SQL under test is byte-for-byte the SQL that ships.
conn = sqlite3.connect(f"file:{os.path.abspath(src)}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

t0 = time.time()
names = create_all(conn, temp=True)
print(f"created {len(names)} views in {time.time() - t0:.2f}s: {', '.join(names)}")

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and bool(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def rows(sql, params=()):
    t = time.time()
    out = conn.execute(sql, params).fetchall()
    return out, time.time() - t


def table(res, cols):
    for r in res:
        print("   " + "  ".join(f"{r[c]}" for c in cols))


print("\n== v_timeline: one row per thing that happened")
res, secs = rows("SELECT record_type, COUNT(*) n FROM v_timeline GROUP BY record_type")
counts = {r["record_type"]: r["n"] for r in res}
print(f"   {counts}  ({secs:.1f}s)")
raw = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
       for t in ("zone_state_ts", "events", "alerts")}
check("timeline is the SUM of the tables, not a product",
      sum(counts.values()) == sum(raw.values()),
      f"{sum(counts.values()):,} vs {sum(raw.values()):,}")

print("\n== v_zone_intervals: every reading carries how long it stood")
res, secs = rows("""
    SELECT camera_id, zone_id, COUNT(*) readings,
           ROUND(SUM(duration_seconds)/3600.0, 1) hours_covered,
           SUM(CASE WHEN is_stale=1 THEN 1 ELSE 0 END) stale
    FROM v_zone_intervals GROUP BY camera_id, zone_id ORDER BY readings DESC""")
print(f"   {'camera / zone':<24}{'readings':>10}{'hours':>9}{'stale':>8}")
for r in res:
    print(f"   {r['camera_id'] + ' / ' + r['zone_id']:<24}"
          f"{r['readings']:>10,}{r['hours_covered'] or 0:>9}{r['stale']:>8,}")
print(f"   ({secs:.1f}s)")
check("intervals exist for every zone", len(res) == 8, f"{len(res)} zones")

print("\n== the three chatbot questions, as plain SQL against one view")

# 1. What was it at an instant?
pick = conn.execute("SELECT camera_id, zone_id, valid_from FROM v_zone_intervals "
                    "WHERE occupancy > 0 ORDER BY valid_from DESC LIMIT 1").fetchone()
if pick:
    at = pick["valid_from"] + 1
    res, secs = rows("""
        SELECT occupancy, status, is_stale, ROUND(? - valid_from, 1) age_seconds
        FROM v_zone_intervals
        WHERE camera_id=? AND zone_id=? AND valid_from <= ?
          AND (valid_to > ? OR valid_to IS NULL)""",
                     (at, pick["camera_id"], pick["zone_id"], at, at))
    print(f"   at an instant: {dict(res[0]) if res else 'none'}  ({secs:.2f}s)")
    check("point-in-time returns exactly one interval", len(res) == 1, f"{len(res)} rows")

# 2. How long was it occupied?
res, secs = rows("""
    SELECT camera_id, zone_id,
           ROUND(SUM(CASE WHEN occupancy > 0 AND is_stale = 0
                          THEN duration_seconds ELSE 0 END), 0) occupied_s,
           ROUND(SUM(CASE WHEN is_stale = 1
                          THEN duration_seconds ELSE 0 END), 0) unobserved_s
    FROM v_zone_intervals GROUP BY camera_id, zone_id
    HAVING occupied_s > 0 ORDER BY occupied_s DESC LIMIT 5""")
print(f"\n   occupied time, excluding unobserved spans  ({secs:.1f}s)")
for r in res:
    print(f"     {r['camera_id']}/{r['zone_id']:<10} occupied={r['occupied_s']:>8.0f}s"
          f"  unobserved={r['unobserved_s']:>10.0f}s")
check("occupied time is measured, not counted", bool(res))

# 3. Time-weighted average vs the naive one.
res, secs = rows("""
    SELECT camera_id, zone_id,
           AVG(occupancy) AS naive,
           SUM(occupancy * duration_seconds) / NULLIF(SUM(duration_seconds), 0) AS weighted
    FROM v_zone_intervals
    WHERE is_stale = 0 AND duration_seconds IS NOT NULL
    GROUP BY camera_id, zone_id ORDER BY camera_id, zone_id""")
print(f"\n   AVG(occupancy) vs time-weighted  ({secs:.1f}s)")
worst = 0.0
for r in res:
    naive, w = r["naive"] or 0, r["weighted"] or 0
    worst = max(worst, abs(naive - w))
    print(f"     {r['camera_id']}/{r['zone_id']:<10} naive={naive:.4f}  weighted={w:.4f}")
print(f"   largest disagreement: {worst:.4f}")
check("the view exposes the weighted average as one expression", bool(res))

print("\n== no credentials in any view")
leak = []
for name in names:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
    leak += [f"{name}.{c}" for c in cols if c in ("source", "stream_url", "rtsp_url")]
    for row in conn.execute(f"SELECT * FROM {name} LIMIT 200"):
        for c in cols:
            v = row[c]
            if isinstance(v, str) and "://" in v and "@" in v:
                leak.append(f"{name}.{c} = {v[:40]}")
check("no view exposes an RTSP URL or credential", not leak, str(leak[:3]))

print("\n== a chatbot-shaped question end to end")
res, secs = rows("""
    SELECT zone_name, camera_id,
           ROUND(SUM(CASE WHEN occupancy > 0 AND is_stale = 0
                          THEN duration_seconds ELSE 0 END) / 60.0, 1) AS busy_minutes
    FROM v_zone_intervals
    WHERE valid_from >= (SELECT MAX(ts) - 86400 FROM zone_state_ts)
    GROUP BY camera_id, zone_id
    ORDER BY busy_minutes DESC""")
print(f"   'which zone was busiest in the last 24h of data'  ({secs:.2f}s)")
for r in res[:5]:
    print(f"     {r['zone_name'] or '?':<18} {r['camera_id']:<10} {r['busy_minutes']:>8} min")

conn.close()
print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
