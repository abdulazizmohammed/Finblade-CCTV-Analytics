"""Direction and group-crossing rules over the confirmed transition stream.

Both rules here read the SAME signal the zone engine already produces — a
confirmed, debounced move from one zone to another — rather than looking at
video again. That matters for trust: if the transition was good enough to change
occupancy, it is good enough to police, and if it was jitter the debouncer has
already suppressed it and neither rule ever sees it.

WRONG-WAY (REQ-23) is policed only where a direction has been declared. An
unconfigured pair is reported as unpoliced, never as a violation: a system that
invents direction rules from traffic patterns would alert on the first person to
walk an ordinary corridor backwards.

GROUP CROSSING (REQ-24) counts DISTINCT people across a boundary inside a
window. Distinct matters — one person stepping in and out of a doorway is not a
group, and counting crossings rather than people is how tailgating detectors
produce their most embarrassing false positives.

Pure stdlib — unit-testable without cv2, torch or a camera.
"""

from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

Pair = Tuple[str, str]

ALLOWED = "allowed"
WRONG_WAY = "wrong_way"
UNPOLICED = "unpoliced"


class DirectionPolicy:
    """Declared one-way routes between zones.

    A route is declared as an ordered pair — walking ZONE-A to ZONE-B is
    permitted. The reverse of a declared route is a violation. Everything else
    is unpoliced.

    Deliberately NOT symmetric with an "everything not allowed is denied"
    reading. Most zone pairs in a building are two-way and always will be, so
    the safe default is to police only what an operator has actually declared.
    """

    def __init__(self, allowed: Iterable[Pair] = ()):
        self.allowed: Set[Pair] = {(str(a), str(b)) for a, b in (allowed or ())}

    @classmethod
    def from_zones(cls, zones) -> "DirectionPolicy":
        """Read `allowed_from` on each zone: the zones one may arrive from.

        Expressed on the destination because that is how an operator describes
        it — "you may only reach the platform from the concourse" — and because
        it keeps the rule next to the zone it protects.
        """
        allowed: List[Pair] = []
        for z in zones or []:
            get = (z.get if isinstance(z, dict) else (lambda k, d=None: getattr(z, k, d)))
            zid = get("zone_id")
            if not zid:
                continue
            if get("enabled", True) in (False, 0):
                continue
            for src in (get("allowed_from") or []):
                if src:
                    allowed.append((str(src), str(zid)))
        return cls(allowed)

    def verdict(self, zone_from, zone_to) -> str:
        if not zone_from or not zone_to:
            return UNPOLICED
        pair = (str(zone_from), str(zone_to))
        if pair in self.allowed:
            return ALLOWED
        if (pair[1], pair[0]) in self.allowed:
            return WRONG_WAY
        return UNPOLICED

    def policed_pairs(self) -> List[Pair]:
        return sorted(self.allowed)


class WrongWayDetector:
    """Raises one violation per person per route, not one per frame.

    A person who has gone the wrong way is usually still going the wrong way on
    the next transition, and an operator who is sent the same violation
    repeatedly stops reading the feed. The latch clears once that person walks
    the route correctly, or after ``cooldown_s``.
    """

    def __init__(self, policy: Optional[DirectionPolicy] = None,
                 cooldown_s: float = 60.0):
        self.policy = policy or DirectionPolicy()
        self.cooldown_s = cooldown_s
        self._fired: Dict[Tuple[str, str, str], float] = {}
        self.stats = {"violations": 0, "suppressed_repeat": 0, "policed": 0}

    def check(self, person_ref: str, zone_from, zone_to,
              now: float) -> Optional[dict]:
        verdict = self.policy.verdict(zone_from, zone_to)
        if verdict == UNPOLICED:
            return None
        self.stats["policed"] += 1
        key = (str(person_ref), str(zone_from), str(zone_to))
        if verdict == ALLOWED:
            # Walking it correctly clears the latch, so a genuine second
            # violation later is reported rather than swallowed.
            self._fired.pop((key[0], key[2], key[1]), None)
            return None
        last = self._fired.get(key)
        if last is not None and (now - last) < self.cooldown_s:
            self.stats["suppressed_repeat"] += 1
            return None
        self._fired[key] = now
        self.stats["violations"] += 1
        return {"person_ref": person_ref, "zone_from": zone_from,
                "zone_to": zone_to, "ts": now,
                "allowed_direction": f"{zone_to} -> {zone_from}"}

    def drop_person(self, person_ref: str) -> None:
        for key in [k for k in self._fired if k[0] == person_ref]:
            del self._fired[key]


class GroupCrossingDetector:
    """Distinct people crossing into one zone inside a rolling window.

    Threshold and window are per zone because the question differs by place: a
    restricted doorway may care about two people in three seconds, while a main
    entrance at shift change would fire constantly on the same numbers.
    """

    def __init__(self, window_s: float = 3.0, threshold: int = 5,
                 cooldown_s: float = 10.0):
        self.window_s = window_s
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._recent: Dict[str, Deque[Tuple[float, str]]] = {}
        self._last_fired: Dict[str, float] = {}
        self.stats = {"groups": 0}

    def _window_for(self, zone_id: str, per_zone: Optional[dict]) -> Tuple[float, int]:
        cfg = (per_zone or {}).get(zone_id) or {}
        return (float(cfg.get("window_s", self.window_s)),
                int(cfg.get("threshold", self.threshold)))

    def record(self, zone_id: str, person_ref: str, now: float,
               per_zone: Optional[dict] = None) -> Optional[dict]:
        """Register one crossing into ``zone_id``. Returns a group event or None."""
        if not zone_id or not person_ref:
            return None
        window_s, threshold = self._window_for(zone_id, per_zone)
        if threshold <= 0:
            return None
        dq = self._recent.setdefault(zone_id, deque())
        dq.append((now, str(person_ref)))
        cutoff = now - window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

        # DISTINCT people. One person oscillating in a doorway is not a group,
        # and counting crossings instead of people is the classic way these
        # detectors produce nonsense.
        people = {ref for _, ref in dq}
        if len(people) < threshold:
            return None
        last = self._last_fired.get(zone_id)
        if last is not None and (now - last) < self.cooldown_s:
            return None
        self._last_fired[zone_id] = now
        self.stats["groups"] += 1
        return {"zone_id": zone_id, "count": len(people), "window_s": window_s,
                "threshold": threshold, "ts": now}

    def recent_count(self, zone_id: str, now: float,
                     per_zone: Optional[dict] = None) -> int:
        window_s, _ = self._window_for(zone_id, per_zone)
        dq = self._recent.get(zone_id)
        if not dq:
            return 0
        cutoff = now - window_s
        return len({ref for t, ref in dq if t >= cutoff})
