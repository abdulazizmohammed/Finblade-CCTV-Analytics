"""Track model + registry — the per-track record the vision loop maintains.

Consolidates the rich per-track fields (Req 4) in one place: id, camera, bbox,
confidence, first/last seen, age, current/previous zone, confirmed entry time,
dwell, anonymous ref, state. The registry is *additive* — the confirmed-zone and
dwell values are still computed by the tested BoundaryDebouncer / DwellTracker;
this just records them so later phases can render live overlays, build movement
records, and persist completed-track summaries.

Pure Python (stdlib only) so it is unit-testable without cv2/YOLO.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

BBox = Tuple[float, float, float, float]  # x1, y1, x2, y2

STATE_TENTATIVE = "tentative"   # seen, but no confirmed zone yet
STATE_TRACKED = "tracked"       # has a confirmed zone
STATE_COMPLETED = "completed"   # track has left the scene


@dataclass
class Track:
    track_id: int
    camera_id: str
    first_seen: float
    last_seen: float
    bbox: BBox
    confidence: float
    person_ref: Optional[str] = None
    current_zone_id: Optional[str] = None
    previous_zone_id: Optional[str] = None
    confirmed_zone_entry_time: Optional[float] = None
    dwell_time: float = 0.0
    state: str = STATE_TENTATIVE
    zones_visited: List[str] = field(default_factory=list)

    @property
    def track_age(self) -> float:
        return self.last_seen - self.first_seen

    def summary(self) -> dict:
        """Flat dict for persistence / API (a completed_person_tracks row)."""
        return {
            "track_id": self.track_id,
            "camera_id": self.camera_id,
            "person_ref": self.person_ref,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "track_age": self.track_age,
            "current_zone_id": self.current_zone_id,
            "previous_zone_id": self.previous_zone_id,
            "confirmed_zone_entry_time": self.confirmed_zone_entry_time,
            "dwell_time": self.dwell_time,
            "zones_visited": list(self.zones_visited),
            "state": self.state,
        }


class TrackRegistry:
    def __init__(self):
        self._tracks: Dict[int, Track] = {}

    def observe(self, track_id: int, camera_id: str, bbox: BBox, confidence: float,
                confirmed_zone: Optional[str], zone_changed: bool, dwell: float,
                person_ref: Optional[str], now: float) -> Track:
        t = self._tracks.get(track_id)
        if t is None:
            t = Track(track_id=track_id, camera_id=camera_id, first_seen=now,
                      last_seen=now, bbox=bbox, confidence=confidence,
                      person_ref=person_ref, current_zone_id=confirmed_zone)
            if confirmed_zone:
                t.state = STATE_TRACKED
                t.confirmed_zone_entry_time = now
                t.zones_visited.append(confirmed_zone)
            self._tracks[track_id] = t
            return t

        t.last_seen = now
        t.bbox = bbox
        t.confidence = confidence
        if person_ref and not t.person_ref:
            t.person_ref = person_ref
        if zone_changed:
            t.previous_zone_id = t.current_zone_id
            t.current_zone_id = confirmed_zone
            t.confirmed_zone_entry_time = now if confirmed_zone else None
            if confirmed_zone:
                t.state = STATE_TRACKED
                if not t.zones_visited or t.zones_visited[-1] != confirmed_zone:
                    t.zones_visited.append(confirmed_zone)
        t.dwell_time = dwell
        return t

    def get(self, track_id: int) -> Optional[Track]:
        return self._tracks.get(track_id)

    def active(self) -> List[Track]:
        return list(self._tracks.values())

    def complete(self, track_id: int) -> Optional[Track]:
        """Remove a departed track and return its final record (or None)."""
        t = self._tracks.pop(track_id, None)
        if t is not None:
            t.state = STATE_COMPLETED
        return t

    def __len__(self) -> int:
        return len(self._tracks)
