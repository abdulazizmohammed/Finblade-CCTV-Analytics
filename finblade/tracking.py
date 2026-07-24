"""Track lifecycle helper: evict per-track state for tracks that have gone.

The vision loop keeps per-track state in several dicts/sets (debounce, dwell,
previous-zone, rule-engine loiter/intrusion). ByteTrack stops emitting a track
id once a person leaves, but nothing tells those dicts to forget it — so they
grow without bound over a long run. TrackReaper records when each track was last
seen and reports the ids that have been absent longer than a TTL, so the caller
can drop them from every per-track structure in one place.

Pure Python (stdlib only) so it is unit-testable without cv2/YOLO.
"""

from typing import Dict, List


class TrackReaper:
    def __init__(self, ttl_seconds: float = 5.0):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.ttl = ttl_seconds
        self._last_seen: Dict[int, float] = {}

    def see(self, track_id: int, now: float) -> None:
        """Record that ``track_id`` is present at time ``now`` (monotonic sec)."""
        self._last_seen[int(track_id)] = now

    def reap(self, now: float) -> List[int]:
        """Return + forget track ids not seen within the TTL window.

        The returned ids should be dropped from every per-track structure by the
        caller. Ids still within the TTL are retained.
        """
        stale = [tid for tid, ts in self._last_seen.items() if (now - ts) > self.ttl]
        for tid in stale:
            del self._last_seen[tid]
        return stale

    def active_count(self) -> int:
        return len(self._last_seen)
