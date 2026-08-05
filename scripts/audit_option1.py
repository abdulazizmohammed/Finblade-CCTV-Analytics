"""Option 1 integration audit probes. Read-only: changes no application code.

Run:  .venv/bin/python scripts/audit_option1.py

Exercises the real FastAPI app object through Starlette's ASGI client (same
middleware, routes and handlers as a network request; the network layer itself
was covered separately by scripts/smoke_pull_integration.sh).
"""
import json
import os
import sys

os.environ["FINBLADE_INMEMORY"] = "1"
os.environ["FINBLADE_API_KEY"] = "full-key"
os.environ["FINBLADE_INTEGRATION_KEY"] = "scoped-key"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient          # noqa: E402
from services.api.app import app, svc              # noqa: E402

FULL = {"Authorization": "Bearer full-key"}
SCOPED = {"Authorization": "Bearer scoped-key"}
c = TestClient(app)

RTSP_WITH_SECRET = "rtsp://admin:SuperSecret123@192.168.1.50:554/Streaming/Channels/101"


def hdr(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# --------------------------------------------------------------- OpenAPI ----
hdr("1. OPENAPI CONTRACT")
spec = c.get("/openapi.json").json()
paths = spec.get("paths", {})
print("openapi version   :", spec.get("openapi"))
print("title/version     :", spec["info"]["title"], spec["info"]["version"])
print("documented paths  :", len(paths))
print("securitySchemes   :", (spec.get("components") or {}).get("securitySchemes", "ABSENT"))
print("global security   :", spec.get("security", "ABSENT"))

typed = untyped = 0
for p, ops in paths.items():
    for m, op in ops.items():
        schema = (((op.get("responses") or {}).get("200") or {})
                  .get("content", {}).get("application/json", {}).get("schema"))
        if schema and schema != {}:
            typed += 1
        else:
            untyped += 1
print(f"200 responses with a declared schema : {typed}")
print(f"200 responses with NO schema         : {untyped}")
params = paths.get("/api/v1/history/alerts", {}).get("get", {}).get("parameters", [])
print("history/alerts params:", [q["name"] for q in params])
params = paths.get("/api/v1/alerts", {}).get("get", {}).get("parameters", [])
print("alerts params        :", [q["name"] for q in params])

# ------------------------------------------------- RTSP credential leak ----
hdr("2. RTSP CREDENTIALS IN READ RESPONSES")
r = c.post("/api/v1/cameras", headers=FULL,
           json={"camera_id": "CAM-AUDIT", "site_id": "SITE-01",
                 "source": RTSP_WITH_SECRET, "name": "Lobby cam"})
print("POST /cameras ->", r.status_code)
svc.record_camera_health({"camera_id": "CAM-AUDIT", "site_id": "SITE-01",
                          "ts": __import__("time").time(),
                          "health": {"state": "ONLINE", "input_fps": 24.0,
                                     "resolution": "1920x1080",
                                     "stream_url": "http://127.0.0.1:8090/stream"}})

for route in ("/api/v1/cameras", "/api/v1/summary"):
    body = c.get(route, headers=SCOPED).text
    leaked = "SuperSecret123" in body
    print(f"{route:24} secret present: {leaked}")
    if leaked:
        row = next(x for x in c.get(route, headers=SCOPED).json()["cameras"]
                   if x["camera_id"] == "CAM-AUDIT")
        print("    leaking field(s):",
              [k for k, v in row.items() if isinstance(v, str) and "SuperSecret123" in v])

# --------------------------------------------------------- unauth assets ----
hdr("3. UNAUTHENTICATED STATIC PREFIXES")
for path in ("/bookmarks/", "/media/", "/web/dashboard.html", "/openapi.json"):
    r = c.get(path)
    print(f"{path:26} no-key status: {r.status_code}")
print("NOTE: /bookmarks holds incident snapshots (images of people);")
print("      /media holds reference stills of the monitored space.")

# ---------------------------------------------------------------- alerts ----
hdr("4. ALERT LIFECYCLE + AUDIT FIELDS")
aid = str(svc.raise_alert({"rule_id": "R-06", "severity": "RED",
                           "message": "restricted zone ZONE-02 entered",
                           "zone_id": "ZONE-02", "camera_id": "CAM-AUDIT",
                           "person_ref": "pr_" + "a" * 16, "ts": 1000.0,
                           "kind": "FIRE", "frame": "/bookmarks/bm_CAM-AUDIT_1.jpg"}))
alert = c.get("/api/v1/alerts", headers=SCOPED).json()["alerts"][0]
print("alert fields:", sorted(alert.keys()))
print("has site_id :", "site_id" in alert)
print("has event_id:", "event_id" in alert)
print("alert_id    :", repr(alert["alert_id"]), "(type:", type(alert["alert_id"]).__name__ + ")")
r = c.post(f"/api/v1/alerts/{aid}/ack", headers=SCOPED,
           json={"acknowledged_by": "operator@finblade"})
print("ack         :", r.status_code, r.json())
r = c.post(f"/api/v1/alerts/{aid}/resolve", headers=SCOPED,
           json={"action": "RESOLVED", "resolved_by": "operator@finblade",
                 "note": "security dispatched"})
print("resolve     :", r.status_code, r.json())
r = c.post(f"/api/v1/alerts/{aid}/resolve", headers=SCOPED,
           json={"action": "RESOLVED", "resolved_by": "operator@finblade"})
print("re-resolve  :", r.status_code, "(idempotency / conflict handling)")

# ------------------------------------------------------------- zone state ----
hdr("5. ZONE STATE FIELDS")
ok = c.post("/api/v1/zones/state", headers=FULL, json={
    "zone_id": "ZONE-01", "camera_id": "CAM-AUDIT", "zone_name": "Lobby",
    "occupancy": 12, "density": 2.4, "capacity_pct": 30.0,
    "inflow_per_min": 8.0, "outflow_per_min": 5.0, "status": "WARNING",
    "ts": 1000.0})
print("POST /zones/state ->", ok.status_code)
z = c.get("/api/v1/zones/state", headers=SCOPED).json()["zones"]
print("zone fields:", sorted(z[0].keys()) if z else "EMPTY")
print("has site_id:", "site_id" in (z[0] if z else {}))
print("has dwell  :", any("dwell" in k for k in (z[0] if z else {})))
print("filterable per camera/zone:",
      [q["name"] for q in paths.get("/api/v1/zones/state", {})
       .get("get", {}).get("parameters", [])] or "NO query parameters")

# ----------------------------------------------------------- camera state ----
hdr("6. CAMERA STATE FIELDS")
cam = next(x for x in c.get("/api/v1/cameras", headers=SCOPED).json()["cameras"]
           if x["camera_id"] == "CAM-AUDIT")
print("camera fields:", sorted(cam.keys()))
for f in ("effective_state", "state", "last_seen", "seconds_since_seen",
          "input_fps", "resolution", "people_in_view", "name", "site_id"):
    print(f"  {f:20} {'present' if f in cam else 'MISSING'}")
for f in ("processing_fps", "last_frame_ts", "degradation_reason", "error"):
    print(f"  {f:20} {'present' if f in cam else 'MISSING'}")

# ---------------------------------------------------------------- websocket ----
hdr("7. WEBSOCKET")
try:
    with c.websocket_connect("/ws?key=scoped-key") as ws:
        frame = ws.receive_json()
    payload = json.dumps(frame)
    print("frame keys      :", sorted(frame.keys()))
    print("frame bytes     :", len(payload))
    print("has site_id     :", "site_id" in frame)
    print("secret in frame :", "SuperSecret123" in payload)
    print("cadence         : 0.5s (services/api/app.py, asyncio.sleep(0.5))")
except Exception as exc:
    print("websocket probe failed:", exc)
try:
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
    print("no-key connect  : ACCEPTED  <-- would be a hole")
except Exception as exc:
    print("no-key connect  : rejected (", type(exc).__name__, ")")

# ---------------------------------------------------------------- history ----
hdr("8. HISTORY / REPORTS")
for route in ("/api/v1/history/events", "/api/v1/history/alerts",
              "/api/v1/reports/occupancy.json", "/api/v1/reports/occupancy.csv",
              "/api/v1/movement"):
    r = c.get(route, headers=SCOPED, params={"from": 0, "to": 9e9})
    body = r.text
    print(f"{route:36} {r.status_code}  {len(body):6d} bytes  "
          f"ct={r.headers.get('content-type','?')[:28]}")
r = c.get("/api/v1/history/alerts", headers=SCOPED, params={"from": 0, "to": 9e9})
print("pagination fields in body:",
      [k for k in (r.json() if r.headers.get('content-type', '').startswith('application/json') else {})
       if k in ("next", "next_cursor", "total", "offset", "page")] or "NONE")

# -------------------------------------------------------------- forwarder ----
hdr("9. FORWARDER: RETRY, REPLAY, RESTART")
from services.api.forwarder import FinBladeForwarder      # noqa: E402
from services.api.store import InMemoryStore              # noqa: E402
import time as _t                                          # noqa: E402

store = InMemoryStore()
sent, fail = [], {"on": True}


def fake_post(path, payload):
    if fail["on"]:
        raise RuntimeError("FinBlade unavailable")
    sent.append((path, payload))
    class R:  # noqa: E306
        status_code = 200
        def json(self):  # noqa: E301
            return {"accepted": True}
    return {"accepted": True}


fw = FinBladeForwarder(store, base_url="http://finblade.test", post=fake_post)
now = _t.time()
store.save_event({"event_id": "e1", "event_type": "ZONE_ENTRY", "camera_id": "C1",
                  "site_id": "SITE-01", "ts": now, "timestamp": now})
fw.tick()
print("with FinBlade down  -> sent:", len(sent), "| failures:", fw.stats["failures"],
      "| last_error:", (fw.last_error or "")[:44])
fail["on"] = False
fw.tick()
print("after recovery      -> sent:", len(sent), "(replayed from the store)")
print("cursor persisted?   :",
      "NO — in-memory only, reset to now() in __init__"
      if not hasattr(fw, "load_cursors") else "yes")
fw2 = FinBladeForwarder(store, base_url="http://finblade.test", post=fake_post)
print("after a restart, cursor starts at:", "now()" if fw2.cursors["events"] >= now else "stored value")
print("=> events written while the process was down are SKIPPED, not replayed")
print("status() keys       :", sorted(fw.status().keys()))
print("stats keys          :", sorted(fw.stats.keys()))
print("backoff strategy    : fixed", os.environ.get("FINBLADE_FORWARD_INTERVAL", "5"),
      "s tick; no exponential backoff, no dead-letter queue")

hdr("10. RATE LIMITING / REQUEST SIZE / CORS")
print("CORS allow_origins  : ['*'] (services/api/app.py:219)")
mw = [m.cls.__name__ for m in app.user_middleware]
print("middleware stack    :", mw)
print("rate limiter present:", any("rate" in m.lower() or "limit" in m.lower() for m in mw))
print("body size limit     :", "none found in app code (relies on the ASGI server default)")
print("health/readiness    :",
      [p for p in paths if "health" in p or "ready" in p or "live" in p] or "NONE on the API")
