"""Paired zone events and the richer event envelope (REQ-07, REQ-30).

A confirmed move between zones is ONE movement described by three events: the
authoritative ZONE_TRANSITION plus a derived ZONE_EXIT/ZONE_ENTRY pair for
consumers that tally per-zone entries and exits.

The danger the derived pair creates is double counting, and the facility roster
is where that would do real damage: three arrivals at one doorway would open a
second crossing and lose the origin zone — the half that carries the direction —
so someone walking out could be recorded as walking in. These tests exist mainly
to hold that line.
"""

import unittest

from finblade.events import (
    RESTRICTED_ZONE_ENTRY, ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION,
    new_event, validate_event,
)
from finblade.presence import DoorPolicy, FacilityRoster, apply_event

CAM, SITE = "CAM-A-01", "SITE-DXB-01"
REF = "pr_0123456789abcdef"

POLICY = DoorPolicy({"MAIN-DOOR": "DOOR", "LOBBY": "MONITORED",
                     "ZONE-01": "MONITORED"})


def ev(event_type, ts, **payload):
    e = new_event(event_type, CAM, SITE, ts, person_ref=REF, **payload)
    e["ts"] = ts          # apply_event reads `ts`; the wire envelope uses both
    return e


class TestEnvelope(unittest.TestCase):
    """REQ-30 — track_id and a real detector confidence on person events."""

    def test_track_id_and_confidence_validate(self):
        e = new_event(ZONE_TRANSITION, CAM, SITE, 10.0, person_ref=REF,
                      zone_from="ZONE-01", zone_to="LOBBY",
                      track_id=125, confidence=0.96)
        ok, errors = validate_event(e)
        self.assertTrue(ok, errors)

    def test_confidence_is_range_checked_on_every_type(self):
        # It used to be a constant on zone entry, so nothing else was checked.
        e = new_event(RESTRICTED_ZONE_ENTRY, CAM, SITE, 10.0, person_ref=REF,
                      zone_id="BAY", track_id=7, confidence=1.4)
        ok, errors = validate_event(e)
        self.assertFalse(ok)
        self.assertIn("confidence must be in [0, 1]", errors)

    def test_negative_track_id_rejected(self):
        e = new_event(ZONE_EXIT, CAM, SITE, 10.0, person_ref=REF,
                      zone_from="LOBBY", track_id=-3)
        ok, errors = validate_event(e)
        self.assertFalse(ok)

    def test_track_id_must_be_an_integer_not_a_bool(self):
        e = new_event(ZONE_EXIT, CAM, SITE, 10.0, person_ref=REF,
                      zone_from="LOBBY", track_id=True)
        self.assertFalse(validate_event(e)[0])

    def test_derived_flag_must_be_boolean(self):
        e = new_event(ZONE_ENTRY, CAM, SITE, 10.0, person_ref=REF,
                      zone_to="LOBBY", confidence=0.5, derived="yes")
        self.assertFalse(validate_event(e)[0])

    def test_the_envelope_still_carries_no_pii(self):
        e = new_event(ZONE_ENTRY, CAM, SITE, 10.0, person_ref="Jane Doe",
                      zone_to="LOBBY", confidence=0.5, track_id=1)
        ok, errors = validate_event(e)
        self.assertFalse(ok)
        self.assertIn("person_ref is not an anonymous hash (possible PII)", errors)


class TestDerivedEventsDoNotDoubleCount(unittest.TestCase):
    """REQ-07's extra events must not move the facility count."""

    def setUp(self):
        self.r = FacilityRoster()

    def test_a_derived_entry_at_a_door_is_ignored(self):
        act = apply_event(self.r, ev(ZONE_ENTRY, 10.0, zone_to="MAIN-DOOR",
                                     derived=True), POLICY)
        self.assertIsNone(act)
        self.assertEqual(self.r.pending_crossings(), 0,
                         "a derived event must not open a crossing")

    def test_full_three_event_walk_in_admits_exactly_once(self):
        # Arrive in the doorway from the street.
        apply_event(self.r, ev(ZONE_ENTRY, 10.0, zone_to="MAIN-DOOR"), POLICY)
        # Step inside — emitted as the derived pair plus the transition, in the
        # order the camera worker produces them.
        apply_event(self.r, ev(ZONE_EXIT, 12.0, zone_from="MAIN-DOOR",
                               derived=True), POLICY)
        apply_event(self.r, ev(ZONE_ENTRY, 12.0, zone_to="LOBBY",
                               derived=True), POLICY)
        act = apply_event(self.r, ev(ZONE_TRANSITION, 12.0, zone_from="MAIN-DOOR",
                                     zone_to="LOBBY"), POLICY)
        self.assertEqual(act, "admit")
        self.assertEqual(self.r.occupancy(), 1)
        self.assertEqual(self.r.stats["admitted"], 1)
        self.assertEqual(self.r.doors.totals("MAIN-DOOR")["entries"], 1,
                         "the door must count one crossing, not three")

    def test_full_three_event_walk_out_discharges_exactly_once(self):
        self.r.admit(REF, 5.0, "LOBBY")
        apply_event(self.r, ev(ZONE_EXIT, 20.0, zone_from="LOBBY",
                               derived=True), POLICY)
        apply_event(self.r, ev(ZONE_ENTRY, 20.0, zone_to="MAIN-DOOR",
                               derived=True), POLICY)
        apply_event(self.r, ev(ZONE_TRANSITION, 20.0, zone_from="LOBBY",
                               zone_to="MAIN-DOOR"), POLICY)
        self.assertEqual(self.r.occupancy(), 1, "still in the doorway")

        act = apply_event(self.r, ev(ZONE_EXIT, 22.0, zone_from="MAIN-DOOR"), POLICY)
        self.assertEqual(act, "discharge")
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.doors.totals("MAIN-DOOR")["exits"], 1)

    def test_direction_survives_the_derived_pair(self):
        """The regression this whole flag exists to prevent.

        If the derived ZONE_ENTRY at the door were acted on, it would reopen the
        crossing with no origin zone — and an exit would then resolve as
        beyond->door->beyond, i.e. ambiguous, silently losing the departure.
        """
        self.r.admit(REF, 5.0, "LOBBY")
        apply_event(self.r, ev(ZONE_ENTRY, 20.0, zone_to="MAIN-DOOR",
                               derived=True), POLICY)
        apply_event(self.r, ev(ZONE_TRANSITION, 20.0, zone_from="LOBBY",
                               zone_to="MAIN-DOOR"), POLICY)
        apply_event(self.r, ev(ZONE_EXIT, 22.0, zone_from="MAIN-DOOR"), POLICY)
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.stats["ambiguous_crossings"], 0,
                         "origin zone must survive the derived event")


if __name__ == "__main__":
    unittest.main()
