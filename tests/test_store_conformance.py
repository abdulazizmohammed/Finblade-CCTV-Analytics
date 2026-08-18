"""One suite, two backends. InMemoryStore and PostgresStore.

The application picks its store from an environment variable, so a behaviour
that differs between them is a bug that only appears in one deployment. This
file is how PostgresStore earns the right to be used: every assertion here runs
against all three, and the Postgres cases skip cleanly when no server is
configured rather than silently passing.

Most of these encode a bug that has actually happened in this codebase. The
class names say which.

Point a real server at it with:
    FINBLADE_TEST_DSN=postgresql://... .venv/bin/python -m unittest discover -s tests
or let it find the local test cluster started by scripts/pg_local_install.sh.
"""

import os
import sys
import tempfile
import time
import unittest

from services.api.store import InMemoryStore

# Relative to now, and evaluated PER TEST rather than once at import.
#
# latest_zone_states() drops readings older than 30 seconds of WALL CLOCK — a
# zone that stopped reporting is not a zone at zero, so it disappears rather
# than reading empty. Two ways to get this wrong, and this file has now hit
# both:
#
#   A fixed epoch (2023) fails immediately, everywhere. Obvious.
#   A module-level time.time() passes until the suite takes longer than 30
#   seconds, then fails on a stale constant with nothing wrong in the code
#   under test. That happened the moment the Postgres conformance cases joined
#   and the run went from 22s to 50s.
#
# setUp assigns self.T0, so every test measures from its own start.
def _now():
    return time.time()


def _pg_dsn():
    """A DSN for a scratch Postgres, or None to skip.

    Delegates to tests/pgfixture.py. This used to be a hand-rolled copy of the
    same resolution order, and the copy went on calling pgserver.get_server()
    after the shared one had stopped — which OWNS the cluster and fast-shuts it
    at process exit. Three resolvers meant fixing one fixed nothing; the
    developer's server still died on every test run.
    """
    from tests import pgfixture
    return pgfixture.dsn()


PG_DSN = _pg_dsn()


T0 = _now()          # module import time; rebound per test in setUp below


def state(ts, occupancy, zone="ZONE-01", camera="CAM-01", status="NORMAL", **kw):
    row = {"zone_id": zone, "camera_id": camera, "zone_name": "Lobby",
           "zone_type": "MONITORED", "restricted": False, "ts": ts,
           "occupancy": occupancy, "density": occupancy / 10.0,
           "capacity_pct": occupancy * 5.0, "status": status,
           "inflow_per_min": 0.0, "outflow_per_min": 0.0, "site_id": "SITE-01"}
    row.update(kw)
    return row


def event(eid, etype="ZONE_ENTRY", ts=T0, **kw):
    row = {"event_id": eid, "event_type": etype, "camera_id": "CAM-01",
           "site_id": "SITE-01", "zone_id": "ZONE-01",
           "person_ref": "pr_" + "a" * 16, "timestamp": ts}
    row.update(kw)
    return row


def alert(ts=T0, **kw):
    row = {"rule_id": "R-01", "severity": "WARNING", "message": "busy",
           "zone_id": "ZONE-01", "camera_id": "CAM-01", "ts": ts,
           "site_id": "SITE-01", "kind": "FIRE"}
    row.update(kw)
    return row


class StoreContract:
    """Assertions every backend must satisfy. Mixed into one class per store."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        # Rebind the module-level T0 to THIS test's start. The helpers above
        # close over the global, so refreshing it here is what keeps a slow
        # suite from ageing every live-state fixture past the 30s window.
        global T0
        T0 = _now()
        self.store = self.make_store()

    # -- zone state ---------------------------------------------------------
    def test_a_saved_state_is_the_live_reading(self):
        self.store.save_zone_state(state(T0, 4))
        live = self.store.latest_zone_states()
        self.assertEqual(1, len(live))
        self.assertEqual(4, live[0]["occupancy"])
        self.assertEqual("CAM-01", live[0]["camera_id"])

    def test_the_live_reading_is_keyed_on_camera_and_zone(self):
        """Zone ids are unique only within a camera. Keying on the id alone
        made two cameras' zones overwrite each other: on a six-camera site,
        seven zones existed and five came back."""
        self.store.save_zone_state(state(T0, 4, camera="CAM-01"))
        self.store.save_zone_state(state(T0, 9, camera="CAM-02"))
        live = {r["camera_id"]: r["occupancy"]
                for r in self.store.latest_zone_states()}
        self.assertEqual({"CAM-01": 4, "CAM-02": 9}, live)

    def test_the_live_reading_never_moves_backwards(self):
        """A delayed post from a slow camera arriving after a newer one must
        not rewind the current state."""
        self.store.save_zone_state(state(T0 + 60, 7))
        self.store.save_zone_state(state(T0, 1))          # late arrival
        self.assertEqual(7, self.store.latest_zone_states()[0]["occupancy"])

    def test_history_can_be_suppressed_while_the_live_reading_updates(self):
        """Write-on-change: the repeat updates the live row and appends no
        history. Gating the live write too would make a settled zone vanish
        from /zones/state after thirty seconds."""
        self.store.save_zone_state(state(T0, 3), history=True)
        self.store.save_zone_state(state(T0 + 5, 3), history=False)
        rows = self.store.zone_state_rows(0, T0 + 100)
        self.assertEqual(1, len(rows))
        self.assertEqual(T0 + 5, self.store.latest_zone_states()[0]["ts"])

    def test_a_write_is_visible_to_the_very_next_read(self):
        """The durable stores cache the live snapshot for a second. Relying on
        the TTL alone meant a post followed immediately by a read returned the
        state from BEFORE the post — invisible for as long as InMemoryStore,
        which has no cache, backed every test. Three app-level tests failed the
        moment the suite ran on Postgres."""
        self.store.save_zone_state(state(T0, 1))
        self.assertEqual(1, self.store.latest_zone_states()[0]["occupancy"])
        self.store.save_zone_state(state(T0 + 1, 8))
        self.assertEqual(8, self.store.latest_zone_states()[0]["occupancy"])

    def test_zone_state_rows_filter_by_camera_and_zone(self):
        self.store.save_zone_state(state(T0, 1, camera="CAM-01", zone="ZONE-01"))
        self.store.save_zone_state(state(T0, 2, camera="CAM-02", zone="ZONE-01"))
        self.store.save_zone_state(state(T0, 3, camera="CAM-01", zone="ZONE-02"))
        got = self.store.zone_state_rows(0, T0 + 1, camera_id="CAM-01",
                                         zone_id="ZONE-01")
        self.assertEqual([1], [r["occupancy"] for r in got])

    def test_zone_state_prior_is_the_last_row_at_or_before(self):
        for i, occ in enumerate([1, 2, 3]):
            self.store.save_zone_state(state(T0 + i * 100, occ))
        got = self.store.zone_state_prior(T0 + 150, camera_id="CAM-01")
        self.assertEqual(1, len(got))
        self.assertEqual(2, got[0]["occupancy"])

    def test_zone_state_prior_is_per_camera_and_zone(self):
        self.store.save_zone_state(state(T0, 1, camera="CAM-01"))
        self.store.save_zone_state(state(T0, 5, camera="CAM-02"))
        got = {r["camera_id"]: r["occupancy"]
               for r in self.store.zone_state_prior(T0 + 10)}
        self.assertEqual({"CAM-01": 1, "CAM-02": 5}, got)

    def test_stats_group_on_camera_and_zone_not_zone_alone(self):
        """Grouping on zone_id merged the lobby on one camera with the loading
        bay on another into a single report row, averaging across both."""
        for i in range(4):
            self.store.save_zone_state(state(T0 + i, 10, camera="CAM-01"))
            self.store.save_zone_state(state(T0 + i, 0, camera="CAM-02"))
        rows = {r["camera_id"]: r for r in self.store.zone_state_stats(0, T0 + 100)}
        self.assertEqual({"CAM-01", "CAM-02"}, set(rows))
        self.assertEqual(10, rows["CAM-01"]["peak_occupancy"])
        self.assertEqual(0, rows["CAM-02"]["peak_occupancy"])

    def test_stats_averages_are_plain_floats(self):
        """Postgres AVG() returns Decimal. Callers do arithmetic on these and
        json.dumps them, and a Decimal raises on encode."""
        self.store.save_zone_state(state(T0, 3))
        row = self.store.zone_state_stats(0, T0 + 10)[0]
        for key in ("avg_occupancy", "avg_density", "avg_capacity_pct"):
            self.assertIsInstance(row[key], float, key)

    # -- events -------------------------------------------------------------
    def test_events_round_trip(self):
        self.store.save_event(event("e1"))
        got = self.store.list_events(0, T0 + 10)
        self.assertEqual(1, len(got))
        self.assertEqual("ZONE_ENTRY", got[0]["event_type"])

    def test_events_filter_by_type_and_person(self):
        self.store.save_event(event("e1", "ZONE_ENTRY"))
        self.store.save_event(event("e2", "ZONE_EXIT", person_ref="pr_" + "b" * 16))
        self.assertEqual(1, len(self.store.list_events(0, T0 + 10,
                                                       event_type="ZONE_EXIT")))
        self.assertEqual(1, len(self.store.list_events(
            0, T0 + 10, person_ref="pr_" + "b" * 16)))

    def test_an_event_id_is_not_duplicated(self):
        """Replay must overwrite, not append — the forwarder deliberately
        re-sends an overlap and the receiver is required to be idempotent."""
        self.store.save_event(event("e1", ts=T0))
        self.store.save_event(event("e1", ts=T0))
        self.assertEqual(1, len(self.store.list_events(0, T0 + 10)))

    def test_extra_payload_fields_survive(self):
        """Fields with no dedicated column — dwell_time, occupancy — must come
        back, or the events lose the numbers step 1 added to them."""
        self.store.save_event(event("e1", occupancy=5))
        got = self.store.list_events(0, T0 + 10)[0]
        self.assertEqual(5, got["occupancy"])

    def test_zone_filter_matches_transitions_either_way(self):
        self.store.save_event(event("e1", "ZONE_TRANSITION", zone_id=None,
                                    zone_from="ZONE-09", zone_to="ZONE-01"))
        self.assertEqual(1, len(self.store.list_events(0, T0 + 10,
                                                       zone_id="ZONE-09")))

    # -- alerts -------------------------------------------------------------
    def test_alert_lifecycle(self):
        aid = self.store.save_alert(alert())
        self.assertTrue(self.store.acknowledge_alert(aid, "ana", T0 + 1))
        self.assertTrue(self.store.update_alert(aid, "RESOLVED", "ana", T0 + 2))
        self.assertEqual([], self.store.list_alerts())

    def test_a_terminal_alert_cannot_be_acknowledged(self):
        aid = self.store.save_alert(alert())
        self.store.update_alert(aid, "DISMISSED", "ana", T0 + 1)
        self.assertFalse(self.store.acknowledge_alert(aid, "bo", T0 + 2))

    def test_resolving_without_acking_stamps_the_acknowledgement(self):
        aid = self.store.save_alert(alert())
        self.store.update_alert(aid, "RESOLVED", "ana", T0 + 5)
        got = self.store.list_alerts_history(0, T0 + 10)[0]
        self.assertEqual("ana", got["acknowledged_by"])

    def test_alert_ids_are_strings(self):
        """The API puts them in URLs; an int here and a str there is a 404 in
        one backend only."""
        aid = self.store.save_alert(alert())
        self.assertIsInstance(aid, str)
        self.assertIsInstance(self.store.list_alerts()[0]["alert_id"], str)

    def test_an_unknown_alert_id_is_false_not_an_exception(self):
        self.assertFalse(self.store.update_alert("999999", "ACK", "ana", T0))
        self.assertFalse(self.store.update_alert("not-a-number", "ACK", "ana", T0))

    def test_deleting_alerts_returns_the_frames_to_clean_up(self):
        """Selected before the delete: afterwards nothing points at the JPEGs
        and they sit on disk forever."""
        self.store.save_alert(alert(frame="a.jpg"))
        aid = self.store.save_alert(alert(frame="b.jpg"))
        self.store.update_alert(aid, "RESOLVED", "ana", T0 + 1)
        count, frames = self.store.delete_alerts("closed")
        self.assertEqual(1, count)
        self.assertEqual(["b.jpg"], frames)
        count, frames = self.store.delete_alerts("all")
        self.assertEqual(["a.jpg"], frames)

    def test_clear_alerts_are_not_in_the_active_feed(self):
        self.store.save_alert(alert(kind="CLEAR"))
        self.assertEqual([], self.store.list_alerts())

    # -- cameras ------------------------------------------------------------
    def test_marking_a_camera_seen_does_not_reset_the_people_counts(self):
        """This shipped. mark_camera_seen ended with
        people_in_view=excluded.people_in_view, where `excluded` was the column
        default of 0 — so every event ingested zeroed the counts while keeping
        last_seen fresh. The dashboard read 0 people with someone in frame."""
        self.store.record_camera_health(
            "CAM-01", {"state": "ONLINE", "people_in_view": 5,
                       "people_in_zones": 3}, T0)
        self.store.mark_camera_seen("CAM-01", T0 + 10)
        cam = self.store.list_cameras()[0]
        self.assertEqual(5, cam["people_in_view"])
        self.assertEqual(3, cam["people_in_zones"])
        self.assertEqual(T0 + 10, cam["last_seen"])

    def test_health_resolution_accepts_a_tuple(self):
        self.store.record_camera_health("CAM-01", {"resolution": (1920, 1080)}, T0)
        self.assertEqual("1920x1080", self.store.list_cameras()[0]["resolution"])

    def test_camera_booleans_come_back_as_bools(self):
        self.store.record_camera_health("CAM-01", {"frozen": True, "enabled": True}, T0)
        cam = self.store.list_cameras()[0]
        self.assertIs(True, cam["frozen"])
        self.assertIs(True, cam["enabled"])

    def test_upsert_creates_then_updates(self):
        self.store.upsert_camera("CAM-01", name="Front")
        self.store.upsert_camera("CAM-01", name="Front door")
        cams = self.store.list_cameras()
        self.assertEqual(1, len(cams))
        self.assertEqual("Front door", cams[0]["name"])

    def test_upsert_ignores_none_fields(self):
        self.store.upsert_camera("CAM-01", name="Front")
        self.store.upsert_camera("CAM-01", name=None, site_id="SITE-01")
        cam = self.store.list_cameras()[0]
        self.assertEqual("Front", cam["name"])
        self.assertEqual("SITE-01", cam["site_id"])

    def test_delete_camera_reports_whether_it_existed(self):
        self.store.upsert_camera("CAM-01", name="x")
        self.assertTrue(self.store.delete_camera("CAM-01"))
        self.assertFalse(self.store.delete_camera("CAM-01"))

    def test_sim_failure_toggles(self):
        self.store.set_camera_sim("CAM-01", True)
        self.assertIs(True, self.store.list_cameras()[0]["sim_failure"])
        self.store.set_camera_sim("CAM-01", False)
        self.assertIs(False, self.store.list_cameras()[0]["sim_failure"])

    # -- zones --------------------------------------------------------------
    def test_zones_round_trip_with_their_polygons(self):
        self.store.save_zones("CAM-01", [{
            "zone_id": "ZONE-01", "zone_name": "Lobby", "restricted": True,
            "capacity_max": 40, "area_sqm": 60.0,
            "polygon": [[0, 0], [1, 0], [1, 1]],
            "normalized_polygon": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5]]}])
        got = self.store.list_zones("CAM-01")
        self.assertEqual(1, len(got))
        self.assertIs(True, got[0]["restricted"])
        self.assertEqual([[0, 0], [1, 0], [1, 1]], got[0]["polygon"])
        self.assertEqual(3, len(got[0]["normalized_polygon"]))

    def test_saving_zones_replaces_that_cameras_set_only(self):
        self.store.save_zones("CAM-01", [{"zone_id": "ZONE-01"}])
        self.store.save_zones("CAM-02", [{"zone_id": "ZONE-01"}])
        self.store.save_zones("CAM-01", [{"zone_id": "ZONE-09"}])
        self.assertEqual(["ZONE-09"], [z["zone_id"]
                                       for z in self.store.list_zones("CAM-01")])
        self.assertEqual(["ZONE-01"], [z["zone_id"]
                                       for z in self.store.list_zones("CAM-02")])

    # -- reports ------------------------------------------------------------
    def test_reports_round_trip(self):
        rid = self.store.save_report({
            "kind": "ondemand", "generated_at": T0, "from": T0 - 60, "to": T0,
            "zones": [{"zone_id": "ZONE-01"}],
            "totals": {"peak_total_occupancy": 9, "total_alerts": 2}})
        self.assertEqual(9, self.store.get_report(rid)["totals"]["peak_total_occupancy"])
        self.assertEqual(1, len(self.store.list_reports()))

    def test_an_unknown_report_is_none(self):
        self.assertIsNone(self.store.get_report("999999"))
        self.assertIsNone(self.store.get_report("nonsense"))

    # -- retention + cursors ------------------------------------------------
    def test_delete_before_prunes_telemetry_only(self):
        """Alerts are deliberately NOT pruned: an incident record outlives the
        telemetry around it."""
        self.store.save_zone_state(state(T0, 1))
        self.store.save_zone_state(state(T0 + 10_000, 1))
        self.store.save_event(event("old", ts=T0))
        self.store.save_alert(alert(ts=T0))
        deleted = self.store.delete_before(T0 + 5_000)
        self.assertEqual(1, deleted["zone_state_ts"])
        self.assertEqual(1, deleted["events"])
        self.assertEqual(1, len(self.store.list_alerts_history(0, T0 + 20_000)))

    def test_pruning_does_not_remove_the_live_reading(self):
        """zone_live exists so retention cannot delete a zone's current state."""
        self.store.save_zone_state(state(T0, 6))
        self.store.delete_before(T0 + 10_000)
        self.assertEqual(6, self.store.latest_zone_states()[0]["occupancy"])

    def test_cursors_round_trip(self):
        self.store.save_cursors({"events": T0, "alerts": T0 + 5})
        self.store.save_cursors({"events": T0 + 100})
        got = self.store.load_cursors()
        self.assertEqual(T0 + 100, got["events"])
        self.assertEqual(T0 + 5, got["alerts"])

    # ---- cross-camera identity in stored history --------------------------
    # This whole block existed only for SQLite. PostgresStore inherited the base
    # class's silent no-op defaults instead, so areas accepted writes and
    # returned nothing, the roster reset to zero on every restart, and an
    # identity merge corrected the gallery while history kept two people. None
    # of it raised. The suite passed because it never asked.

    def test_global_ref_survives_a_round_trip(self):
        self.store.save_event(event("g1", global_ref="gp_aaa"))
        got = self.store.list_events(0, T0 + 10_000)
        self.assertEqual("gp_aaa", got[0].get("global_ref"))

    def test_events_filter_by_global_ref(self):
        self.store.save_event(event("g1", global_ref="gp_aaa"))
        self.store.save_event(event("g2", global_ref="gp_bbb"))
        self.store.save_event(event("g3"))
        hit = self.store.list_events(0, T0 + 10_000, global_ref="gp_aaa")
        self.assertEqual(["g1"], [e["event_id"] for e in hit])

    def test_listing_events_accepts_a_null_global_ref(self):
        # app.py passes global_ref= on EVERY call to /history/events, not only
        # filtered ones. A backend whose signature lacked the parameter raised
        # TypeError on every request; passing None must behave as "no filter".
        self.store.save_event(event("g1"))
        self.assertEqual(1, len(self.store.list_events(0, T0 + 10_000,
                                                       global_ref=None)))

    def test_a_merge_rebinds_stored_history(self):
        self.store.save_event(event("m1", global_ref="gp_drop"))
        self.store.save_event(event("m2", global_ref="gp_keep"))
        moved = self.store.rebind_global_ref("gp_drop", "gp_keep")
        self.assertEqual(1, moved)
        self.assertEqual(2, len(self.store.list_events(0, T0 + 10_000,
                                                       global_ref="gp_keep")))
        self.assertEqual(0, len(self.store.list_events(0, T0 + 10_000,
                                                       global_ref="gp_drop")))

    def test_rebinding_to_itself_is_a_no_op(self):
        self.store.save_event(event("m1", global_ref="gp_x"))
        self.assertEqual(0, self.store.rebind_global_ref("gp_x", "gp_x"))
        self.assertEqual(0, self.store.rebind_global_ref("", "gp_x"))

    # ---- physical areas ----------------------------------------------------
    def test_areas_round_trip(self):
        self.store.save_area({"area_id": "OFFICE-01", "name": "Office",
                              "area_type": "ROOM", "capacity_max": 12,
                              "area_sqm": 40.0, "site_id": "S"})
        got = self.store.list_areas()
        self.assertEqual(1, len(got))
        self.assertEqual("OFFICE-01", got[0]["area_id"])
        self.assertEqual(12, got[0]["capacity_max"])

    def test_saving_an_area_twice_updates_rather_than_duplicates(self):
        self.store.save_area({"area_id": "A", "name": "First"})
        self.store.save_area({"area_id": "A", "name": "Second"})
        got = self.store.list_areas()
        self.assertEqual(1, len(got))
        self.assertEqual("Second", got[0]["name"])

    def test_deleting_an_area_detaches_its_zones(self):
        # A zone left pointing at a deleted area drops out of every area total
        # while still looking mapped. Detached, it falls back to single-camera
        # behaviour, which is the honest state.
        self.store.save_area({"area_id": "A", "name": "Room"})
        self.store.save_zones("CAM-01", [{"zone_id": "ZONE-01",
                                          "physical_area_id": "A"}])
        self.assertTrue(self.store.delete_area("A"))
        self.assertIsNone(self.store.list_zones("CAM-01")[0].get("physical_area_id"))
        self.assertEqual([], self.store.list_areas())

    def test_deleting_an_unknown_area_is_false_not_an_exception(self):
        self.assertFalse(self.store.delete_area("nope"))

    def test_area_state_history_round_trips(self):
        for i, occ in enumerate((3, 5, 4)):
            self.store.save_area_state({"area_id": "A", "ts": T0 + i, "occupancy": occ,
                                        "capacity_pct": occ * 10.0, "density": 0.1,
                                        "summed_observations": occ + 1,
                                        "camera_count": 2, "site_id": "S"})
        got = self.store.area_state_range("A", 0, T0 + 100)
        self.assertEqual([3, 5, 4], [r["occupancy"] for r in got])
        self.assertEqual(2, got[0]["camera_count"])

    def test_area_state_is_filtered_by_area_and_window(self):
        self.store.save_area_state({"area_id": "A", "ts": T0, "occupancy": 1})
        self.store.save_area_state({"area_id": "B", "ts": T0, "occupancy": 9})
        self.assertEqual([1], [r["occupancy"] for r in
                               self.store.area_state_range("A", 0, T0 + 100)])
        self.assertEqual([], self.store.area_state_range("A", T0 + 50, T0 + 100))

    # ---- zone_live carries who, not just how many -------------------------
    def test_occupants_and_area_survive_a_zone_state_write(self):
        self.store.save_zone_state(dict(state(T0, 2),
                                        occupants=["gp_a", "gp_b"],
                                        physical_area_id="OFFICE-01"))
        live = self.store.latest_zone_states()[0]
        self.assertEqual(["gp_a", "gp_b"], live["occupants"])
        self.assertEqual("OFFICE-01", live["physical_area_id"])

    def test_no_occupants_reported_stays_none_not_empty(self):
        # "nobody is here" and "this worker does not tell us who" are different
        # claims, and the area layer treats them differently: a count with no
        # identities cannot be de-duplicated against another camera's.
        self.store.save_zone_state(state(T0, 2))
        self.assertIsNone(self.store.latest_zone_states()[0].get("occupants"))

    def test_an_empty_occupant_list_is_preserved_as_empty(self):
        self.store.save_zone_state(dict(state(T0, 0), occupants=[]))
        self.assertEqual([], self.store.latest_zone_states()[0]["occupants"])

    # ---- facility roster persistence --------------------------------------
    def test_presence_round_trips(self):
        people = [{"ref": "gp_1", "admitted_at": T0, "last_seen": T0 + 5,
                   "entry_zone": "IN", "last_zone": "LOBBY", "sightings": 3}]
        doors = [{"door_zone_id": "IN", "entries": 7, "exits": 2}]
        self.store.save_presence(people, {"admitted": 7, "baseline": 4}, doors)
        back, stats, back_doors = self.store.load_presence()
        self.assertEqual(1, len(back))
        self.assertEqual("gp_1", back[0]["ref"])
        self.assertEqual(3, back[0]["sightings"])
        self.assertEqual(7, stats["admitted"])
        self.assertEqual(4, stats["baseline"], "the declared opening count")
        self.assertEqual(7, back_doors[0]["entries"])

    def test_saving_presence_replaces_rather_than_merges(self):
        # The roster is a SET and a person's absence from it is as meaningful as
        # their presence. Merging would leave a discharged person in the table
        # for ever, which is the one error this table must not make.
        self.store.save_presence([{"ref": "a", "admitted_at": T0, "last_seen": T0}],
                                 {}, [])
        self.store.save_presence([{"ref": "b", "admitted_at": T0, "last_seen": T0}],
                                 {}, [])
        back, _, _ = self.store.load_presence()
        self.assertEqual(["b"], [p["ref"] for p in back])

    def test_an_emptied_roster_persists_as_empty(self):
        self.store.save_presence([{"ref": "a", "admitted_at": T0, "last_seen": T0}],
                                 {}, [])
        self.store.save_presence([], {}, [])
        back, _, _ = self.store.load_presence()
        self.assertEqual([], back)

    def test_door_tallies_accumulate_across_saves(self):
        self.store.save_presence([], {}, [{"door_zone_id": "IN", "entries": 1,
                                           "exits": 0}])
        self.store.save_presence([], {}, [{"door_zone_id": "IN", "entries": 9,
                                           "exits": 4}])
        _, _, doors = self.store.load_presence()
        self.assertEqual(1, len(doors))
        self.assertEqual(9, doors[0]["entries"])

    # ---- camera detection quality -----------------------------------------
    def test_detection_quality_reaches_the_store(self):
        self.store.record_camera_health("CAM-01", {
            "state": "OK", "tracking_quality": "STRAINED",
            "counts_reliable": False, "counting_mode": "track_degraded",
            "mean_confidence": 0.65, "track_churn_per_min": 3.84,
            "detector_saturation": 0.0}, T0)
        cam = [c for c in self.store.list_cameras() if c["camera_id"] == "CAM-01"][0]
        self.assertEqual("STRAINED", cam["tracking_quality"])
        self.assertIs(False, cam["counts_reliable"])
        self.assertEqual("track_degraded", cam["counting_mode"])
        self.assertAlmostEqual(3.84, cam["track_churn_per_min"], places=2)

    def test_unreported_reliability_stays_unknown_not_false(self):
        # Tri-state. A worker running older code sends nothing, and a missing
        # value must read as "not reported", never as "reliable" or "unreliable".
        self.store.record_camera_health("CAM-01", {"state": "OK"}, T0)
        cam = [c for c in self.store.list_cameras() if c["camera_id"] == "CAM-01"][0]
        self.assertIsNone(cam.get("counts_reliable"))



class TestInMemoryStore(StoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class TestPostgresStore(StoreContract, unittest.TestCase):
    """Runs against a real server on a scratch schema.

    Each test gets its own empty schema rather than its own database: creating
    a database per test is slow and cannot be done inside a transaction, while
    a schema is instant and search_path makes it invisible to the others.
    """

    _counter = 0

    def make_store(self):
        from services.api.postgres_store import PostgresStore

        TestPostgresStore._counter += 1
        schema = f"conf_{os.getpid()}_{TestPostgresStore._counter}"

        import psycopg
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.execute(f'CREATE SCHEMA "{schema}"')

        # options=-c search_path=... puts every statement in this test's schema
        # without any change to the store's SQL.
        sep = "&" if "?" in PG_DSN else "?"
        dsn = f"{PG_DSN}{sep}options=-c%20search_path%3D{schema}"
        store = PostgresStore(dsn, min_size=1, max_size=2)

        def cleanup():
            store.close()
            with psycopg.connect(PG_DSN, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

        self.addCleanup(cleanup)
        return store


if __name__ == "__main__":
    unittest.main()
