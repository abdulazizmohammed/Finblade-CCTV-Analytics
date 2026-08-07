"""Movement events carry the occupancy they resulted in.

Part A step 1 of the event-driven storage plan. Today the only event carrying
occupancy is DENSITY_UPDATE, emitted per zone every 5 seconds — 1,674,979 rows
against 1,674,955 in zone_state_ts, the same measurement stored twice. The plan
is to stop emitting it; without these fields on entry/exit/transition the
occupancy series could not be rebuilt from the event stream afterwards.

The fields are OPTIONAL by design: a camera worker running older code must keep
validating, or upgrading the API ahead of the workers rejects everything they
post.
"""

import unittest

from finblade.events import (ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION,
                             CAMERA_ONLINE, new_event, validate_event)

PR = "pr_" + "a" * 16


def entry(**over):
    evt = new_event(ZONE_ENTRY, "CAM-01", "SITE-01", 100.0,
                    zone_to="ZONE-01", person_ref=PR, confidence=0.9)
    evt.update(over)
    return evt


class TestOptionalOccupancyFields(unittest.TestCase):
    def test_absent_is_still_valid(self):
        """An older worker sends no occupancy. It must not be rejected."""
        ok, errors = validate_event(entry())
        self.assertTrue(ok, errors)

    def test_present_and_well_formed_is_valid(self):
        ok, errors = validate_event(entry(occupancy=4, density=0.067))
        self.assertTrue(ok, errors)

    def test_wrong_type_is_rejected(self):
        ok, errors = validate_event(entry(occupancy="4"))
        self.assertFalse(ok)
        self.assertTrue(any("occupancy" in e for e in errors), errors)

    def test_booleans_are_not_integers_here(self):
        ok, _ = validate_event(entry(occupancy=True))
        self.assertFalse(ok)

    def test_negative_counts_are_rejected(self):
        for field, value in (("occupancy", -1), ("density", -0.5),
                             ("occupancy_from", -2), ("density_from", -0.1)):
            ok, errors = validate_event(entry(**{field: value}))
            self.assertFalse(ok, f"{field}={value} should be rejected")
            self.assertTrue(any(field in e for e in errors), errors)

    def test_exit_carries_the_zone_it_left(self):
        evt = new_event(ZONE_EXIT, "CAM-01", "SITE-01", 100.0,
                        zone_from="ZONE-01", person_ref=PR,
                        occupancy=0, density=0.0)
        ok, errors = validate_event(evt)
        self.assertTrue(ok, errors)

    def test_transition_carries_both_zones(self):
        """One pair of numbers cannot describe a transition — two zones changed."""
        evt = new_event(ZONE_TRANSITION, "CAM-01", "SITE-01", 100.0,
                        zone_from="ZONE-01", zone_to="ZONE-02", person_ref=PR,
                        occupancy=3, density=0.05,
                        occupancy_from=1, density_from=0.02)
        ok, errors = validate_event(evt)
        self.assertTrue(ok, errors)


class TestCameraOnlineSnapshot(unittest.TestCase):
    """A restart leaves every zone's count unknown until someone next moves —
    possibly hours. CAMERA_ONLINE carries the state at that boundary."""

    def online(self, **over):
        evt = new_event(CAMERA_ONLINE, "CAM-01", "SITE-01", 100.0)
        evt.update(over)
        return evt

    def test_absent_is_valid(self):
        self.assertTrue(validate_event(self.online())[0])

    def test_map_of_counts_is_valid(self):
        ok, errors = validate_event(
            self.online(zone_occupancy={"ZONE-01": 4, "ZONE-02": 0}))
        self.assertTrue(ok, errors)

    def test_negative_or_non_integer_counts_rejected(self):
        for bad in ({"ZONE-01": -1}, {"ZONE-01": 1.5}, {"ZONE-01": True},
                    {"ZONE-01": "4"}):
            ok, _ = validate_event(self.online(zone_occupancy=bad))
            self.assertFalse(ok, f"{bad} should be rejected")

    def test_empty_zone_id_rejected(self):
        ok, _ = validate_event(self.online(zone_occupancy={"": 1}))
        self.assertFalse(ok)

    def test_wrong_container_type_rejected(self):
        ok, _ = validate_event(self.online(zone_occupancy=[("ZONE-01", 1)]))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
