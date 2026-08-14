"""Facility counting end-to-end through the real ingest path (REQ-12/13/14/15).

These are the specification's TEST-07 to TEST-10 driven the way a camera drives
them: build an event, post it to IngestService.ingest_event, read the facility
figure back. Nothing is called directly on the roster, so a break anywhere in
the chain — schema validation, door policy, persistence — fails here.
"""

import os
import tempfile
import unittest

from finblade.events import (
    ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, new_event,
)
from services.api.service import IngestService
from services.api.sqlite_store import SQLiteStore

CAM = "CAM-A-01"
SITE = "SITE-DXB-01"

# One doorway used both ways, a lobby drawn against its inside face, and the
# forecourt typed OUTSIDE so stepping onto it is not read as coming in.
ZONES = [
    {"zone_id": "MAIN-DOOR", "zone_name": "Main door", "zone_type": "DOOR",
     "capacity_max": 10, "area_sqm": 6.0, "polygon": [[0, 0], [10, 0], [10, 10]]},
    {"zone_id": "LOBBY", "zone_name": "Lobby", "zone_type": "MONITORED",
     "capacity_max": 40, "area_sqm": 60.0, "polygon": [[0, 0], [10, 0], [10, 10]]},
    {"zone_id": "FORECOURT", "zone_name": "Forecourt", "zone_type": "OUTSIDE",
     "capacity_max": 99, "area_sqm": 80.0, "polygon": [[0, 0], [10, 0], [10, 10]]},
]


class FacilityCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SQLiteStore(self.path)
        self.store.save_zones(CAM, ZONES)
        self.svc = IngestService(self.store)
        self.t = 1000.0

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _anon(label):
        """A readable label as a real-shaped anonymous ref.

        Ingest rejects any person_ref that is not `pr_` + 16 hex — the guard
        that keeps identifiable values out of the event store. Hashing the label
        here keeps the tests readable while still going through that validator
        rather than around it.
        """
        import hashlib
        return "pr_" + hashlib.sha256(label.encode()).hexdigest()[:16]

    def post(self, event_type, ref, **payload):
        self.t += 2.0
        evt = new_event(event_type, CAM, SITE, self.t,
                        person_ref=self._anon(ref), **payload)
        if event_type == ZONE_ENTRY:
            evt.setdefault("confidence", 0.9)
        code, body = self.svc.ingest_event(evt)
        self.assertEqual(code, 202, body)
        return body

    def walk_in(self, ref):
        self.post(ZONE_ENTRY, ref, zone_to="MAIN-DOOR")
        return self.post(ZONE_TRANSITION, ref, zone_from="MAIN-DOOR", zone_to="LOBBY")

    def walk_out(self, ref):
        self.post(ZONE_TRANSITION, ref, zone_from="LOBBY", zone_to="MAIN-DOOR")
        return self.post(ZONE_EXIT, ref, zone_from="MAIN-DOOR")

    def occupancy(self):
        return self.svc.facility_state(now=self.t)["occupancy"]


class TestAcceptanceScenarios(FacilityCase):
    def test_07_facility_entry(self):
        body = self.walk_in("pr_1")
        self.assertEqual(body["facility"]["action"], "admit")
        self.assertEqual(self.occupancy(), 1)

    def test_08_facility_exit(self):
        self.walk_in("pr_1")
        body = self.walk_out("pr_1")
        self.assertEqual(body["facility"]["action"], "discharge")
        self.assertEqual(self.occupancy(), 0)

    def test_09_approaches_door_but_does_not_cross(self):
        # Already inside; walks up to the door and returns to the lobby.
        self.walk_in("pr_1")
        self.post(ZONE_TRANSITION, "pr_1", zone_from="LOBBY", zone_to="MAIN-DOOR")
        self.post(ZONE_TRANSITION, "pr_1", zone_from="MAIN-DOOR", zone_to="LOBBY")
        self.assertEqual(self.occupancy(), 1, "no crossing, so no change")
        state = self.svc.facility_state(now=self.t)
        self.assertEqual(state["stats"]["discharged"], 0)
        self.assertEqual(state["stats"]["turned_back"], 1)

    def test_09b_walks_out_of_forecourt_without_entering(self):
        # Steps into the doorway from the street and steps back out. Never
        # inside, so nothing to count.
        self.post(ZONE_TRANSITION, "pr_x", zone_from="FORECOURT", zone_to="MAIN-DOOR")
        self.post(ZONE_TRANSITION, "pr_x", zone_from="MAIN-DOOR", zone_to="FORECOURT")
        self.assertEqual(self.occupancy(), 0)

    def test_10_five_people_each_counted_once(self):
        for i in range(5):
            self.walk_in(f"pr_{i}")
        self.assertEqual(self.occupancy(), 5)
        # Re-crossing the doorway must not add a sixth.
        self.post(ZONE_TRANSITION, "pr_0", zone_from="LOBBY", zone_to="MAIN-DOOR")
        self.post(ZONE_TRANSITION, "pr_0", zone_from="MAIN-DOOR", zone_to="LOBBY")
        self.assertEqual(self.occupancy(), 5)


class TestFacilityOccupancyRules(FacilityCase):
    def test_person_in_an_uncovered_area_is_still_counted(self):
        """The requirement zone occupancy cannot meet (REQ-15)."""
        self.walk_in("pr_1")
        # Leaves the lobby into a corridor no camera watches: a zone exit and
        # then silence. Zone occupancy is now zero everywhere.
        self.post(ZONE_EXIT, "pr_1", zone_from="LOBBY")
        self.t += 1800.0
        self.assertEqual(self.occupancy(), 1, "still inside the building")

    def test_occupancy_is_entries_minus_exits(self):
        for i in range(4):
            self.walk_in(f"pr_{i}")
        self.walk_out("pr_1")
        self.walk_out("pr_2")
        state = self.svc.facility_state(now=self.t)
        self.assertEqual(state["occupancy"], 2)
        self.assertEqual(state["stats"]["admitted"], 4)
        self.assertEqual(state["stats"]["discharged"], 2)

    def test_zone_events_inside_never_move_the_facility_count(self):
        self.walk_in("pr_1")
        for _ in range(20):
            self.post(ZONE_TRANSITION, "pr_1", zone_from="LOBBY", zone_to="LOBBY")
        self.assertEqual(self.occupancy(), 1)


class TestDoorCounters(FacilityCase):
    """REQ-14 — each door keeps its own totals and rates."""

    def test_door_totals_and_net(self):
        for i in range(3):
            self.walk_in(f"pr_{i}")
        self.walk_out("pr_0")
        doors = self.svc.facility_state(now=self.t)["doors"]
        self.assertEqual(len(doors), 1)
        d = doors[0]
        self.assertEqual(d["door_zone_id"], "MAIN-DOOR")
        self.assertEqual(d["entries"], 3)
        self.assertEqual(d["exits"], 1)
        self.assertEqual(d["net"], 2)

    def test_rates_are_reported_per_minute(self):
        for i in range(4):
            self.walk_in(f"pr_{i}")
        d = self.svc.facility_state(now=self.t)["doors"][0]
        self.assertGreater(d["entry_rate_per_min"], 0.0)
        self.assertEqual(d["exit_rate_per_min"], 0.0)
        self.assertEqual(d["net_flow_per_min"], d["entry_rate_per_min"])


class TestPersistence(FacilityCase):
    """Strict discharge means the roster cannot be rebuilt — it must survive."""

    def test_roster_and_door_totals_survive_a_restart(self):
        for i in range(3):
            self.walk_in(f"pr_{i}")
        self.walk_out("pr_0")
        self.assertEqual(self.occupancy(), 2)

        # Restart: brand-new store handle and service over the same file.
        reopened = SQLiteStore(self.path)
        svc2 = IngestService(reopened)
        state = svc2.facility_state(now=self.t)
        self.assertEqual(state["occupancy"], 2, "occupancy must not reset to zero")
        self.assertEqual(state["stats"]["admitted"], 3)
        self.assertEqual(state["stats"]["discharged"], 1)
        self.assertEqual(state["doors"][0]["entries"], 3)
        self.assertEqual(state["doors"][0]["exits"], 1)

    def test_a_restored_roster_still_discharges(self):
        self.walk_in("pr_1")
        svc2 = IngestService(SQLiteStore(self.path))
        self.t += 10.0
        evt = new_event(ZONE_TRANSITION, CAM, SITE, self.t,
                        person_ref=self._anon("pr_1"),
                        zone_from="LOBBY", zone_to="MAIN-DOOR")
        svc2.ingest_event(evt)
        self.t += 2.0
        evt = new_event(ZONE_EXIT, CAM, SITE, self.t,
                        person_ref=self._anon("pr_1"), zone_from="MAIN-DOOR")
        svc2.ingest_event(evt)
        self.assertEqual(svc2.facility_state(now=self.t)["occupancy"], 0)


class TestDoorPolicyFromZones(FacilityCase):
    def test_typing_a_zone_as_a_door_takes_effect_immediately(self):
        # Start with the doorway as ordinary floor: the same walk is just two
        # sightings and must not admit anyone.
        plain = [dict(z) for z in ZONES]
        for z in plain:
            if z["zone_id"] == "MAIN-DOOR":
                z["zone_type"] = "MONITORED"
        self.svc.save_zones({"camera_id": CAM, "zones": plain})
        self.walk_in("pr_1")
        self.assertEqual(self.occupancy(), 0, "not a door, so not a crossing")

        # Type it as a door through the same route the editor uses. The policy
        # cache is invalidated on save, so this applies to the next event rather
        # than up to the TTL later.
        code, _ = self.svc.save_zones({"camera_id": CAM, "zones": ZONES})
        self.assertEqual(code, 200)
        self.walk_in("pr_2")
        self.assertEqual(self.occupancy(), 1)

    def test_a_disabled_door_stops_acting_as_one(self):
        disabled = [dict(z) for z in ZONES]
        for z in disabled:
            if z["zone_id"] == "MAIN-DOOR":
                z["enabled"] = False
        self.svc.save_zones({"camera_id": CAM, "zones": disabled})
        self.walk_in("pr_1")
        self.assertEqual(self.occupancy(), 0,
                         "a retired door must not keep admitting people")


if __name__ == "__main__":
    unittest.main()
