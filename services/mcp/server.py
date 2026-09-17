"""FinBlade CCTV — MCP server.

The chatbot's whole view of the system, as Model Context Protocol tools,
resources and a prompt. A chatbot (FinBlade's platform, Claude, anything that
speaks MCP) connects here and discovers what it can ask; it never reads the
database and never learns a URL.

    .venv/bin/python -m services.mcp.server                # streamable HTTP on :8010
    FINBLADE_MCP_TOKEN=<secret> .venv/bin/python -m services.mcp.server
    .venv/bin/python -m services.mcp.server --stdio        # for a local MCP client

WHY TOOLS OVER THE API AND NOT VIEWS OVER THE DATABASE. The numbers this
system produces are only correct with their caveats attached: a missing row is
"unchanged", not zero; a `null` bucket is "camera not watching", not "empty";
`zone_id` is unique only within a camera; an average over a partly observed
window carries `coverage` and must be qualified. SQL views expose the rows and
leave the caveats to whoever writes the query. Tools expose the ANSWERS, with
the caveats in the description the model reads before it calls. The views stay
(docs/CHATBOT_DATABASE_INSTRUCTIONS.md) for ad-hoc analyst SQL; this is the
chatbot's front door.

WHAT IT REACHES. Every tool is a thin call to the REST API through one Backend
(below) with the INTEGRATION key: every GET, plus exactly two writes — alert
acknowledge and resolve — which is the integration role's whole permission set
(services/api/auth.py). Nothing here can delete, provision or reconfigure.
Credentials are redacted by the API before they get here; the MCP server never
sees an RTSP URL.

ONE MAPPING RULE, REPEATED FOR THE MODEL: `site_id` on cameras, zones, events
and alerts IS the branch id. Region -> City -> Branch narrowing on every listing
tool uses `region_id` / `city_id` / `branch_id`, which intersect.

A tracker is a VEHICLE or an ASSET, never a person. No tool here returns or
accepts a driver identity, and no tool returns a person identifier that is
not an opaque salted hash.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

DEFAULT_API = os.environ.get("FINBLADE_API_URL", "http://127.0.0.1:8000")
DEFAULT_PORT = int(os.environ.get("FINBLADE_MCP_PORT", "8010"))


# ------------------------------------------------------------------ backend --
class Backend:
    """HTTP to the FinBlade API. Injected, so tests run the tools against
    FastAPI's TestClient and never open a socket."""

    def __init__(self, base_url: str = DEFAULT_API, api_key: Optional[str] = None,
                 timeout: float = 20.0):
        import requests
        self.base = base_url.rstrip("/")
        self.key = api_key or os.environ.get("CCTV_API_KEY") or os.environ.get("FINBLADE_INTEGRATION_KEY")
        self.timeout = timeout
        self._s = requests.Session()
        if self.key:
            self._s.headers["Authorization"] = f"Bearer {self.key}"

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._s.get(self.base + path, params=_clean(params), timeout=self.timeout)
        return _decode(r.status_code, r.headers.get("content-type", ""), r.content)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        r = self._s.post(self.base + path, json=body or {}, timeout=self.timeout)
        return _decode(r.status_code, r.headers.get("content-type", ""), r.content)

    def get_bytes(self, path: str, params: Optional[Dict[str, Any]] = None):
        r = self._s.get(self.base + path, params=_clean(params), timeout=self.timeout)
        return r.status_code, r.headers.get("content-type", ""), r.content


class TestClientBackend(Backend):
    """Same interface over a FastAPI TestClient (tests only)."""

    __test__ = False        # not a test case, whatever the name suggests to pytest

    def __init__(self, client, api_key: Optional[str] = None):   # noqa: D107
        self.c = client
        self.key = api_key
        self.h = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def get(self, path, params=None):
        r = self.c.get(path, params=_clean(params), headers=self.h)
        return _decode(r.status_code, r.headers.get("content-type", ""), r.content)

    def post(self, path, body=None):
        r = self.c.post(path, json=body or {}, headers=self.h)
        return _decode(r.status_code, r.headers.get("content-type", ""), r.content)

    def get_bytes(self, path, params=None):
        r = self.c.get(path, params=_clean(params), headers=self.h)
        return r.status_code, r.headers.get("content-type", ""), r.content


def _clean(params):
    return {k: v for k, v in (params or {}).items() if v is not None and v != ""}


def _decode(status: int, ctype: str, body: bytes) -> Any:
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        data = {"raw": body[:500].decode("utf-8", "replace")}
    if status >= 400:
        # Surface the API's own message; a 409 on zone_id carries the
        # candidate cameras, which is exactly what the model needs next.
        return {"error": True, "status": status, "detail": data}
    return data


class ApiError(ToolError):
    """A ToolError so the message reaches the model verbatim. A generic
    exception is masked as 'Error executing tool X', which tells the model
    nothing it can act on; a 409 with candidate cameras tells it exactly
    what to send next."""


def _ok(data: Any) -> Any:
    if isinstance(data, dict) and data.get("error") is True:
        raise ApiError(f"API {data.get('status')}: {json.dumps(data.get('detail'))[:600]}")
    return data


# ------------------------------------------------------------------ helpers --
def _window(hours: Optional[float], from_ts: Optional[float], to_ts: Optional[float],
            default_hours: float) -> Dict[str, float]:
    """from/to however the model expressed it. Explicit bounds win; `hours`
    is the fallback; nothing at all means the default look-back."""
    if from_ts is not None or to_ts is not None:
        out = {}
        if from_ts is not None:
            out["from"] = float(from_ts)
        if to_ts is not None:
            out["to"] = float(to_ts)
        return out
    now = time.time()
    h = float(hours) if hours is not None else default_hours
    return {"from": now - h * 3600.0, "to": now}


def _scope(region_id, city_id, branch_id) -> Dict[str, str]:
    return {"region_id": region_id, "city_id": city_id, "branch_id": branch_id}


_SCOPE_DOC = ("Narrow by the customer's hierarchy with region_id, city_id and/or "
              "branch_id (they intersect; an unknown id returns nothing). "
              "Omit all three for the whole network.")

_COVERAGE_DOC = (" The response carries `coverage` (0.0-1.0), the fraction of the "
                 "window the camera was actually observing. Below 0.95 you MUST say "
                 "so in the answer, e.g. 'the camera only covered 4 of those 24 hours'. "
                 "A `null` bucket means the camera was not watching, not that the "
                 "zone was empty.")

_ZONE_ID_DOC = (" zone_id is unique only WITHIN a camera; if the API answers 409 the "
                "zone name exists on several cameras and the candidates are listed — "
                "pass camera_id to pick one.")

RULES = {
    "R-01": "Density above the zone's warning threshold (default 2.0 people/m²) — AMBER. Hysteresis: clears only below the clear threshold, and a 10 s debounce stops oscillation producing many alerts.",
    "R-02": "Density above the critical threshold (default 4.0 people/m²) — RED.",
    "R-03": "Occupancy at or above 90% of the zone's configured capacity — AMBER.",
    "R-04": "Bottleneck detection — NOT BUILT (cut).",
    "R-05": "Loitering: one person in a zone longer than its dwell threshold — AMBER.",
    "R-06": "Restricted-zone intrusion: anyone inside a RESTRICTED zone — immediate, RED, one alert per visit.",
    "R-07": "Camera silent for more than 30 s — RED; auto-resolves with an INFO 'recovered' when it reports again.",
    "R-08": "Scheduled and on-demand occupancy report.",
    "R-09": "Head count above a per-zone threshold, independent of area — AMBER.",
    "R-10": "Sustained fire or smoke detected in view — evaluation model, not a certified fire alarm.",
    "R-11": "Required PPE missing on a tracked person inside a compliance zone — COMPLIANCE severity (violet). Evaluation model; per person, per zone, slow to accuse.",
    "R-12": "GPS tracker (vehicle) silent for more than 5 minutes — AMBER; auto-resolves on the next report.",
}

SEVERITIES = "INFO | AMBER (warning) | RED / CRITICAL | COMPLIANCE (a person-policy breach, R-11)"

DATA_NOTES = """# Reading FinBlade CCTV numbers correctly

* A camera detection is NOT a person. One human can be seen by two cameras at
  once, lost and re-acquired by the tracker, and recorded in three event rows
  for one movement. Occupancy figures that say "distinct" have removed the
  overlap; "summed" and "in view" figures have not, and say so.
* Zone occupancy = people standing inside a drawn polygon this instant.
  Facility occupancy = people admitted through a door and not yet seen to
  leave (survives walking out of camera view). They are different measures.
* History is written on change. A zone with four rows in a day is not a zone
  with four minutes of data. Every history tool holds readings forward and
  reports `coverage`; below 0.95, say so.
* `null` in a time bucket means the camera was not observing. It is not zero.
* zone_id is unique only within a camera. Always pair it with camera_id when
  the API says it is ambiguous (HTTP 409 with candidates).
* site_id on any record IS the branch id (Region -> City -> Branch).
* person_ref / global_ref are opaque salted hashes. Nobody can be identified
  from them and they must never be presented as identities.
* A tracker is a vehicle or an asset. There is no driver.
* R-10 (fire/smoke) and R-11 (PPE) are EVALUATION models, not certified
  safety systems; present their alerts as "the detector reported", not fact.
"""

ANALYST_PROMPT = """You are the FinBlade CCTV analyst for {tenant}. You answer questions
about cameras, zones, occupancy, alerts, facility presence and vehicle
tracking across a network organised as Region -> City -> Branch.

Rules:
1. Never guess a number. Call a tool. If a tool returns `coverage` below
   0.95, qualify the answer with how much of the window was observed.
2. Say what a figure IS: zone occupancy (inside polygons), facility
   occupancy (door-counted), or people in view (detections, may double count).
3. Never present a person_ref, global_ref or track id as an identity, and
   never speculate about who somebody is. A tracker is a vehicle, not a driver.
4. Fire/smoke (R-10) and PPE (R-11) alerts come from evaluation models:
   report them as detector output, with confidence when given.
5. When a zone name is ambiguous across cameras, ask which camera or use the
   camera the user has been talking about.
6. Prefer the narrowest scope the user named (a branch, a city, a region) and
   say which scope the answer covers.
7. Timestamps from tools are Unix epoch seconds UTC; the site timezone is
   {tz}. Convert when you present times.
"""


# ------------------------------------------------------------------ server ---
def build_server(backend: Backend, name: str = "finblade-cctv") -> MCPServer:
    """All tools, resources and prompts bound to one backend."""
    s = MCPServer(
        name, version="1.0",
        instructions=("FinBlade CCTV crowd analytics for a lab network organised "
                      "Region -> City -> Branch. Start with network_overview or "
                      "summary; read resource finblade://data-notes before "
                      "interpreting numbers. site_id == branch_id everywhere."),
    )

    # ---- network / hierarchy ----------------------------------------------
    @s.tool(description=(
        "The whole network: tenant, Region -> City -> Branch tree, and at every "
        "level the roll-up of cameras (total/online/offline), people in view, "
        "zones not normal, and open alerts by severity. Cameras whose site_id "
        "matches no branch appear under `unassigned`. Use this first for any "
        "'how is <region/city/branch> doing' question."))
    def network_overview() -> dict:
        return _ok(backend.get("/api/v1/org"))

    @s.tool(description=(
        "Raw hierarchy rows: regions, cities, branches (with lat/lon and "
        "geofence radius) and tenant meta. Use to resolve a name to an id."))
    def org_index() -> dict:
        return _ok(backend.get("/api/v1/org/index"))

    @s.tool(description=(
        "One branch: its roll-up, cameras with state, and the vehicles currently "
        "inside its geofence. branch_id is the site_id."))
    def branch(branch_id: str) -> dict:
        tree = _ok(backend.get("/api/v1/org"))
        for r in tree.get("regions", []):
            for c in r.get("cities", []):
                for b in c.get("branches", []):
                    if b["branch_id"] == branch_id:
                        vehicles = _ok(backend.get("/api/v1/trackers")).get("trackers", [])
                        b = dict(b, region=r["name"], city=c["name"],
                                 vehicles_present=[v for v in vehicles if v.get("at_branch_id") == branch_id])
                        return b
        raise ApiError(f"unknown branch_id {branch_id!r}; see org_index")

    @s.tool(description=(
        "Everything a dashboard shows at one instant, for a scope: cameras, live "
        "zone states, open alerts, vehicles, plus a `summary` block with the "
        "tallies. " + _SCOPE_DOC))
    def summary(region_id: Optional[str] = None, city_id: Optional[str] = None,
                branch_id: Optional[str] = None) -> dict:
        return _ok(backend.get("/api/v1/summary", dict(_scope(region_id, city_id, branch_id), charts=0)))

    # ---- cameras -----------------------------------------------------------
    @s.tool(description=(
        "Cameras with health: effective_state (ONLINE/DEGRADED/RECONNECTING/OFFLINE/"
        "DISABLED), seconds_since_seen, input_fps, resolution, people_in_view "
        "(detections in frame — may double count across cameras), "
        "people_in_zones, tracking_quality and counts_reliable (tri-state: null "
        "means not reported). Optional `state` filters on effective_state. "
        + _SCOPE_DOC))
    def cameras(region_id: Optional[str] = None, city_id: Optional[str] = None,
                branch_id: Optional[str] = None, state: Optional[str] = None) -> dict:
        rows = _ok(backend.get("/api/v1/cameras", _scope(region_id, city_id, branch_id))).get("cameras", [])
        if state:
            rows = [c for c in rows if str(c.get("effective_state", "")).upper() == state.upper()]
        return {"cameras": rows, "count": len(rows)}

    @s.tool(description="One camera's health record and its zones' live state.")
    def camera(camera_id: str) -> dict:
        cams = _ok(backend.get("/api/v1/cameras")).get("cameras", [])
        cam = next((c for c in cams if c.get("camera_id") == camera_id), None)
        if not cam:
            raise ApiError(f"unknown camera_id {camera_id!r}")
        zones = _ok(backend.get("/api/v1/zones/state", {"camera_id": camera_id, "charts": 0})).get("zones", [])
        return {"camera": cam, "zones": zones}

    @s.tool(description=(
        "A current JPEG frame from a camera, annotated with boxes and zones. Use "
        "it to describe what is happening now. Returns an image; no people are "
        "identified and none may be."))
    def camera_snapshot(camera_id: str) -> Image:
        code, ctype, body = backend.get_bytes(f"/api/v1/cameras/{camera_id}/snapshot")
        if code >= 400 or not body:
            raise ApiError(f"no snapshot for {camera_id} (HTTP {code})")
        return Image(data=body, format="jpeg")

    # ---- zones -------------------------------------------------------------
    @s.tool(description=(
        "Live state of every zone: occupancy, density (people/m²), capacity_pct, "
        "status NORMAL/WARNING/CRITICAL, restricted flag, trend, inflow/outflow "
        "per minute, PPE compliance counts where required, and `occupants` "
        "(opaque refs, for de-duplication only). Optional filters: camera_id, "
        "status, restricted_only. " + _SCOPE_DOC))
    def zones_live(region_id: Optional[str] = None, city_id: Optional[str] = None,
                   branch_id: Optional[str] = None, camera_id: Optional[str] = None,
                   status: Optional[str] = None, restricted_only: bool = False) -> dict:
        p = dict(_scope(region_id, city_id, branch_id), camera_id=camera_id, charts=0)
        rows = _ok(backend.get("/api/v1/zones/state", p)).get("zones", [])
        if status:
            rows = [z for z in rows if str(z.get("status", "")).upper() == status.upper()]
        if restricted_only:
            rows = [z for z in rows if z.get("restricted")]
        return {"zones": rows, "count": len(rows),
                "not_normal": sum(1 for z in rows if str(z.get("status", "NORMAL")).upper() != "NORMAL")}

    @s.tool(description=(
        "Zone DEFINITIONS (not live numbers): type (MONITORED/RESTRICTED/ENTRANCE/"
        "EXIT/DOOR/OUTSIDE/TRANSITION/UNMONITORED), capacity_max, area_sqm, "
        "warning/critical density thresholds, loitering threshold, physical_area_id, "
        "required PPE and profile. Filter by camera_id."))
    def zone_config(camera_id: Optional[str] = None) -> dict:
        rows = _ok(backend.get("/api/v1/zones", {"camera_id": camera_id})).get("zones", [])
        for z in rows:
            z.pop("polygon", None); z.pop("normalized_polygon", None); z.pop("adjacency_list", None)
        return {"zones": rows, "count": len(rows)}

    @s.tool(description=(
        "Restricted (no-go) zones and whether anyone is inside RIGHT NOW. An "
        "occupancy above 0 in a restricted zone is an intrusion (R-06). "
        + _SCOPE_DOC))
    def restricted_zones(region_id: Optional[str] = None, city_id: Optional[str] = None,
                         branch_id: Optional[str] = None) -> dict:
        rows = _ok(backend.get("/api/v1/zones/state", dict(_scope(region_id, city_id, branch_id), charts=0))).get("zones", [])
        rz = [z for z in rows if z.get("restricted")]
        return {"restricted_zones": rz, "count": len(rz),
                "intrusions_now": [z for z in rz if (z.get("occupancy") or 0) > 0]}

    @s.tool(description=(
        "Occupancy / density of one zone over time, in buckets (default 300 s). "
        "Give `hours` to look back from now, or from_ts/to_ts (epoch seconds UTC)."
        + _COVERAGE_DOC + _ZONE_ID_DOC))
    def zone_history(zone_id: str, camera_id: Optional[str] = None, hours: Optional[float] = None,
                     from_ts: Optional[float] = None, to_ts: Optional[float] = None,
                     bucket_seconds: float = 300.0) -> dict:
        p = dict(_window(hours, from_ts, to_ts, 24.0), camera_id=camera_id, bucket=bucket_seconds, charts=0)
        return _ok(backend.get(f"/api/v1/zones/{zone_id}/series", p))

    @s.tool(description=(
        "The state of one zone at one instant (epoch seconds UTC): the reading "
        "that was in force then, and how old it was. `null` = not observed."
        + _ZONE_ID_DOC))
    def zone_at_time(zone_id: str, ts: float, camera_id: Optional[str] = None) -> dict:
        return _ok(backend.get(f"/api/v1/zones/{zone_id}/at", {"ts": ts, "camera_id": camera_id}))

    @s.tool(description=(
        "How long a zone satisfied a condition in a window, e.g. occupancy gt 10, "
        "density gte 2.0, or status CRITICAL. field: occupancy|density|capacity_pct; "
        "op: gt|gte|lt|lte|eq. Returns seconds and the intervals."
        + _COVERAGE_DOC + _ZONE_ID_DOC))
    def zone_duration(zone_id: str, camera_id: Optional[str] = None, field: str = "occupancy",
                      op: str = "gt", value: float = 0.0, status: Optional[str] = None,
                      hours: Optional[float] = None, from_ts: Optional[float] = None,
                      to_ts: Optional[float] = None) -> dict:
        p = dict(_window(hours, from_ts, to_ts, 24.0), camera_id=camera_id, field=field, op=op,
                 value=value, status=status)
        return _ok(backend.get(f"/api/v1/zones/{zone_id}/duration", p))

    @s.tool(description=(
        "Zone-to-zone movement counts (people who walked from one zone into "
        "another) in the last `minutes`, optionally for one camera."))
    def zone_movement(minutes: float = 15.0, camera_id: Optional[str] = None) -> dict:
        return _ok(backend.get("/api/v1/movement", {"minutes": minutes, "camera_id": camera_id, "charts": 0}))

    # ---- physical areas & facility -----------------------------------------
    @s.tool(description=(
        "Physical areas — one real room watched by several cameras — with "
        "DISTINCT-person occupancy (the overlap removed) alongside each camera's "
        "own observation. The honest room count when two cameras overlap."))
    def areas_live() -> dict:
        return _ok(backend.get("/api/v1/areas/state"))

    @s.tool(description=(
        "Facility (building) occupancy: people admitted through DOOR zones and "
        "not yet seen to leave, door entry/exit rates, declared baseline and "
        "stale entries. Different from zone occupancy — it survives a person "
        "walking into a corridor no camera watches. If no door zones are "
        "configured the roster cannot move; say so rather than reporting 0."))
    def facility_occupancy() -> dict:
        return _ok(backend.get("/api/v1/facility/occupancy"))

    @s.tool(description=(
        "Distinct-people counts from cross-camera identity: live bindings and "
        "cumulative unique people per camera, optionally within a window "
        "(hours or from_ts/to_ts). Cumulative counts are footfall; `live` can "
        "drift above the true headcount after a worker restart — prefer "
        "people_in_view from cameras() for 'right now'."))
    def people_counts(hours: Optional[float] = None, from_ts: Optional[float] = None,
                      to_ts: Optional[float] = None) -> dict:
        p = {"charts": 0}
        if hours is not None or from_ts is not None or to_ts is not None:
            p.update(_window(hours, from_ts, to_ts, 24.0))
        return _ok(backend.get("/api/v1/identity/counts", p))

    # ---- alerts ------------------------------------------------------------
    @s.tool(description=(
        "OPEN and ACKNOWLEDGED alerts (the active feed), newest first, at most "
        f"the 200 most recent. Severity is {SEVERITIES}. Filters: severity, "
        "status (OPEN|ACK), rule_id (R-01..R-12), camera_id, zone_id. If count is "
        "200 the feed is saturated — say 'at least 200' and suggest narrowing. "
        + _SCOPE_DOC))
    def alerts_active(region_id: Optional[str] = None, city_id: Optional[str] = None,
                      branch_id: Optional[str] = None, severity: Optional[str] = None,
                      status: Optional[str] = None, rule_id: Optional[str] = None,
                      camera_id: Optional[str] = None, zone_id: Optional[str] = None) -> dict:
        p = dict(_scope(region_id, city_id, branch_id), severity=severity, status=status,
                 rule_id=rule_id, camera_id=camera_id, zone_id=zone_id)
        rows = _ok(backend.get("/api/v1/alerts", p)).get("alerts", [])
        by = {}
        for a in rows:
            by[str(a.get("severity"))] = by.get(str(a.get("severity")), 0) + 1
        return {"alerts": rows, "count": len(rows), "by_severity": by}

    @s.tool(description=(
        "Alert history including RESOLVED and DISMISSED, newest first, paged. "
        "Window by hours or from_ts/to_ts. Filters: severity, status, rule_id, "
        "camera_id, zone_id. " + _SCOPE_DOC))
    def alerts_history(hours: Optional[float] = None, from_ts: Optional[float] = None,
                       to_ts: Optional[float] = None, region_id: Optional[str] = None,
                       city_id: Optional[str] = None, branch_id: Optional[str] = None,
                       severity: Optional[str] = None, status: Optional[str] = None,
                       rule_id: Optional[str] = None, camera_id: Optional[str] = None,
                       zone_id: Optional[str] = None, limit: int = 200, offset: int = 0) -> dict:
        p = dict(_window(hours, from_ts, to_ts, 24.0), **_scope(region_id, city_id, branch_id),
                 severity=severity, status=status, rule_id=rule_id, camera_id=camera_id,
                 zone_id=zone_id, limit=limit, offset=offset)
        return _ok(backend.get("/api/v1/history/alerts", p))

    @s.tool(description="One alert by id, open or closed, with its full record.")
    def alert(alert_id: str) -> dict:
        return _ok(backend.get(f"/api/v1/alerts/{alert_id}"))

    @s.tool(description=(
        "The saved JPEG frame for an alert (the scene when it fired; for R-11 a "
        "crop of the person accused, unidentified). Returns an image, or an error "
        "when no frame was saved."))
    def incident_frame(alert_id: str) -> Image:
        code, ctype, body = backend.get_bytes(f"/api/v1/incidents/{alert_id}/frame")
        if code >= 400 or not body:
            raise ApiError(f"no frame saved for alert {alert_id} (HTTP {code})")
        return Image(data=body, format="jpeg")

    @s.tool(description=(
        "Acknowledge an alert on behalf of an operator (it stays in the active "
        "feed as ACK). `by` is the operator's name or login as they gave it."))
    def acknowledge_alert(alert_id: str, by: str = "chatbot") -> dict:
        return _ok(backend.post(f"/api/v1/alerts/{alert_id}/ack", {"acknowledged_by": by}))

    @s.tool(description=(
        "Close an alert: action RESOLVED (dealt with) or DISMISSED (false alarm), "
        "with an optional note. It leaves the active feed."))
    def resolve_alert(alert_id: str, action: str = "RESOLVED", by: str = "chatbot",
                      note: Optional[str] = None) -> dict:
        return _ok(backend.post(f"/api/v1/alerts/{alert_id}/resolve",
                                {"action": action.upper(), "resolved_by": by, "note": note}))

    # ---- events ------------------------------------------------------------
    @s.tool(description=(
        "Raw events, newest first, paged: ZONE_ENTRY/EXIT/TRANSITION, "
        "RESTRICTED_ZONE_ENTRY/EXIT, LOITERING_*, CAMERA_*, FACILITY_ENTRY/EXIT, "
        "HAZARD_FIRE/SMOKE, PPE_VIOLATION/COMPLIANT, TRACKER_ARRIVED/DEPARTED. "
        "A confirmed move produces one ZONE_TRANSITION plus a derived "
        "ZONE_EXIT and ZONE_ENTRY — count transitions, not the pair. Filters: "
        "event_type, camera_id, zone_id, global_ref (opaque). " + _SCOPE_DOC))
    def events_history(hours: Optional[float] = None, from_ts: Optional[float] = None,
                       to_ts: Optional[float] = None, event_type: Optional[str] = None,
                       camera_id: Optional[str] = None, zone_id: Optional[str] = None,
                       global_ref: Optional[str] = None, region_id: Optional[str] = None,
                       city_id: Optional[str] = None, branch_id: Optional[str] = None,
                       limit: int = 200, offset: int = 0) -> dict:
        p = dict(_window(hours, from_ts, to_ts, 24.0), **_scope(region_id, city_id, branch_id),
                 event_type=event_type, camera_id=camera_id, zone_id=zone_id,
                 global_ref=global_ref, limit=limit, offset=offset)
        return _ok(backend.get("/api/v1/history/events", p))

    # ---- reports -----------------------------------------------------------
    @s.tool(description=(
        "Occupancy report for a window: per zone, samples, average/peak "
        "occupancy and density, time above thresholds. Window by hours or "
        "from_ts/to_ts; optional camera_id / zone_id."))
    def occupancy_report(hours: Optional[float] = None, from_ts: Optional[float] = None,
                         to_ts: Optional[float] = None, camera_id: Optional[str] = None,
                         zone_id: Optional[str] = None) -> dict:
        p = dict(_window(hours, from_ts, to_ts, 24.0), camera_id=camera_id, zone_id=zone_id, charts=0)
        return _ok(backend.get("/api/v1/reports/occupancy.json", p))

    @s.tool(description="Previously generated reports (R-08), newest first.")
    def reports_list(limit: int = 20) -> dict:
        return _ok(backend.get("/api/v1/reports", {"limit": limit}))

    # ---- vehicles / trackers -----------------------------------------------
    @s.tool(description=(
        "GPS trackers on lab vehicles and devices, with latest position, state "
        "(MOVING/STOPPED/OFFLINE/NEVER_SEEN), speed, battery, seconds since the "
        "last report, and at_branch_id when inside a branch geofence. A tracker "
        "is a vehicle or an asset, never a person. Scope narrows by home branch. "
        + _SCOPE_DOC))
    def vehicles(region_id: Optional[str] = None, city_id: Optional[str] = None,
                 branch_id: Optional[str] = None, state: Optional[str] = None) -> dict:
        d = _ok(backend.get("/api/v1/trackers", _scope(region_id, city_id, branch_id)))
        rows = d.get("trackers", [])
        if state:
            rows = [t for t in rows if str(t.get("state", "")).upper() == state.upper()]
        return {"vehicles": rows, "count": len(rows), "silent_after_s": d.get("silent_after_s")}

    @s.tool(description="One tracker's record and latest position.")
    def vehicle(tracker_id: str) -> dict:
        rows = _ok(backend.get("/api/v1/trackers")).get("trackers", [])
        t = next((x for x in rows if x.get("tracker_id") == tracker_id), None)
        if not t:
            raise ApiError(f"unknown tracker_id {tracker_id!r}")
        return t

    @s.tool(description=(
        "The path a tracker drove: positions (lat, lon, speed_kmh, heading, ts) "
        "in a window, oldest first. Window by `minutes` back from now, or "
        "from_ts/to_ts."))
    def vehicle_track(tracker_id: str, minutes: float = 60.0, from_ts: Optional[float] = None,
                      to_ts: Optional[float] = None, limit: int = 2000) -> dict:
        p = {"minutes": minutes, "from": from_ts, "to": to_ts, "limit": limit}
        return _ok(backend.get(f"/api/v1/trackers/{tracker_id}/track", p))

    @s.tool(description=(
        "Vehicle arrivals at and departures from branches (geofence crossings): "
        "TRACKER_ARRIVED / TRACKER_DEPARTED events with branch_id, distance_m and "
        "dwell_s on departure. Window by hours or from_ts/to_ts; optional "
        "tracker_id and branch_id (the event's site_id)."))
    def vehicle_arrivals(hours: Optional[float] = None, from_ts: Optional[float] = None,
                         to_ts: Optional[float] = None, tracker_id: Optional[str] = None,
                         branch_id: Optional[str] = None, limit: int = 200) -> dict:
        w = _window(hours, from_ts, to_ts, 24.0)
        out = []
        for et in ("TRACKER_ARRIVED", "TRACKER_DEPARTED"):
            p = dict(w, event_type=et, camera_id=tracker_id, site_id=branch_id, limit=limit)
            out += _ok(backend.get("/api/v1/history/events", p)).get("events", [])
        out.sort(key=lambda e: e.get("timestamp") or e.get("ts") or 0, reverse=True)
        return {"events": out[:limit], "count": len(out)}

    @s.tool(description="Vehicles currently inside a branch's geofence, with how long they have been there.")
    def vehicles_at_branch(branch_id: str) -> dict:
        rows = _ok(backend.get("/api/v1/trackers")).get("trackers", [])
        here = [t for t in rows if t.get("at_branch_id") == branch_id]
        return {"branch_id": branch_id, "vehicles": here, "count": len(here)}

    # ---- appearance search -------------------------------------------------
    @s.tool(description=(
        "Find people by what they were WEARING or CARRYING — a description, "
        "never an identity: upper_colour / lower_colour (black, white, grey, blue, "
        "red, green, yellow, brown, beige, pink, purple, orange), headwear (none, "
        "cap, hat, headscarf, helmet), mask (yes/no), bag (none, backpack, handbag, "
        "shoulder bag, box or case), outerwear (none, lab coat, jacket, abaya, "
        "vest). Give only the attributes the user stated. Results are grouped by "
        "person with a timeline of sightings (camera, zone, time) and a crop per "
        "sighting; they are CANDIDATES for a human to confirm — colour under lab "
        "lighting is unreliable, so say 'matches the description' not 'found'. "
        "Never infer or report gender, age or ethnicity. Window by hours or "
        "from_ts/to_ts. Every search is audited. " + _SCOPE_DOC))
    def find_people(upper_colour: Optional[str] = None, lower_colour: Optional[str] = None,
                    headwear: Optional[str] = None, mask: Optional[str] = None,
                    bag: Optional[str] = None, outerwear: Optional[str] = None,
                    hours: Optional[float] = None, from_ts: Optional[float] = None,
                    to_ts: Optional[float] = None, camera_id: Optional[str] = None,
                    region_id: Optional[str] = None, city_id: Optional[str] = None,
                    branch_id: Optional[str] = None, limit: int = 200) -> dict:
        p = dict(_window(hours, from_ts, to_ts, 2.0), **_scope(region_id, city_id, branch_id),
                 upper_colour=upper_colour, lower_colour=lower_colour, headwear=headwear,
                 mask=mask, bag=bag, outerwear=outerwear, camera_id=camera_id, limit=limit)
        return _ok(backend.get("/api/v1/search/people", p))

    @s.tool(description=(
        "Every sighting of one person ref (a `person` value from find_people that "
        "starts with gp_) in the last `hours`: where they were seen, when, with a "
        "crop each. The ref is an opaque hash and names nobody."))
    def person_timeline(global_ref: str, hours: float = 24.0) -> dict:
        return _ok(backend.get(f"/api/v1/search/people/{global_ref}", {"hours": hours}))

    # ---- system ------------------------------------------------------------
    @s.tool(description=(
        "Is the CCTV system healthy: database, bus, cameras online, and the "
        "background loops (offline monitor, report scheduler, forwarder, tracker "
        "monitor) with their error counts."))
    def system_health() -> dict:
        return _ok(backend.get("/api/v1/health"))

    @s.tool(description="What each rule R-01..R-12 means, its severity, and the hysteresis/debounce discipline.")
    def rules_reference() -> dict:
        return {"rules": RULES, "severities": SEVERITIES,
                "discipline": ("Every threshold rule has separate on/off thresholds "
                               "(hysteresis) and a 10 s debounce, so rapid oscillation "
                               "produces one alert, not many. R-06 is immediate.")}

    # ---- resources & prompt ------------------------------------------------
    @s.resource("finblade://data-notes", mime_type="text/markdown",
                description="How to read the numbers without the three classic errors.")
    def data_notes() -> str:
        return DATA_NOTES

    @s.resource("finblade://rules", mime_type="application/json",
                description="Rule catalogue R-01..R-12.")
    def rules_res() -> str:
        return json.dumps({"rules": RULES, "severities": SEVERITIES}, indent=1)

    @s.resource("finblade://capabilities", mime_type="text/markdown",
                description="docs/CAPABILITIES.md — what the system can do, with status markers.")
    def capabilities() -> str:
        path = os.path.join(REPO, "docs", "CAPABILITIES.md")
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return "capabilities document not available on this host"

    @s.prompt(description="System prompt for a CCTV analyst chatbot over this server.")
    def cctv_analyst(tenant: str = "the customer", timezone: str = "Asia/Riyadh") -> str:
        return ANALYST_PROMPT.format(tenant=tenant, tz=timezone)

    return s


# ------------------------------------------------------------------ transport
def make_app(server: MCPServer, token: Optional[str], host: str = "0.0.0.0"):
    """The Starlette app with bearer-token auth in front of /mcp.

    A tracker unit needed ?key=; an MCP client can send headers, so this is
    header-only. No token configured = open, which is the same posture as
    the API itself with FINBLADE_API_KEY unset — and logged the same way.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from mcp.server.transport_security import TransportSecuritySettings

    app = server.streamable_http_app(
        host=host, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    class Bearer(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if token:
                auth = request.headers.get("authorization", "")
                if not (auth.lower().startswith("bearer ") and auth[7:].strip() == token):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(Bearer)
    return app


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="FinBlade CCTV MCP server")
    ap.add_argument("--api-url", default=DEFAULT_API)
    ap.add_argument("--api-key", default=os.environ.get("CCTV_API_KEY") or os.environ.get("FINBLADE_INTEGRATION_KEY"),
                    help="integration key for the CCTV API (if the API has one set)")
    ap.add_argument("--token", default=os.environ.get("FINBLADE_MCP_TOKEN"),
                    help="bearer token MCP clients must present (unset = open)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    args = ap.parse_args(argv)

    server = build_server(Backend(args.api_url, args.api_key))
    if args.stdio:
        server.run(transport="stdio")
        return 0
    if not args.token:
        print("WARNING: FINBLADE_MCP_TOKEN not set — the MCP endpoint is open to anyone who can reach it",
              file=sys.stderr)
    import uvicorn
    print(f"FinBlade MCP: http://{args.host}:{args.port}/mcp -> API {args.api_url}", file=sys.stderr)
    uvicorn.run(make_app(server, args.token, host=args.host), host=args.host, port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
