"""Fixes for what the live restart exposed.

Live, the roster reported 600 people inside from a camera showing 21. The state
machine was correct; the key was not. These tests pin the four corrections:
identity-keyed counting, a reset control, door policy scoped to cameras that
still exist, and detection quality actually reaching the store.
"""

import hashlib
import os
import tempfile
import unittest

from finblade.events import ZONE_ENTRY, ZONE_TRANSITION, new_event
from finblade.presence import DoorPolicy
from services.api.service import IngestService
from tests.pgfixture import store_for

CAM, SITE = "CAM-01", "SITE-01"
ZONES = [
    {"zone_id": "ZONE-01", "zone_name": "Entrance", "zone_type": "ENTRANCE",
     "capacity_max": 10, "area_sqm": 6.0, "polygon": [[0, 0], [9, 0], [9, 9]]},
    {"zone_id": "ZONE-02", "zone_name": "Exit", "zone_type": "EXIT",
     "capacity_max": 10, "area_sqm": 6.0, "polygon": [[0, 0], [9, 0], [9, 9]]},
    {"zone_id": "LOBBY", "zone_name": "Lobby", "zone_type": "MONITORED",
     "capacity_max": 40, "area_sqm": 60.0, "polygon": [[0, 0], [9, 0], [9, 9]]},
]


def pr(label):
    return "pr_" + hashlib.sha256(label.encode()).hexdigest()[:16]


class Case(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = store_for(self.path)
        self.store.upsert_camera(CAM, site_id=SITE)
        self.store.save_zones(CAM, ZONES)
        self.svc = IngestService(self.store)
        self.t = 1000.0

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def enter(self, person_label, global_ref=None, zone="ZONE-01"):
        self.t += 2.0
        kw = {"person_ref": pr(person_label), "zone_to": zone, "confidence": 0.9}
        if global_ref:
            kw["global_ref"] = global_ref
        evt = new_event(ZONE_ENTRY, CAM, SITE, self.t, **kw)
        code, body = self.svc.ingest_event(evt)
        self.assertEqual(code, 202, body)
        return body

    def occ(self):
        return self.svc.facility_state(now=self.t)["occupancy"]


class TestIdentityKeyedCounting(Case):
    """The actual bug: one human, many tracker ids, many admissions."""

    def test_track_fragmentation_no_longer_multiplies_one_person(self):
        # Same human, six tracker breaks — six different person_refs, one
        # global_ref. This is precisely the live 600-vs-21 scenario.
        for i in range(6):
            self.enter(f"fragment-{i}", global_ref="gp_aaaaaaaaaaaaaaaa")
        self.assertEqual(self.occ(), 1, "one identity is one person")

    def test_without_reid_it_still_counts_but_says_so(self):
        for i in range(6):
            self.enter(f"fragment-{i}")          # no global_ref
        state = self.svc.facility_state(now=self.t)
        self.assertEqual(state["occupancy"], 6, "no stable key, so no dedupe")
        self.assertEqual(state["stats"]["provisional_admits"], 6,
                         "the untrustworthy share must be a readable number")

    def test_distinct_identities_still_count_separately(self):
        self.enter("a", global_ref="gp_aaaaaaaaaaaaaaaa")
        self.enter("b", global_ref="gp_bbbbbbbbbbbbbbbb")
        self.assertEqual(self.occ(), 2)

    def test_exit_matches_on_identity_not_tracker_id(self):
        # Enters as one track, leaves as another — the case that left 616
        # entries against 0 exits live.
        self.enter("in", global_ref="gp_aaaaaaaaaaaaaaaa")
        self.assertEqual(self.occ(), 1)
        self.t += 2.0
        self.svc.ingest_event(new_event(
            ZONE_TRANSITION, CAM, SITE, self.t, person_ref=pr("out-different"),
            global_ref="gp_aaaaaaaaaaaaaaaa", zone_from="LOBBY", zone_to="ZONE-02"))
        self.assertEqual(self.occ(), 0, "the person who left is the one who came in")


class TestRosterReset(Case):
    def test_clear_empties_the_roster(self):
        for i in range(5):
            self.enter(f"p{i}")
        self.assertEqual(self.occ(), 5)
        code, body = self.svc.clear_facility()
        self.assertEqual(code, 200)
        self.assertEqual(body["removed"], 5)
        self.assertEqual(self.occ(), 0)

    def test_clear_keeps_the_evidence_that_drift_happened(self):
        for i in range(4):
            self.enter(f"p{i}")
        self.svc.clear_facility()
        state = self.svc.facility_state(now=self.t)
        self.assertEqual(state["doors"][0]["entries"], 4,
                         "observed traffic stays true after a reset")
        self.assertEqual(state["stats"]["admitted"], 4)
        self.assertEqual(state["stats"]["cleared"], 4)

    def test_clear_survives_a_restart(self):
        for i in range(3):
            self.enter(f"p{i}")
        self.svc.clear_facility()
        self.assertEqual(IngestService(store_for(self.path))
                         .facility_state(now=self.t)["occupancy"], 0)

    def test_counting_resumes_after_a_clear(self):
        self.enter("p0")
        self.svc.clear_facility()
        self.enter("p1", global_ref="gp_cccccccccccccccc")
        self.assertEqual(self.occ(), 1)


class TestDoorPolicyScoping(unittest.TestCase):
    """A deleted camera's zone must stop acting as a boundary."""

    ZONES = [
        {"zone_id": "ZONE-01", "camera_id": "CAM-01", "zone_type": "ENTRANCE"},
        {"zone_id": "ZONE-07", "camera_id": "CAM-05", "zone_type": "ENTRANCE"},
    ]

    def test_zones_of_deleted_cameras_are_dropped(self):
        p = DoorPolicy.from_zones(self.ZONES, cameras={"CAM-01"})
        self.assertTrue(p.is_door("ZONE-01"))
        self.assertFalse(p.is_door("ZONE-07"),
                         "CAM-05 is gone; its door must not keep admitting")
        self.assertTrue(p.is_interior("ZONE-07"))

    def test_omitting_the_camera_set_keeps_every_zone(self):
        p = DoorPolicy.from_zones(self.ZONES)
        self.assertTrue(p.is_door("ZONE-01"))
        self.assertTrue(p.is_door("ZONE-07"))

    def test_service_scopes_the_policy_to_live_cameras(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = store_for(path)
            store.upsert_camera("CAM-01", site_id=SITE)
            store.save_zones("CAM-01", [ZONES[0]])
            store.save_zones("CAM-05", [{"zone_id": "ZONE-07",
                                         "zone_name": "orphan",
                                         "zone_type": "ENTRANCE",
                                         "capacity_max": 1, "area_sqm": 1.0,
                                         "polygon": [[0, 0], [1, 0], [1, 1]]}])
            svc = IngestService(store)          # CAM-05 was never registered
            policy = svc.door_policy(now=1000.0)
            self.assertTrue(policy.is_door("ZONE-01"))
            self.assertFalse(policy.is_door("ZONE-07"))
        finally:
            os.unlink(path)


class TestHealthQualityPersists(Case):
    """REQ-27 reached the worker but not the store."""

    def test_quality_fields_round_trip(self):
        self.svc.record_camera_health({
            "camera_id": CAM, "site_id": SITE, "ts": self.t,
            "health": {"state": "ONLINE", "people_in_view": 21,
                       "tracking_quality": "SATURATED", "counts_reliable": False,
                       "counting_mode": "track_degraded", "mean_confidence": 0.42,
                       "track_churn_per_min": 7.5, "detector_saturation": 0.9},
        })
        cam = next(c for c in self.svc.cameras() if c["camera_id"] == CAM)
        self.assertEqual(cam["tracking_quality"], "SATURATED")
        self.assertEqual(cam["counting_mode"], "track_degraded")
        self.assertAlmostEqual(cam["mean_confidence"], 0.42)
        self.assertAlmostEqual(cam["track_churn_per_min"], 7.5)
        self.assertFalse(cam["counts_reliable"])

    def test_a_worker_that_reports_nothing_is_not_recorded_as_reliable(self):
        self.svc.record_camera_health({
            "camera_id": CAM, "ts": self.t,
            "health": {"state": "ONLINE", "people_in_view": 3}})
        cam = next(c for c in self.svc.cameras() if c["camera_id"] == CAM)
        self.assertIsNone(cam["tracking_quality"])
        self.assertIsNone(cam["counts_reliable"],
                          "absent must never read as reliable")


if __name__ == "__main__":
    unittest.main()
