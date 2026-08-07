"""Drives the three Part B endpoints over real HTTP, then the tool dispatch.

Unit tests exercise the service class. This proves the routes are wired, the
query aliases parse, the error statuses come out as HTTP codes, and the tool
layer in integrations/finblade_ai produces requests those routes accept.

Driven by scripts/live_chatbot_check.sh, which supplies a uvicorn on a
throwaway database.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

PORT = os.environ["FINBLADE_PORT"]
KEY = os.environ["FINBLADE_KEY"]
BASE = f"http://127.0.0.1:{PORT}"

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and bool(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def call(path, payload=None, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        BASE + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        method="GET" if payload is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"_raw": raw[:200].decode("utf-8", "replace")}


def post_state(ts, occupancy, zone="ZONE-01", camera="CAM-01", status="NORMAL"):
    return call("/api/v1/zones/state", {
        "zone_id": zone, "camera_id": camera, "ts": ts,
        "occupancy": occupancy, "density": round(occupancy / 12.0, 4),
        "capacity_pct": occupancy * 4.0, "status": status,
        "inflow_per_min": 0.0, "outflow_per_min": 0.0})


def emit(etype, ts, camera="CAM-01"):
    evt = {"event_id": f"{etype}-{ts}-{camera}", "event_type": etype,
           "camera_id": camera, "site_id": "SITE-01", "timestamp": ts}
    if etype == "CAMERA_OFFLINE":
        evt["last_seen"] = ts - 31.0
    return call("/api/v1/events/ingest", evt)


# ---------------------------------------------------------------- fixture ---
# Four hours, ending now. Empty, then six people for the third hour, then a
# twenty-minute logged outage, then empty again. Keepalives throughout, as the
# real writer produces them.
now = time.time()
start = now - 4 * 3600
KEEP = 300.0

print("posting four hours of traffic...")
codes = set()
t = start
while t < now:
    into = t - start
    if 7200 <= into < 10800:
        occ = 6
    else:
        occ = 0
    if 10800 <= into < 12000:            # camera down; nothing posted
        t += KEEP
        continue
    codes.add(post_state(t, occ)[0])
    t += KEEP
emit("CAMERA_OFFLINE", start + 10800)
emit("CAMERA_ONLINE", start + 12000)
check("all state posts accepted", codes == {202}, f"codes={sorted(codes)}")

# A second camera with the SAME zone id, to prove ambiguity is refused.
call("/api/v1/zones", {"camera_id": "CAM-02", "zones": [
    {"zone_id": "ZONE-01", "zone_name": "Loading bay", "area_sqm": 30.0,
     "polygon": [[0, 0], [1, 0], [1, 1]]}]})
post_state(now - 60, 1, camera="CAM-02")

# ----------------------------------------------------------------- series ---
print("\n== /series")
code, body = call("/api/v1/zones/ZONE-01/series",
                  params={"from": start, "to": now, "bucket": 600,
                          "camera_id": "CAM-01"})
check("series returns 200", code == 200, str(body)[:120])
if code == 200:
    pts = body["points"]
    filled = [p for p in pts if p["occupancy"] is not None]
    blank = [p for p in pts if p["occupancy"] is None]
    busy = [p for p in pts if (p["occupancy"] or 0) > 5]
    print(f"  buckets={len(pts)} filled={len(filled)} null={len(blank)} "
          f"coverage={body['coverage']}")
    print(f"  rows stored in window: {body['rows_in_window']} "
          f"(vs {int(4 * 3600 / 5)} posts a 5s sampler would have made)")
    print(f"  gaps: {json.dumps(body['gaps'])}")
    check("the busy hour appears", len(busy) >= 5, f"{len(busy)} busy buckets")
    # One fully blank bucket, not two: a 1200s outage that does not start on a
    # bucket boundary leaves the buckets at each end PARTIALLY observed, and
    # those legitimately carry a value for the part the camera did see.
    check("the outage leaves a hole, not a zero", len(blank) >= 1,
          f"{len(blank)} nulls")
    partial = [p for p in pts if 0 < (p["coverage"] or 0) < 1]
    check("the buckets either side are partial, not full", len(partial) >= 1,
          f"{len(partial)} partial")
    check("the outage is reported as camera_offline",
          [g["reason"] for g in body["gaps"]] == ["camera_offline"],
          str([g["reason"] for g in body["gaps"]]))
    check("coverage reflects the outage", 0.85 < body["coverage"] < 0.95,
          str(body["coverage"]))
    check("chart tag present with nulls intact",
          any(None in d["data"]
              for c in body.get("finblade", {}).get("charts", [])
              for d in c.get("datasets", [])))

code, body = call("/api/v1/zones/ZONE-01/series", params={"hours": 2})
check("a bare zone_id on two cameras is 409", code == 409, str(body)[:160])
check("the 409 names the candidates",
      code == 409 and len(body.get("candidates", [])) == 2)

code, body = call("/api/v1/zones/ZONE-99/series", params={"hours": 2})
check("an unknown zone is 404", code == 404)

code, body = call("/api/v1/zones/ZONE-01/series",
                  params={"from": start, "to": now, "bucket": 1,
                          "camera_id": "CAM-01"})
check("an absurd bucket is coarsened and reported",
      code == 200 and body["bucket_adjusted"] and len(body["points"]) <= 1000,
      f"bucket={body.get('bucket_seconds')} points={len(body.get('points', []))}")

# --------------------------------------------------------------------- at ---
print("\n== /at")
code, body = call("/api/v1/zones/ZONE-01/at",
                  params={"ts": start + 9000, "camera_id": "CAM-01"})
check("mid-busy-hour reads 6", code == 200 and body["state"]["occupancy"] == 6,
      str(body.get("state", {}).get("occupancy")))
check("and is trustworthy", body.get("trustworthy") is True)

code, body = call("/api/v1/zones/ZONE-01/at",
                  params={"ts": start + 11400, "camera_id": "CAM-01"})
print(f"  during the outage: occupancy={body.get('state', {}).get('occupancy')} "
      f"age={body.get('state', {}).get('age_seconds')} "
      f"offline={body.get('camera_offline')} trust={body.get('trustworthy')}")
check("a reading during the outage is flagged untrustworthy",
      body.get("trustworthy") is False)

code, body = call("/api/v1/zones/ZONE-01/at",
                  params={"ts": start - 86400, "camera_id": "CAM-01"})
check("before any data, state is null not zero",
      code == 200 and body.get("state") is None, str(body)[:120])

# --------------------------------------------------------------- duration ---
print("\n== /duration")
code, body = call("/api/v1/zones/ZONE-01/duration",
                  params={"from": start, "to": now, "camera_id": "CAM-01",
                          "field": "occupancy", "op": "gt", "value": 0})
check("duration returns 200", code == 200, str(body)[:120])
if code == 200:
    print(f"  occupied {body['total_seconds']:.0f}s in {body['episode_count']} "
          f"episode(s); unobserved {body['unobserved_seconds']:.0f}s; "
          f"coverage {body['coverage']}")
    check("about an hour occupied", 3300 <= body["total_seconds"] <= 3900,
          f"{body['total_seconds']}")
    check("the outage is excluded, not counted as occupied",
          body["unobserved_seconds"] >= 1000, f"{body['unobserved_seconds']}")

code, body = call("/api/v1/zones/ZONE-01/duration",
                  params={"hours": 4, "camera_id": "CAM-01",
                          "field": "occupanci", "op": "gt", "value": 0})
check("a misspelt field is 422, not an answer of zero", code == 422,
      f"{code} {str(body)[:100]}")

# ------------------------------------------------------------------ report --
print("\n== report gaps and coverage")
code, body = call("/api/v1/reports/occupancy.json", params={"from": start, "to": now})
zone = next((z for z in body.get("zones", []) if z.get("camera_id") == "CAM-01"), None)
check("report carries per-zone gaps", zone is not None and "gaps" in zone)
if zone:
    print(f"  coverage={zone['coverage']} gaps={len(zone['gaps'])} "
          f"avg={zone['avg_occupancy']:.4f} sampled={zone['sampled']['avg_occupancy']:.4f}")
    check("min_coverage is present in totals",
          body["totals"].get("min_coverage") is not None)

# ------------------------------------------------------------- tool layer ---
print("\n== tool dispatch against the live API")
from integrations.finblade_ai.cctv_client import CCTVClient      # noqa: E402
from integrations.finblade_ai.chat import run_tool_safely        # noqa: E402
from integrations.finblade_ai.tools import TOOLS                 # noqa: E402

client = CCTVClient(base_url=BASE, api_key=KEY)
results = {}
for name, args in (
        ("cctv_live_state", {"camera_id": "CAM-01"}),
        ("cctv_zone_history", {"zone_id": "ZONE-01", "camera_id": "CAM-01",
                               "hours": 4, "from": None, "to": None,
                               "bucket": 600}),
        ("cctv_zone_at_time", {"zone_id": "ZONE-01", "camera_id": "CAM-01",
                               "ts": start + 9000}),
        ("cctv_zone_duration", {"zone_id": "ZONE-01", "camera_id": "CAM-01",
                                "hours": 4, "from": None, "to": None,
                                "field": "occupancy", "op": "gt", "value": 0,
                                "status": None}),
        ("cctv_alerts", {"hours": 4, "from": None, "to": None,
                         "camera_id": None, "zone_id": None, "rule_id": None,
                         "severity": None, "status": None, "limit": 50}),
        ("cctv_occupancy_report", {"hours": 4, "from": None, "to": None,
                                   "camera_id": None, "zone_id": None})):
    out = run_tool_safely(client, name, args)
    results[name] = out
    failed = isinstance(out, dict) and "error" in out
    check(f"tool {name}", not failed, str(out)[:120] if failed else "")

check("every declared tool was exercised",
      {t["name"] for t in TOOLS} == set(results))
check("the history tool sees the same nulls",
      any(p["occupancy"] is None
          for p in results["cctv_zone_history"].get("points", [])))

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
