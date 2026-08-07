"""Read-only: does time-weighting reproduce the old average on real data?

The migration safety net for Part A step 3. On today's evenly-spaced 5-second
samples the two must agree — AVG() over rows is correct precisely because every
row covers the same duration. If they disagree here, the new maths is wrong
before sparse data ever reaches it.

Opens the database read-only; changes nothing.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from finblade.timeweight import time_weighted            # noqa: E402

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

zones = conn.execute(
    "SELECT camera_id, zone_id, COUNT(*) n, MIN(ts) lo, MAX(ts) hi "
    "FROM zone_state_ts GROUP BY camera_id, zone_id ORDER BY n DESC").fetchall()
if not zones:
    print("no zone state in this database")
    raise SystemExit(0)

CADENCE = 5.0          # ZoneStateAggregator(period_s=5.0) in run_cpu.py

print(f"{'camera / zone':<26} {'rows':>8} {'AVG()':>9} {'weighted':>9} "
      f"{'diff':>8} {'cover':>6} {'medgap':>7}")
print("-" * 80)

worst = 0.0
for z in zones:
    rows = conn.execute(
        "SELECT ts, occupancy, density, capacity_pct FROM zone_state_ts "
        "WHERE camera_id IS ? AND zone_id=? ORDER BY ts",
        (z["camera_id"], z["zone_id"])).fetchall()
    samples = [dict(r) for r in rows]
    t0, t1 = z["lo"], z["hi"]
    if t1 <= t0:
        continue

    plain = sum(r["occupancy"] for r in rows) / len(rows)
    gaps = sorted(samples[i + 1]["ts"] - samples[i]["ts"]
                  for i in range(len(samples) - 1))
    median_gap = gaps[len(gaps) // 2] if gaps else CADENCE
    # max_hold comes from the CONFIGURED cadence, not the observed median.
    #
    # Inferring it from the data was wrong here: the median gap in this
    # database is 0.1s, not 5s, because several workers ran against the same
    # camera_id at once and their writes interleave. Three times that made
    # max_hold 0.3s and marked 85% of the window unknown — a heuristic that
    # looked principled and measured the wrong thing.
    result = time_weighted(samples, t0, t1 + CADENCE, ("occupancy",),
                           max_hold=CADENCE * 3)
    tw = result["fields"]["occupancy"]["mean"] or 0.0

    diff = abs(plain - tw)
    worst = max(worst, diff)
    label = f"{z['camera_id']} / {z['zone_id']}"
    print(f"{label:<26} {z['n']:>8,} {plain:>9.4f} {tw:>9.4f} "
          f"{diff:>8.5f} {result['coverage']:>6.2f} {median_gap:>7.2f}")

print("-" * 80)
print(f"largest disagreement: {worst:.6f}")
print(f"max_hold: {CADENCE * 3:.0f}s (3x the configured 5s aggregation period)")
print("\nCoverage below 1.00 is the honest reading: these cameras were started")
print("and stopped across the nine days, and the old AVG() counted every hour")
print("the worker was down as if the last sample had been observed throughout.")
print("\nA median gap far below 5s means several workers wrote for the same")
print("camera_id concurrently — worth knowing before trusting this history.")
