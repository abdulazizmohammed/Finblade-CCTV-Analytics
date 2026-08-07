"""Posts a realistic 10 minutes of zone state over HTTP; reports what stuck.

Driven by scripts/live_state_gate_check.sh, which supplies a running uvicorn on
a throwaway database. Timestamps are synthetic (10 minutes of 5-second ticks
compressed into a couple of seconds of wall clock) but they travel the real
route: HTTP -> auth -> Pydantic -> IngestService -> StateWriteGate -> SQLite.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

PORT = os.environ["FINBLADE_PORT"]
KEY = os.environ["FINBLADE_KEY"]
DB = os.environ["FINBLADE_DB"]
BASE = f"http://127.0.0.1:{PORT}"


def call(path, payload=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        method="GET" if payload is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _body(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _body(exc.read())


def _body(raw):
    # Some routes render HTML; returning the raw text rather than exploding on
    # it keeps a wrong-URL mistake legible instead of a JSONDecodeError.
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"_raw": raw[:200].decode("utf-8", "replace")}


def post_state(ts, occupancy, zone="ZONE-01", camera="CAM-01"):
    return call("/api/v1/zones/state", {
        "zone_id": zone, "camera_id": camera, "ts": ts,
        "occupancy": occupancy, "density": round(occupancy / 12.0, 4),
        "capacity_pct": occupancy * 4.0,
        "inflow_per_min": 0.0, "outflow_per_min": 0.0,
        "status": "WARNING" if occupancy / 12.0 > 2.0 else "NORMAL"})


# Ten minutes of 5-second ticks: empty, a group of four arriving at 4:00 and
# leaving at 5:00, empty again. 120 posts.
now = time.time()
base = now - 600
plan = []
for i in range(120):
    ts = base + i * 5
    occ = 4 if 240 <= i * 5 < 300 else 0
    plan.append((ts, occ))

codes, recorded = set(), 0
for ts, occ in plan:
    code, body = post_state(ts, occ)
    codes.add(code)
    recorded += bool(body.get("recorded"))

print(f"posts sent      : {len(plan)}")
print(f"HTTP codes      : {sorted(codes)}")
print(f"reported written: {recorded}")

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT ts, occupancy, status FROM zone_state_ts ORDER BY ts").fetchall()
live = conn.execute("SELECT ts, occupancy FROM zone_live").fetchall()

print(f"zone_state_ts   : {len(rows)} rows")
print(f"zone_live       : {len(live)} row(s)")
for ts, occ, status in rows:
    print(f"  +{ts - base:6.0f}s  occ={occ}  {status}")

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and condition
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


print()
check("every post accepted", codes == {202}, f"codes={sorted(codes)}")
check("history is sparse", len(rows) < len(plan), f"{len(rows)} of {len(plan)}")
check("both ends of the visit recorded",
      [r[1] for r in rows].count(4) >= 1 and [r[1] for r in rows].count(0) >= 2,
      f"occupancies={[r[1] for r in rows]}")
check("`recorded` flag matches the table", recorded == len(rows),
      f"{recorded} vs {len(rows)}")
check("live reading tracks the last post", live and abs(live[0][0] - plan[-1][0]) < 0.01,
      f"live ts offset {live[0][0] - plan[-1][0] if live else 'n/a'}")
check("live reading is the last occupancy", live and live[0][1] == plan[-1][1])

# The zone must still be visible: /zones/state hides anything older than 30s,
# and the last post is at "now", so a suppressed history write must not have
# stopped the live row from advancing.
code, state = call("/api/v1/zones/state")
zones = state.get("zones", state) if isinstance(state, dict) else state
check("zone still visible on /zones/state", code == 200 and len(zones) == 1,
      f"code={code} zones={len(zones) if isinstance(zones, list) else zones}")

code, health = call("/api/v1/health")
sw = (health.get("checks") or {}).get("state_writes")
print(f"\nhealth state_writes: {json.dumps(sw)}")
check("health reports the gate", isinstance(sw, dict) and sw.get("mode") == "change")
check("health counts suppression", bool(sw) and sw.get("suppressed", 0) > 0)

code, report = call(f"/api/v1/reports/occupancy.json?from={base}&to={now}")
if code == 200 and report.get("zones"):
    z = report["zones"][0]
    print(f"\nreport avg_occupancy   : {z.get('avg_occupancy')}")
    print(f"report sampled avg     : {(z.get('sampled') or {}).get('avg_occupancy')}")
    print(f"report coverage        : {z.get('coverage')}")
    # 4 people for 60s of a 600s window = 0.4 time-weighted. Averaging the
    # surviving rows instead would give roughly 4/len(rows).
    check("report average is time-weighted",
          z.get("avg_occupancy") is not None and abs(z["avg_occupancy"] - 0.4) < 0.05,
          f"got {z.get('avg_occupancy')}")
else:
    print(f"\nreport endpoint returned {code}: {json.dumps(report)[:200]}")
    check("report endpoint reachable", False)

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
