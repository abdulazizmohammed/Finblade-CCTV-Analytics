"""Facility roster tests.

The roster's whole reason to exist is that it keeps counting someone a camera
cannot see, so most of these tests are about what does NOT happen: occupancy not
dropping when sightings stop, not going negative, not resurrecting someone who
has left, not silently discarding the long-unseen.
"""

import unittest

from finblade.presence import (
    ADMIT, AMBIGUOUS, AT_DOOR, DISCHARGE, SEEN, TURNED_BACK,
    DoorPolicy, FacilityRoster, apply_event,
)

# One entrance, one exit, ordinary floor in between.
ZONE_TYPES = {
    "DOOR-IN": "ENTRANCE",
    "DOOR-OUT": "EXIT",
    "ZONE-01": "MONITORED",
    "ZONE-02": "MONITORED",
    "BAY": "RESTRICTED",
}

# The bidirectional case: ONE doorway used both ways, with the lobby drawn
# against its inside face and a forecourt zone on the street side.
TWO_WAY = DoorPolicy(
    {
        "MAIN-DOOR": "DOOR",
        "LOBBY": "MONITORED",
        "ZONE-01": "MONITORED",
        "FORECOURT": "MONITORED",
    },
    outside=("FORECOURT",),
)

# The same doorway with NO zone drawn on its inside face — the mis-sited case.
TWO_WAY_UNCOVERED = DoorPolicy({"MAIN-DOOR": "DOOR"})


def ev(event_type, ref, ts, **kw):
    d = {"event_type": event_type, "person_ref": ref, "ts": ts}
    d.update(kw)
    return d


class TestOccupancySurvivesInvisibility(unittest.TestCase):
    """The requirement the instantaneous count cannot meet."""

    def test_person_out_of_all_camera_view_is_still_counted(self):
        r = FacilityRoster()
        r.admit("p1", 1000.0, "DOOR-IN")
        self.assertEqual(r.occupancy(), 1)
        # No sightings at all for an hour — a corridor, a meeting room, a
        # stairwell. Zone occupancy would read 0 for every zone; the facility
        # is still holding one person.
        self.assertEqual(r.occupancy(), 1)
        self.assertEqual(len(r.stale(older_than_s=60.0, now=4600.0)), 1)
        self.assertEqual(r.occupancy(), 1, "stale() must not remove anyone")

    def test_full_journey_with_a_dead_zone_gap(self):
        r = FacilityRoster()
        apply_event(r, ev("ZONE_ENTRY", "p1", 100.0, zone_to="DOOR-IN"), ZONE_TYPES)
        apply_event(r, ev("ZONE_TRANSITION", "p1", 110.0,
                          zone_from="DOOR-IN", zone_to="ZONE-01"), ZONE_TYPES)
        self.assertEqual(r.occupancy(), 1)

        # 20 minutes unmonitored, then they surface on a different camera.
        apply_event(r, ev("ZONE_ENTRY", "p1", 1310.0, zone_to="ZONE-02"), ZONE_TYPES)
        self.assertEqual(r.occupancy(), 1)
        self.assertEqual(r.get("p1").last_zone, "ZONE-02")
        self.assertEqual(r.get("p1").entry_zone, "DOOR-IN")

        apply_event(r, ev("ZONE_TRANSITION", "p1", 1400.0,
                          zone_from="ZONE-02", zone_to="DOOR-OUT"), ZONE_TYPES)
        self.assertEqual(r.occupancy(), 0)


class TestRosterArithmetic(unittest.TestCase):
    def test_admit_is_idempotent(self):
        r = FacilityRoster()
        self.assertTrue(r.admit("p1", 10.0, "DOOR-IN"))
        self.assertFalse(r.admit("p1", 12.0, "DOOR-IN"),
                         "a second admit must not add a second person")
        self.assertEqual(r.occupancy(), 1)
        self.assertEqual(r.stats["admitted"], 1)
        self.assertEqual(r.stats["readmit_ignored"], 1)
        self.assertEqual(r.get("p1").last_seen, 12.0, "still refreshes the sighting")

    def test_discharge_of_someone_never_admitted_cannot_go_negative(self):
        # The cold-start case: the building is already full when the roster
        # starts empty, so the first exits belong to people it never saw arrive.
        r = FacilityRoster()
        self.assertFalse(r.discharge("ghost", 10.0, "DOOR-OUT"))
        self.assertEqual(r.occupancy(), 0)
        self.assertEqual(r.stats["discharge_unknown"], 1)
        self.assertEqual(r.stats["discharged"], 0)

    def test_double_discharge_counts_once(self):
        r = FacilityRoster()
        r.admit("p1", 10.0, "DOOR-IN")
        self.assertTrue(r.discharge("p1", 20.0, "DOOR-OUT"))
        self.assertFalse(r.discharge("p1", 21.0, "DOOR-OUT"))
        self.assertEqual(r.occupancy(), 0)
        self.assertEqual(r.stats["discharged"], 1)

    def test_trailing_events_do_not_resurrect_a_departed_person(self):
        # A worker can emit a last sighting after the exit crossing. If that
        # re-admitted them the exit would silently un-count itself.
        r = FacilityRoster()
        r.admit("p1", 10.0, "DOOR-IN")
        r.discharge("p1", 20.0, "DOOR-OUT")
        self.assertFalse(r.note_seen("p1", 21.0, "ZONE-01"))
        self.assertEqual(r.occupancy(), 0)

    def test_occupancy_counts_distinct_people(self):
        r = FacilityRoster()
        for ref in ("p1", "p2", "p3"):
            r.admit(ref, 10.0, "DOOR-IN")
        r.discharge("p2", 50.0, "DOOR-OUT")
        self.assertEqual(r.occupancy(), 2)
        self.assertEqual([m["ref"] for m in r.members()], ["p1", "p3"])


class TestEventMapping(unittest.TestCase):
    """Only ARRIVAL into a boundary zone moves the count."""

    def setUp(self):
        self.r = FacilityRoster()

    def test_entry_to_entrance_zone_admits(self):
        act = apply_event(self.r, ev("ZONE_ENTRY", "p1", 1.0, zone_to="DOOR-IN"),
                          ZONE_TYPES)
        self.assertEqual(act, ADMIT)
        self.assertEqual(self.r.occupancy(), 1)

    def test_arrival_in_exit_zone_discharges(self):
        self.r.admit("p1", 1.0, "DOOR-IN")
        act = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 9.0,
                                     zone_from="ZONE-01", zone_to="DOOR-OUT"),
                          ZONE_TYPES)
        self.assertEqual(act, DISCHARGE)
        self.assertEqual(self.r.occupancy(), 0)

    def test_ordinary_transition_is_only_a_sighting(self):
        self.r.admit("p1", 1.0, "DOOR-IN")
        act = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 9.0,
                                     zone_from="ZONE-01", zone_to="ZONE-02"),
                          ZONE_TYPES)
        self.assertEqual(act, SEEN)
        self.assertEqual(self.r.occupancy(), 1)
        self.assertEqual(self.r.get("p1").last_zone, "ZONE-02")

    def test_leaving_the_exit_zone_does_not_move_the_roster(self):
        # They were discharged on arrival. A ZONE_EXIT out of DOOR-OUT must not
        # discharge a second time, nor read as re-entering the building.
        self.r.admit("p1", 1.0, "DOOR-IN")
        apply_event(self.r, ev("ZONE_TRANSITION", "p1", 9.0,
                               zone_from="ZONE-01", zone_to="DOOR-OUT"), ZONE_TYPES)
        act = apply_event(self.r, ev("ZONE_EXIT", "p1", 11.0, zone_from="DOOR-OUT"),
                          ZONE_TYPES)
        self.assertIsNone(act)
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.stats["discharged"], 1)

    def test_in_place_events_are_sightings(self):
        self.r.admit("p1", 1.0, "DOOR-IN")
        for et in ("LOITERING_START", "RESTRICTED_ZONE_ENTRY"):
            act = apply_event(self.r, ev(et, "p1", 20.0, zone_id="BAY"), ZONE_TYPES)
            self.assertEqual(act, SEEN, et)
        self.assertEqual(self.r.occupancy(), 1)
        self.assertEqual(self.r.get("p1").last_zone, "BAY")

    def test_events_without_a_ref_or_timestamp_are_ignored(self):
        self.assertIsNone(apply_event(self.r, {"event_type": "CAMERA_OFFLINE",
                                               "ts": 5.0}, ZONE_TYPES))
        self.assertIsNone(apply_event(self.r, ev("ZONE_ENTRY", "p1", None,
                                                 zone_to="DOOR-IN"), ZONE_TYPES))
        self.assertIsNone(apply_event(self.r, "not a dict", ZONE_TYPES))
        self.assertEqual(self.r.occupancy(), 0)

    def test_arrival_in_an_unknown_zone_is_a_sighting_not_an_admission(self):
        # A zone added from the UI that is not yet in zone_types must not be
        # guessed as a door — that would invent admissions.
        self.r.admit("p1", 1.0, "DOOR-IN")
        act = apply_event(self.r, ev("ZONE_ENTRY", "p1", 5.0, zone_to="ZONE-99"),
                          ZONE_TYPES)
        self.assertEqual(act, SEEN)
        self.assertEqual(self.r.occupancy(), 1)


class TestBidirectionalDoor(unittest.TestCase):
    """One polygon walked both ways. Direction comes from the zones either side.

    Arriving in a two-way door proves nothing on its own, so the roster must
    hold the crossing open and settle it on departure.
    """

    def setUp(self):
        self.r = FacilityRoster()

    def _walk_in(self, ref, t0=100.0):
        # Appears in the doorway (from the street, which no camera covers),
        # then steps into the lobby.
        a = apply_event(self.r, ev("ZONE_ENTRY", ref, t0, zone_to="MAIN-DOOR"), TWO_WAY)
        b = apply_event(self.r, ev("ZONE_TRANSITION", ref, t0 + 2,
                                   zone_from="MAIN-DOOR", zone_to="LOBBY"), TWO_WAY)
        return a, b

    def _walk_out(self, ref, t0=500.0):
        # Lobby -> doorway -> off camera.
        a = apply_event(self.r, ev("ZONE_TRANSITION", ref, t0,
                                   zone_from="LOBBY", zone_to="MAIN-DOOR"), TWO_WAY)
        b = apply_event(self.r, ev("ZONE_EXIT", ref, t0 + 2,
                                   zone_from="MAIN-DOOR"), TWO_WAY)
        return a, b

    def test_walking_in_through_a_two_way_door_admits(self):
        # Stepped through one event at a time: the state BETWEEN them is the
        # whole point, and a helper that runs both would hide it.
        first = apply_event(self.r, ev("ZONE_ENTRY", "p1", 100.0,
                                       zone_to="MAIN-DOOR"), TWO_WAY)
        self.assertEqual(first, AT_DOOR, "arrival alone must not decide")
        self.assertEqual(self.r.occupancy(), 0, "not counted until direction is known")

        second = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 102.0,
                                        zone_from="MAIN-DOOR", zone_to="LOBBY"), TWO_WAY)
        self.assertEqual(second, ADMIT)
        self.assertEqual(self.r.occupancy(), 1)
        self.assertEqual(self.r.get("p1").last_zone, "LOBBY")

    def test_walking_out_through_the_same_door_discharges(self):
        self._walk_in("p1")
        first = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 500.0,
                                       zone_from="LOBBY", zone_to="MAIN-DOOR"), TWO_WAY)
        self.assertEqual(first, AT_DOOR)
        self.assertEqual(self.r.occupancy(), 1, "still inside while in the doorway")

        second = apply_event(self.r, ev("ZONE_EXIT", "p1", 502.0,
                                        zone_from="MAIN-DOOR"), TWO_WAY)
        self.assertEqual(second, DISCHARGE)
        self.assertEqual(self.r.occupancy(), 0)

    def test_in_and_out_through_one_door_nets_to_zero(self):
        self._walk_in("p1", t0=100.0)
        self._walk_out("p1", t0=900.0)
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.stats["admitted"], 1)
        self.assertEqual(self.r.stats["discharged"], 1)
        self.assertEqual(self.r.stats["ambiguous_crossings"], 0)

    def test_leaving_onto_an_outside_zone_discharges(self):
        # The street side IS covered here, so departure is a transition into a
        # zone rather than into nothing. Without `outside` naming it, the
        # forecourt would look like interior floor and this would read as entry.
        self._walk_in("p1")
        apply_event(self.r, ev("ZONE_TRANSITION", "p1", 600.0,
                               zone_from="LOBBY", zone_to="MAIN-DOOR"), TWO_WAY)
        act = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 602.0,
                                     zone_from="MAIN-DOOR", zone_to="FORECOURT"), TWO_WAY)
        self.assertEqual(act, DISCHARGE)
        self.assertEqual(self.r.occupancy(), 0)

    def test_arriving_from_outside_zone_and_going_in_admits(self):
        apply_event(self.r, ev("ZONE_TRANSITION", "p1", 10.0,
                               zone_from="FORECOURT", zone_to="MAIN-DOOR"), TWO_WAY)
        act = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 12.0,
                                     zone_from="MAIN-DOOR", zone_to="LOBBY"), TWO_WAY)
        self.assertEqual(act, ADMIT)
        self.assertEqual(self.r.occupancy(), 1)

    def test_reaching_the_door_and_turning_back_changes_nothing(self):
        self._walk_in("p1")
        apply_event(self.r, ev("ZONE_TRANSITION", "p1", 700.0,
                               zone_from="LOBBY", zone_to="MAIN-DOOR"), TWO_WAY)
        act = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 704.0,
                                     zone_from="MAIN-DOOR", zone_to="ZONE-01"), TWO_WAY)
        self.assertEqual(act, TURNED_BACK)
        self.assertEqual(self.r.occupancy(), 1, "they never left")
        self.assertEqual(self.r.stats["turned_back"], 1)
        self.assertEqual(self.r.stats["discharged"], 0)

    def test_undecidable_crossing_is_counted_not_guessed(self):
        # No zone on the inside face of the door: walking in and walking out
        # produce the identical event pair, so neither may be assumed.
        act1 = apply_event(self.r, ev("ZONE_ENTRY", "p1", 10.0,
                                      zone_to="MAIN-DOOR"), TWO_WAY_UNCOVERED)
        act2 = apply_event(self.r, ev("ZONE_EXIT", "p1", 14.0,
                                      zone_from="MAIN-DOOR"), TWO_WAY_UNCOVERED)
        self.assertEqual(act1, AT_DOOR)
        self.assertEqual(act2, AMBIGUOUS)
        self.assertEqual(self.r.occupancy(), 0, "must not invent an admission")
        self.assertEqual(self.r.stats["ambiguous_crossings"], 1)
        self.assertEqual(self.r.stats["admitted"], 0)
        self.assertEqual(self.r.stats["discharged"], 0)

    def test_loitering_in_the_doorway_keeps_the_crossing_open(self):
        apply_event(self.r, ev("ZONE_ENTRY", "p1", 10.0, zone_to="MAIN-DOOR"), TWO_WAY)
        apply_event(self.r, ev("LOITERING_START", "p1", 40.0,
                               zone_id="MAIN-DOOR"), TWO_WAY)
        self.assertEqual(self.r.pending_crossings(), 1)
        act = apply_event(self.r, ev("ZONE_TRANSITION", "p1", 60.0,
                                     zone_from="MAIN-DOOR", zone_to="LOBBY"), TWO_WAY)
        self.assertEqual(act, ADMIT, "the crossing must survive an in-place event")
        self.assertEqual(self.r.occupancy(), 1)

    def test_pending_crossings_are_not_counted_as_occupancy(self):
        apply_event(self.r, ev("ZONE_ENTRY", "p1", 10.0, zone_to="MAIN-DOOR"), TWO_WAY)
        snap = self.r.snapshot(now=20.0)
        self.assertEqual(snap["occupancy"], 0)
        self.assertEqual(snap["pending_crossings"], 1)

    def test_two_people_cross_in_opposite_directions_at_once(self):
        self._walk_in("inbound", t0=100.0)          # already inside
        self.r.admit("outbound", 50.0, "LOBBY")
        # Both step into the doorway before either leaves it.
        apply_event(self.r, ev("ZONE_TRANSITION", "outbound", 200.0,
                               zone_from="LOBBY", zone_to="MAIN-DOOR"), TWO_WAY)
        apply_event(self.r, ev("ZONE_ENTRY", "newcomer", 201.0,
                               zone_to="MAIN-DOOR"), TWO_WAY)
        self.assertEqual(self.r.pending_crossings(), 2)
        apply_event(self.r, ev("ZONE_EXIT", "outbound", 203.0,
                               zone_from="MAIN-DOOR"), TWO_WAY)
        apply_event(self.r, ev("ZONE_TRANSITION", "newcomer", 204.0,
                               zone_from="MAIN-DOOR", zone_to="LOBBY"), TWO_WAY)
        # inbound + newcomer inside, outbound gone. Crossings do not interfere.
        self.assertEqual(self.r.occupancy(), 2)
        self.assertFalse(self.r.contains("outbound"))
        self.assertTrue(self.r.contains("newcomer"))
        self.assertEqual(self.r.pending_crossings(), 0)


class TestDoorPolicy(unittest.TestCase):
    def test_unknown_zone_is_interior_not_a_door(self):
        p = DoorPolicy({"MAIN-DOOR": "DOOR"})
        self.assertTrue(p.is_interior("ZONE-NEW"))
        self.assertFalse(p.is_door("ZONE-NEW"))

    def test_no_zone_at_all_is_beyond(self):
        p = DoorPolicy({})
        self.assertTrue(p.is_beyond(None))
        self.assertTrue(p.is_beyond(""))

    def test_named_outside_zone_is_beyond_not_interior(self):
        p = DoorPolicy({"FORECOURT": "MONITORED"}, outside=("FORECOURT",))
        self.assertTrue(p.is_beyond("FORECOURT"))
        self.assertFalse(p.is_interior("FORECOURT"))

    def test_one_way_types_still_classify_as_doors(self):
        p = DoorPolicy(ZONE_TYPES)
        self.assertTrue(p.is_door("DOOR-IN"))
        self.assertTrue(p.is_door("DOOR-OUT"))
        self.assertFalse(p.is_door("ZONE-01"))


class TestDriftIsVisible(unittest.TestCase):
    def test_stale_lists_the_long_unseen_oldest_first_and_removes_nobody(self):
        r = FacilityRoster()
        now = 4000.0
        r.admit("fresh", 3500.0, "DOOR-IN")     # unseen 500s — under the threshold
        r.admit("old", 100.0, "DOOR-IN")
        r.admit("oldest", 10.0, "DOOR-IN")
        stale = r.stale(older_than_s=1800.0, now=now)
        self.assertEqual([s["ref"] for s in stale], ["oldest", "old"])
        self.assertEqual(r.occupancy(), 3, "reporting drift must not correct it")

    def test_snapshot_reports_occupancy_and_drift_together(self):
        r = FacilityRoster(site_id="SITE-DXB-01")
        r.admit("p1", 10.0, "DOOR-IN")
        r.admit("p2", 3000.0, "DOOR-IN")
        snap = r.snapshot(now=4000.0, stale_after_s=1800.0)
        self.assertEqual(snap["occupancy"], 2)
        self.assertEqual(snap["stale"], 1)
        self.assertEqual(snap["site_id"], "SITE-DXB-01")
        self.assertEqual(snap["stats"]["admitted"], 2)


class TestPersistence(unittest.TestCase):
    """Under strict discharge the roster cannot be rebuilt from live frames."""

    def test_round_trip_preserves_occupancy_members_and_counters(self):
        r = FacilityRoster(site_id="SITE-DXB-01")
        r.admit("p1", 10.0, "DOOR-IN")
        r.admit("p2", 20.0, "DOOR-IN")
        r.note_seen("p1", 30.0, "ZONE-01")
        r.discharge("p2", 40.0, "DOOR-OUT")
        r.discharge("ghost", 41.0, "DOOR-OUT")

        restored = FacilityRoster.from_records(r.to_records(),
                                               site_id=r.site_id, stats=r.stats)
        self.assertEqual(restored.occupancy(), 1)
        self.assertTrue(restored.contains("p1"))
        self.assertEqual(restored.get("p1").last_zone, "ZONE-01")
        self.assertEqual(restored.get("p1").admitted_at, 10.0)
        self.assertEqual(restored.stats["discharge_unknown"], 1,
                         "drift counters must survive a restart, not reset")
        self.assertEqual(restored.stats["admitted"], 2)

    def test_restored_roster_keeps_accepting_transitions(self):
        r = FacilityRoster()
        r.admit("p1", 10.0, "DOOR-IN")
        restored = FacilityRoster.from_records(r.to_records())
        apply_event(restored, ev("ZONE_TRANSITION", "p1", 60.0,
                                 zone_from="ZONE-01", zone_to="DOOR-OUT"),
                    ZONE_TYPES)
        self.assertEqual(restored.occupancy(), 0)

    def test_records_carry_no_appearance_data(self):
        r = FacilityRoster()
        r.admit("p1", 10.0, "DOOR-IN")
        keys = set(r.to_records()[0])
        self.assertEqual(keys, {"ref", "admitted_at", "last_seen", "entry_zone",
                                "last_zone", "sightings"})


if __name__ == "__main__":
    unittest.main()
