"""FastAPI HTTP + WebSocket adapter over IngestService (UC-28/29/44/45/46/50).

Thin: every request delegates to IngestService, whose logic is unit-tested
without FastAPI. Needs fastapi+uvicorn installed and (for persistence) Postgres
+ Redis reachable — none present in the authoring env (BLOCKERS.md B-3), so this
file is written and reviewed but not executed here.

Run: uvicorn services.api.app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

log = logging.getLogger("finblade.api")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import (JSONResponse, HTMLResponse, Response,
                               StreamingResponse)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import charts as _charts
from . import redact as _redact
from .service import IngestService
from .store import InMemoryStore
from .bus import InMemoryBus
from .report import render_report_html, render_report_csv

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

# Cross-camera identity. Lives in the API process because it is the only one
# that sees every camera; each inference worker resolves its local track ids
# against this single registry. Embeddings stay in this process's RAM and are
# never handed to `store` — see services/api/identity.py.
from .identity import IdentityService                            # noqa: E402
_TOPOLOGY_PATH = os.environ.get(
    "FINBLADE_TOPOLOGY",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "topology.yaml"))
id_svc = IdentityService(topology_path=_TOPOLOGY_PATH)

# Manages detection pipelines for UI-provisioned cameras (Add camera + a source).
from .camera_manager import CameraManager                       # noqa: E402
_SELF_URL = os.environ.get("FINBLADE_SELF_URL", "http://127.0.0.1:8000")
cam_mgr = CameraManager(api_url=_SELF_URL)


def _valid_source(src: str) -> bool:
    if not isinstance(src, str) or not src.strip():
        return False
    s = src.strip()
    if s.startswith(("rtsp://", "http://", "https://")):
        return True
    return os.path.exists(os.path.join(os.path.dirname(__file__), "..", "..", s))


# --- server-side camera-offline monitor (R-07) -----------------------------
OFFLINE_S = 30.0
_cam_offline: dict = {}

# Background loops swallow their exceptions so one bad tick cannot kill them.
# These counters are what makes that survivable rather than silent: they surface
# in /api/v1/health, so a loop that has been failing for an hour is visible.
_loop_errors = {"offline": 0, "report": 0}


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
                    # A dead worker never releases its tracks, and a bound
                    # identity is never expired — so without this its people
                    # are counted as present forever.
                    freed = id_svc.release_camera(cid)
                    if freed:
                        log.info("camera %s offline: released %d identity binding(s)",
                                 cid, freed)
                    svc.raise_alert({"rule_id": "R-07", "severity": "RED",
                                     "message": f"camera {cid} offline >{int(OFFLINE_S)}s",
                                     "camera_id": cid, "ts": now, "kind": "FIRE"})
                elif silent <= OFFLINE_S and was:
                    _cam_offline[cid] = False
                    # auto-resolve the open offline alert so it leaves the active feed
                    for a in svc.list_alerts(unacked_only=False):
                        if a.get("rule_id") == "R-07" and a.get("camera_id") == cid:
                            svc.resolve(str(a.get("alert_id")), "RESOLVED",
                                        "system-recovery", now, note="camera recovered")
                    svc.raise_alert({"rule_id": "R-07", "severity": "INFO",
                                     "message": f"camera {cid} recovered",
                                     "camera_id": cid, "ts": now, "kind": "CLEAR"})
        except asyncio.CancelledError:
            break
        except Exception:
            # Must not kill the monitor — but must not vanish either. R-07 is
            # the only rule the API owns; if this loop is failing, camera
            # offline alerts silently stop and nothing anywhere says so.
            _loop_errors["offline"] += 1
            log.exception("camera-offline monitor tick failed")


# R-08: generate an occupancy report on a fixed cadence (hourly by default; set
# FINBLADE_REPORT_INTERVAL short for demos). In-process — no external scheduler.
REPORT_INTERVAL = float(os.environ.get("FINBLADE_REPORT_INTERVAL", "3600"))


async def _report_scheduler():
    while True:
        try:
            await asyncio.sleep(REPORT_INTERVAL)
            now = time.time()
            svc.generate_report(now - REPORT_INTERVAL, now, kind="scheduled")
        except asyncio.CancelledError:
            break
        except Exception:
            # A failed report must never kill the scheduler, but a scheduler
            # that has stopped producing reports must be visible in /health.
            _loop_errors["report"] += 1
            log.exception("scheduled report generation failed")


# --- FinBlade forwarder ----------------------------------------------------
from .forwarder import FinBladeForwarder                        # noqa: E402


def _apply_finblade_ack(ack: dict) -> None:
    """Apply an operator action taken in FinBlade to the local alert."""
    aid = str(ack.get("alert_id"))
    action = str(ack.get("action") or "ACK").upper()
    who = ack.get("by") or "finblade"
    ts = float(ack.get("ts") or time.time())
    if action == "ACK":
        svc.acknowledge(aid, who, ts)
    else:
        svc.resolve(aid, action, who, ts, note=ack.get("note"))


def _fetch_camera_snapshot(camera_id: str):
    """One JPEG from a camera, over loopback. Used by the forwarder."""
    port = _camera_local_port(camera_id)
    if port is None:
        return None
    import requests
    r = requests.get(f"http://127.0.0.1:{port}/snapshot", timeout=4.0)
    return r.content if r.status_code == 200 else None


forwarder = FinBladeForwarder(
    store, identity_service=id_svc,
    base_url=os.environ.get("FINBLADE_URL"),
    # DISTINCT from FINBLADE_API_KEY. That one is INBOUND — the key callers must
    # present to us. This is OUTBOUND — the credential we present to FinBlade.
    # They are different secrets held by different parties; sharing one variable
    # would mean handing FinBlade the key that unlocks this API, and rotating
    # either would silently break the other.
    api_key=os.environ.get("FINBLADE_OUTBOUND_KEY"),
    apply_ack=_apply_finblade_ack,
    # 0 disables. Kept well above the 5s telemetry tick because a JPEG is ~1000x
    # the size of the JSON — see the note in forwarder.py.
    snapshot_interval=float(os.environ.get("FINBLADE_SNAPSHOT_INTERVAL", "30")),
    fetch_snapshot=_fetch_camera_snapshot,
    site_id=os.environ.get("FINBLADE_SITE_ID"),
)
FORWARD_INTERVAL = float(os.environ.get("FINBLADE_FORWARD_INTERVAL", "5"))


async def _forward_loop():
    """Push live analytics to FinBlade on a fixed cadence.

    Runs in this process because it is where the data already is. A failure
    here must never touch video processing or alerting, so tick() swallows its
    own errors and this loop only handles cancellation.
    """
    if not forwarder.enabled:
        log.info("FinBlade forwarding disabled (set FINBLADE_URL to enable)")
        return
    log.info("FinBlade forwarding to %s every %.0fs",
             forwarder.base_url, FORWARD_INTERVAL)
    while True:
        try:
            await asyncio.sleep(FORWARD_INTERVAL)
            await asyncio.to_thread(forwarder.tick)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("forwarder loop error")


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(_offline_monitor()),
             asyncio.create_task(_report_scheduler()),
             asyncio.create_task(_forward_loop())]
    yield
    for t in tasks:
        t.cancel()
    cam_mgr.stop_all()                            # stop any UI-launched pipelines


app = FastAPI(title="FinBlade CCTV API", version="1.0", lifespan=lifespan)

def _openapi_with_auth():
    """Publish the auth scheme in the generated spec.

    The key is enforced by @app.middleware("http"), which FastAPI cannot see, so
    without this the spec says every route is public. A client generated from it
    then sends no credential and gets a 401 the integrator has no way to explain
    from the contract they were given.
    """
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    spec = get_openapi(title=app.title, version=app.version, routes=app.routes)
    spec.setdefault("components", {})["securitySchemes"] = {
        "BearerKey": {"type": "http", "scheme": "bearer",
                      "description": "Authorization: Bearer <API key>"},
        "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key",
                         "description": "Equivalent to the bearer form."},
        "QueryKey": {"type": "apiKey", "in": "query", "name": "key",
                     "description": "Accepted ONLY on /stream, /snapshot, /ws, "
                                    "/bookmarks/* and /media/* — browser <img> "
                                    "and WebSocket cannot send headers."},
    }
    spec["security"] = [{"BearerKey": []}, {"ApiKeyHeader": []}]
    app.openapi_schema = spec
    return spec


app.openapi = _openapi_with_auth

# Air-gapped LAN demo: allow the dashboard (file:// or another origin) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from . import auth as _auth                                     # noqa: E402


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    """Gate /api/v1 behind an API key when FINBLADE_API_KEY is set.

    Off by default so an existing deployment keeps working until someone turns
    it on. CORS preflight is exempt: the browser sends OPTIONS without
    Authorization by design, and rejecting it breaks every cross-origin call
    before the real request is ever made.

    A valid but scoped key on a route it may not use returns 403, not 401.
    Collapsing both into 401 sends an integrator hunting for a credentials bug
    that does not exist.
    """
    if request.method == "OPTIONS":
        return await call_next(request)
    allowed, reason = _auth.authorise(request.url.path, request.headers,
                                      request.query_params, request.method)
    if allowed:
        return await call_next(request)
    if reason == "forbidden":
        return JSONResponse(status_code=403, content={
            "error": "forbidden",
            "detail": "this key has integration scope: it may read any route and "
                      "POST only /api/v1/alerts/{id}/ack and "
                      "/api/v1/alerts/{id}/resolve"})
    return JSONResponse(status_code=401, content={
        "error": "unauthorized",
        "detail": "supply the API key as 'Authorization: Bearer <key>' or "
                  "'X-API-Key: <key>'"})

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

# Serve reference stills (media/<camera>_frame.jpg) so the zone editor can load
# the frame straight from the server instead of a manual file picker.
_MEDIA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "media"))
# Create it, then mount UNCONDITIONALLY. This used to be `if os.path.isdir(...)`,
# which silently disabled the whole route on a fresh clone: nothing under media/
# is tracked by git, so the directory does not exist until a camera worker writes
# its first still — and the mount is decided once, at startup, before any worker
# has run. The zone editor then had no reference frame to draw on and simply hung,
# with no error anywhere, on every fresh deployment.
os.makedirs(_MEDIA_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=_MEDIA_DIR), name="media")


# --- history / logs (date-filterable) --------------------------------------
def _paginate(rows, offset: int, limit: int):
    """Slice a result window and say whether more exist.

    Callers fetch one row past the window, so `has_more` is a fact rather than a
    guess. Without this a client could not tell a full page from the end of the
    data, and a silent truncation at `limit` reads as "that is everything" —
    which is how a report ends up quietly missing a day.
    """
    window = rows[offset:offset + limit]
    return window, {"limit": limit, "offset": offset, "returned": len(window),
                    "has_more": len(rows) > offset + limit}


@app.get("/api/v1/history/events")
async def history_events(frm: float = Query(0, alias="from"),
                         to: float = Query(9_000_000_000_000.0, alias="to"),
                         camera_id: str = Query(None), zone_id: str = Query(None),
                         event_type: str = Query(None), person_ref: str = Query(None),
                         site_id: str = Query(None),
                         limit: int = Query(500), offset: int = Query(0)):
    rows = svc.events_history(frm, to, camera_id=camera_id, zone_id=zone_id,
                              event_type=event_type, person_ref=person_ref,
                              limit=offset + limit + 1)
    if site_id:
        rows = [e for e in rows if e.get("site_id") == site_id]
    window, page = _paginate(rows, offset, limit)
    return {"events": window, "page": page}


@app.get("/api/v1/movement")
async def movement(minutes: float = Query(15.0), camera_id: str = Query(None),
                   frm: float = Query(None, alias="from"),
                   to: float = Query(None, alias="to"),
                   charts: int = Query(1)):
    """Zone->zone transition counts.

    `minutes` counts back from now and stays the default so existing callers are
    unaffected. `from`/`to` (epoch seconds) address a fixed window instead —
    without them a question like "last Tuesday's flows" cannot be asked.
    """
    now = time.time()
    if frm is not None or to is not None:
        t0 = frm if frm is not None else 0.0
        t1 = to if to is not None else now
        body = {"from": t0, "to": t1,
                "flows": svc.movement(t0, t1, camera_id=camera_id)}
    else:
        body = {"minutes": minutes, "from": now - minutes * 60.0, "to": now,
                "flows": svc.movement(now - minutes * 60.0, now, camera_id=camera_id)}
    # FinBlade chart tag (live-feed-chart-tags.md). Additive; ?charts=0 omits it.
    return _charts.attach(body, _charts.movement_charts(body["flows"])) \
        if charts else body


@app.get("/api/v1/history/alerts")
async def history_alerts(frm: float = Query(0, alias="from"),
                         to: float = Query(9_000_000_000_000.0, alias="to"),
                         camera_id: str = Query(None), rule_id: str = Query(None),
                         severity: str = Query(None), status: str = Query(None),
                         zone_id: str = Query(None), site_id: str = Query(None),
                         limit: int = Query(500), offset: int = Query(0)):
    rows = svc.alerts_history(frm, to, camera_id=camera_id, rule_id=rule_id,
                              limit=offset + limit + 1)
    for field, wanted in (("severity", severity), ("status", status),
                          ("zone_id", zone_id), ("site_id", site_id)):
        if wanted:
            rows = [a for a in rows
                    if str(a.get(field) or "").upper() == str(wanted).upper()]
    window, page = _paginate(rows, offset, limit)
    return {"alerts": window, "page": page}


@app.get("/api/v1/reports/occupancy.json")
async def occupancy_report_json(frm: float = Query(0, alias="from"),
                                to: float = Query(9_000_000_000_000.0, alias="to"),
                                camera_id: str = Query(None), zone_id: str = Query(None),
                                charts: int = Query(1)):
    body = svc.occupancy_report(frm, to, camera_id=camera_id, zone_id=zone_id)
    # FinBlade chart tag (live-feed-chart-tags.md). Additive; ?charts=0 omits it.
    return _charts.attach(body, _charts.report_charts(body)) if charts else body


@app.get("/api/v1/reports/occupancy.csv")
async def occupancy_report_csv(frm: float = Query(0, alias="from"),
                               to: float = Query(9_000_000_000_000.0, alias="to"),
                               camera_id: str = Query(None), zone_id: str = Query(None)):
    rep = svc.occupancy_report(frm, to, camera_id=camera_id, zone_id=zone_id)
    csv_text = render_report_csv(rep["zones"])
    return Response(content=csv_text, media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="finblade_occupancy.csv"'})


@app.get("/api/v1/reports")
async def list_reports(limit: int = Query(100)):
    """R-08 scheduled + on-demand occupancy reports (most recent first)."""
    return {"reports": svc.list_reports(limit)}


# --- cross-camera identity -------------------------------------------------
# Inference workers POST embeddings here and get back an opaque global_ref.
# Nothing in this section ever returns or persists a vector.
@app.post("/api/v1/identity/resolve")
async def identity_resolve(request: Request):
    status, body = id_svc.resolve(await request.json())
    return JSONResponse(status_code=status, content=body)


@app.post("/api/v1/identity/release")
async def identity_release(request: Request):
    status, body = id_svc.release(await request.json())
    return JSONResponse(status_code=status, content=body)


@app.get("/api/v1/identity/stats")
async def identity_stats():
    """Registry health: identity count, site occupancy, match/reject counters."""
    return id_svc.stats()


@app.get("/api/v1/cameras/{camera_id}/stream")
async def camera_stream(camera_id: str, request: Request):
    """Proxy a camera's annotated MJPEG through the API's own port.

    Each camera process serves its MJPEG on its own port (8090, 8091, …), so a
    browser needed a direct route to every one of them. That fails on any
    deployment where only the API port is exposed — a container behind a
    port-forward, an ingress, or a Tailscale-mapped port — and shows up as a
    feed stuck on "connecting…" while the analytics update normally.

    The API and the camera processes share a host, so the API can always reach
    127.0.0.1:<port>. Proxying here means a deployment exposes ONE port.

    Query params (the overlay toggles) are passed through unchanged.
    """
    port = cam_mgr.local_port(camera_id)
    if port is None:
        # Camera started outside the manager (CLI): recover the port it
        # advertised, but always dial loopback rather than the advertised host.
        cam = next((c for c in svc.cameras() if c.get("camera_id") == camera_id), None)
        url = (cam or {}).get("stream_url") or ""
        try:
            port = int(url.rsplit(":", 1)[1].split("/")[0])
        except (IndexError, ValueError):
            return JSONResponse(status_code=404,
                                content={"error": f"no stream known for {camera_id}"})

    qs = request.url.query
    upstream = f"http://127.0.0.1:{port}/stream" + (f"?{qs}" if qs else "")

    import httpx

    async def relay():
        # timeout=None: MJPEG is an unbounded stream, any read timeout would
        # sever a healthy feed.
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", upstream) as upstream_resp:
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
            except Exception:
                return          # client navigated away, or the camera died

    return StreamingResponse(
        relay(), media_type="multipart/x-mixed-replace; boundary=frame")


def _camera_local_port(camera_id: str):
    """Loopback port for a camera's MJPEG server, however it was started."""
    port = cam_mgr.local_port(camera_id)
    if port is not None:
        return port
    cam = next((c for c in svc.cameras() if c.get("camera_id") == camera_id), None)
    url = (cam or {}).get("stream_url") or ""
    try:
        return int(url.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return None


@app.get("/api/v1/cameras/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str):
    """A single annotated JPEG — far cheaper than opening the MJPEG stream.

    Use this for thumbnails, alert context, or anything that needs an image
    rather than continuous video. Opening a stream to grab one frame costs a
    held connection and a continuous encode.
    """
    port = _camera_local_port(camera_id)
    if port is None:
        return JSONResponse(status_code=404,
                            content={"error": f"no stream known for {camera_id}"})
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"http://127.0.0.1:{port}/snapshot")
    except Exception as exc:                           # noqa: BLE001
        return JSONResponse(status_code=502,
                            content={"error": f"camera unreachable: {exc}"})
    if r.status_code != 200:
        return JSONResponse(status_code=r.status_code,
                            content={"error": "no frame available"})
    # no-store, because the dashboard now refreshes these tiles about once a
    # second per camera and each URL carries a cache-buster, so every response is
    # unique. Without this the browser writes all of them into its HTTP disk
    # cache — six cameras at roughly 46 KB a frame is on the order of half a
    # gigabyte an hour of pure churn. They are live video frames: there is never
    # a reason to keep one.
    return Response(content=r.content, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def _find_alert(alert_id: str):
    """An alert by id, open or closed. None if unknown."""
    for a in svc.list_alerts(unacked_only=False):
        if str(a.get("alert_id")) == str(alert_id):
            return a
    for a in store.list_alerts_history(0, time.time() + 86400, limit=100000):
        if str(a.get("alert_id")) == str(alert_id):
            return a
    return None


@app.get("/api/v1/incidents/{alert_id}/frame")
async def incident_frame(alert_id: str):
    """The frame captured WHEN the incident happened, by alert id.

    Integrations should use this rather than /bookmarks/<file> or the live
    camera snapshot:

    * /bookmarks exposes a filesystem-shaped path, which is not a contract —
      the layout may change and the name is guessable.
    * A live snapshot is a picture of the room NOW. For an alert raised twenty
      minutes ago that is not evidence of anything; it is a different scene
      wearing the incident's label.

    Only R-02 (critical density) and R-06 (restricted intrusion) carry a frame;
    everything else returns 404 with a reason.
    """
    alert = _find_alert(alert_id)
    if alert is None:
        return JSONResponse(status_code=404,
                            content={"error": "unknown alert", "alert_id": alert_id})
    ref = alert.get("frame")
    if not ref:
        return JSONResponse(status_code=404, content={
            "error": "this alert has no incident frame",
            "detail": "frames are captured for R-02 and R-06 only",
            "alert_id": alert_id, "rule_id": alert.get("rule_id")})

    # The stored ref is a URL path (/bookmarks/<name>.jpg). Resolve it inside
    # the bookmarks directory and refuse anything that escapes — the value
    # reaches here from the database, and a traversal in it must not become a
    # file read.
    name = os.path.basename(str(ref))
    path = os.path.realpath(os.path.join(_BOOKMARKS_DIR, name))
    if not path.startswith(os.path.realpath(_BOOKMARKS_DIR) + os.sep):
        return JSONResponse(status_code=400, content={"error": "invalid frame reference"})
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={
            "error": "frame no longer on disk", "alert_id": alert_id,
            "detail": "incident frames are removed when their alert is cleared"})
    with open(path, "rb") as fh:
        return Response(content=fh.read(), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})


@app.delete("/api/v1/alerts")
async def clear_alerts(scope: str = Query("closed"),
                       delete_frames: bool = Query(True)):
    """Delete alerts and their saved snapshots.

    scope=closed (default) removes only RESOLVED/DISMISSED alerts; scope=all
    removes every alert including open ones. Destructive and not undoable — the
    UI confirms before calling this.
    """
    status, body = svc.clear_alerts(scope=scope, delete_frames=delete_frames)
    return JSONResponse(status_code=status, content=body)


@app.get("/api/v1/frames/orphaned")
async def orphaned_frames():
    """Snapshot files on disk that no alert references any more."""
    return svc.orphaned_frames()


@app.delete("/api/v1/frames/orphaned")
async def delete_orphaned_frames():
    """Remove snapshot files left behind by alerts deleted earlier."""
    status, body = svc.delete_orphaned_frames()
    return JSONResponse(status_code=status, content=body)


@app.get("/api/v1/finblade/status")
async def finblade_status():
    """Is forwarding on, is it succeeding, and how far behind is it?"""
    return forwarder.status()


@app.post("/api/v1/finblade/flush")
async def finblade_flush():
    """Force a forwarding pass now instead of waiting for the timer."""
    if not forwarder.enabled:
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "forwarding disabled — set FINBLADE_URL"})
    await asyncio.to_thread(forwarder.tick)
    return {"ok": True, "status": forwarder.status()}


@app.get("/api/v1/identity/tuning")
async def get_identity_tuning():
    """Current matching parameters and where they came from."""
    return id_svc.get_tuning()


@app.post("/api/v1/identity/tuning")
async def set_identity_tuning(request: Request):
    """Adjust matching on a RUNNING registry — no restart, gallery preserved.

    Tuning needs live footage, and a restart empties the gallery, so you would
    otherwise re-measure from scratch on every nudge. Not persisted: write the
    value you settle on into the topology file's `matching:` block.
    """
    status, body = id_svc.set_tuning(await request.json())
    return JSONResponse(status_code=status, content=body)


@app.get("/api/v1/identity/counts")
async def identity_counts(charts: int = Query(1)):
    """Unique-people counts that work with NO zones defined.

    Zone occupancy needs a polygon; this needs only identity. `live` is distinct
    people visible now, `unique_total` is distinct people since startup.
    """
    body = id_svc.counts()
    # FinBlade chart tag (live-feed-chart-tags.md). Additive; ?charts=0 omits it.
    return _charts.attach(body, _charts.counts_charts(body)) if charts else body


@app.get("/api/v1/identity/list")
async def identity_list(limit: int = Query(200),
                        cross_camera_only: bool = Query(False)):
    return {"identities": id_svc.list_identities(
        limit=limit, cross_camera_only=cross_camera_only)}


@app.post("/api/v1/identity/merge")
async def identity_merge(request: Request):
    """Operator correction: fold one identity into another."""
    status, body = id_svc.merge(await request.json())
    return JSONResponse(status_code=status, content=body)


@app.get("/api/v1/identity/{global_ref}")
async def identity_journey(global_ref: str):
    """Where this person has been: cameras and zones, in order."""
    status, body = id_svc.journey(global_ref)
    return JSONResponse(status_code=status, content=body)


@app.post("/api/v1/reports/generate")
async def generate_report(request: Request):
    """On-demand R-08 report over a window (defaults to the last hour)."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    now = time.time()
    frm = float(body.get("from", now - 3600))
    to = float(body.get("to", now))
    return svc.generate_report(frm, to, kind="ondemand", camera_id=body.get("camera_id"))


@app.get("/api/v1/reports/{report_id:int}")
async def get_report(report_id: int):
    rep = svc.get_report(str(report_id))
    if rep is None:
        return JSONResponse(status_code=404, content={"error": "unknown report"})
    return rep


@app.post("/api/v1/zones")
async def save_zones(request: Request):
    """Persist the zone set for a camera (from the zone editor)."""
    payload = await request.json()
    code, body = svc.save_zones(payload)
    return JSONResponse(status_code=code, content=body)


@app.get("/api/v1/zones")
async def get_zones(camera_id: str = Query(None)):
    return {"zones": svc.list_zones(camera_id)}


def _effective_state(c: dict, now: float) -> str:
    """State to display: the worker's reported state, unless its health snapshot
    has gone stale (runner died) — then the camera reads OFFLINE."""
    if c.get("enabled") is False:
        return "DISABLED"
    hts = c.get("health_ts") or c.get("last_seen")
    if hts is None or (now - hts) > OFFLINE_S:
        return "OFFLINE"
    return c.get("state") or "ONLINE"


def _camera_list(drop_source: bool = False):
    """Camera rows with derived state.

    Shared by GET /cameras and the /ws push so the two can never disagree — the
    dashboard reads people counts from whichever arrives first, and a poll that
    reported different numbers from the socket would be untraceable.

    Credentials are masked on the way out (see redact.py): a camera source is
    normally rtsp://user:password@host, and that secret unlocks the camera, not
    this API. `drop_source` removes the field entirely for integration callers,
    who never need it.
    """
    now = time.time()
    out = []
    for c in svc.cameras():
        c = dict(c)
        c["effective_state"] = _effective_state(c, now)
        c["online"] = c["effective_state"] not in ("OFFLINE", "DISABLED")
        last = c.get("last_seen")
        c["seconds_since_seen"] = round(now - last, 1) if last is not None else None
        out.append(_redact.redact_camera(c, drop_source=drop_source))
    return out


def _is_integration(request) -> bool:
    """True when the caller presented the scoped integration key."""
    return _auth.presented_role(request.url.path, request.headers,
                                request.query_params) == _auth.ROLE_INTEGRATION


@app.get("/api/v1/cameras")
async def cameras(request: Request):
    return {"cameras": _camera_list(drop_source=_is_integration(request))}


@app.post("/api/v1/cameras/health")
async def camera_health(request: Request):
    """Ingest a health snapshot from an inference runner; returns control state."""
    code, body = svc.record_camera_health(await request.json())
    return JSONResponse(status_code=code, content=body)


@app.post("/api/v1/cameras")
async def create_camera(request: Request):
    """Register a camera. If a `source` (RTSP URL or file) is given, ALSO launch a
    detection pipeline for it — the camera comes online with a live stream, no CLI."""
    payload = await request.json()
    code, body = svc.upsert_camera(payload)
    if code != 200:
        return JSONResponse(status_code=code, content=body)
    source = (payload.get("source") or "").strip()
    if source:
        if not _valid_source(source):
            body["pipeline"] = "not started: source must be rtsp://, http(s)://, or an existing file"
        else:
            host = (request.headers.get("host") or "localhost").split(":")[0]
            try:
                info = cam_mgr.launch(payload["camera_id"], source,
                                      site_id=payload.get("site_id"), stream_host=host)
                # reflect the live stream URL immediately (the runner also posts it)
                svc.upsert_camera({"camera_id": payload["camera_id"],
                                   "stream_url": info["stream_url"]})
                body["pipeline"] = {"started": True, **info}
            except Exception as e:
                body["pipeline"] = f"failed to start: {e}"
    return JSONResponse(status_code=code, content=body)


@app.delete("/api/v1/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    cam_mgr.stop(camera_id)                       # stop its pipeline if we launched one
    # Release its identity bindings BEFORE the row goes. Deleting a camera is
    # worse than one crashing: the offline monitor only walks registered
    # cameras, so once the row is gone nothing would ever release these and the
    # people it was tracking would count toward site occupancy permanently,
    # with no recovery path.
    freed = id_svc.release_camera(camera_id)
    if freed:
        log.info("camera %s deleted: released %d identity binding(s)",
                 camera_id, freed)
    code, body = svc.delete_camera(camera_id)
    if code == 200:
        body["identity_bindings_released"] = freed
    return JSONResponse(status_code=code, content=body)


@app.post("/api/v1/cameras/{camera_id}/start")
async def start_camera(camera_id: str, request: Request):
    """(Re)start the pipeline for a camera using its stored source."""
    cam = next((c for c in svc.cameras() if c.get("camera_id") == camera_id), None)
    if not cam:
        return JSONResponse(status_code=404, content={"ok": False, "error": "unknown camera"})
    source = (cam.get("source") or "").strip()
    if not _valid_source(source):
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": "camera has no valid source"})
    host = (request.headers.get("host") or "localhost").split(":")[0]
    info = cam_mgr.launch(camera_id, source, site_id=cam.get("site_id"), stream_host=host)
    svc.upsert_camera({"camera_id": camera_id, "stream_url": info["stream_url"]})
    return {"ok": True, "camera_id": camera_id, **info}


@app.post("/api/v1/cameras/{camera_id}/stop")
async def stop_camera(camera_id: str):
    # Same reasoning as delete: a stopped pipeline releases nothing on its way
    # out, and a bound identity is never expired.
    stopped = cam_mgr.stop(camera_id)
    freed = id_svc.release_camera(camera_id)
    return {"ok": True, "camera_id": camera_id, "stopped": stopped,
            "identity_bindings_released": freed}


@app.post("/api/v1/cameras/{camera_id}/simulate-failure")
async def camera_simulate(camera_id: str):
    """Request a predictable camera failure (applied by the runner's next tick)."""
    code, body = svc.set_camera_sim(camera_id, True)
    return JSONResponse(status_code=code, content=body)


@app.post("/api/v1/cameras/{camera_id}/restore")
async def camera_restore(camera_id: str):
    code, body = svc.set_camera_sim(camera_id, False)
    return JSONResponse(status_code=code, content=body)


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
async def zone_states(camera_id: str = Query(None), zone_id: str = Query(None),
                      site_id: str = Query(None), charts: int = Query(1)):
    """Live zone state, optionally narrowed.

    All filters are optional and default to off, so an existing caller sees
    exactly what it saw before. zone_id is unique only within a camera, so
    filtering by zone_id alone can legitimately return rows from several
    cameras — pass camera_id too to address one zone.
    """
    rows = svc.zone_states()
    if camera_id:
        rows = [z for z in rows if z.get("camera_id") == camera_id]
    if zone_id:
        rows = [z for z in rows if z.get("zone_id") == zone_id]
    if site_id:
        rows = [z for z in rows if z.get("site_id") == site_id]
    body = {"zones": rows}
    # FinBlade chart tag (live-feed-chart-tags.md). Additive; ?charts=0 omits it.
    return _charts.attach(body, _charts.zone_charts(rows)) if charts else body


@app.get("/api/v1/alerts")
async def alerts(unacked_only: bool = False, severity: str = Query(None),
                 status: str = Query(None), zone_id: str = Query(None),
                 camera_id: str = Query(None), rule_id: str = Query(None),
                 site_id: str = Query(None)):
    """The active alert feed (OPEN + ACK), optionally narrowed.

    Filters are case-insensitive and default to off. severity is
    INFO|AMBER|RED|CRITICAL and status is OPEN|ACK — a resolved or dismissed
    alert has left this feed by definition; use /history/alerts for those.
    """
    return {"alerts": svc.list_alerts(
        unacked_only=unacked_only, severity=severity, status=status,
        zone_id=zone_id, camera_id=camera_id, rule_id=rule_id, site_id=site_id)}


@app.get("/api/v1/alerts/{alert_id}")
async def alert_detail(alert_id: str):
    """One alert by id, open or closed.

    Exists so a consumer can follow up on an alert it was pushed without
    scanning the active feed and discovering the alert has since been resolved
    out of it.
    """
    alert = svc.get_alert(alert_id)
    if alert is None:
        return JSONResponse(status_code=404,
                            content={"error": "unknown alert", "alert_id": alert_id})
    out = dict(alert)
    if out.get("frame"):
        # Point at the authenticated route rather than the filesystem path.
        out["frame_url"] = f"/api/v1/incidents/{quote(str(alert_id), safe='')}/frame"
    return out


@app.post("/api/v1/alerts/{alert_id}/ack")
async def ack(alert_id: str, request: Request):
    body = await request.json()
    code, resp = svc.acknowledge(alert_id, body.get("acknowledged_by", ""), time.time())
    return JSONResponse(status_code=code, content=resp)


@app.post("/api/v1/alerts/{alert_id}/resolve")
async def resolve(alert_id: str, request: Request):
    """Close an alert as handled (RESOLVED) or a false alarm (DISMISSED) + note."""
    body = await request.json()
    code, resp = svc.resolve(alert_id, body.get("action", "RESOLVED"),
                             body.get("resolved_by", ""), time.time(),
                             note=body.get("note"))
    return JSONResponse(status_code=code, content=resp)


@app.get("/api/v1/reports/occupancy", response_class=HTMLResponse)
async def occupancy_report():
    return render_report_html(svc.zone_states(), generated_at=time.time())


def _site_id(cameras=None):
    """This deployment's site.

    FINBLADE_SITE_ID wins; otherwise the site the cameras report, which is what
    the workers already send. Returns None rather than guessing when cameras
    disagree — a wrong site label is worse than an absent one for a platform
    routing records by it.
    """
    configured = os.environ.get("FINBLADE_SITE_ID")
    if configured:
        return configured
    sites = {c.get("site_id") for c in (cameras if cameras is not None
                                        else svc.cameras()) if c.get("site_id")}
    return sites.pop() if len(sites) == 1 else None


def _remote_camera_view(cam: dict) -> dict:
    """A camera row safe to hand a remote consumer.

    Two differences from GET /cameras, both deliberate:

    * `stream_url` is DROPPED. It points at an internal per-camera MJPEG server
      on 8090+, whose port is reassigned when a camera restarts and which is not
      exposed on any deployment where only 8000 is open. A remote consumer that
      reads that field gets a URL it cannot reach, intermittently — the worst
      failure shape there is.
    * `snapshot_path` / `stream_path` are ADDED: relative, stable, and the
      correct things to proxy. Relative because the consumer's proxy, not this
      host, decides the public origin.
    """
    cam = _redact.redact_camera(cam, drop_source=True)
    cam.pop("stream_url", None)
    camera_id = cam.get("camera_id")
    if camera_id:
        safe = quote(str(camera_id), safe="")
        cam["snapshot_path"] = f"/api/v1/cameras/{safe}/snapshot"
        cam["stream_path"] = f"/api/v1/cameras/{safe}/stream"
    return cam


@app.get("/healthz")
async def healthz():
    """Liveness. This process is running and serving. Nothing more.

    Open by design: a load balancer cannot present a key, and the answer
    reveals nothing a port scan would not.
    """
    return {"status": "ok", "ts": time.time()}


def _dependency_checks() -> dict:
    """Per-dependency status, so a consumer can tell WHICH thing is broken.

    A single up/down flag makes "CCTV is unavailable", "one camera dropped" and
    "the push to FinBlade is failing" indistinguishable, and they need three
    different responses from whoever is on call.
    """
    checks = {}
    try:
        svc.cameras()
        checks["store"] = {"ok": True}
    except Exception as exc:                        # noqa: BLE001
        log.exception("store health check failed")
        checks["store"] = {"ok": False, "error": exc.__class__.__name__}

    now = time.time()
    try:
        cams = _camera_list()
        live = [c for c in cams if c.get("effective_state") == "ONLINE"]
        checks["cameras"] = {"ok": True, "total": len(cams), "online": len(live),
                             "offline": len([c for c in cams if c.get(
                                 "effective_state") == "OFFLINE"])}
    except Exception as exc:                        # noqa: BLE001
        checks["cameras"] = {"ok": False, "error": exc.__class__.__name__}

    fw = forwarder.status()
    checks["forwarder"] = {
        # Disabled is not unhealthy — most deployments do not push.
        "ok": (not fw["enabled"]) or fw["last_error"] is None,
        "enabled": fw["enabled"], "last_error": fw["last_error"],
        "seconds_since_success": fw["seconds_since_success"]}
    checks["report_scheduler"] = {"ok": _loop_errors["report"] == 0,
                                  "errors": _loop_errors["report"]}
    checks["offline_monitor"] = {"ok": _loop_errors["offline"] == 0,
                                 "errors": _loop_errors["offline"]}
    checks["ts"] = now
    return checks


@app.get("/readyz")
async def readyz():
    """Readiness: can this instance serve requests? Terse and open."""
    checks = _dependency_checks()
    ready = bool(checks["store"]["ok"])
    return JSONResponse(status_code=200 if ready else 503,
                        content={"ready": ready,
                                 "store": checks["store"]["ok"], "ts": time.time()})


@app.get("/api/v1/health")
async def health_detail():
    """Full dependency breakdown. Authenticated — it names failure modes."""
    checks = _dependency_checks()
    healthy = all(v.get("ok", True) for v in checks.values()
                  if isinstance(v, dict))
    return JSONResponse(status_code=200 if healthy else 503,
                        content={"healthy": healthy, "site_id": _site_id(),
                                 "checks": checks})


@app.get("/api/v1/summary")
async def summary(charts: int = Query(1)):
    """Everything a remote dashboard needs, in ONE call at ONE instant.

    The /ws frame in REST form, plus people counts. Built from the same helpers
    as GET /cameras, /zones/state and /alerts, so a poller and the socket can
    never disagree.

    Why it exists: a platform rendering its own tiles from this data otherwise
    makes four round trips per refresh and stitches together four different
    instants. Over a WAN that is both slower and subtly wrong — the camera count
    and the zone occupancy in one render come from moments up to a second apart.

    Cadence note: `zones` is a 5-second aggregate upstream. Polling this faster
    than 5s returns the same zone numbers repeatedly; 5s is the honest rate, or
    use /ws and stop polling.
    """
    cameras = [_remote_camera_view(c) for c in _camera_list()]
    zones = svc.zone_states()
    alerts = svc.list_alerts(unacked_only=False)
    counts = id_svc.counts()

    # Pre-tallied so a tile does not have to reduce three arrays to render one
    # number, and so every consumer counts the same way. The arrays are still
    # here; these are a summary of them, not a replacement.
    cam_states = {"online": 0, "degraded": 0, "reconnecting": 0,
                  "offline": 0, "disabled": 0}
    for c in cameras:
        cam_states[str(c.get("effective_state", "")).lower()] = \
            cam_states.get(str(c.get("effective_state", "")).lower(), 0) + 1
    zone_states = {"normal": 0, "warning": 0, "critical": 0}
    for z in zones:
        key = str(z.get("status", "")).lower()
        if key in zone_states:
            zone_states[key] += 1
    sev = {"amber": 0, "red": 0, "critical": 0, "info": 0}
    for a in alerts:
        key = str(a.get("severity", "")).lower()
        if key in sev:
            sev[key] += 1

    body = {
        "site_id": _site_id(cameras),
        "cameras": cameras,
        "zones": zones,
        # Active feed = OPEN + ACK. Resolved and dismissed drop out.
        "alerts": alerts,
        "counts": counts,
        "summary": {
            # People on the monitored floor. None — not 0 — when no polygons are
            # drawn: occupancy cannot be computed, which is not the same as an
            # empty site, and rendering 0 there is the error this whole
            # integration keeps tripping over.
            "people_in_zones": (sum(int(z.get("occupancy") or 0) for z in zones)
                                if zones else None),
            "people_live": counts.get("live"),
            "cameras": cam_states,
            "zones": zone_states,
            "alerts": dict(sev, open_total=len(alerts)),
        },
        "ts": time.time(),
    }
    # FinBlade chart tag (live-feed-chart-tags.md). Additive; ?charts=0 omits it.
    return _charts.attach(body, _charts.summary_charts(body)) if charts else body


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Sub-second push of cameras + zone states + unacked alerts (UC-45). The
    dashboard falls back to 5s REST polling of the GET endpoints if this drops
    (UC-46)."""
    # Enforce the key HERE. The /api/v1 gate is @app.middleware("http"), and
    # Starlette's BaseHTTPMiddleware only sees scope["type"] == "http" — so a
    # WebSocket bypasses it entirely. Until this check existed, /ws served zone
    # states and alerts to anyone who could reach the port while every REST route
    # was gated. A browser cannot set headers on a WebSocket, so ?key= is
    # accepted here, exactly as for the MJPEG stream.
    if not _auth.request_is_authorised("/ws", websocket.headers,
                                       websocket.query_params):
        await websocket.close(code=1008)   # 1008 = policy violation
        return
    # Same projection rule as GET /cameras: an integration key never receives a
    # camera source, and the frame is credential-masked for everyone.
    drop_source = _auth.presented_role("/ws", websocket.headers,
                                       websocket.query_params) == _auth.ROLE_INTEGRATION
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({
                # Cameras ride the socket too. Without this the dashboard pushed
                # zones at 2/s while people_in_view still waited on a 3s poll, so
                # the headline count lagged the room by up to 8s and looked stuck.
                "cameras": _camera_list(drop_source=drop_source),
                "zones": svc.zone_states(),
                # Active feed = OPEN + ACK (resolved/dismissed drop out); operators
                # resolve acked alerts from here, so it can't be unacked-only.
                "alerts": svc.list_alerts(unacked_only=False),
                "ts": time.time(),
            })
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
