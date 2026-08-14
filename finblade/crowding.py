"""Knowing when to stop trusting the tracker, and where a crowd model would go.

Detect-and-track degrades gradually in a crowd, and the dangerous part is that
it degrades SILENTLY. Boxes merge, ids churn, people behind people are never
detected at all — and the pipeline keeps producing a confident-looking integer.
An occupancy of 40 in a space holding 90 looks exactly like an occupancy of 40
in a space holding 40.

TrackingQualityMonitor turns that into an observable. It reads signals the
pipeline already produces — no second model, no extra inference — and reports a
regime rather than a number:

  RELIABLE   detect-and-track is doing its job; counts mean what they say
  STRAINED   quality is measurably falling; counts are probably an undercount
  SATURATED  individual tracking is no longer credible here

The three signals, and why each one:

  mean detection confidence   falls as boxes overlap and the detector hedges
  track churn                 ids per person per minute; occlusion breaks tracks
  detection saturation        hitting max_det means people are being dropped
                              before any of this code sees them, which no
                              downstream measure can recover

Deliberately NOT a people count. This says "stop believing the count", which is
the honest thing a detector-based pipeline can say about a dense crowd. Getting
a number back needs a different kind of model — see CrowdEstimator.

Pure stdlib — unit-testable without cv2, torch or a camera.
"""

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

RELIABLE = "RELIABLE"
STRAINED = "STRAINED"
SATURATED = "SATURATED"


class TrackingQualityMonitor:
    """Rolling assessment of whether per-person tracking can still be believed.

    Thresholds are defaults, not measurements. They were chosen to be
    conservative — a system that cries saturation early is merely cautious,
    while one that never does is the silent failure this class exists to end —
    but they should be retuned against real footage of a genuinely dense scene.
    """

    def __init__(self, window_s: float = 30.0,
                 conf_strained: float = 0.55, conf_saturated: float = 0.45,
                 churn_strained: float = 3.0, churn_saturated: float = 6.0,
                 saturation_ratio: float = 0.9):
        self.window_s = window_s
        self.conf_strained = conf_strained
        self.conf_saturated = conf_saturated
        self.churn_strained = churn_strained
        self.churn_saturated = churn_saturated
        self.saturation_ratio = saturation_ratio
        # (ts, n_tracks, mean_conf, saturated)
        self._frames: Deque[Tuple[float, int, float, bool]] = deque()
        self._ids: Deque[Tuple[float, int]] = deque()
        self._state = RELIABLE

    def observe(self, now: float, track_ids, confidences,
                max_det: Optional[int] = None) -> None:
        ids = list(track_ids or [])
        confs = [float(c) for c in (confidences or [])]
        mean_conf = (sum(confs) / len(confs)) if confs else 1.0
        saturated = bool(max_det) and len(ids) >= int(max_det) * self.saturation_ratio
        self._frames.append((now, len(ids), mean_conf, saturated))
        for tid in ids:
            self._ids.append((now, int(tid)))
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()
        while self._ids and self._ids[0][0] < cutoff:
            self._ids.popleft()

    def mean_confidence(self) -> float:
        seen = [c for _, n, c, _ in self._frames if n]
        return sum(seen) / len(seen) if seen else 1.0

    def mean_tracks(self) -> float:
        if not self._frames:
            return 0.0
        return sum(n for _, n, _, _ in self._frames) / len(self._frames)

    def churn_per_person_per_min(self) -> float:
        """EXCESS track ids per person per minute — ids beyond the people present.

        Measured as excess rather than as a raw count on purpose. A scene with
        six people visible throughout legitimately shows six ids, and dividing
        that by the window would report a clean scene as churning hard: the
        shorter the window, the worse it would look. What actually signals
        fragmentation is ids minted BEYOND the number of people concurrently
        present, so a stable scene sits at zero however long it is watched, and
        a tracker that reissues ids every frame climbs without bound.
        """
        if not self._frames:
            return 0.0
        span = self._frames[-1][0] - self._frames[0][0]
        if span <= 0:
            return 0.0
        people = max(self.mean_tracks(), 1.0)
        distinct = len({tid for _, tid in self._ids})
        excess = max(0.0, distinct - people)
        return (excess / people) * (60.0 / span)

    def saturation_fraction(self) -> float:
        if not self._frames:
            return 0.0
        return sum(1 for _, _, _, s in self._frames if s) / len(self._frames)

    def assess(self) -> str:
        # Not enough evidence yet: say nothing rather than raise an alarm on
        # two frames of startup.
        if len(self._frames) < 5:
            return RELIABLE
        conf = self.mean_confidence()
        churn = self.churn_per_person_per_min()
        sat = self.saturation_fraction()
        if conf <= self.conf_saturated or churn >= self.churn_saturated or sat >= 0.5:
            self._state = SATURATED
        elif conf <= self.conf_strained or churn >= self.churn_strained or sat > 0.0:
            self._state = STRAINED
        else:
            self._state = RELIABLE
        return self._state

    def snapshot(self) -> dict:
        state = self.assess()
        return {
            "tracking_quality": state,
            "mean_confidence": round(self.mean_confidence(), 4),
            "track_churn_per_min": round(self.churn_per_person_per_min(), 2),
            "detector_saturation": round(self.saturation_fraction(), 3),
            "mean_tracks": round(self.mean_tracks(), 2),
            "window_s": self.window_s,
            # The honest headline. A consumer that shows occupancy should show
            # this next to it rather than quietly presenting an undercount as a
            # measurement.
            "counts_reliable": state == RELIABLE,
        }


class CrowdEstimator:
    """Integration point for a dedicated crowd-counting model.

    REQ-28 asks that the ARCHITECTURE allow a density-estimation model to take
    over where detect-and-track stops working. This is that seam, and it is
    deliberately empty: no such model ships here, and none can be fetched — the
    deployment is air-gapped and dependencies are pinned. Shipping a stub that
    returned plausible numbers would be far worse than shipping nothing, because
    the whole point of the tier is to be trusted where the detector is not.

    To plug one in, implement estimate(frame, zone) -> float and register it.
    Until then estimate() returns None, meaning "no independent estimate
    available", and callers fall back to the tracked count while
    TrackingQualityMonitor says whether that count can be believed.
    """

    def __init__(self, backend=None):
        self.backend = backend

    @property
    def available(self) -> bool:
        return self.backend is not None

    def estimate(self, frame, zone=None) -> Optional[float]:
        if self.backend is None:
            return None
        return float(self.backend.estimate(frame, zone))

    def describe(self) -> dict:
        return {"crowd_model": getattr(self.backend, "name", None),
                "available": self.available}


def select_mode(quality: str, estimator: Optional[CrowdEstimator] = None) -> str:
    """Which counting method to believe, given tracking quality.

    Encodes the tiering the requirement describes, and refuses to pretend: with
    no crowd model registered, a saturated scene reports that its count is
    degraded rather than silently switching to a method that does not exist.
    """
    if quality == RELIABLE:
        return "track"
    if estimator is not None and estimator.available:
        return "crowd_model"
    return "track_degraded"
