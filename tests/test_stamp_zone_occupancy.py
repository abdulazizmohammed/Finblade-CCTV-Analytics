"""_stamp_zone_occupancy — the worker side of Part A step 1.

Placement is the whole point of this function. The occupancy Counter is filled
DURING the track loop, so an event stamped at the moment it is built reads a
count that is still missing every track after it. The stamp therefore runs once
per frame, after the loop and the reaper have both finished.
"""

import unittest
from collections import Counter

from finblade.events import (ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION,
                             DENSITY_UPDATE, new_event)

try:
    from services.inference.run_cpu import _stamp_zone_occupancy
    HAVE_WORKER = True
except Exception:                                  # noqa: BLE001
    HAVE_WORKER = False

PR = "pr_" + "b" * 16


class Zone:
    def __init__(self, zone_id, area_sqm):
        self.zone_id = zone_id
        self.area_sqm = area_sqm


ZONES = [Zone("ZONE-01", 50.0), Zone("ZONE-02", 20.0)]


@unittest.skipUnless(HAVE_WORKER, "inference deps not available")
class TestStamping(unittest.TestCase):
    def stamp(self, events, counts):
        _stamp_zone_occupancy(events, Counter(counts), ZONES)
        return events

    def test_entry_gets_the_zone_it_entered(self):
        evt = new_event(ZONE_ENTRY, "C", "S", 1.0, zone_to="ZONE-01",
                        person_ref=PR, confidence=0.9)
        self.stamp([evt], {"ZONE-01": 5})
        self.assertEqual(5, evt["occupancy"])
        self.assertAlmostEqual(0.1, evt["density"])          # 5 / 50 m2

    def test_exit_gets_the_zone_it_left(self):
        evt = new_event(ZONE_EXIT, "C", "S", 1.0, zone_from="ZONE-02",
                        person_ref=PR)
        self.stamp([evt], {"ZONE-02": 0})
        self.assertEqual(0, evt["occupancy"])
        self.assertEqual(0, evt["density"])

    def test_transition_gets_both_ends(self):
        evt = new_event(ZONE_TRANSITION, "C", "S", 1.0, zone_from="ZONE-01",
                        zone_to="ZONE-02", person_ref=PR)
        self.stamp([evt], {"ZONE-01": 2, "ZONE-02": 3})
        self.assertEqual(3, evt["occupancy"], "plain fields describe zone_to")
        self.assertEqual(2, evt["occupancy_from"])
        self.assertAlmostEqual(0.15, evt["density"])         # 3 / 20 m2
        self.assertAlmostEqual(0.04, evt["density_from"])    # 2 / 50 m2

    def test_zone_with_no_count_reports_zero_not_missing(self):
        """A zone nobody is standing in must still report 0 — that is the
        measurement, and it is what bounds the empty period."""
        evt = new_event(ZONE_EXIT, "C", "S", 1.0, zone_from="ZONE-01",
                        person_ref=PR)
        self.stamp([evt], {})
        self.assertEqual(0, evt["occupancy"])

    def test_unknown_zone_is_left_unstamped(self):
        """'NONE' is the sentinel for a track that left without ever being
        confirmed in a zone. There is no count to report — inventing 0 would
        claim a measurement that was never made."""
        evt = new_event(ZONE_EXIT, "C", "S", 1.0, zone_from="NONE",
                        person_ref=PR)
        self.stamp([evt], {"ZONE-01": 4})
        self.assertNotIn("occupancy", evt)

    def test_other_event_types_are_untouched(self):
        evt = new_event(DENSITY_UPDATE, "C", "S", 1.0, zone_id="ZONE-01",
                        occupancy=9, density=9.9)
        self.stamp([evt], {"ZONE-01": 1})
        self.assertEqual(9, evt["occupancy"], "must not overwrite its own value")

    def test_no_zones_configured_is_a_no_op(self):
        """A camera with no polygons drawn — the common fresh-deployment case."""
        evt = new_event(ZONE_ENTRY, "C", "S", 1.0, zone_to="ZONE-01",
                        person_ref=PR, confidence=0.9)
        _stamp_zone_occupancy([evt], Counter({"ZONE-01": 3}), [])
        self.assertNotIn("occupancy", evt)

    def test_stamped_events_still_validate(self):
        from finblade.events import validate_event
        events = [
            new_event(ZONE_ENTRY, "CAM-01", "SITE-01", 1.0, zone_to="ZONE-01",
                      person_ref=PR, confidence=0.9),
            new_event(ZONE_EXIT, "CAM-01", "SITE-01", 1.0, zone_from="ZONE-02",
                      person_ref=PR),
            new_event(ZONE_TRANSITION, "CAM-01", "SITE-01", 1.0,
                      zone_from="ZONE-01", zone_to="ZONE-02", person_ref=PR),
        ]
        self.stamp(events, {"ZONE-01": 2, "ZONE-02": 1})
        for evt in events:
            ok, errors = validate_event(evt)
            self.assertTrue(ok, f"{evt['event_type']}: {errors}")

    def test_a_whole_frame_of_events_is_stamped_from_the_final_count(self):
        """Two people enter the same zone on one frame. Both events must read
        the finished count of 2, not 1 and 2 — which is what stamping inside
        the track loop would have produced."""
        events = [
            new_event(ZONE_ENTRY, "C", "S", 1.0, zone_to="ZONE-01",
                      person_ref=PR, confidence=0.9),
            new_event(ZONE_ENTRY, "C", "S", 1.0, zone_to="ZONE-01",
                      person_ref="pr_" + "c" * 16, confidence=0.9),
        ]
        self.stamp(events, {"ZONE-01": 2})
        self.assertEqual([2, 2], [e["occupancy"] for e in events])


if __name__ == "__main__":
    unittest.main()
