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

    def __init__(self, window_s: float = 60.0, max_window_s: float = 900.0):
        self.window_s = window_s                       # default rate window (1 min)
        self.max_window_s = max(window_s, max_window_s)  # retain up to 15 min of events
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
        cutoff = now - self.max_window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _rate(self, zone_id: str, kind: str, now: float, window: float) -> float:
        dq = self._events.get(zone_id)
        if not dq:
            return 0.0
        self._evict(dq, now)
        cutoff = now - window
        count = sum(1 for t, k in dq if k == kind and t >= cutoff)
        return count * 60.0 / window

    def inflow_per_min(self, zone_id: str, now: float, window: float = None) -> float:
        return self._rate(zone_id, "in", now, window or self.window_s)

    def outflow_per_min(self, zone_id: str, now: float, window: float = None) -> float:
        return self._rate(zone_id, "out", now, window or self.window_s)

    def net_flow_per_min(self, zone_id: str, now: float, window: float = None) -> float:
        w = window or self.window_s
        return (self.inflow_per_min(zone_id, now, w)
                - self.outflow_per_min(zone_id, now, w))

    def rolling(self, zone_id: str, now: float) -> dict:
        """1-min rates + net, plus 5-min and 15-min rolling averages (Req 11)."""
        return {
            "inflow_per_min": round(self.inflow_per_min(zone_id, now), 2),
            "outflow_per_min": round(self.outflow_per_min(zone_id, now), 2),
            "net_flow": round(self.net_flow_per_min(zone_id, now), 2),
            "inflow_5m": round(self.inflow_per_min(zone_id, now, 300.0), 2),
            "outflow_5m": round(self.outflow_per_min(zone_id, now, 300.0), 2),
            "inflow_15m": round(self.inflow_per_min(zone_id, now, 900.0), 2),
            "outflow_15m": round(self.outflow_per_min(zone_id, now, 900.0), 2),
        }


# Status labels are semantic, not colours. The dashboard maps these to theme
# variables (NORMAL -> --fb-ok grey, never green/teal).
STATUS_NORMAL = "NORMAL"
STATUS_WARNING = "WARNING"
STATUS_CRITICAL = "CRITICAL"


def density_status(density: float, warning_on: float = 2.0, critical_on: float = 4.0) -> str:
    """Zone status by density, using per-zone thresholds. NORMAL is colourless."""
    if density > critical_on:
        return STATUS_CRITICAL
    if density > warning_on:
        return STATUS_WARNING
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
            status=density_status(d, getattr(zone, "warning_density", 2.0),
                                  getattr(zone, "critical_density", 4.0)),
            ts=now,
        )


class ZoneStats:
    """Rolling per-zone occupancy stats over a window: peak, average, trend.

    Peak is session-wide (max occupancy seen). Average + trend are over the last
    ``window_s`` seconds. Trend compares the recent half of the window to the
    older half, so a zone that is filling reads "rising".
    """

    def __init__(self, window_s: float = 300.0):
        self.window_s = window_s
        self._samples: Dict[str, Deque[Tuple[float, int]]] = {}
        self._peak: Dict[str, int] = {}

    def record(self, zone_id: str, occupancy: int, now: float) -> None:
        dq = self._samples.setdefault(zone_id, deque())
        dq.append((now, occupancy))
        cutoff = now - self.window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        self._peak[zone_id] = max(self._peak.get(zone_id, 0), occupancy)

    def peak(self, zone_id: str) -> int:
        return self._peak.get(zone_id, 0)

    def average(self, zone_id: str) -> float:
        dq = self._samples.get(zone_id)
        if not dq:
            return 0.0
        return sum(o for _, o in dq) / len(dq)

    def trend(self, zone_id: str, now: float) -> str:
        dq = self._samples.get(zone_id)
        if not dq or len(dq) < 4:
            return "flat"
        mid = now - self.window_s / 2
        recent = [o for t, o in dq if t >= mid]
        older = [o for t, o in dq if t < mid]
        if not recent or not older:
            return "flat"
        r, o = sum(recent) / len(recent), sum(older) / len(older)
        if r > o * 1.15:
            return "rising"
        if r < o * 0.85:
            return "falling"
        return "flat"
