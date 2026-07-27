"""Camera topology — which cameras can hand a person to which, and how fast.

Appearance similarity alone is not enough to link identities across cameras.
OSNet will happily score two different people in similar clothing at 0.7+, and
in a uniformed environment (staff, hi-vis, school groups) that is the common
case rather than the edge case. What rescues it is physics: a person seen at
the north gate cannot appear at the south gate two seconds later, however alike
the two crops look. Gating candidates by transit feasibility BEFORE scoring
removes most false matches that appearance alone would accept.

Two pair kinds, which behave differently and must not be conflated:

  * OVERLAPPING — both cameras see some of the same floor, so the same person
    is legitimately visible on both AT THE SAME INSTANT. dt near zero is the
    expected case, and simultaneous presence is evidence FOR a match. This is
    also what lets site occupancy avoid double-counting.

  * NON-OVERLAPPING — separate areas with a walk between. The person must be
    absent for at least the minimum walk time. dt near zero is evidence
    AGAINST a match: it would mean being in two places at once.

Because the two are opposites, an unconfigured pair cannot be guessed safely.
Unknown pairs fall back to a permissive walk-time window and are reported as
``"unknown_pair"`` so the caller can log that the topology needs completing.

Pure stdlib — unit-testable without cv2, torch or a camera.
"""

from typing import Dict, Iterable, Optional, Tuple

Pair = Tuple[str, str]


def _key(a: str, b: str) -> Pair:
    """Undirected pair key: transit A->B and B->A share configuration."""
    return (a, b) if a <= b else (b, a)


class CameraTopology:
    """Which camera pairs are reachable, and in what time window.

    ``transits`` maps an undirected camera pair to (min_seconds, max_seconds):
    the fastest a person could physically make the walk, and the longest gap
    still worth treating as the same journey rather than a fresh visit.
    """

    def __init__(
        self,
        overlapping: Iterable[Pair] = (),
        transits: Optional[Dict[Pair, Tuple[float, float]]] = None,
        default_transit: Tuple[float, float] = (2.0, 120.0),
        overlap_tolerance_s: float = 5.0,
        allow_unknown_pairs: bool = True,
    ):
        self.overlapping = {_key(a, b) for a, b in overlapping}
        self.transits: Dict[Pair, Tuple[float, float]] = {}
        for (a, b), window in (transits or {}).items():
            lo, hi = float(window[0]), float(window[1])
            if lo < 0 or hi < lo:
                raise ValueError(f"invalid transit window for {a}->{b}: {window}")
            self.transits[_key(a, b)] = (lo, hi)
        self.default_transit = (float(default_transit[0]), float(default_transit[1]))
        # Clock skew between independent camera processes is real; an
        # overlapping pair can legitimately report dt slightly negative.
        self.overlap_tolerance_s = float(overlap_tolerance_s)
        self.allow_unknown_pairs = bool(allow_unknown_pairs)

    # ---- classification ---------------------------------------------------
    def is_overlapping(self, a: str, b: str) -> bool:
        return _key(a, b) in self.overlapping

    def is_known_pair(self, a: str, b: str) -> bool:
        k = _key(a, b)
        return k in self.overlapping or k in self.transits

    def transit_window(self, a: str, b: str) -> Tuple[float, float]:
        return self.transits.get(_key(a, b), self.default_transit)

    # ---- the gate ---------------------------------------------------------
    def feasible(self, from_cam: str, to_cam: str, dt: float) -> Tuple[bool, str]:
        """Could one person be seen on ``from_cam`` then ``to_cam`` after ``dt``?

        ``dt`` is (time seen on to_cam) - (time last seen on from_cam), so it is
        normally positive. Returns (feasible, reason).
        """
        if from_cam == to_cam:
            # Same camera: re-appearance after an occlusion or a tracker ID
            # break. Always physically possible; the caller applies its own
            # rule about not binding two live tracks on one camera at once.
            return True, "same_camera"

        if self.is_overlapping(from_cam, to_cam):
            # Simultaneous is the norm here. Reject only a gap so large the
            # person plainly left and came back — that is a new visit.
            _, hi = self.transit_window(from_cam, to_cam)
            if dt < -self.overlap_tolerance_s:
                return False, "overlap_clock_skew"
            if dt > hi:
                return False, "overlap_stale"
            return True, "overlapping"

        known = _key(from_cam, to_cam) in self.transits
        if not known and not self.allow_unknown_pairs:
            return False, "unknown_pair_rejected"

        lo, hi = self.transit_window(from_cam, to_cam)
        if dt < 0:
            return False, "negative_dt"
        if dt < lo:
            # The decisive check: too fast to be the same human walking.
            return False, "too_fast"
        if dt > hi:
            return False, "too_slow"
        return True, "unknown_pair" if not known else "transit_ok"

    # ---- config loading ---------------------------------------------------
    @classmethod
    def from_dict(cls, cfg: dict) -> "CameraTopology":
        """Build from parsed YAML/JSON. See config/topology.yaml for the shape."""
        cfg = cfg or {}
        overlapping = []
        for entry in cfg.get("overlapping_pairs", []) or []:
            if isinstance(entry, dict):
                overlapping.append((entry["a"], entry["b"]))
            else:
                overlapping.append((entry[0], entry[1]))

        transits: Dict[Pair, Tuple[float, float]] = {}
        for entry in cfg.get("transits", []) or []:
            a, b = entry["a"], entry["b"]
            transits[_key(a, b)] = (float(entry["min_seconds"]),
                                    float(entry["max_seconds"]))

        d = cfg.get("default_transit", {}) or {}
        default = (float(d.get("min_seconds", 2.0)),
                   float(d.get("max_seconds", 120.0)))

        return cls(
            overlapping=overlapping,
            transits=transits,
            default_transit=default,
            overlap_tolerance_s=float(cfg.get("overlap_tolerance_seconds", 5.0)),
            allow_unknown_pairs=bool(cfg.get("allow_unknown_pairs", True)),
        )

    @classmethod
    def load(cls, path: str) -> "CameraTopology":
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})

    @classmethod
    def empty(cls) -> "CameraTopology":
        """Permissive topology for a single-camera or unconfigured deployment."""
        return cls()
