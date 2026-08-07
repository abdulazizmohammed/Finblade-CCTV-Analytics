"""When to emit a DENSITY_UPDATE, and when to append a zone-state row.

Two gates, same shape of decision: a 5-second sampler repeating itself is not
information, and both the events table and the zone_state_ts history were 99%+
repetition. Neither gate touches what the camera computes or posts — only
whether the unchanged repeat is recorded.

DENSITY_UPDATE was emitted once per zone every 5 seconds, carrying occupancy and
density. Measured on the live database: 1,674,979 of them against 1,674,955
zone_state_ts rows — the same measurement, at the same microsecond, written
twice, because both are appended in the same loop iteration. Every other event
type combined came to 2,553 rows, so 99.85% of the events table was that
duplicate.

The signal worth keeping is the CROSSING: NORMAL -> WARNING -> CRITICAL. That is
the transition an operator reacts to, and it happens a handful of times a day
rather than 21,600 times an hour on a thirty-zone site. "Density is still 0.0"
is not a notification.

Deliberately a gate on emission rather than a deletion. FinBlade receives this
stream, and nothing they may have subscribed to disappears — it stops repeating
itself. `always` restores the old behaviour with one environment variable if
they come back and say they need every tick.

Nothing is lost by thresholding. A person entering and leaving inside one
5-second window was already invisible here, because a sampler cannot see
anything shorter than its sample interval — both readings say "0, NORMAL". That
visit lives in ZONE_ENTRY and ZONE_EXIT, which fire per person, immediately, and
are not affected by any of this.
"""

from typing import Dict, Optional

THRESHOLD = "threshold"
ALWAYS = "always"
OFF = "off"
MODES = (THRESHOLD, ALWAYS, OFF)


class DensityUpdateGate:
    """Per-camera emission policy for DENSITY_UPDATE.

    One instance per camera worker; `should_emit` is called once per zone per
    aggregation tick.
    """

    def __init__(self, mode: str = THRESHOLD):
        mode = (mode or THRESHOLD).strip().lower()
        # An unrecognised mode falls back to the default rather than raising:
        # a typo in a deployment's environment must not stop a camera starting.
        self.mode = mode if mode in MODES else THRESHOLD
        self.invalid_mode = None if mode in MODES else mode
        self._last: Dict[str, Optional[str]] = {}

    def should_emit(self, zone_id: str, status: Optional[str]) -> bool:
        if self.mode == ALWAYS:
            return True
        if self.mode == OFF:
            return False
        previous = self._last.get(zone_id, _UNSEEN)
        self._last[zone_id] = status
        # The first observation of a zone always emits. A consumer joining the
        # stream needs a starting value; without it a zone that sits at NORMAL
        # all day would never appear at all.
        return previous is _UNSEEN or previous != status

    def reset(self) -> None:
        """Forget every zone's last status.

        Called when the scene resets — a camera reconnecting, or a looping test
        clip starting over. The rule latches are cleared at the same moment, so
        the next tick re-establishes each zone's status rather than suppressing
        it as unchanged against a status from before the gap.
        """
        self._last.clear()

    def stats(self) -> dict:
        return {"mode": self.mode, "zones_tracked": len(self._last)}


CHANGE = "change"
STATE_MODES = (CHANGE, ALWAYS)
DEFAULT_KEEPALIVE = 300.0


class StateWriteGate:
    """Whether a zone-state post is appended to the zone_state_ts history.

    Part A step 4. The camera posts every 5 seconds and that does not change —
    this decides whether the post becomes a new history row or only refreshes
    the live reading.

    Measured on the live database: 1.1 GB over nine days, 97.7% of rows at zero
    occupancy and 99.7% byte-identical to the row before them. A zone nobody
    walks into wrote 17,280 rows a day saying so.

    The change key is (occupancy, status) and nothing else, which is narrower
    than it looks. density and capacity_pct are pure functions of occupancy and
    the zone's area_sqm / capacity_max, so they cannot move while occupancy
    holds unless the polygon is edited — and that restarts the worker, which
    starts a fresh gate. What the key deliberately excludes is avg_occupancy,
    trend and the rolling inflow/outflow rates: those drift by fractions between
    ticks and would defeat the gate entirely, writing every row anyway for
    numbers a reader can recompute from the history. They can be up to one
    keepalive interval stale in the history; the live reading is exact.

    WHY THIS RUNS IN THE API AND NOT THE CAMERA. The worker is untouched, so the
    wire format, the heartbeat, R-07 offline detection and every deployed camera
    keep working unchanged, and reverting is one environment variable rather
    than a redeploy of the fleet.

    WHAT A SUB-5-SECOND VISIT DOES. Nothing here, and nothing here is what it
    did before either. Someone entering and leaving between two ticks was never
    in this table — a sampler cannot see anything shorter than its interval, and
    both surrounding rows read "0, NORMAL". That visit is carried by ZONE_ENTRY
    and ZONE_EXIT, which fire per person at the frame they happen on, are not
    sampled, and are not affected by this gate. Step 1 stamped occupancy onto
    them, so they carry the count as well as the fact.

    THE KEEPALIVE IS NOT COSMETIC. Once writes are sparse, a gap in the data is
    ambiguous: it means either "nothing changed" or "the camera was dead". A
    periodic write is the only thing that separates them, and it is what lets
    finblade.timeweight trust a held sample for max_hold seconds and call the
    rest unknown. Setting keepalive to 0 disables it and makes long-quiet
    stretches indistinguishable from downtime.
    """

    def __init__(self, mode: str = CHANGE, keepalive_s: float = DEFAULT_KEEPALIVE):
        mode = (mode or CHANGE).strip().lower()
        # As with DensityUpdateGate: a typo in a deployment's environment must
        # not stop ingest, and the safe direction to fall back in is the one
        # that records more, not less.
        self.mode = mode if mode in STATE_MODES else CHANGE
        self.invalid_mode = None if mode in STATE_MODES else mode
        self.keepalive_s = max(float(keepalive_s or 0), 0.0)
        self._last: Dict[tuple, tuple] = {}
        self.written = 0
        self.suppressed = 0

    def should_write(self, camera_id, zone_id, occupancy, status, ts) -> bool:
        if self.mode == ALWAYS:
            self.written += 1
            return True
        key = (camera_id or "", zone_id)
        previous = self._last.get(key)
        now = float(ts or 0)
        current = (occupancy, status)
        # First post for a zone always writes, including the first after an API
        # restart. That is not a concession — a restart is a gap in coverage,
        # and the reader needs an anchor at the point observation resumed rather
        # than a held value stretched across the outage.
        write = (previous is None
                 or previous[0] != current
                 or (self.keepalive_s > 0 and now - previous[1] >= self.keepalive_s))
        if write:
            self._last[key] = (current, now)
            self.written += 1
        else:
            self.suppressed += 1
        return write

    def stats(self) -> dict:
        total = self.written + self.suppressed
        return {
            "mode": self.mode,
            "keepalive_s": self.keepalive_s,
            "zones_tracked": len(self._last),
            "written": self.written,
            "suppressed": self.suppressed,
            "suppressed_pct": round(100.0 * self.suppressed / total, 2) if total else None,
        }


class _Unseen:
    """Distinct from None, which is a legitimate status for a zone with no
    thresholds configured."""

    def __repr__(self):
        return "<unseen>"


_UNSEEN = _Unseen()
