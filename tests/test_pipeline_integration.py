"""Headless integration smoke test of the POST-detection pipeline.

IMPORTANT: this does NOT mock or stand in for YOLO. It feeds *synthetic track
coordinates* (as if a detector had produced them) through the real
zones -> debounce -> metrics -> events -> rules code and asserts the pipeline
runs many frames without exception and emits > 0 events. It makes NO claim that
detection works — that is gated on real weights + video (see BLOCKERS.md).

The real detector's output plugs in at the exact same interface: a per-frame
list of (track_id, x1, y1, x2, y2).
"""

import unittest

from finblade.debounce import BoundaryDebouncer
from finblade.events import (
    CAMERA_HEARTBEAT,
    DENSITY_UPDATE,
    ZONE_ENTRY,
    ZONE_EXIT,
    ZONE_TRANSITION,
    new_event,
    validate_event,
)
from finblade.geometry import foot_point
from finblade.identity import PersonRefHasher
from finblade.metrics import DwellTracker, FlowCounter, ZoneStateAggregator, density_per_sqm
from finblade.rules import RuleEngine
from finblade.zones import Zone, zone_of

LOBBY = Zone("ZONE-01", "Lobby", False, 40, 60.0, [(100, 300), (1180, 300), (1180, 700), (100, 700)])
BAY = Zone("ZONE-02", "Restricted Bay", True, 5, 12.0, [(850, 120), (1180, 120), (1180, 300), (850, 300)])
ZONES = [LOBBY, BAY]


def synthetic_track(frame_idx):
    """One synthetic person walking from the lobby up into the restricted bay.

    Returns a list of (track_id, x1, y1, x2, y2) for the frame.
    """
    # Person 1 walks upward (y decreases) from lobby into the bay over 60 frames.
    y = 680 - frame_idx * 8
    box = (980, y - 120, 1040, y)  # ~120px tall person, feet at y
    return [(1, *box)]


class TestPipelineIntegration(unittest.TestCase):
    def test_runs_many_frames_and_emits_events(self):
        deb = BoundaryDebouncer(n=3)
        dwell = DwellTracker()
        flow = FlowCounter()
        agg = ZoneStateAggregator(period_s=1.0)
        eng = RuleEngine()
        hasher = PersonRefHasher(session_salt="itest")

        events = []
        alerts = []
        frames = 60
        fps = 12.0

        for i in range(frames):
            now = i / fps
            dets = synthetic_track(i)
            occupancy = {z.zone_id: 0 for z in ZONES}

            for tid, x1, y1, x2, y2 in dets:
                fp = foot_point(x1, y1, x2, y2)
                observed = zone_of(fp, ZONES)
                confirmed, changed = deb.update(tid, observed)
                if confirmed is not None:
                    occupancy[confirmed] += 1
                pr = hasher.ref(tid)

                if changed:
                    # Emit entry/exit/transition depending on prev vs new.
                    events.append(new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", now,
                                            zone_to=confirmed or "NONE", person_ref=pr,
                                            confidence=0.9))
                    flow.record_entry(confirmed or "NONE", now)

                d = dwell.update(tid, confirmed, now)
                if confirmed == BAY.zone_id:
                    a = eng.evaluate_intrusion(pr, confirmed, restricted=True, now=now)
                    if a:
                        alerts.append(a)
                lo = eng.evaluate_loiter(pr, confirmed, d, now)
                if lo:
                    alerts.append(lo)

            # Density + zone-state events on cadence.
            if agg.due(now):
                for z in ZONES:
                    occ = occupancy[z.zone_id]
                    dens = density_per_sqm(occ, z.area_sqm)
                    events.append(new_event(DENSITY_UPDATE, "CAM-A-01", "SITE-1", now,
                                            zone_id=z.zone_id, occupancy=occ, density=dens))
                    for al in eng.evaluate_zone(z.zone_id, dens,
                                                100.0 * occ / z.capacity_max, now):
                        alerts.append(al)

            events.append(new_event(CAMERA_HEARTBEAT, "CAM-A-01", "SITE-1", now))

        # Assertions: pipeline ran and produced valid events + at least one alert.
        self.assertGreater(len(events), 0)
        for e in events:
            ok, errs = validate_event(e)
            self.assertTrue(ok, f"invalid event {e['event_type']}: {errs}")
        # The synthetic person crosses into the restricted bay -> R-06 must fire.
        self.assertTrue(any(a.rule_id == "R-06" for a in alerts),
                        "expected a restricted-zone intrusion alert")


if __name__ == "__main__":
    unittest.main()
