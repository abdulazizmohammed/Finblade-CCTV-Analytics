"""Boundary debounce: a track must be seen in a new zone for N consecutive
frames before the change is committed. Prevents per-frame flicker when a person
straddles a boundary line.

Semantics (UC-12): N-1 consecutive frames do NOT commit; the Nth DOES.
The very first observation of a brand-new track also requires N frames before
its zone is confirmed (initial confirmed zone is None).
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class _TrackState:
    confirmed: Optional[str] = None
    candidate: Optional[str] = None
    count: int = 0


class BoundaryDebouncer:
    def __init__(self, n: int = 3):
        if n < 1:
            raise ValueError("n must be >= 1")
        self.n = n
        self._state: Dict[int, _TrackState] = {}

    def update(self, track_id: int, observed_zone: Optional[str]) -> Tuple[Optional[str], bool]:
        """Feed one frame's observed zone for a track.

        Returns (confirmed_zone, changed) where ``changed`` is True only on the
        frame the confirmed zone actually flips.
        """
        st = self._state.get(track_id)
        if st is None:
            st = _TrackState()
            self._state[track_id] = st

        if observed_zone == st.confirmed:
            # Back to (or still at) the confirmed zone: reset any pending candidate.
            st.candidate = observed_zone
            st.count = 0
            return st.confirmed, False

        # Observed differs from confirmed -> accumulate evidence for the change.
        if observed_zone == st.candidate:
            st.count += 1
        else:
            st.candidate = observed_zone
            st.count = 1

        if st.count >= self.n:
            st.confirmed = observed_zone
            st.count = 0
            return st.confirmed, True

        return st.confirmed, False

    def confirmed_zone(self, track_id: int) -> Optional[str]:
        st = self._state.get(track_id)
        return st.confirmed if st else None

    def drop(self, track_id: int) -> None:
        """Forget a track (e.g. when it leaves the frame)."""
        self._state.pop(track_id, None)
