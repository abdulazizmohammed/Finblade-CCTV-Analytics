"""Time-weighted aggregation over a sampled series.

Part A step 3. Reports currently do AVG(occupancy) across rows, which is only
correct because rows arrive on a fixed 5-second cadence — every sample carries
the same weight because every sample covers the same duration.

Step 4 makes writes sparse: a row is written when something changes, so one row
may stand for four seconds and the next for four hours. Averaging rows then
answers a question nobody asked. A zone that was empty all night and briefly
busy at 09:00 would report a high average, because "empty" wrote one row and
"busy" wrote many.

Each sample holds until the next one, so the weight is that duration:

    mean = sum(value_i * duration_i) / sum(duration_i)

Two things this must get right, and both are about honesty rather than maths.

A sample from BEFORE the window still describes the window's opening state. A
zone whose last write was yesterday was not "unknown" at midnight — it was
whatever that write said. Pass it as `prior` and its segment is clamped to the
window start.

Camera downtime is UNKNOWN, not zero, and must leave the denominator. A camera
down for half the window should report the average of the half it saw, and say
so via `coverage` — not dilute the number with hours it could not observe.
Under sparse writes a gap in the data means "nothing changed"; a gap in camera
liveness means "we don't know". They look identical in this table, which is why
the unknown intervals have to be supplied from outside it.
"""

from typing import Iterable, List, Optional, Sequence, Tuple


def merge_intervals(intervals: Iterable[Tuple[float, float]]
                    ) -> List[Tuple[float, float]]:
    """Sort and coalesce overlapping intervals. Zero-length ones are dropped."""
    cleaned = sorted((a, b) for a, b in intervals if b > a)
    merged: List[Tuple[float, float]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap(a0: float, a1: float, intervals: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for b0, b1 in intervals:
        if b1 <= a0:
            continue
        if b0 >= a1:
            break                       # intervals are sorted; nothing further can overlap
        total += min(a1, b1) - max(a0, b0)
    return total


class Weighted:
    """Accumulates time-weighted statistics over one zone's samples."""

    __slots__ = ("_weighted", "_seconds", "peak", "minimum", "samples")

    def __init__(self):
        self._weighted = 0.0
        self._seconds = 0.0
        self.peak: Optional[float] = None
        self.minimum: Optional[float] = None
        self.samples = 0

    def add(self, value: Optional[float], seconds: float) -> None:
        self.samples += 1
        if value is None:
            return
        # A zero-duration segment still counts toward peak and minimum: a spike
        # that was immediately superseded happened, even though it contributed
        # nothing to the average.
        self.peak = value if self.peak is None else max(self.peak, value)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        if seconds > 0:
            self._weighted += value * seconds
            self._seconds += seconds

    @property
    def mean(self) -> Optional[float]:
        """None when no observed time carried a value — not 0.

        Zero is a measurement; "we never saw this zone" is not, and reporting
        them the same way is how an outage becomes an empty building.
        """
        return (self._weighted / self._seconds) if self._seconds > 0 else None

    @property
    def observed_seconds(self) -> float:
        return self._seconds


def time_weighted(samples: Sequence[dict], t0: float, t1: float,
                  fields: Sequence[str],
                  unknown: Iterable[Tuple[float, float]] = (),
                  prior: Optional[dict] = None,
                  max_hold: Optional[float] = None) -> dict:
    """Aggregate `fields` across `samples` over the window [t0, t1].

    samples   rows carrying at least "ts", ordered oldest first
    prior     the last row at or before t0, if any — sets the opening state
    unknown   intervals the camera could not observe; excluded from both the
              numerator and the denominator
    max_hold  the longest a single sample may stand for. Beyond it, the rest of
              the gap becomes unknown.

    WHY max_hold EXISTS. A camera whose process is killed emits no
    CAMERA_OFFLINE — the API notices the silence and raises R-07, but no event
    marks the gap, so `unknown` cannot see it. The sample before the gap then
    "holds" for however many hours the worker was down, and its value is
    weighted as if observed the whole time.

    Measured on the live database, this is not hypothetical: CAM-03/ZONE-01
    reported a plain average of 0.0676 against 0.0079 time-weighted, an 8.5x
    disagreement caused entirely by such gaps.

    Once writes are sparse a long gap is legitimate — it means nothing changed —
    so the keepalive is what makes the two distinguishable. With a write at
    least every N seconds, a gap materially longer than N means the camera was
    not running. Set max_hold to roughly twice the keepalive interval.

    Returns per-field mean/peak/min plus window-level coverage.
    """
    if t1 <= t0:
        return {"from": t0, "to": t1, "window_seconds": 0.0,
                "observed_seconds": 0.0, "coverage": None, "samples": 0,
                "fields": {f: {"mean": None, "peak": None, "min": None}
                           for f in fields}}

    ordered = sorted(samples, key=lambda r: float(r.get("ts") or 0))
    if prior is not None:
        ordered = [prior] + ordered

    # A segment longer than max_hold is only trusted for its first max_hold
    # seconds; the remainder joins `unknown`, so it leaves both the average and
    # the coverage rather than silently extending the last reading.
    stale: List[Tuple[float, float]] = []
    if max_hold and max_hold > 0:
        for i, row in enumerate(ordered):
            start = max(float(row.get("ts") or 0), t0)
            end = (float(ordered[i + 1].get("ts") or 0)
                   if i + 1 < len(ordered) else t1)
            end = min(end, t1)
            if end - start > max_hold:
                stale.append((start + max_hold, end))
        # Nothing before the first sample is observed either.
        if ordered:
            first = max(float(ordered[0].get("ts") or 0), t0)
            if first > t0:
                stale.append((t0, first))
        else:
            stale.append((t0, t1))

    gaps = merge_intervals(list(unknown) + stale)
    window = t1 - t0
    unknown_seconds = _overlap(t0, t1, gaps)
    observable = max(window - unknown_seconds, 0.0)

    acc = {f: Weighted() for f in fields}
    counted = 0
    for i, row in enumerate(ordered):
        start = max(float(row.get("ts") or 0), t0)
        end = float(ordered[i + 1].get("ts") or 0) if i + 1 < len(ordered) else t1
        end = min(end, t1)
        if end < start:
            continue                    # sample entirely before the window
        counted += 1
        # Time inside this segment the camera could actually see.
        seconds = max(end - start - _overlap(start, end, gaps), 0.0)
        for field in fields:
            value = row.get(field)
            acc[field].add(None if value is None else float(value), seconds)

    return {
        "from": t0, "to": t1,
        "window_seconds": round(window, 3),
        "observed_seconds": round(min(observable, window), 3),
        # Fraction of the window the camera was up. A caller rendering an
        # average is expected to say so when this is below 1.
        "coverage": round(observable / window, 4) if window > 0 else None,
        "samples": counted,
        "fields": {f: {"mean": acc[f].mean,
                       "peak": acc[f].peak,
                       "min": acc[f].minimum} for f in fields},
    }


def offline_intervals(events: Iterable[dict], t0: float, t1: float
                      ) -> List[Tuple[float, float]]:
    """Camera-down periods, derived from CAMERA_OFFLINE / _ONLINE / _RECOVERED.

    A camera still offline at the end of the window stays offline to t1, and one
    that was already offline when the window opened counts from t0 — otherwise a
    window entirely inside an outage would report full coverage of nothing.
    """
    ordered = sorted(events, key=lambda e: float(e.get("ts")
                                                 or e.get("timestamp") or 0))
    out: List[Tuple[float, float]] = []
    down_since: Optional[float] = None
    for evt in ordered:
        ts = float(evt.get("ts") or evt.get("timestamp") or 0)
        etype = evt.get("event_type")
        if etype == "CAMERA_OFFLINE" and down_since is None:
            down_since = ts
        elif etype in ("CAMERA_ONLINE", "CAMERA_RECOVERED") and down_since is not None:
            out.append((max(down_since, t0), min(ts, t1)))
            down_since = None
    if down_since is not None:
        out.append((max(down_since, t0), t1))
    return merge_intervals(out)
