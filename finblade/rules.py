"""Rule engine (Level 7): R-01/02/03/05/06/07/08.

Design goals from CLAUDE.md:
  * Hysteresis: separate on/off thresholds so a value hovering at the line does
    not flap. Falling to 1.9 must NOT clear an amber armed at 2.0; only falling
    below the OFF threshold clears it.
  * 10s debounce on ALL rules: rapid oscillation produces exactly one alert.
  * R-06 restricted intrusion is IMMEDIATE (no debounce) but still one alert per
    (person, zone) visit.
  * R-07 camera offline after >30s silence; clears on recovery.

Time is passed in as a monotonic float (seconds) everywhere -> deterministic.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# --- alert model ----------------------------------------------------------
SEV_INFO = "INFO"
SEV_AMBER = "AMBER"
SEV_RED = "RED"
SEV_CRITICAL = "CRITICAL"  # restricted intrusion (magenta-flashing-red in UI)


@dataclass
class Alert:
    rule_id: str
    severity: str
    message: str
    ts: float
    zone_id: Optional[str] = None
    camera_id: Optional[str] = None
    person_ref: Optional[str] = None
    kind: str = "FIRE"  # "FIRE" or "CLEAR"

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "ts": self.ts,
            "zone_id": self.zone_id,
            "camera_id": self.camera_id,
            "person_ref": self.person_ref,
            "kind": self.kind,
        }


@dataclass
class RuleThresholds:
    amber_on: float = 2.0
    amber_off: float = 1.8
    red_on: float = 4.0
    red_off: float = 3.6
    capacity_on_pct: float = 90.0
    capacity_off_pct: float = 85.0
    loiter_seconds: float = 30.0
    offline_seconds: float = 30.0
    debounce_seconds: float = 10.0


# --- generic hysteresis + debounce latch -----------------------------------
class HysteresisLatch:
    """One armed/disarmed latch with hysteresis and a debounce guard.

    ``update(value, now)`` returns "FIRE", "CLEAR", or None.

    * Arms (FIRE) when value >= on_threshold while disarmed.
    * Disarms (CLEAR) only when value <= off_threshold while armed.
    * A state change within ``debounce_s`` of the previous change is suppressed
      (the latch's internal state does not flip), so oscillation yields one alert.
    """

    def __init__(self, on_threshold: float, off_threshold: float, debounce_s: float = 10.0):
        self.on = on_threshold
        self.off = off_threshold
        self.debounce_s = debounce_s
        self.armed = False
        self._last_change: Optional[float] = None

    def update(self, value: float, now: float) -> Optional[str]:
        if not self.armed:
            target = value >= self.on
        else:
            target = not (value <= self.off)  # stay armed unless we fall to/below off

        if target == self.armed:
            return None  # no desired transition

        # A transition is desired; enforce debounce.
        if self._last_change is not None and (now - self._last_change) < self.debounce_s:
            return None  # suppressed flap

        self.armed = target
        self._last_change = now
        return "FIRE" if target else "CLEAR"


# --- camera offline monitor (R-07) -----------------------------------------
class CameraOfflineMonitor:
    def __init__(self, offline_seconds: float = 30.0):
        self.offline_seconds = offline_seconds
        self._last_seen: Dict[str, float] = {}
        self._offline: Dict[str, bool] = {}

    def heartbeat(self, camera_id: str, now: float) -> Optional[Alert]:
        was_offline = self._offline.get(camera_id, False)
        self._last_seen[camera_id] = now
        self._offline[camera_id] = False
        if was_offline:
            return Alert("R-07", SEV_INFO, f"camera {camera_id} recovered", now,
                         camera_id=camera_id, kind="CLEAR")
        return None

    def check(self, camera_id: str, now: float) -> Optional[Alert]:
        last = self._last_seen.get(camera_id)
        if last is None:
            return None  # never seen -> not yet monitored
        silent = now - last
        already = self._offline.get(camera_id, False)
        if silent > self.offline_seconds and not already:
            self._offline[camera_id] = True
            return Alert("R-07", SEV_RED,
                         f"camera {camera_id} offline >{self.offline_seconds:.0f}s", now,
                         camera_id=camera_id, kind="FIRE")
        return None


# --- the engine ------------------------------------------------------------
class RuleEngine:
    def __init__(self, thresholds: RuleThresholds = None):
        self.t = thresholds or RuleThresholds()
        self._amber: Dict[str, HysteresisLatch] = {}
        self._red: Dict[str, HysteresisLatch] = {}
        self._cap: Dict[str, HysteresisLatch] = {}
        self._loiter_fired: set = set()      # (person_ref, zone_id)
        self._intrusion_active: set = set()  # (person_ref, zone_id)
        self.camera = CameraOfflineMonitor(self.t.offline_seconds)

    # -- density R-01 / R-02 and capacity R-03 --
    def _latch(self, store, key, on, off) -> HysteresisLatch:
        latch = store.get(key)
        if latch is None:
            latch = HysteresisLatch(on, off, self.t.debounce_seconds)
            store[key] = latch
        return latch

    def evaluate_zone(self, zone_id: str, density: float, capacity_pct: float, now: float,
                      warning_on: float = None, critical_on: float = None,
                      hysteresis: float = 0.9) -> List[Alert]:
        """Density (R-01/R-02) + capacity (R-03) rules with hysteresis + debounce.

        warning_on/critical_on override the global density thresholds per zone
        (from the zone's warning_density/critical_density); the clear-thresholds
        are derived as ``on * hysteresis``. Falls back to the global thresholds
        when not supplied.
        """
        alerts: List[Alert] = []

        if critical_on is None:
            r_on, r_off = self.t.red_on, self.t.red_off
        else:
            r_on, r_off = critical_on, critical_on * hysteresis
        if warning_on is None:
            a_on, a_off = self.t.amber_on, self.t.amber_off
        else:
            a_on, a_off = warning_on, warning_on * hysteresis

        red = self._latch(self._red, zone_id, r_on, r_off)
        r = red.update(density, now)
        if r == "FIRE":
            alerts.append(Alert("R-02", SEV_RED,
                                f"density critical {density:.2f}/m2 in {zone_id}", now,
                                zone_id=zone_id))
        elif r == "CLEAR":
            alerts.append(Alert("R-02", SEV_INFO, f"density critical cleared in {zone_id}",
                                now, zone_id=zone_id, kind="CLEAR"))

        amber = self._latch(self._amber, zone_id, a_on, a_off)
        a = amber.update(density, now)
        if a == "FIRE":
            alerts.append(Alert("R-01", SEV_AMBER,
                                f"density warning {density:.2f}/m2 in {zone_id}", now,
                                zone_id=zone_id))
        elif a == "CLEAR":
            alerts.append(Alert("R-01", SEV_INFO, f"density warning cleared in {zone_id}",
                                now, zone_id=zone_id, kind="CLEAR"))

        cap = self._latch(self._cap, zone_id, self.t.capacity_on_pct, self.t.capacity_off_pct)
        c = cap.update(capacity_pct, now)
        if c == "FIRE":
            alerts.append(Alert("R-03", SEV_AMBER,
                                f"zone {zone_id} at {capacity_pct:.0f}% capacity - restrict entry",
                                now, zone_id=zone_id))
        elif c == "CLEAR":
            alerts.append(Alert("R-03", SEV_INFO, f"capacity pressure cleared in {zone_id}",
                                now, zone_id=zone_id, kind="CLEAR"))
        return alerts

    # -- loitering R-05 --
    def evaluate_loiter(self, person_ref: str, zone_id: Optional[str], dwell_s: float,
                        now: float) -> Optional[Alert]:
        if zone_id is None:
            return None
        key = (person_ref, zone_id)
        if dwell_s >= self.t.loiter_seconds and key not in self._loiter_fired:
            self._loiter_fired.add(key)
            return Alert("R-05", SEV_AMBER,
                         f"loitering {dwell_s:.0f}s in {zone_id}", now,
                         zone_id=zone_id, person_ref=person_ref)
        return None

    def reset_loiter(self, person_ref: str, zone_id: str) -> None:
        """Call when the person leaves the zone so a future visit can re-alert."""
        self._loiter_fired.discard((person_ref, zone_id))

    # -- restricted intrusion R-06 (immediate, one per visit) --
    def evaluate_intrusion(self, person_ref: str, zone_id: str, restricted: bool,
                           now: float) -> Optional[Alert]:
        key = (person_ref, zone_id)
        if restricted:
            if key not in self._intrusion_active:
                self._intrusion_active.add(key)
                return Alert("R-06", SEV_CRITICAL,
                             f"RESTRICTED intrusion in {zone_id}", now,
                             zone_id=zone_id, person_ref=person_ref)
        else:
            self._intrusion_active.discard(key)
        return None

    def clear_intrusion(self, person_ref: str, zone_id: str) -> None:
        self._intrusion_active.discard((person_ref, zone_id))

    def drop_person(self, person_ref: str) -> None:
        """Forget all per-person state when a track leaves the scene.

        Clears the one-shot loiter + intrusion latches for this person across all
        zones, so (a) the sets do not grow without bound and (b) a genuine future
        re-entry re-alerts instead of being silently suppressed.
        """
        self._loiter_fired = {k for k in self._loiter_fired if k[0] != person_ref}
        self._intrusion_active = {k for k in self._intrusion_active if k[0] != person_ref}


# --- R-08 occupancy report --------------------------------------------------
class ReportScheduler:
    """Hourly + on-demand occupancy report (UC-39/UC-50)."""

    def __init__(self, period_s: float = 3600.0):
        self.period_s = period_s
        self._last: Optional[float] = None

    def due(self, now: float) -> bool:
        return self._last is None or (now - self._last) >= self.period_s

    def generate(self, zone_states: List[dict], now: float, on_demand: bool = False) -> dict:
        self._last = now
        total = sum(z.get("occupancy", 0) for z in zone_states)
        peak = max((z.get("occupancy", 0) for z in zone_states), default=0)
        return {
            "report_id": f"rpt-{int(now)}",
            "generated_at": now,
            "trigger": "on_demand" if on_demand else "scheduled",
            "total_occupancy": total,
            "peak_zone_occupancy": peak,
            "zones": list(zone_states),
        }
