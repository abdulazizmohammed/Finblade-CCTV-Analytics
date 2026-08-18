"""Rebuilding a person's path when cross-camera identity did NOT hold.

The premise these test: one person walking the building picks up several
global_refs, so querying by identity returns a fragment of the journey and gives
no sign the rest exists. v_journey_fragments / _links / _traces reconstruct the
path from time and surveyed walk times instead.

The fixtures below therefore write events with DELIBERATELY DIFFERENT global
refs for what is one person. A test that gave them the same ref would pass
without exercising anything — identity would already have done the work.

Needs a real Postgres, like the other view tests. Each test gets a scratch
schema built from services/api/ddl_pg.sql.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import pgfixture

from services.api.analytics_views import (SAFE_COLUMNS, create_all,
                                          view_definitions)
from scripts.sync_topology import rows_for
from finblade.topology import CameraTopology

T0 = 1_700_000_000.0


class _Conn:
    """? -> %s, and no interpolation when there are no parameters."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        return self._raw.execute(sql, params) if params else self._raw.execute(sql)


@pgfixture.skip_without_pg
class Base(unittest.TestCase):
    # CAM-01 -> CAM-02 is a surveyed 30-90s walk. CAM-03 overlaps CAM-01.
    TOPOLOGY = {
        "overlapping_pairs": [{"a": "CAM-01", "b": "CAM-03"}],
        "transits": [
            {"a": "CAM-01", "b": "CAM-02", "min_seconds": 30, "max_seconds": 90},
            {"a": "CAM-02", "b": "CAM-04", "min_seconds": 20, "max_seconds": 60},
        ],
        "default_transit": {"min_seconds": 0, "max_seconds": 120},
        "overlap_tolerance_seconds": 5.0,
        "allow_unknown_pairs": True,
    }
    CAMERAS = ("CAM-01", "CAM-02", "CAM-03", "CAM-04")

    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row

        self._store, self._teardown = pgfixture.make_store("journey")
        self._raw = psycopg.connect(self._store.dsn, autocommit=True,
                                    row_factory=dict_row)
        self.conn = _Conn(self._raw)
        self._eid = 0

        for cam in self.CAMERAS:
            self.conn.execute(
                "INSERT INTO cameras(camera_id, site_id) VALUES (?, 'SITE-01')",
                (cam,))
        self.sync_topology()

    def tearDown(self):
        try:
            self._raw.close()
        finally:
            self._teardown()

    def sync_topology(self, cfg=None):
        """Populate camera_transits the way scripts/sync_topology.py does.

        Calls the script's own rows_for(), so a change to the projection rules
        breaks these tests rather than quietly diverging from what deploys.
        """
        topo = CameraTopology.from_dict(cfg if cfg is not None else self.TOPOLOGY)
        self.conn.execute("DELETE FROM camera_transits")
        for row in rows_for(topo, self.CAMERAS, T0):
            self.conn.execute(
                "INSERT INTO camera_transits(from_camera, to_camera, "
                "min_seconds, max_seconds, pair_kind, updated_at) "
                "VALUES (?,?,?,?,?,?)", row)

    def seen(self, camera, ts, zone, ref=None, event_type="ZONE_ENTRY"):
        """One person-bearing event. `ref` is the global_ref — pass different
        values for one person to simulate a ReID split, which is the point."""
        self._eid += 1
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, site_id, "
            "zone_id, zone_to, person_ref, global_ref, ts) "
            "VALUES (?,?,?,'SITE-01',?,?,?,?,?)",
            (f"ev-{self._eid:04d}", event_type, camera,
             zone if event_type != "ZONE_TRANSITION" else None,
             zone, f"pr_{self._eid:016x}", ref, ts))

    def build(self, **kw):
        return create_all(self.conn, **kw)

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()


class TestFragments(Base):
    def test_one_appearance_on_one_camera_is_one_fragment(self):
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_aaa")
        self.seen("CAM-01", T0 + 5, "ZONE-B", ref="gp_aaa")
        self.seen("CAM-01", T0 + 9, "ZONE-B", ref="gp_aaa")
        self.build()
        got = self.rows("SELECT * FROM v_journey_fragments")
        self.assertEqual(1, len(got))
        self.assertEqual(3, got[0]["event_count"])
        self.assertEqual("ZONE-A", got[0]["entry_zone"])
        self.assertEqual("ZONE-B", got[0]["exit_zone"])
        self.assertAlmostEqual(9.0, got[0]["duration_seconds"])

    def test_a_long_absence_starts_a_new_fragment(self):
        """A global_ref outlives 300s of absence, so without this split one
        fragment would span a gap the person was not there for — and claim an
        exit zone from before it."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_aaa")
        self.seen("CAM-01", T0 + 240, "ZONE-A", ref="gp_aaa")
        self.build(fragment_gap=60.0)
        self.assertEqual(2, len(self.rows("SELECT * FROM v_journey_fragments")))

    def test_a_short_dropout_does_not(self):
        """Occlusion and a missed frame or two are not an absence."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_aaa")
        self.seen("CAM-01", T0 + 40, "ZONE-A", ref="gp_aaa")
        self.build(fragment_gap=60.0)
        self.assertEqual(1, len(self.rows("SELECT * FROM v_journey_fragments")))

    def test_the_same_person_on_two_cameras_is_two_fragments(self):
        """Even when ReID DID link them. A fragment is per-camera by
        definition; linking is the next view's job."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_same")
        self.seen("CAM-02", T0 + 50, "ZONE-C", ref="gp_same")
        self.build()
        got = self.rows("SELECT camera_id FROM v_journey_fragments "
                        "ORDER BY camera_id")
        self.assertEqual(["CAM-01", "CAM-02"], [r["camera_id"] for r in got])

    def test_an_unresolved_track_still_makes_a_fragment(self):
        """global_ref NULL is the normal case when ReID never fired. The
        person_key fallback keeps them separable per camera."""
        self.seen("CAM-01", T0, "ZONE-A", ref=None)
        self.seen("CAM-01", T0 + 3, "ZONE-A", ref=None)
        self.build()
        got = self.rows("SELECT identity_resolved, person_key "
                        "FROM v_journey_fragments")
        # Two distinct person_refs, no global_ref: two people as far as
        # anything here can tell. Over-counting, which is the house bias.
        self.assertEqual(2, len(got))
        self.assertEqual([False, False], [r["identity_resolved"] for r in got])

    def test_events_without_a_person_are_excluded(self):
        """A heartbeat would extend a fragment past the last real sighting."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_aaa")
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, ts) "
            "VALUES ('hb-1','CAMERA_HEARTBEAT','CAM-01',?)", (T0 + 30,))
        self.build()
        got = self.rows("SELECT event_count FROM v_journey_fragments")
        self.assertEqual(1, len(got))
        self.assertEqual(1, got[0]["event_count"])


class TestLinks(Base):
    def walk(self, ref1="gp_one", ref2="gp_two"):
        """One person, CAM-01 then CAM-02 sixty seconds later — inside the
        surveyed 30-90s window. Two DIFFERENT refs: ReID split them."""
        self.seen("CAM-01", T0, "ZONE-A", ref=ref1)
        self.seen("CAM-01", T0 + 10, "ZONE-A", ref=ref1)
        self.seen("CAM-02", T0 + 70, "ZONE-C", ref=ref2)
        self.seen("CAM-02", T0 + 80, "ZONE-C", ref=ref2)

    def test_a_feasible_hop_is_linked_despite_different_identities(self):
        """The whole point. global_ref says two people; physics says one."""
        self.walk()
        self.build()
        got = self.rows("SELECT * FROM v_journey_links")
        self.assertEqual(1, len(got))
        self.assertEqual("CAM-01", got[0]["from_camera"])
        self.assertEqual("CAM-02", got[0]["to_camera"])
        self.assertAlmostEqual(60.0, got[0]["gap_seconds"])
        self.assertTrue(got[0]["is_unique"])
        self.assertFalse(got[0]["identity_agrees"],
                         "the fixture must not have ReID already linking them")

    def test_too_fast_is_not_a_link(self):
        """Five seconds is not a 30-second walk, however alike they look."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_one")
        self.seen("CAM-02", T0 + 5, "ZONE-C", ref="gp_two")
        self.build()
        self.assertEqual([], self.rows("SELECT * FROM v_journey_links"))

    def test_too_slow_is_not_a_link(self):
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_one")
        self.seen("CAM-02", T0 + 600, "ZONE-C", ref="gp_two")
        self.build()
        self.assertEqual([], self.rows("SELECT * FROM v_journey_links"))

    def test_competition_is_counted_not_resolved(self):
        """Two people leave CAM-01 and two arrive at CAM-02 in the window. Every
        pairing is feasible and none is unique. Declining is the answer."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_a")
        self.seen("CAM-01", T0 + 2, "ZONE-A", ref="gp_b")
        self.seen("CAM-02", T0 + 60, "ZONE-C", ref="gp_c")
        self.seen("CAM-02", T0 + 62, "ZONE-C", ref="gp_d")
        self.build()
        crossing = self.rows(
            "SELECT successor_options, predecessor_options FROM v_journey_links "
            "WHERE from_camera <> to_camera")
        self.assertEqual(4, len(crossing), "2x2 pairings across the two cameras")
        unique = self.rows("SELECT * FROM v_journey_links WHERE is_unique")
        self.assertEqual([], unique, "nothing here is certain")

    def test_two_people_passing_one_camera_also_compete(self):
        """gp_a vanishes and gp_b appears two seconds later on CAM-01. That is
        either a tracking dropout or two people walking past — indistinguishable
        from here, so it counts as a candidate and drives everything to
        ambiguous. Adding candidates is the safe direction: it can only stop a
        chain forming, never invent one."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_a")
        self.seen("CAM-01", T0 + 2, "ZONE-A", ref="gp_b")
        self.build()
        got = self.rows("SELECT from_camera, to_camera, pair_kind, gap_seconds "
                        "FROM v_journey_links")
        self.assertEqual(1, len(got), "forwards only — time does not reverse")
        self.assertEqual("same_camera", got[0]["pair_kind"])
        self.assertAlmostEqual(2.0, got[0]["gap_seconds"])

    def test_confidence_tracks_how_busy_the_building_was(self):
        """One walker alone is certain; add a second and neither is. This is
        the property that makes the technique work after hours."""
        self.walk()
        self.build()
        self.assertTrue(self.rows(
            "SELECT is_unique FROM v_journey_links")[0]["is_unique"])

        self.seen("CAM-01", T0 + 11, "ZONE-A", ref="gp_other")
        self.assertEqual(
            [False, False],
            [r["is_unique"] for r in self.rows(
                "SELECT is_unique FROM v_journey_links "
                "WHERE to_camera = 'CAM-02'")])

    def test_overlapping_cameras_are_not_a_hop(self):
        """CAM-01 and CAM-03 watch the same floor. One person seen on both at
        once is one place. Linking them would insert a phantom step, and being
        feasible in both directions would form a cycle the trace then drops."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_one")
        self.seen("CAM-03", T0 + 1, "ZONE-A", ref="gp_two")
        self.build()
        self.assertEqual([], self.rows(
            "SELECT * FROM v_journey_links "
            "WHERE from_camera IN ('CAM-01','CAM-03') "
            "  AND to_camera IN ('CAM-01','CAM-03')"))

    def test_an_unsurveyed_pair_is_flagged_as_a_guess(self):
        """CAM-03 -> CAM-04 is in no transit list, so its window is invented.
        The hop is still offered — it just says the window is a fallback."""
        self.seen("CAM-03", T0, "ZONE-E", ref="gp_one")
        self.seen("CAM-04", T0 + 30, "ZONE-F", ref="gp_two")
        self.build()
        got = self.rows("SELECT pair_kind FROM v_journey_links")
        self.assertEqual(["default"], [r["pair_kind"] for r in got])

    def test_an_absent_transit_row_means_unreachable(self):
        """allow_unknown_pairs: false writes no row for an unsurveyed pair, and
        the join then drops the hop. No SQL knows this rule; the missing row
        IS the rule."""
        cfg = dict(self.TOPOLOGY, allow_unknown_pairs=False)
        self.sync_topology(cfg)
        self.seen("CAM-03", T0, "ZONE-E", ref="gp_one")
        self.seen("CAM-04", T0 + 30, "ZONE-F", ref="gp_two")
        self.build()
        self.assertEqual([], self.rows("SELECT * FROM v_journey_links"))

    def test_identity_agreement_is_reported_when_reid_did_work(self):
        self.walk(ref1="gp_same", ref2="gp_same")
        self.build()
        got = self.rows("SELECT identity_agrees, is_unique FROM v_journey_links")
        self.assertEqual(1, len(got))
        self.assertTrue(got[0]["identity_agrees"])

    def test_a_reacquisition_on_one_camera_can_link(self):
        """Same camera, two fragments split by a real absence. ByteTrack lost
        them; the link says it was probably the same walk."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_one")
        self.seen("CAM-01", T0 + 90, "ZONE-A", ref="gp_two")
        self.build(fragment_gap=60.0)
        got = self.rows("SELECT from_camera, to_camera, pair_kind "
                        "FROM v_journey_links")
        self.assertEqual(1, len(got))
        self.assertEqual("same_camera", got[0]["pair_kind"])


class TestTraces(Base):
    # A SURVEYED site, which is what CAM-01..CAM-06 is: every pair described,
    # and allow_unknown_pairs off so an unlisted pair means unreachable.
    #
    # This is not fixture tidying. With the permissive default of the class
    # above, CAM-01 -> CAM-04 is an unlisted pair and inherits a 0-120s window,
    # which a 110s three-hop walk fits — so CAM-01 gains a second feasible
    # successor and the chain never forms. Chaining is only as good as the
    # survey; see test_a_permissive_topology_cannot_chain below, which pins
    # exactly that.
    TOPOLOGY = {
        "overlapping_pairs": [{"a": "CAM-01", "b": "CAM-03"}],
        "transits": [
            {"a": "CAM-01", "b": "CAM-02", "min_seconds": 30, "max_seconds": 90},
            {"a": "CAM-02", "b": "CAM-04", "min_seconds": 20, "max_seconds": 60},
        ],
        "default_transit": {"min_seconds": 0, "max_seconds": 120},
        "overlap_tolerance_seconds": 5.0,
        "allow_unknown_pairs": False,
    }

    def three_hop_walk(self):
        """CAM-01 -> CAM-02 -> CAM-04, one person, three different refs."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_1")
        self.seen("CAM-01", T0 + 10, "ZONE-A", ref="gp_1")
        self.seen("CAM-02", T0 + 70, "ZONE-C", ref="gp_2")
        self.seen("CAM-02", T0 + 80, "ZONE-C", ref="gp_2")
        self.seen("CAM-04", T0 + 120, "ZONE-F", ref="gp_3")
        self.seen("CAM-04", T0 + 130, "ZONE-F", ref="gp_3")

    def test_a_split_journey_is_reassembled_end_to_end(self):
        """Three global_refs, one path. This is the deliverable."""
        self.three_hop_walk()
        self.build()
        got = self.rows("SELECT hop_no, camera_id, journey_hops "
                        "FROM v_journey_traces ORDER BY journey_id, hop_no")
        self.assertEqual([0, 1, 2], [r["hop_no"] for r in got])
        self.assertEqual(["CAM-01", "CAM-02", "CAM-04"],
                         [r["camera_id"] for r in got])
        self.assertEqual([3, 3, 3], [r["journey_hops"] for r in got])

    def test_the_journey_is_one_id_across_three_identities(self):
        self.three_hop_walk()
        self.build()
        got = self.rows("SELECT DISTINCT journey_id FROM v_journey_traces")
        self.assertEqual(1, len(got), "three refs must collapse to one journey")
        people = self.rows("SELECT DISTINCT person_key FROM v_journey_traces")
        self.assertEqual(3, len(people),
                         "and the underlying identities must still be three, "
                         "or the fixture is not testing a split")

    def test_start_and_end_span_the_whole_walk(self):
        self.three_hop_walk()
        self.build()
        r = self.rows("SELECT journey_start, journey_end FROM v_journey_traces "
                      "ORDER BY hop_no")[0]
        self.assertAlmostEqual(T0, r["journey_start"])
        self.assertAlmostEqual(T0 + 130, r["journey_end"])

    def test_a_chain_stops_where_it_becomes_ambiguous(self):
        """Two candidates at the second hop, so the chain ends after one rather
        than guessing through it. Short and honest beats long and wrong."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_1")
        self.seen("CAM-02", T0 + 70, "ZONE-C", ref="gp_2")
        self.seen("CAM-02", T0 + 72, "ZONE-C", ref="gp_3")
        self.build()
        hops = self.rows("SELECT journey_id, COUNT(*) n FROM v_journey_traces "
                         "GROUP BY journey_id")
        self.assertEqual([1, 1, 1], sorted(r["n"] for r in hops),
                         "three isolated fragments, no chain")

    def test_an_isolated_fragment_is_its_own_journey(self):
        """It happened. We cannot say what came before or after, and saying
        nothing at all would hide the sighting."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_lone")
        self.build()
        got = self.rows("SELECT hop_no, journey_hops FROM v_journey_traces")
        self.assertEqual(1, len(got))
        self.assertEqual(0, got[0]["hop_no"])
        self.assertEqual(1, got[0]["journey_hops"])

    def test_every_fragment_appears_exactly_once(self):
        """The invariant. A fragment in two journeys means one person was in
        two places, and a fragment in none means a sighting silently vanished
        — the failure the recursion is most likely to produce."""
        self.three_hop_walk()
        self.seen("CAM-03", T0 + 500, "ZONE-E", ref="gp_far")
        self.build()
        frags = self.rows("SELECT fragment_id FROM v_journey_fragments")
        traced = self.rows("SELECT fragment_id, COUNT(*) n "
                           "FROM v_journey_traces GROUP BY fragment_id")
        self.assertEqual(len(frags), len(traced), "a fragment went missing")
        self.assertEqual([1] * len(traced), [r["n"] for r in traced],
                         "a fragment was traced into more than one journey")

    def test_the_shortcut_hop_is_reduced_away(self):
        """Why transitive reduction is not optional on a real site.

        Surveyed windows are wide — CAM-01 to CAM-05 is 17 to 240 seconds on
        the live topology — so the direct first-to-last hop of a three-hop walk
        is ALSO feasible. Left in, it gives CAM-01 two successors and a plainly
        single path chains into nothing. Being seen at the intermediate camera
        is what rules the shortcut out."""
        self.sync_topology(dict(self.TOPOLOGY, allow_unknown_pairs=True))
        self.three_hop_walk()
        self.build()
        direct = self.rows("SELECT * FROM v_journey_links "
                           "WHERE from_camera = 'CAM-01' AND to_camera = 'CAM-04'")
        self.assertEqual([], direct, "the shortcut survived the reduction")
        got = self.rows("SELECT camera_id FROM v_journey_traces "
                        "ORDER BY journey_id, hop_no")
        self.assertEqual(["CAM-01", "CAM-02", "CAM-04"],
                         [r["camera_id"] for r in got])

    def test_reduction_never_routes_through_a_reacquisition(self):
        """The merge this could cause, pinned.

        Two strangers pass CAM-01 two seconds apart. That IS a legitimate
        same-camera re-acquisition candidate, and if the reduction were allowed
        to treat it as a detour it would read A->B->C as explaining A->C, drop
        the true cross-camera hop, and chain two people into one journey. A
        re-acquisition is not a detour through anywhere."""
        self.seen("CAM-01", T0, "ZONE-A", ref="gp_a")
        self.seen("CAM-01", T0 + 2, "ZONE-A", ref="gp_b")
        self.seen("CAM-02", T0 + 70, "ZONE-C", ref="gp_c")
        self.build()
        surviving = {(r["from_camera"], r["to_camera"]) for r in self.rows(
            "SELECT from_camera, to_camera FROM v_journey_links")}
        self.assertIn(("CAM-01", "CAM-02"), surviving,
                      "the real hop was reduced away by a re-acquisition")
        self.assertEqual([], self.rows(
            "SELECT * FROM v_journey_links WHERE is_unique"),
            "two candidates for one arrival is ambiguous, not a chain")

    def test_no_journey_travels_backwards_in_time(self):
        self.three_hop_walk()
        self.build()
        got = self.rows("SELECT hop_no, appeared FROM v_journey_traces "
                        "ORDER BY journey_id, hop_no")
        stamps = [r["appeared"] for r in got]
        self.assertEqual(sorted(stamps), stamps)


class TestItCannotLeak(Base):
    """The journey views reach into events, which carry person_ref."""

    def test_no_journey_view_exposes_person_ref(self):
        for view in ("v_journey_fragments", "v_journey_links",
                     "v_journey_traces"):
            self.assertNotIn("person_ref", SAFE_COLUMNS[view],
                             f"{view} would grant the column that counts churn")

    def test_no_journey_view_touches_the_cameras_table(self):
        """cameras.source holds RTSP URLs with passwords. These views need the
        camera id, which every event already carries."""
        for name, sql in view_definitions():
            if name.startswith("v_journey"):
                self.assertNotIn("cameras", sql.replace("camera_transits", ""),
                                 f"{name} joins the cameras table")

    def test_the_allowlist_matches_what_the_views_actually_have(self):
        self.build()
        for view in ("v_journey_fragments", "v_journey_links",
                     "v_journey_traces"):
            real = {r["column_name"] for r in self.rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?", (view,))}
            self.assertTrue(real, f"{view} was not created")
            for col in SAFE_COLUMNS[view]:
                self.assertIn(col, real, f"{view} has no column {col}")


class TestTopologyProjection(unittest.TestCase):
    """rows_for() is pure, so this needs no server."""

    CAMS = ("CAM-01", "CAM-02", "CAM-03")

    def topo(self, **over):
        cfg = {
            "overlapping_pairs": [{"a": "CAM-01", "b": "CAM-03"}],
            "transits": [{"a": "CAM-01", "b": "CAM-02",
                          "min_seconds": 30, "max_seconds": 90}],
            "default_transit": {"min_seconds": 0, "max_seconds": 120},
            "overlap_tolerance_seconds": 5.0,
        }
        cfg.update(over)
        return CameraTopology.from_dict(cfg)

    def kinds(self, rows):
        return {(a, b): k for a, b, _lo, _hi, k, _t in rows}

    def test_both_directions_are_written(self):
        """The YAML pair is undirected; a join does not want to normalise it."""
        k = self.kinds(rows_for(self.topo(), self.CAMS, T0))
        self.assertEqual("surveyed", k[("CAM-01", "CAM-02")])
        self.assertEqual("surveyed", k[("CAM-02", "CAM-01")])

    def test_an_overlapping_pair_opens_before_zero(self):
        """Independent camera processes disagree about the clock, so a
        simultaneous sighting can report a slightly negative gap."""
        rows = {(a, b): (lo, hi) for a, b, lo, hi, _k, _t
                in rows_for(self.topo(), self.CAMS, T0)}
        lo, _hi = rows[("CAM-01", "CAM-03")]
        self.assertEqual(-5.0, lo)

    def test_same_camera_gets_a_reacquisition_window(self):
        k = self.kinds(rows_for(self.topo(), self.CAMS, T0))
        self.assertEqual("same_camera", k[("CAM-01", "CAM-01")])

    def test_unsurveyed_pairs_default_when_permitted(self):
        k = self.kinds(rows_for(self.topo(), self.CAMS, T0))
        self.assertEqual("default", k[("CAM-02", "CAM-03")])

    def test_unsurveyed_pairs_vanish_when_not(self):
        """No row, not a wide window. On a surveyed site an unlisted pair means
        unreachable, and the join says so by finding nothing."""
        rows = rows_for(self.topo(allow_unknown_pairs=False), self.CAMS, T0)
        k = self.kinds(rows)
        self.assertNotIn(("CAM-02", "CAM-03"), k)
        self.assertIn(("CAM-01", "CAM-02"), k, "surveyed pairs must survive")
        self.assertIn(("CAM-01", "CAM-01"), k, "so must re-acquisition")

    def test_every_pair_is_covered_by_default(self):
        rows = rows_for(self.topo(), self.CAMS, T0)
        self.assertEqual(len(self.CAMS) ** 2, len(rows))


if __name__ == "__main__":
    unittest.main()
