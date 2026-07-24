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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .service import IngestService
from .store import InMemoryStore
from .bus import InMemoryBus
from .report import render_report_html

# Backend selection: default to in-memory so the API boots even before Postgres/
# Redis exist; switch to Postgres/Redis via env when the services are up.
if os.environ.get("DATABASE_URL"):
    from .store import PostgresStore
    store = PostgresStore(os.environ["DATABASE_URL"])
else:
    store = InMemoryStore()

if os.environ.get("REDIS_URL"):
    from .bus import RedisStreamBus
    bus = RedisStreamBus(os.environ["REDIS_URL"])
else:
    bus = InMemoryBus()

svc = IngestService(store, bus)
app = FastAPI(title="FinBlade CCTV API", version="1.0")

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
