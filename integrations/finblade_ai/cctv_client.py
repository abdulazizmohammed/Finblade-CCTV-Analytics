"""Reference client for reading this CCTV service from the FinBlade backend.

Drop this into the FinBlade app and expose thin views over it. It is written
for the "native tiles" integration (option 1): FinBlade's BACKEND talks to the
CCTV host, FinBlade's browsers talk only to FinBlade.

WHY THE BACKEND AND NOT THE BROWSER

  * Mixed content. FinBlade is served over https and this API is http. An
    https page cannot fetch http, and the browser blocks it before it leaves
    the page — which looks like a network fault and is not one.
  * The CCTV host allows a specific egress IP, not every user's browser.
  * The API key stays server-side. In the browser it is readable by anyone who
    opens devtools.

THREE THINGS THIS MODULE DOES THAT A BARE HTTP CALL DOES NOT

  1. Allowlist. ROUTES is the whole surface. Routes that mutate state are not
     reachable through it at all, so an integration bug cannot call
     DELETE /api/v1/alerts. Use a FINBLADE_INTEGRATION_KEY as well — this is
     defence in depth, and only the key survives someone editing this file.
  2. Cache with request coalescing. Zone state upstream is a FIVE SECOND
     aggregate; polling it faster returns the same numbers. Twenty open
     dashboards should be one upstream call, not twenty per second.
  3. A shared frame cache. Snapshots are the expensive payload. One poll per
     camera serves every viewer, so upstream load is independent of how many
     people have the dashboard open.

Sync on purpose (`requests`), so it drops into a Django view or a Celery task
unchanged. For an async stack, swap the two transport methods for httpx and
make `read`/`frame` coroutines; nothing else changes.
"""

import os
import threading
import time
from typing import Callable, Dict, Optional, Tuple

DEFAULT_TIMEOUT = 5.0

# name -> (upstream path, cache seconds)
#
# Cadences are the ones the CCTV service actually refreshes at. Polling faster
# than these returns identical data:
#   camera health / people_in_view   ~1s
#   zone occupancy, density, flow     5s aggregate
#   alerts                            on rule fire
ROUTES: Dict[str, Tuple[str, float]] = {
    # One call, one instant — prefer this to stitching the next three together.
    "summary":      ("/api/v1/summary",                  1.0),

    "cameras":      ("/api/v1/cameras",                  1.0),
    "zones":        ("/api/v1/zones",                   60.0),   # static config
    "zone_state":   ("/api/v1/zones/state",              5.0),
    "alerts":       ("/api/v1/alerts",                   5.0),
    "counts":       ("/api/v1/identity/counts",          5.0),
    "identity":     ("/api/v1/identity/list",           10.0),
    "movement":     ("/api/v1/movement",                30.0),
    "history_events": ("/api/v1/history/events",        30.0),
    "history_alerts": ("/api/v1/history/alerts",        30.0),
    "report_json":  ("/api/v1/reports/occupancy.json",  30.0),
    "cctv_status":  ("/api/v1/finblade/status",         10.0),
}


class CCTVError(RuntimeError):
    """Upstream failed. Render last-known data with a staleness marker."""


class CCTVClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT, frame_ttl: float = 2.0,
                 get: Optional[Callable] = None, post: Optional[Callable] = None):
        self.base_url = (base_url or os.environ.get("CCTV_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("CCTV_API_KEY", "")
        self.timeout = timeout
        self.frame_ttl = frame_ttl
        self._get = get or self._http_get
        self._post = post or self._http_post
        self._cache: Dict[tuple, Tuple[float, object]] = {}
        self._frames: Dict[str, Tuple[float, bytes]] = {}
        self._locks: Dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---- transport ---------------------------------------------------------
    @property
    def _headers(self) -> dict:
        # Header form, never ?key=. A key in a URL leaks into access logs,
        # browser history and Referer headers. The CCTV service accepts ?key=
        # on three read-only routes precisely because a browser <img> cannot
        # send a header — that reason does not apply to server-side code.
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _http_get(self, path: str, params: dict, stream: bool = False):
        import requests
        return requests.get(self.base_url + path, params=params,
                            headers=self._headers, timeout=self.timeout)

    def _http_post(self, path: str, payload: dict):
        import requests
        return requests.post(self.base_url + path, json=payload,
                             headers=self._headers, timeout=self.timeout)

    def _lock_for(self, key) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    # ---- reads -------------------------------------------------------------
    def read(self, name: str, **params):
        """Fetch an allowlisted route, cached at its natural cadence.

        Concurrent misses for the same key collapse into one upstream call, so
        a burst of dashboard loads does not become a burst of CCTV requests.
        """
        try:
            path, ttl = ROUTES[name]
        except KeyError:
            # Not an oversight to fix by adding a passthrough: routes outside
            # ROUTES include the destructive ones.
            raise CCTVError(f"{name!r} is not an allowlisted CCTV route") from None

        key = (name, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and (now - hit[0]) < ttl:
            return hit[1]

        with self._lock_for(key):
            hit = self._cache.get(key)          # another thread may have filled it
            if hit and (time.monotonic() - hit[0]) < ttl:
                return hit[1]
            try:
                resp = self._get(path, params)
            except Exception as exc:            # noqa: BLE001
                raise CCTVError(f"GET {path}: {exc.__class__.__name__}: {exc}") from exc
            if resp.status_code == 401:
                raise CCTVError(f"GET {path}: 401 — CCTV_API_KEY missing or wrong")
            if resp.status_code == 403:
                raise CCTVError(f"GET {path}: 403 — key is out of scope for this route")
            if resp.status_code >= 400:
                raise CCTVError(f"GET {path}: HTTP {resp.status_code}")
            data = resp.json()
            self._cache[key] = (time.monotonic(), data)
            return data

    def frame(self, camera_id: str) -> bytes:
        """Latest annotated JPEG for one camera, shared across all viewers.

        Deliberately NOT the MJPEG stream. Frames are re-encoded as JPEG at
        roughly 10x the size of the equivalent H.265 — 20-40 Mbit/s PER VIEWER
        at 1080p with no fan-out. Proxying that puts every byte through the
        FinBlade backend. A snapshot every couple of seconds is a rounding
        error by comparison and degrades gracefully on a bad link.
        """
        hit = self._frames.get(camera_id)
        now = time.monotonic()
        if hit and (now - hit[0]) < self.frame_ttl:
            return hit[1]

        with self._lock_for(("frame", camera_id)):
            hit = self._frames.get(camera_id)
            if hit and (time.monotonic() - hit[0]) < self.frame_ttl:
                return hit[1]
            path = f"/api/v1/cameras/{camera_id}/snapshot"
            try:
                resp = self._get(path, {}, stream=True)
            except Exception as exc:            # noqa: BLE001
                raise CCTVError(f"GET {path}: {exc.__class__.__name__}: {exc}") from exc
            if resp.status_code >= 400:
                raise CCTVError(f"GET {path}: HTTP {resp.status_code}")
            self._frames[camera_id] = (time.monotonic(), resp.content)
            return resp.content

    # ---- the two permitted writes -----------------------------------------
    def acknowledge(self, alert_id: str, by: str) -> dict:
        """Acknowledge an alert as a named FinBlade user.

        Pass the real user — the CCTV alert record stores it, so acknowledgement
        is attributable to a person rather than to "the integration".
        """
        return self._act(f"/api/v1/alerts/{alert_id}/ack", {"acknowledged_by": by})

    def resolve(self, alert_id: str, by: str, action: str = "RESOLVED",
                note: str = None) -> dict:
        """Close an alert. action is RESOLVED (handled) or DISMISSED (false alarm)."""
        return self._act(f"/api/v1/alerts/{alert_id}/resolve",
                         {"action": action, "resolved_by": by, "note": note})

    def _act(self, path: str, payload: dict) -> dict:
        try:
            resp = self._post(path, payload)
        except Exception as exc:                # noqa: BLE001
            raise CCTVError(f"POST {path}: {exc.__class__.__name__}: {exc}") from exc

        # 409 IS TERMINAL. It means the alert is already in that state OR the id
        # is unknown — the response cannot distinguish them. The effect is
        # idempotent, so treat it as done. A client that retries every non-2xx
        # retries this one forever.
        if resp.status_code == 409:
            return {"ok": True, "already_applied": True}
        if resp.status_code >= 400:
            raise CCTVError(f"POST {path}: HTTP {resp.status_code}")
        return resp.json()

    # ---- helpers for the tiles --------------------------------------------
    def site_occupancy(self, summary: dict = None) -> Optional[int]:
        """People on the monitored floor, or None if it cannot be computed.

        NEVER sum people_in_view across cameras for this. That double-counts
        anyone visible to two cameras and includes detections anywhere in frame,
        including off the monitored floor. The CCTV team's own dashboard did it
        and reported 5-7 people while three were in the building.

        None means no zone polygons are drawn — which is NOT zero people. Render
        it as "zones not configured" and fall back to per-camera people_in_view,
        shown per camera and never added up.
        """
        summary = summary if summary is not None else self.read("summary")
        zones = summary.get("zones") or []
        if not zones:
            return None
        return sum(int(z.get("occupancy") or 0) for z in zones)

    @staticmethod
    def zone_key(zone: dict) -> tuple:
        """zone_id is unique per camera, NOT globally. ZONE-01 exists on several
        cameras as different areas, so keying on zone_id alone silently merges
        them."""
        return (zone.get("camera_id"), zone.get("zone_id"))


_default: Optional[CCTVClient] = None


def client() -> CCTVClient:
    """Process-wide client. The caches only help if everyone shares one."""
    global _default
    if _default is None:
        _default = CCTVClient()
    return _default
