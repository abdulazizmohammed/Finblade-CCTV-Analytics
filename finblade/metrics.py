"""Derived metrics: density, capacity %, dwell, inflow/outflow, 5s aggregate.

All time is passed in explicitly as a monotonic float (seconds) so the module is
fully deterministic and unit-testable without sleeping or a real clock.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple


def density_per_sqm(occupancy: int, area_sqm: float) -> float:
    """Persons per square metre. Zero area -> 0.0 (avoid div-by-zero)."""
    if area_sqm <= 0:
        return 0.0
    return occupancy / area_sqm


def capacity_pct(occupancy: int, capacity_max: int) -> float:
    """Occupancy as a percentage of capacity. Zero capacity -> 0.0."""
    if capacity_max <= 0:
        return 0.0
    return 100.0 * occupancy / capacity_max


class DwellTracker:
    """Accumulates seconds a track has continuously been in one zone.

    Dwell resets to 0 when the track changes zone or leaves (zone=None).
    """

    def __init__(self):
        # track_id -> (zone_id, entered_at)
        self._since: Dict[int, Tuple[str, float]] = {}

    def update(self, track_id: int, zone_id: Optional[str], now: float) -> float:
        cur = self._since.get(track_id)
        if zone_id is None:
            self._since.pop(track_id, None)
            return 0.0
        if cur is None or cur[0] != zone_id:
            self._since[track_id] = (zone_id, now)
            return 0.0
        return now - cur[1]

    def dwell(self, track_id: int, now: float) -> float:
        cur = self._since.get(track_id)
        if cur is None:
            return 0.0
        return now - cur[1]

    def drop(self, track_id: int) -> None:
        self._since.pop(track_id, None)


class FlowCounter:
    """Inflow/outflow rate per zone over a rolling window (persons per minute).

    Only *confirmed* (debounced) boundary crossings should be recorded, so a
    person hovering on a line does not inflate the flow.
    """

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        # zone_id -> deque[(t, kind)] kind in {"in","out"}
        self._events: Dict[str, Deque[Tuple[float, str]]] = {}

    def _log(self, zone_id: str, kind: str, now: float) -> None:
        dq = self._events.setdefault(zone_id, deque())
        dq.append((now, kind))
        self._evict(dq, now)

    def record_entry(self, zone_id: str, now: float) -> None:
        self._log(zone_id, "in", now)

    def record_exit(self, zone_id: str, now: float) -> None:
        self._log(zone_id, "out", now)

    def _evict(self, dq: Deque[Tuple[float, str]], now: float) -> None:
        cutoff = now - self.window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _rate(self, zone_id: str, kind: str, now: float) -> float:
        dq = self._events.get(zone_id)
        if not dq:
            return 0.0
        self._evict(dq, now)
        count = sum(1 for _, k in dq if k == kind)
        return count * 60.0 / self.window_s

    def inflow_per_min(self, zone_id: str, now: float) -> float:
        return self._rate(zone_id, "in", now)

    def outflow_per_min(self, zone_id: str, now: float) -> float:
        return self._rate(zone_id, "out", now)


# Status labels are semantic, not colours. The dashboard maps these to theme
# variables (NORMAL -> --fb-ok grey, never green/teal).
STATUS_NORMAL = "NORMAL"
STATUS_AMBER = "AMBER"
STATUS_RED = "RED"


def density_status(density: float, amber_on: float = 2.0, red_on: float = 4.0) -> str:
    if density > red_on:
        return STATUS_RED
    if density > amber_on:
        return STATUS_AMBER
    return STATUS_NORMAL


@dataclass
class ZoneState:
    zone_id: str
    occupancy: int
    density: float
    capacity_pct: float
    inflow_per_min: float
    outflow_per_min: float
    status: str
    ts: float


class ZoneStateAggregator:
    """Emits a ZoneState snapshot per zone every ``period_s`` (default 5s)."""

    def __init__(self, period_s: float = 5.0):
        self.period_s = period_s
        self._last_emit: Optional[float] = None

    def due(self, now: float) -> bool:
        return self._last_emit is None or (now - self._last_emit) >= self.period_s

    def snapshot(self, zone, occupancy: int, flow: FlowCounter, now: float) -> ZoneState:
        self._last_emit = now
        d = density_per_sqm(occupancy, zone.area_sqm)
        return ZoneState(
            zone_id=zone.zone_id,
            occupancy=occupancy,
            density=d,
            capacity_pct=capacity_pct(occupancy, zone.capacity_max),
            inflow_per_min=flow.inflow_per_min(zone.zone_id, now),
            outflow_per_min=flow.outflow_per_min(zone.zone_id, now),
            status=density_status(d),
            ts=now,
        )
