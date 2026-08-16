"""Two fixes to the facility count, and the interaction between them.

REKEY. A person is admitted at the door under a tracker-scoped person_ref,
because ReID has not resolved them yet — and it usually has not, since the
crops of somebody in a doorway are edge-truncated and the quality gate throws
them away. A second later they resolve, every event switches to the global ref,
and their exit removes nothing. One phantom per visitor. These tests pin the
rekey that closes that.

BASELINE. A cold start into an occupied building reads zero and stays wrong
until the whole opening population has turned over. There is no way to observe
that number — an interior camera cannot see the rooms it cannot see — so it is
declared, and then drained by exactly the event that means "somebody left who
we never saw arrive".

The two interact: the rekey removes a large source of false discharge_unknown,
which is what the baseline decays on, so getting the rekey wrong would quietly
drain the baseline early. test_rekeyed_exit_does_not_drain_the_baseline is the
guard on that.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade.presence import DoorPolicy, FacilityRoster, apply_event  # noqa: E402
from services.api.service import IngestService                        # noqa: E402
from services.api.sqlite_store import SQLiteStore                     # noqa: E402
from services.api.store import InMemoryStore                          # noqa: E402

ZONES = {"IN": "ENTRANCE", "OUT": "EXIT", "LOBBY": "MONITORED"}
POLICY = DoorPolicy(ZONES)


def ev(t, etype, **kw):
    e = {"event_type": etype, "ts": t}
    e.update(kw)
    return e


class RekeyTest(unittest.TestCase):
    """FacilityRoster.rekey in isolation."""

    def setUp(self):
        self.r = FacilityRoster()

    def test_the_whole_point_admitted_provisional_exits_resolved(self):
        # Admitted before ReID resolved the track...
        apply_event(self.r, ev(0, "ZONE_ENTRY", zone_to="IN",
                               person_ref="p_track7"), POLICY)
        self.assertEqual(self.r.occupancy(), 1)
        # ...ReID resolves, and the roster moves with it.
        self.assertTrue(self.r.rekey("p_track7", "gp_abc"))
        # ...so the exit, which now carries the global ref, actually discharges.
        apply_event(self.r, ev(60, "ZONE_TRANSITION", zone_from="LOBBY",
                               zone_to="OUT", person_ref="gp_abc"), POLICY)
        self.assertEqual(self.r.occupancy(), 0, "no phantom left behind")
        self.assertEqual(self.r.stats["discharge_unknown"], 0)
        self.assertEqual(self.r.stats["rekeyed"], 1)

    def test_without_the_rekey_the_phantom_is_still_there(self):
        # The bug this exists to fix, pinned so a regression is loud.
        apply_event(self.r, ev(0, "ZONE_ENTRY", zone_to="IN",
                               person_ref="p_track7"), POLICY)
        apply_event(self.r, ev(60, "ZONE_TRANSITION", zone_from="LOBBY",
                               zone_to="OUT", person_ref="gp_abc"), POLICY)
        self.assertEqual(self.r.occupancy(), 1)
        self.assertEqual(self.r.stats["discharge_unknown"], 1)

    def test_the_entry_keeps_its_arrival_time(self):
        apply_event(self.r, ev(11.5, "ZONE_ENTRY", zone_to="IN",
                               person_ref="p_1"), POLICY)
        self.r.rekey("p_1", "gp_1")
        moved = self.r.get("gp_1")
        self.assertIsNotNone(moved)
        self.assertEqual(moved.admitted_at, 11.5)
        self.assertEqual(moved.entry_zone, "IN")
        self.assertEqual(moved.ref, "gp_1", "the entry knows its own new key")

    def test_both_keys_on_the_roster_fold_into_one(self):
        # One human counted twice: admitted before resolution, then admitted
        # again after it. Merging is the correction, not a second entry.
        self.r.admit("p_1", 10.0, "IN")
        self.r.admit("gp_1", 40.0, "IN")
        self.assertEqual(self.r.occupancy(), 2)
        self.assertTrue(self.r.rekey("p_1", "gp_1"))
        self.assertEqual(self.r.occupancy(), 1)
        kept = self.r.get("gp_1")
        self.assertEqual(kept.admitted_at, 10.0, "earliest arrival is the real one")
        self.assertEqual(kept.sightings, 2, "observations add up")

    def test_a_later_sighting_wins_when_folding(self):
        self.r.admit("p_1", 10.0, "IN")
        self.r.note_seen("p_1", 90.0, "LOBBY")
        self.r.admit("gp_1", 40.0, "IN")
        self.r.rekey("p_1", "gp_1")
        self.assertEqual(self.r.get("gp_1").last_seen, 90.0)
        self.assertEqual(self.r.get("gp_1").last_zone, "LOBBY")

    def test_a_crossing_in_progress_moves_too(self):
        # Resolving mid-doorway must not orphan the pending crossing, or the
        # direction is lost and the crossing resolves as ambiguous.
        p = DoorPolicy({"HALL": "MONITORED", "DOORWAY": "DOOR"})
        self.r.admit("p_1", 0.0, "HALL")
        apply_event(self.r, ev(10, "ZONE_TRANSITION", zone_from="HALL",
                               zone_to="DOORWAY", person_ref="p_1"), p)
        self.assertEqual(self.r.pending_crossings(), 1)
        self.r.rekey("p_1", "gp_1")
        self.assertEqual(self.r.crossing_zone("gp_1"), "DOORWAY")
        self.assertIsNone(self.r.crossing_zone("p_1"))
        self.assertEqual(self.r.pending_crossings(), 1, "moved, not duplicated")

    def test_unknown_and_no_op_moves_are_safe(self):
        self.assertFalse(self.r.rekey("nobody", "gp_1"))
        self.assertFalse(self.r.rekey("", "gp_1"))
        self.assertFalse(self.r.rekey("p_1", ""))
        self.r.admit("p_1", 0.0)
        self.assertFalse(self.r.rekey("p_1", "p_1"), "same key is not a move")
        self.assertEqual(self.r.occupancy(), 1)
        self.assertEqual(self.r.stats["rekeyed"], 0)


class BaselineTest(unittest.TestCase):
    """A declared opening count, and how it drains."""

    def setUp(self):
        self.r = FacilityRoster()

    def test_occupancy_includes_the_baseline(self):
        self.r.set_baseline(40)
        self.assertEqual(self.r.occupancy(), 40)
        self.assertEqual(self.r.observed(), 0)
        apply_event(self.r, ev(0, "ZONE_ENTRY", zone_to="IN",
                               person_ref="gp_1"), POLICY)
        self.assertEqual(self.r.occupancy(), 41)
        self.assertEqual(self.r.observed(), 1, "the two are reported separately")

    def test_an_unknown_exit_drains_it(self):
        # Somebody who was inside before we could see anyone now leaves.
        self.r.set_baseline(40)
        apply_event(self.r, ev(10, "ZONE_TRANSITION", zone_from="LOBBY",
                               zone_to="OUT", person_ref="gp_stranger"), POLICY)
        self.assertEqual(self.r.baseline, 39)
        self.assertEqual(self.r.occupancy(), 39)
        self.assertEqual(self.r.stats["discharge_unknown"], 1,
                         "still recorded — it is the drift signal too")
        self.assertEqual(self.r.stats["baseline_discharged"], 1)

    def test_a_known_exit_does_not_touch_it(self):
        self.r.set_baseline(5)
        apply_event(self.r, ev(0, "ZONE_ENTRY", zone_to="IN",
                               person_ref="gp_1"), POLICY)
        apply_event(self.r, ev(10, "ZONE_TRANSITION", zone_from="LOBBY",
                               zone_to="OUT", person_ref="gp_1"), POLICY)
        self.assertEqual(self.r.baseline, 5, "they were not one of the baseline")
        self.assertEqual(self.r.occupancy(), 5)

    def test_it_drains_to_zero_and_stops(self):
        self.r.set_baseline(2)
        for i in range(6):
            apply_event(self.r, ev(i, "ZONE_TRANSITION", zone_from="LOBBY",
                                   zone_to="OUT", person_ref=f"gp_x{i}"), POLICY)
        self.assertEqual(self.r.baseline, 0)
        self.assertEqual(self.r.occupancy(), 0, "never negative")
        self.assertEqual(self.r.stats["baseline_discharged"], 2,
                         "only the two it could account for")
        self.assertEqual(self.r.stats["discharge_unknown"], 6)

    def test_the_whole_opening_population_turning_over(self):
        # 3 already inside, 3 new arrivals, then everybody leaves.
        self.r.set_baseline(3)
        for i in range(3):
            apply_event(self.r, ev(i, "ZONE_ENTRY", zone_to="IN",
                                   person_ref=f"gp_new{i}"), POLICY)
        self.assertEqual(self.r.occupancy(), 6)
        for i in range(3):                      # the pre-existing three leave
            apply_event(self.r, ev(100 + i, "ZONE_TRANSITION", zone_from="LOBBY",
                                   zone_to="OUT", person_ref=f"gp_old{i}"), POLICY)
        self.assertEqual(self.r.occupancy(), 3)
        for i in range(3):                      # then the three we watched arrive
            apply_event(self.r, ev(200 + i, "ZONE_TRANSITION", zone_from="LOBBY",
                                   zone_to="OUT", person_ref=f"gp_new{i}"), POLICY)
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.baseline, 0)

    def test_setting_it_replaces_rather_than_adds(self):
        self.r.set_baseline(10)
        self.r.set_baseline(4)
        self.assertEqual(self.r.baseline, 4, "a recount means the new number")

    def test_negative_is_floored_not_accepted(self):
        self.r.set_baseline(-5)
        self.assertEqual(self.r.baseline, 0)

    def test_clearing_the_roster_clears_the_baseline(self):
        self.r.set_baseline(12)
        self.r.admit("gp_1", 0.0)
        removed = self.r.clear()
        self.assertEqual(removed, 13, "reports everyone it stopped counting")
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.baseline, 0,
                         "resetting to empty contradicts a claim people are inside")

    def test_members_is_smaller_than_occupancy_while_a_baseline_runs(self):
        # Documented consequence: baseline people have no refs, so they can be
        # counted but never listed. Anything summing members() to check the
        # headline must use observed().
        self.r.set_baseline(7)
        self.r.admit("gp_1", 0.0)
        self.assertEqual(len(self.r.members()), 1)
        self.assertEqual(self.r.observed(), 1)
        self.assertEqual(self.r.occupancy(), 8)

    def test_snapshot_shows_the_decomposition(self):
        self.r.set_baseline(9)
        self.r.admit("gp_1", 0.0)
        snap = self.r.snapshot(now=100.0)
        self.assertEqual(snap["occupancy"], 10)
        self.assertEqual(snap["observed"], 1)
        self.assertEqual(snap["baseline"], 9)

    def test_zero_baseline_leaves_the_old_behaviour_exactly(self):
        apply_event(self.r, ev(10, "ZONE_TRANSITION", zone_from="LOBBY",
                               zone_to="OUT", person_ref="gp_nobody"), POLICY)
        self.assertEqual(self.r.occupancy(), 0)
        self.assertEqual(self.r.stats["discharge_unknown"], 1)
        self.assertEqual(self.r.stats["baseline_discharged"], 0)


class BaselinePersistenceTest(unittest.TestCase):
    """It has to survive a restart, or it silently drops mid-day."""

    def test_baseline_round_trips_through_the_store(self):
        r = FacilityRoster()
        r.set_baseline(23)
        r.admit("gp_1", 5.0, "IN")
        stats = dict(r.stats, baseline=r.baseline)
        back = FacilityRoster.from_records(r.to_records(), stats=stats,
                                           doors=r.doors.to_records())
        self.assertEqual(back.baseline, 23)
        self.assertEqual(back.observed(), 1)
        self.assertEqual(back.occupancy(), 24)

    def test_baseline_is_not_left_behind_in_the_counters(self):
        # It travels in the stats dict but is not a counter; leaking it into
        # stats would put a headcount in with the tallies.
        back = FacilityRoster.from_records([], stats={"baseline": 8,
                                                      "admitted": 3})
        self.assertEqual(back.baseline, 8)
        self.assertNotIn("baseline", back.stats)
        self.assertEqual(back.stats["admitted"], 3)

    def test_an_absent_baseline_restores_as_zero(self):
        back = FacilityRoster.from_records([], stats={"admitted": 1})
        self.assertEqual(back.baseline, 0)


# Ingest validates person_ref as an anonymous hash (pr_ + 16 hex), so the
# service-level tests have to use the real shapes rather than readable stubs.
PR = "pr_" + "a1b2c3d4e5f60718"
GP = "gp_" + "0f1e2d3c4b5a6978"


class ServiceIntegrationTest(unittest.TestCase):
    """Through IngestService, which is where the rekey is actually triggered."""

    def setUp(self):
        self.store = InMemoryStore()
        self.store.save_zones("CAM-1", [
            {"zone_id": "IN", "zone_name": "Entrance", "zone_type": "ENTRANCE",
             "normalized_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            {"zone_id": "OUT", "zone_name": "Exit", "zone_type": "EXIT",
             "normalized_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            {"zone_id": "LOBBY", "zone_name": "Lobby", "zone_type": "MONITORED",
             "normalized_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        ])
        self.store.upsert_camera("CAM-1")
        self.svc = IngestService(self.store)

    def _post(self, n, etype, t, **kw):
        e = {"event_id": f"e{n}", "event_type": etype, "camera_id": "CAM-1",
             "site_id": "S", "timestamp": t}
        e.update(kw)
        code, body = self.svc.ingest_event(e)
        self.assertEqual(code, 202, body)
        return body

    def test_resolution_after_admission_no_longer_strands_the_entry(self):
        # 1. arrives at the door, ReID has not resolved it: person_ref only
        self._post(1, "ZONE_ENTRY", 0.0, zone_to="IN", person_ref=PR,
                   confidence=0.9)
        self.assertEqual(self.svc.roster.occupancy(), 1)
        self.assertEqual(self.svc.roster.stats["provisional_admits"], 1)
        # 2. ReID resolves; the worker now sends BOTH refs
        self._post(2, "ZONE_TRANSITION", 20.0, zone_from="IN", zone_to="LOBBY",
                   person_ref=PR, global_ref=GP)
        self.assertEqual(self.svc.roster.stats["rekeyed"], 1)
        # 3. leaves, keyed on the global ref
        self._post(3, "ZONE_TRANSITION", 60.0, zone_from="LOBBY", zone_to="OUT",
                   person_ref=PR, global_ref=GP)
        self.assertEqual(self.svc.roster.occupancy(), 0)
        self.assertEqual(self.svc.roster.stats["discharge_unknown"], 0)

    def test_a_track_that_never_resolves_still_works_as_before(self):
        self._post(1, "ZONE_ENTRY", 0.0, zone_to="IN", person_ref=PR,
                   confidence=0.9)
        self._post(2, "ZONE_TRANSITION", 60.0, zone_from="LOBBY", zone_to="OUT",
                   person_ref=PR)
        self.assertEqual(self.svc.roster.occupancy(), 0)
        self.assertEqual(self.svc.roster.stats["rekeyed"], 0)

    def test_rekeyed_exit_does_not_drain_the_baseline(self):
        # The interaction that matters. Before the rekey, this visitor's exit
        # was an unknown discharge and would have eaten one of the declared
        # opening count — so a working rekey is what keeps the baseline honest.
        self.svc.set_facility_baseline({"count": 5})
        self._post(1, "ZONE_ENTRY", 0.0, zone_to="IN", person_ref=PR,
                   confidence=0.9)
        self._post(2, "ZONE_TRANSITION", 20.0, zone_from="IN", zone_to="LOBBY",
                   person_ref=PR, global_ref=GP)
        self._post(3, "ZONE_TRANSITION", 60.0, zone_from="LOBBY", zone_to="OUT",
                   person_ref=PR, global_ref=GP)
        self.assertEqual(self.svc.roster.baseline, 5, "untouched")
        self.assertEqual(self.svc.roster.occupancy(), 5)

    def test_setting_the_baseline_validates_its_input(self):
        for bad in ({"count": -1}, {"count": "twelve"}, {"count": 1.5},
                    {"count": True}, {"count": None}, {}, "not a dict"):
            code, body = self.svc.set_facility_baseline(bad)
            self.assertEqual(code, 422, f"{bad!r} should be rejected")
            self.assertFalse(body["ok"])
        self.assertEqual(self.svc.roster.baseline, 0, "nothing applied")

    def test_setting_the_baseline_reports_the_decomposition(self):
        self._post(1, "ZONE_ENTRY", 0.0, zone_to="IN", person_ref=PR,
                   confidence=0.9)
        code, body = self.svc.set_facility_baseline({"count": 30})
        self.assertEqual(code, 200)
        self.assertEqual(body["baseline"], 30)
        self.assertEqual(body["observed"], 1)
        self.assertEqual(body["occupancy"], 31)

    def test_facility_state_exposes_baseline_and_observed(self):
        self.svc.set_facility_baseline({"count": 3})
        body = self.svc.facility_state(now=100.0)
        self.assertEqual(body["occupancy"], 3)
        self.assertEqual(body["observed"], 0)
        self.assertEqual(body["baseline"], 3)

    def test_clearing_the_roster_clears_the_baseline_too(self):
        self.svc.set_facility_baseline({"count": 9})
        code, body = self.svc.clear_facility()
        self.assertEqual(code, 200)
        self.assertEqual(body["occupancy"], 0)
        self.assertEqual(self.svc.roster.baseline, 0)


class BaselineSurvivesRestartTest(unittest.TestCase):
    """On a backend that actually persists presence.

    Deliberately SQLite rather than InMemoryStore: presence persistence is one
    of the methods InMemoryStore and PostgresStore inherit as silent no-op
    defaults from the base Store, so a restart test against either of those
    would pass by testing nothing. A baseline that evaporated on restart would
    be worse than none at all — the count would drop mid-day with no event to
    explain it — so this has to be exercised against real storage.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_baseline_and_roster_both_come_back(self):
        store = SQLiteStore(self.db)
        svc = IngestService(store)
        svc.set_facility_baseline({"count": 17})
        svc.roster.admit(GP, 5.0, "IN")
        svc._flush_presence(force=True)
        store.close() if hasattr(store, "close") else None

        restarted = IngestService(SQLiteStore(self.db))
        self.assertEqual(restarted.roster.baseline, 17)
        self.assertEqual(restarted.roster.observed(), 1)
        self.assertEqual(restarted.roster.occupancy(), 18)

    def test_a_drained_baseline_stays_drained(self):
        store = SQLiteStore(self.db)
        svc = IngestService(store)
        svc.set_facility_baseline({"count": 3})
        svc.roster.discharge("gp_someone_we_never_admitted", 10.0)
        svc._flush_presence(force=True)
        store.close() if hasattr(store, "close") else None

        restarted = IngestService(SQLiteStore(self.db))
        self.assertEqual(restarted.roster.baseline, 2)
        self.assertEqual(restarted.roster.stats["baseline_discharged"], 1)


class BaselineHttpTest(unittest.TestCase):
    """The endpoint itself — routing, body handling and the reported shape.

    The service-level tests above already cover the validation rules; this is
    here because a correct service behind an unrouted or unparsed endpoint is
    still a broken feature.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("FINBLADE_INMEMORY", "1")
        from fastapi.testclient import TestClient
        from services.api.app import app, svc
        cls._svc = svc
        cls._client_cls = TestClient
        cls._app = app

    def setUp(self):
        from services.api.store import InMemoryStore
        from finblade.presence import FacilityRoster
        self._svc.store = InMemoryStore()
        self._svc.roster = FacilityRoster()
        self.client = self._client_cls(self._app)

    def test_setting_and_reading_it_back(self):
        r = self.client.post("/api/v1/facility/baseline", json={"count": 25})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["baseline"], 25)

        got = self.client.get("/api/v1/facility/occupancy")
        self.assertEqual(got.status_code, 200)
        body = got.json()
        self.assertEqual(body["occupancy"], 25)
        self.assertEqual(body["observed"], 0)
        self.assertEqual(body["baseline"], 25)

    def test_zero_removes_it(self):
        self.client.post("/api/v1/facility/baseline", json={"count": 25})
        r = self.client.post("/api/v1/facility/baseline", json={"count": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(
            "/api/v1/facility/occupancy").json()["occupancy"], 0)

    def test_a_bad_body_is_422_not_500(self):
        r = self.client.post("/api/v1/facility/baseline", json={"count": -3})
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.json()["ok"])

    def test_a_non_json_body_is_422_not_500(self):
        r = self.client.post("/api/v1/facility/baseline",
                             content=b"not json",
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)

    def test_clearing_the_roster_clears_it_over_http(self):
        self.client.post("/api/v1/facility/baseline", json={"count": 8})
        r = self.client.delete("/api/v1/facility/roster")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["occupancy"], 0)
        self.assertEqual(self.client.get(
            "/api/v1/facility/occupancy").json()["baseline"], 0)


if __name__ == "__main__":
    unittest.main()
