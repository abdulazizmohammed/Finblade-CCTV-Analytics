"""Framework-agnostic ingest/query service.

All the API's business logic lives here so it is unit-testable without FastAPI,
Redis, or Postgres. app.py is a thin HTTP adapter over this class.
"""

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

    def occupancy_stats(self, t0, t1, **f):
        return self.store.zone_state_stats(t0, t1, **f)

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

    # -- dashboard reads --
    def zone_states(self) -> List[dict]:
        return self.store.latest_zone_states()

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return self.store.zone_state_range(zone_id, t0, t1)
