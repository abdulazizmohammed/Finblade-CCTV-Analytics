"""FastAPI HTTP + WebSocket adapter over IngestService (UC-28/29/44/45/46/50).

Thin: every request delegates to IngestService, whose logic is unit-tested
without FastAPI. Needs fastapi+uvicorn installed and (for persistence) Postgres
+ Redis reachable — none present in the authoring env (BLOCKERS.md B-3), so this
file is written and reviewed but not executed here.

Run: uvicorn services.api.app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .service import IngestService
from .store import InMemoryStore
from .bus import InMemoryBus
from .report import render_report_html

# Backend selection: durable SQLite by default (survives restarts, date-queryable).
# DATABASE_URL -> Postgres; FINBLADE_INMEMORY=1 -> in-memory (tests/ephemeral).
if os.environ.get("DATABASE_URL"):
    from .store import PostgresStore
    store = PostgresStore(os.environ["DATABASE_URL"])
elif os.environ.get("FINBLADE_INMEMORY"):
    store = InMemoryStore()
else:
    from .sqlite_store import SQLiteStore
    store = SQLiteStore(os.environ.get("FINBLADE_DB", "data/finblade.db"))

if os.environ.get("REDIS_URL"):
    from .bus import RedisStreamBus
    bus = RedisStreamBus(os.environ["REDIS_URL"])
else:
    bus = InMemoryBus()

svc = IngestService(store, bus)

# --- server-side camera-offline monitor (R-07) -----------------------------
OFFLINE_S = 30.0
_cam_offline: dict = {}


async def _offline_monitor():
    """Fire R-07 when a camera stops posting for >30s; clear + auto-ack on return."""
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            for c in svc.cameras():
                cid, last = c.get("camera_id"), c.get("last_seen")
                if not cid or last is None:
                    continue
                silent = now - last
                was = _cam_offline.get(cid, False)
                if silent > OFFLINE_S and not was:
                    _cam_offline[cid] = True
                    svc.raise_alert({"rule_id": "R-07", "severity": "RED",
                                     "message": f"camera {cid} offline >{int(OFFLINE_S)}s",
                                     "camera_id": cid, "ts": now, "kind": "FIRE"})
                elif silent <= OFFLINE_S and was:
                    _cam_offline[cid] = False
                    # auto-ack the open offline alert so the health grid recovers
                    for a in svc.list_alerts(unacked_only=True):
                        if a.get("rule_id") == "R-07" and a.get("camera_id") == cid:
                            svc.acknowledge(str(a.get("alert_id")), "system-recovery", now)
                    svc.raise_alert({"rule_id": "R-07", "severity": "INFO",
                                     "message": f"camera {cid} recovered",
                                     "camera_id": cid, "ts": now, "kind": "CLEAR"})
        except asyncio.CancelledError:
            break
        except Exception:
            pass  # never let the monitor kill the app


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_offline_monitor())
    yield
    task.cancel()


app = FastAPI(title="FinBlade CCTV API", version="1.0", lifespan=lifespan)

# Air-gapped LAN demo: allow the dashboard (file:// or another origin) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the dashboard + theme so the operator can just open one URL.
_WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web"))
if os.path.isdir(_WEB_DIR):
    app.mount("/web", StaticFiles(directory=_WEB_DIR, html=True), name="web")

# Serve saved event/alert frames (video-clip bookmarks) so logs can pull them.
_BOOKMARKS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                              "evidence", "bookmarks"))
os.makedirs(_BOOKMARKS_DIR, exist_ok=True)
app.mount("/bookmarks", StaticFiles(directory=_BOOKMARKS_DIR), name="bookmarks")

# Serve the zone editor tool.
_TOOLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
if os.path.isdir(_TOOLS_DIR):
    app.mount("/tools", StaticFiles(directory=_TOOLS_DIR, html=True), name="tools")


# --- history / logs (date-filterable) --------------------------------------
@app.get("/api/v1/history/events")
async def history_events(frm: float = Query(0, alias="from"),
                         to: float = Query(9_000_000_000_000.0, alias="to"),
                         camera_id: str = Query(None), zone_id: str = Query(None),
                         event_type: str = Query(None), person_ref: str = Query(None),
                         limit: int = Query(500)):
    return {"events": svc.events_history(frm, to, camera_id=camera_id, zone_id=zone_id,
                                         event_type=event_type, person_ref=person_ref,
                                         limit=limit)}


@app.get("/api/v1/movement")
async def movement(minutes: float = Query(15.0), camera_id: str = Query(None)):
    """Zone->zone transition counts over the last N minutes (Req 12)."""
    now = time.time()
    return {"minutes": minutes,
            "flows": svc.movement(now - minutes * 60.0, now, camera_id=camera_id)}


@app.get("/api/v1/history/alerts")
async def history_alerts(frm: float = Query(0, alias="from"),
                         to: float = Query(9_000_000_000_000.0, alias="to"),
                         camera_id: str = Query(None), rule_id: str = Query(None),
                         limit: int = Query(500)):
    return {"alerts": svc.alerts_history(frm, to, camera_id=camera_id, rule_id=rule_id,
                                         limit=limit)}


@app.get("/api/v1/reports/occupancy.json")
async def occupancy_report_json(frm: float = Query(0, alias="from"),
                                to: float = Query(9_000_000_000_000.0, alias="to"),
                                camera_id: str = Query(None), zone_id: str = Query(None)):
    zones = svc.occupancy_stats(frm, to, camera_id=camera_id, zone_id=zone_id)
    return {
        "from": frm, "to": to, "generated_at": time.time(),
        "zones": zones,
        "totals": {
            "zones": len(zones),
            "peak_total_occupancy": sum(int(z.get("peak_occupancy", 0)) for z in zones),
            "peak_density": max((float(z.get("peak_density", 0)) for z in zones), default=0.0),
        },
    }


@app.post("/api/v1/zones")
async def save_zones(request: Request):
    """Persist the zone set for a camera (from the zone editor)."""
    payload = await request.json()
    code, body = svc.save_zones(payload)
    return JSONResponse(status_code=code, content=body)


@app.get("/api/v1/zones")
async def get_zones(camera_id: str = Query(None)):
    return {"zones": svc.list_zones(camera_id)}


@app.get("/api/v1/cameras")
async def cameras():
    now = time.time()
    out = []
    for c in svc.cameras():
        last = c.get("last_seen")
        c = dict(c)
        c["online"] = (last is not None and (now - last) <= OFFLINE_S)
        out.append(c)
    return {"cameras": out}


@app.post("/api/v1/alerts")
async def raise_alert(request: Request):
    """Ingest an alert fired by the inference-side rule engine (UC-44 feed).

    The rule engine currently runs in the inference process; this lets it push
    fired alerts to the store the dashboard reads. (A server-side consumer of the
    event bus is the alternative wiring — see bus.py.)"""
    alert = await request.json()
    alert_id = svc.raise_alert(alert)
    return JSONResponse(status_code=202, content={"accepted": True, "alert_id": alert_id})


@app.post("/api/v1/events/ingest")
async def ingest(request: Request):
    payload = await request.json()
    code, body = svc.ingest_event(payload)
    return JSONResponse(status_code=code, content=body)


@app.post("/api/v1/zones/state")
async def zone_state(request: Request):
    payload = await request.json()
    code, body = svc.record_zone_state(payload)
    return JSONResponse(status_code=code, content=body)


@app.get("/api/v1/zones/state")
async def zone_states():
    return {"zones": svc.zone_states()}


@app.get("/api/v1/alerts")
async def alerts(unacked_only: bool = False):
    return {"alerts": svc.list_alerts(unacked_only=unacked_only)}


@app.post("/api/v1/alerts/{alert_id}/ack")
async def ack(alert_id: str, request: Request):
    body = await request.json()
    code, resp = svc.acknowledge(alert_id, body.get("acknowledged_by", ""), time.time())
    return JSONResponse(status_code=code, content=resp)


@app.get("/api/v1/reports/occupancy", response_class=HTMLResponse)
async def occupancy_report():
    return render_report_html(svc.zone_states(), generated_at=time.time())


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Sub-second push of zone states + unacked alerts (UC-45). The dashboard
    falls back to 5s REST polling of the GET endpoints if this drops (UC-46)."""
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({
                "zones": svc.zone_states(),
                "alerts": svc.list_alerts(unacked_only=True),
                "ts": time.time(),
            })
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
