"""Option 1 audit, pass 2. Corrects two probes from pass 1 and adds privacy checks.

Pass 1 posted a 1970 timestamp (filtered by the zone-state freshness window) and
an incomplete health payload, so those two field lists were not representative.
"""
import json
import os
import sys
import time

os.environ["FINBLADE_INMEMORY"] = "1"
os.environ["FINBLADE_API_KEY"] = "full-key"
os.environ["FINBLADE_INTEGRATION_KEY"] = "scoped-key"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient          # noqa: E402
from services.api.app import app, svc              # noqa: E402

FULL = {"Authorization": "Bearer full-key"}
SCOPED = {"Authorization": "Bearer scoped-key"}
c = TestClient(app)
now = time.time()


def hdr(t):
    print("\n" + "=" * 68); print(t); print("=" * 68)


hdr("A. ZONE STATE — fresh timestamp")
c.post("/api/v1/zones/state", headers=FULL, json={
    "zone_id": "ZONE-01", "camera_id": "CAM-AUDIT", "zone_name": "Lobby",
    "zone_type": "ENTRANCE", "restricted": False,
    "occupancy": 12, "density": 2.4, "capacity_pct": 30.0, "capacity_max": 40,
    "area_sqm": 5.0, "peak_occupancy": 15, "avg_occupancy": 9.2, "trend": "rising",
    "inflow_per_min": 8.0, "outflow_per_min": 5.0, "status": "WARNING", "ts": now})
z = c.get("/api/v1/zones/state", headers=SCOPED).json()["zones"]
print("zone fields :", sorted(z[0].keys()) if z else "EMPTY")
for f in ("site_id", "area_sqm", "capacity_max", "warning_density",
          "critical_density", "dwell_avg_sec", "zone_type", "trend", "ts"):
    print(f"  {f:18} {'present' if z and f in z[0] else 'MISSING'}")

hdr("B. ZONE DEFINITIONS — /api/v1/zones")
c.post("/api/v1/zones", headers=FULL, json={"camera_id": "CAM-AUDIT", "zones": [
    {"zone_id": "ZONE-01", "zone_name": "Lobby", "zone_type": "ENTRANCE",
     "capacity_max": 40, "area_sqm": 60.0, "warning_density": 2.0,
     "critical_density": 4.0, "loitering_threshold_sec": 30.0,
     "normalized_polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]}]})
zd = c.get("/api/v1/zones", headers=SCOPED, params={"camera_id": "CAM-AUDIT"}).json()["zones"]
print("definition fields:", sorted(zd[0].keys()) if zd else "EMPTY")
print("has site_id      :", bool(zd) and "site_id" in zd[0])

hdr("C. CAMERA — full health payload")
svc.record_camera_health({
    "camera_id": "CAM-AUDIT", "site_id": "SITE-01", "ts": now,
    "health": {"state": "DEGRADED", "input_fps": 24.0, "resolution": "1920x1080",
               "dropped_frames": 11, "reconnects": 3, "frozen": True,
               "enabled": True, "loops": 0}})
svc.store.upsert_camera("CAM-AUDIT", people_in_view=4, people_in_zones=2)
cam = next(x for x in c.get("/api/v1/cameras", headers=SCOPED).json()["cameras"]
           if x["camera_id"] == "CAM-AUDIT")
print("camera fields:", sorted(cam.keys()))
print("state / effective_state:", cam.get("state"), "/", cam.get("effective_state"))
for f in ("people_in_view", "people_in_zones", "frozen", "dropped_frames",
          "reconnects", "processing_fps", "last_frame_ts", "degradation_reason"):
    print(f"  {f:20} {'present' if f in cam else 'MISSING'}")

hdr("D. INCIDENT SNAPSHOT ACCESS CONTROL")
bm = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                  "evidence", "bookmarks"))
os.makedirs(bm, exist_ok=True)
probe = os.path.join(bm, "audit_probe.jpg")
with open(probe, "wb") as fh:
    fh.write(b"\xff\xd8\xff\xe0AUDIT-PROBE-IMAGE")
try:
    r = c.get("/bookmarks/audit_probe.jpg")
    print("GET /bookmarks/<file> with NO key ->", r.status_code,
          "| content-type:", r.headers.get("content-type"),
          "| body served:", r.content[:4] == b"\xff\xd8\xff\xe0")
    r2 = c.get("/bookmarks/../../services/api/auth.py")
    print("path traversal attempt              ->", r2.status_code)
finally:
    os.remove(probe)

hdr("E. PRIVACY — identity endpoints")
for route in ("/api/v1/identity/counts", "/api/v1/identity/list",
              "/api/v1/identity/stats"):
    body = c.get(route, headers=SCOPED).text
    print(f"{route:32} {len(body):5d} bytes | 'embedding' in body:",
          "embedding" in body.lower())
ev = {"event_id": "aud-1", "event_type": "ZONE_ENTRY", "camera_id": "CAM-AUDIT",
      "site_id": "SITE-01", "timestamp": now, "zone_to": "ZONE-01",
      "person_ref": "NOT-A-HASH-john.smith@corp.com", "confidence": 0.9}
r = c.post("/api/v1/events/ingest", headers=FULL, json=ev)
print("ingest with a non-hash person_ref ->", r.status_code,
      json.dumps(r.json())[:120])

hdr("F. WRITE-SIDE AUTHORIZATION SPOT CHECKS")
checks = [("POST", "/api/v1/cameras", {"camera_id": "X"}),
          ("DELETE", "/api/v1/cameras/CAM-AUDIT", None),
          ("POST", "/api/v1/cameras/CAM-AUDIT/stop", {}),
          ("POST", "/api/v1/zones", {"camera_id": "X", "zones": []}),
          ("POST", "/api/v1/identity/tuning", {}),
          ("DELETE", "/api/v1/alerts", None),
          ("POST", "/api/v1/events/ingest", {}),
          ("POST", "/api/v1/reports/generate", {})]
for method, path, body in checks:
    r = c.request(method, path, headers=SCOPED, json=body)
    print(f"  scoped {method:6} {path:36} -> {r.status_code}")

hdr("G. AUDIT LOG FOR WRITE ACTIONS")
aid = str(svc.raise_alert({"rule_id": "R-01", "severity": "AMBER", "message": "m",
                           "zone_id": "ZONE-01", "camera_id": "CAM-AUDIT",
                           "ts": now, "kind": "FIRE"}))
c.post(f"/api/v1/alerts/{aid}/ack", headers=SCOPED,
       json={"acknowledged_by": "operator@finblade"})
row = next(a for a in c.get("/api/v1/alerts", headers=SCOPED).json()["alerts"]
           if str(a["alert_id"]) == aid)
print("post-ack row:", {k: row[k] for k in
                        ("alert_id", "status", "acknowledged_by", "acknowledged_at")})
print("separate immutable audit trail table:",
      "audit" in open(os.path.join(os.path.dirname(__file__), "..", "services",
                                   "api", "sqlite_store.py")).read().lower())
r = c.post(f"/api/v1/alerts/{aid}/ack", headers=SCOPED, json={"acknowledged_by": ""})
print("ack with empty acknowledged_by ->", r.status_code, r.json())
