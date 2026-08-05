"""Forward live analytics to the FinBlade platform.

Pushes the five streams described in docs/FINBLADE_API_REQUIREMENTS.md — zone
state, events, alerts, camera health and cross-camera people counts — to a
FinBlade API over batched REST.

DESIGN: it TAILS THE STORE rather than intercepting writes.

Every candidate design for this ends up needing an answer to "what happens to a
message while FinBlade is down". An in-memory queue answers it badly: it grows
without bound, and everything in it is lost on restart — exactly when you most
want the backlog. Tailing the store instead means the database IS the queue. A
cursor advances only after a successful post, so a FinBlade outage costs
nothing: when it returns, the next tick replays from wherever the cursor stopped.

That also makes retry trivial. There is no retry logic here at all — a failed
post simply leaves the cursor where it was, and the next tick tries again.

The cursor deliberately re-sends a small overlap rather than using a strict
`ts >` boundary. Two events can share a timestamp, and a strict boundary would
silently drop the second one forever. The receiver is required to be idempotent
on event_id (spec §8), so a duplicate is free while a gap is not.

Best-effort throughout: a FinBlade outage must never affect video processing,
alerting, or the local dashboard.
"""

import logging
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("finblade.forwarder")

# Re-send this many seconds of already-sent records on each tick. Covers
# same-timestamp ties and clock jitter; duplicates are the receiver's problem
# to deduplicate and it is specified to do so.
CURSOR_OVERLAP_S = 2.0

# Stamped on every outbound envelope. Bump on a breaking change to the payload
# shape so a receiver can branch on it instead of sniffing for fields.
SCHEMA_VERSION = "1.0"


class FinBladeForwarder:
    """Batched push of live analytics to FinBlade. Disabled unless a URL is set."""

    def __init__(
        self,
        store,
        identity_service=None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        post: Optional[Callable] = None,
        batch_limit: int = 500,
        apply_ack: Optional[Callable] = None,
        snapshot_interval: float = 0.0,
        fetch_snapshot: Optional[Callable] = None,
        site_id: Optional[str] = None,
    ):
        self.store = store
        self.site_id = site_id
        self.identity = identity_service
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.batch_limit = batch_limit
        self._post = post or self._http_post
        self._apply_ack = apply_ack

        # Periodic JPEG thumbnails — NOT continuous video.
        #
        # Streaming live video to FinBlade is not viable over a WAN: frames are
        # re-encoded as JPEG at roughly 10x the size of the equivalent H.265,
        # which is 20-40 Mbit/s PER VIEWER at 1080p, with no fan-out. Pushing
        # one frame per camera every 30s is ~2 KB/s per camera instead — four
        # orders of magnitude less — and is enough for a dashboard tile,
        # incident context, or a "what did it look like" thumbnail.
        #
        # For genuine live video FinBlade should PULL the MJPEG endpoint, and
        # only over a link that can carry it.
        self.snapshot_interval = float(snapshot_interval or 0.0)
        self._fetch_snapshot = fetch_snapshot
        self._last_snapshot = 0.0

        # Start from "now" on a FIRST run: backfilling a site's whole database
        # into FinBlade on first connect is rarely what anyone wants.
        #
        # But resume from the stored position if there is one. Without that the
        # cursor lived only in this process, so "the store is the queue" held
        # only while the process did: an API restart during a FinBlade outage
        # skipped the backlog instead of replaying it — the precise scenario the
        # tailing design exists to survive, failing silently.
        now = time.time()
        self.cursors = {"events": now, "alerts": now}
        try:
            stored = store.load_cursors() or {}
        except Exception:                          # noqa: BLE001
            log.exception("could not load forwarder cursors; starting from now")
            stored = {}
        for kind in ("events", "alerts"):
            if kind in stored:
                self.cursors[kind] = float(stored[kind])
        self.stats = {"events": 0, "alerts": 0, "zone_state": 0, "health": 0,
                      "counts": 0, "snapshots": 0, "snapshot_bytes": 0,
                      "acks_applied": 0, "failures": 0, "ticks": 0}
        self.last_error: Optional[str] = None
        self.last_success: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    # ---- transport --------------------------------------------------------
    def _http_post(self, path: str, payload: dict) -> Optional[dict]:
        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = requests.post(self.base_url + path, json=payload,
                          headers=headers, timeout=5.0)
        # 4xx is permanent: the payload is wrong and resending cannot fix it, so
        # let the cursor advance past it rather than wedging the stream forever
        # on one bad record. 5xx raises, so the cursor holds and we retry.
        if 500 <= r.status_code:
            raise RuntimeError(f"{path} -> HTTP {r.status_code}")
        if r.status_code >= 400:
            log.warning("finblade rejected %s: HTTP %s %s", path, r.status_code,
                        r.text[:200])
            return None
        try:
            return r.json()
        except ValueError:
            return {}

    def _send(self, path: str, payload: dict, count_key: str, n: int) -> bool:
        # Stamped on every envelope so the receiver can branch on the contract
        # version rather than sniffing for fields, and can attribute a batch to
        # a site without joining camera data. Both are additive: a receiver that
        # ignores them is unaffected.
        payload = dict(payload, schema_version=SCHEMA_VERSION, source="cctv")
        if self.site_id and "site_id" not in payload:
            payload["site_id"] = self.site_id
        try:
            resp = self._post(path, payload)
        except Exception as exc:                       # noqa: BLE001
            self.stats["failures"] += 1
            self.last_error = f"{path}: {exc.__class__.__name__}: {exc}"
            return False
        self.stats[count_key] += n
        self.last_success = time.time()
        self.last_error = None
        self._handle_acks(resp)
        return True

    # ---- operator acknowledgements coming back ----------------------------
    def _handle_acks(self, resp) -> None:
        """Apply operator actions taken in FinBlade (spec §12).

        They ride in the RESPONSE BODY because a CCTV host is usually not
        reachable inbound — site LAN, behind NAT or a VPN, dialling out.
        """
        if not isinstance(resp, dict) or not self._apply_ack:
            return
        for ack in resp.get("pending_acks") or []:
            if not isinstance(ack, dict) or not ack.get("alert_id"):
                continue
            try:
                self._apply_ack(ack)
                self.stats["acks_applied"] += 1
            except Exception:                          # noqa: BLE001
                log.exception("failed applying FinBlade ack %s", ack.get("alert_id"))

    def _persist_cursors(self) -> None:
        """Best effort. A cursor that fails to save costs a replayed batch on
        the next restart — the receiver is required to be idempotent on
        event_id — whereas raising here would stop forwarding altogether."""
        try:
            self.store.save_cursors(dict(self.cursors))
        except Exception:                          # noqa: BLE001
            log.exception("could not persist forwarder cursors")

    # ---- the five streams -------------------------------------------------
    def _tail(self, kind: str, fetch) -> None:
        since = self.cursors[kind] - CURSOR_OVERLAP_S
        rows = fetch(since, time.time() + 1.0, limit=self.batch_limit)
        if not rows:
            return
        newest = max((float(r.get("ts") or r.get("timestamp") or 0) for r in rows),
                     default=self.cursors[kind])
        if self._send(f"/api/v1/cctv/{kind}", {"batch": rows}, kind, len(rows)):
            # Only now. A failed post leaves the cursor put, so the next tick
            # replays these same rows — the store is the queue.
            self.cursors[kind] = max(self.cursors[kind], newest)
            self._persist_cursors()

    def tick(self) -> None:
        """One forwarding pass. Safe to call on a timer; never raises."""
        if not self.enabled:
            return
        self.stats["ticks"] += 1
        try:
            self._tail("events", lambda a, b, limit: self.store.list_events(
                a, b, limit=limit))
            self._tail("alerts", lambda a, b, limit: self.store.list_alerts_history(
                a, b, limit=limit))

            zones = self.store.latest_zone_states()
            if zones:
                self._send("/api/v1/cctv/zone-state", {"batch": zones},
                           "zone_state", len(zones))

            cams = self.store.list_cameras()
            if cams:
                self._send("/api/v1/cctv/camera-health", {"batch": cams},
                           "health", len(cams))

            if self.identity is not None:
                counts = self.identity.counts()
                self._send("/api/v1/cctv/people-counts", counts, "counts", 1)

            self._push_snapshots(cams)
        except Exception as exc:                       # noqa: BLE001
            # A forwarding fault must never reach video processing or alerting.
            self.stats["failures"] += 1
            self.last_error = f"tick: {exc.__class__.__name__}: {exc}"
            log.exception("forwarder tick failed")

    @staticmethod
    def _camera_is_live(cam: dict, now: float, max_age: float = 60.0) -> bool:
        """Is this camera currently producing frames?

        Deliberately not just `effective_state == ONLINE`: that field is
        computed by the SQLite store and absent from others, so keying on it
        alone made snapshots silently never send on any other backend. Falls
        back to the raw state, and requires a recent heartbeat either way —
        a camera whose row says ONLINE but stopped reporting ten minutes ago
        is not live.
        """
        state = (cam.get("effective_state") or cam.get("state") or "").upper()
        if state and state != "ONLINE":
            return False
        last = cam.get("last_seen") or cam.get("health_ts") or 0
        return bool(state) and (now - float(last)) <= max_age

    def _push_snapshots(self, cameras) -> None:
        """Send one JPEG per online camera, at most every snapshot_interval.

        Rate-limited independently of the tick so the interval can be far longer
        than 5 seconds — images are three orders of magnitude larger than the
        telemetry and should not ride every tick.

        Only ONLINE cameras: a snapshot from an offline camera is a stale frame
        from before it dropped, which is worse than none because it looks live.
        """
        if not self.snapshot_interval or not self._fetch_snapshot:
            return
        now = time.time()
        if (now - self._last_snapshot) < self.snapshot_interval:
            return
        self._last_snapshot = now

        import base64
        for cam in cameras or []:
            cid = cam.get("camera_id")
            if not cid or not self._camera_is_live(cam, now):
                continue
            try:
                jpeg = self._fetch_snapshot(cid)
            except Exception:                          # noqa: BLE001
                continue
            if not jpeg:
                continue
            payload = {
                "camera_id": cid,
                "site_id": cam.get("site_id"),
                "ts": now,
                "format": "jpeg",
                "resolution": cam.get("resolution"),
                "people_in_view": cam.get("people_in_view"),
                "bytes": len(jpeg),
                "image_base64": base64.b64encode(jpeg).decode("ascii"),
            }
            if self._send("/api/v1/cctv/snapshots", payload, "snapshots", 1):
                self.stats["snapshot_bytes"] += len(jpeg)

    # ---- observability ----------------------------------------------------
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "target": self.base_url or None,
            "authenticated": bool(self.api_key),
            "last_success": self.last_success,
            "seconds_since_success": (time.time() - self.last_success)
                                     if self.last_success else None,
            "last_error": self.last_error,
            "cursors": dict(self.cursors),
            "snapshot_interval": self.snapshot_interval or None,
            "stats": dict(self.stats),
        }
