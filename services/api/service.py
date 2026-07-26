"""Framework-agnostic ingest/query service.

All the API's business logic lives here so it is unit-testable without FastAPI,
Redis, or Postgres. app.py is a thin HTTP adapter over this class.
"""

import time
from typing import List, Optional, Tuple

from .schema import validate_ingest, validate_zone_state, validate_zones
from .store import Store


class IngestService:
    def __init__(self, store: Store, bus=None):
        self.store = store
        self.bus = bus  # optional event bus with .publish(evt); None = skip

    # -- POST /api/v1/events/ingest --
    def ingest_event(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_ingest(payload)
        if not ok:
            return 422, {"accepted": False, "errors": errors}
        self.store.save_event(payload)
        # Any event from a camera counts as a heartbeat for offline detection.
        self.store.mark_camera_seen(payload.get("camera_id"), payload.get("timestamp"),
                                    payload.get("site_id"))
        if self.bus is not None:
            self.bus.publish(payload)
        return 202, {"accepted": True, "event_id": payload.get("event_id")}

    # -- POST /api/v1/zones/state --
    def record_zone_state(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_zone_state(payload)
        if not ok:
            return 422, {"accepted": False, "errors": errors}
        self.store.save_zone_state(payload)
        # 5s zone-state posts are the camera's primary heartbeat.
        self.store.mark_camera_seen(payload.get("camera_id"), payload.get("ts"))
        return 202, {"accepted": True, "zone_id": payload["zone_id"]}

    # -- history / logs --
    def events_history(self, t0, t1, **f):
        return self.store.list_events(t0, t1, **f)

    def alerts_history(self, t0, t1, **f):
        return self.store.list_alerts_history(t0, t1, **f)

    def cameras(self):
        return self.store.list_cameras()

    def record_camera_health(self, payload: dict) -> Tuple[int, dict]:
        """Ingest a health snapshot from an inference runner (Req 4/5).

        Returns the desired control state so the runner can drive simulate/restore
        centrally on its next heartbeat (no inbound connection to the runner needed).
        """
        cid = payload.get("camera_id")
        if not cid:
            return 422, {"ok": False, "errors": ["camera_id required"]}
        health = payload.get("health") or payload
        ts = payload.get("ts") or payload.get("timestamp") or time.time()
        self.store.record_camera_health(cid, health, ts, site_id=payload.get("site_id"))
        cam = next((c for c in self.store.list_cameras()
                    if c.get("camera_id") == cid), {})
        return 200, {"ok": True, "control": {"simulate": bool(cam.get("sim_failure"))}}

    def set_camera_sim(self, camera_id: str, on: bool) -> Tuple[int, dict]:
        self.store.set_camera_sim(camera_id, on)
        return 200, {"ok": True, "camera_id": camera_id, "simulate": bool(on)}

    def upsert_camera(self, payload: dict) -> Tuple[int, dict]:
        cid = (payload.get("camera_id") or "").strip()
        if not cid:
            return 422, {"ok": False, "errors": ["camera_id required"]}
        self.store.upsert_camera(cid, site_id=payload.get("site_id"),
                                 name=payload.get("name"),
                                 stream_url=payload.get("stream_url"),
                                 source=payload.get("source"),
                                 enabled=(None if payload.get("enabled") is None
                                          else (1 if payload.get("enabled") else 0)))
        return 200, {"ok": True, "camera_id": cid}

    def delete_camera(self, camera_id: str) -> Tuple[int, dict]:
        ok = self.store.delete_camera(camera_id)
        return (200 if ok else 404), {"ok": ok, "camera_id": camera_id}

    def movement(self, t0, t1, camera_id=None):
        """Aggregate zone->zone transitions in a window into from/to counts."""
        from collections import Counter
        evs = self.store.list_events(t0, t1, camera_id=camera_id,
                                     event_type="ZONE_TRANSITION", limit=5000)
        c = Counter((e.get("zone_from"), e.get("zone_to")) for e in evs
                    if e.get("zone_from") and e.get("zone_to"))
        return [{"zone_from": f, "zone_to": t, "count": n}
                for (f, t), n in c.most_common()]

    def occupancy_stats(self, t0, t1, **f):
        return self.store.zone_state_stats(t0, t1, **f)

    def occupancy_report(self, t0, t1, camera_id=None, zone_id=None, generated_at=None):
        """Windowed occupancy report: per-zone stats enriched with alert counts,
        plus totals. Shared by the JSON/CSV endpoints and the R-08 scheduler."""
        from collections import Counter
        zones = self.store.zone_state_stats(t0, t1, camera_id=camera_id, zone_id=zone_id)
        alerts = self.store.list_alerts_history(t0, t1, camera_id=camera_id, limit=5000)
        by_zone = Counter(a.get("zone_id") for a in alerts if a.get("zone_id"))
        for z in zones:
            z["alert_count"] = by_zone.get(z.get("zone_id"), 0)
        return {
            "from": t0, "to": t1,
            "generated_at": time.time() if generated_at is None else generated_at,
            "zones": zones,
            "totals": {
                "zones": len(zones),
                "peak_total_occupancy": sum(int(z.get("peak_occupancy") or 0) for z in zones),
                "peak_density": max((float(z.get("peak_density") or 0) for z in zones),
                                    default=0.0),
                "total_alerts": len(alerts),
            },
        }

    def generate_report(self, t0, t1, kind="ondemand", camera_id=None) -> dict:
        rep = self.occupancy_report(t0, t1, camera_id=camera_id)
        rep["kind"] = kind
        rep["report_id"] = self.store.save_report(rep)
        return rep

    def list_reports(self, limit=100):
        return self.store.list_reports(limit)

    def get_report(self, report_id):
        return self.store.get_report(report_id)

    # -- zones (editor save/load) --
    def save_zones(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_zones(payload)
        if not ok:
            return 422, {"saved": False, "errors": errors}
        self.store.save_zones(payload["camera_id"], payload["zones"])
        return 200, {"saved": True, "camera_id": payload["camera_id"],
                     "count": len(payload["zones"])}

    def list_zones(self, camera_id=None):
        return self.store.list_zones(camera_id)

    # -- alerts --
    def raise_alert(self, alert: dict) -> str:
        return self.store.save_alert(alert)

    def list_alerts(self, unacked_only: bool = False) -> List[dict]:
        return self.store.list_alerts(unacked_only=unacked_only)

    def acknowledge(self, alert_id: str, who: str, ts: float) -> Tuple[int, dict]:
        if not who:
            return 400, {"acknowledged": False, "error": "acknowledged_by required"}
        ok = self.store.acknowledge_alert(alert_id, who, ts)
        if not ok:
            return 409, {"acknowledged": False,
                         "error": "unknown or already-acknowledged alert"}
        return 200, {"acknowledged": True, "alert_id": alert_id,
                     "acknowledged_by": who, "acknowledged_at": ts}

    def resolve(self, alert_id: str, action: str, who: str, ts: float,
                note: str = None) -> Tuple[int, dict]:
        """Close an alert: action 'RESOLVED' (handled) or 'DISMISSED' (false alarm),
        with an optional operator note."""
        action = (action or "").upper()
        if action not in ("RESOLVED", "DISMISSED"):
            return 400, {"ok": False, "error": "action must be RESOLVED or DISMISSED"}
        if not who:
            return 400, {"ok": False, "error": "resolved_by required"}
        ok = self.store.update_alert(alert_id, action, who, ts, note)
        if not ok:
            return 409, {"ok": False, "error": "unknown or already-closed alert"}
        return 200, {"ok": True, "alert_id": alert_id, "status": action,
                     "resolved_by": who, "resolved_at": ts, "note": note}

    # -- dashboard reads --
    def zone_states(self) -> List[dict]:
        return self.store.latest_zone_states()

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return self.store.zone_state_range(zone_id, t0, t1)
