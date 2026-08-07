"""When to emit a DENSITY_UPDATE.

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


class _Unseen:
    """Distinct from None, which is a legitimate status for a zone with no
    thresholds configured."""

    def __repr__(self):
        return "<unseen>"


_UNSEEN = _Unseen()
