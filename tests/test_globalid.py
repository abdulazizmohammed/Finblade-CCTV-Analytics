import unittest

from finblade.appearance import TrackFeatureBank
from finblade.globalid import GlobalIdentityRegistry
from finblade.topology import CameraTopology

# Synthetic embeddings. Unit vectors in 3-d so every cosine below is readable
# by hand; the real model emits 512-d but the matching maths is identical.
PERSON_A = [1.0, 0.0, 0.0]
PERSON_A_ALT = [0.98, 0.199, 0.0]     # same person, another view: cos ~0.98
PERSON_B = [0.0, 1.0, 0.0]            # a different person: cos 0.0
LOOKALIKE = [0.99, 0.1411, 0.0]       # cos ~0.99 to PERSON_A — a uniform
BETWEEN = [0.9962, 0.0872, 0.0]       # nearly equidistant from A and LOOKALIKE


def bank(*vectors, capacity=5):
    b = TrackFeatureBank(capacity=capacity)
    for v in vectors:
        b.add(v)
    return b


def walk_topology():
    """Non-overlapping pair: 10-90s walk between CAM-A and CAM-B."""
    return CameraTopology(transits={("CAM-A", "CAM-B"): (10.0, 90.0)},
                          allow_unknown_pairs=False)


class TestIdentityCreation(unittest.TestCase):
    def test_first_track_creates_an_identity(self):
        r = GlobalIdentityRegistry(topology=walk_topology())
        res = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        self.assertFalse(res.matched)
        self.assertEqual(res.reason, "no_candidates")
        self.assertEqual(len(r), 1)

    def test_binding_is_sticky(self):
        r = GlobalIdentityRegistry(topology=walk_topology())
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        again = r.resolve("CAM-A", 1, bank(PERSON_A), now=101.0)
        self.assertEqual(again.global_ref, first.global_ref)
        self.assertEqual(again.reason, "existing_binding")
        self.assertEqual(len(r), 1)

    def test_ref_is_anonymous(self):
        r = GlobalIdentityRegistry()
        ref = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        self.assertTrue(ref.startswith("gp_"))
        body = ref[3:]
        self.assertEqual(len(body), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in body))
        # Nothing traceable to the camera the person was seen on.
        self.assertNotIn("CAM", ref)

    def test_refs_differ_across_sessions(self):
        a = GlobalIdentityRegistry(session_salt="salt-one")
        b = GlobalIdentityRegistry(session_salt="salt-two")
        ref_a = a.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        ref_b = b.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        self.assertNotEqual(ref_a, ref_b)


class TestCrossCameraMatching(unittest.TestCase):
    def test_same_person_matches_after_a_plausible_walk(self):
        r = GlobalIdentityRegistry(topology=walk_topology())
        first = r.resolve("CAM-A", 1, bank(PERSON_A, PERSON_A_ALT), now=100.0)
        r.release("CAM-A", 1)
        second = r.resolve("CAM-B", 7, bank(PERSON_A_ALT), now=140.0)  # 40s walk
        self.assertTrue(second.matched)
        self.assertEqual(second.global_ref, first.global_ref)
        self.assertEqual(second.reason, "appearance_match")
        self.assertEqual(len(r), 1)

    def test_physics_beats_appearance(self):
        # THE case that makes cross-camera ReID trustworthy: an identical-looking
        # person appearing at the far camera 2s later cannot be the same human.
        r = GlobalIdentityRegistry(topology=walk_topology())
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        second = r.resolve("CAM-B", 7, bank(PERSON_A), now=102.0)
        self.assertFalse(second.matched)
        self.assertNotEqual(second.global_ref, first.global_ref)
        self.assertEqual(second.reason, "no_candidates")   # gated before scoring
        self.assertEqual(r.stats["rejected_topology"], 1)

    def test_person_arriving_too_late_is_a_new_visit(self):
        r = GlobalIdentityRegistry(topology=walk_topology())
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        second = r.resolve("CAM-B", 7, bank(PERSON_A), now=100.0 + 200.0)
        self.assertFalse(second.matched)

    def test_different_person_does_not_match(self):
        r = GlobalIdentityRegistry(topology=walk_topology())
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        second = r.resolve("CAM-B", 7, bank(PERSON_B), now=140.0)
        self.assertFalse(second.matched)
        self.assertNotEqual(second.global_ref, first.global_ref)
        self.assertEqual(second.reason, "below_threshold")


class TestAmbiguity(unittest.TestCase):
    def test_ambiguous_candidates_split_rather_than_guess(self):
        # Two people in matching uniforms already known on CAM-A. A third
        # arrives at CAM-B looking like both. Guessing would attach a stranger's
        # movements to someone else's ref; splitting is the safe error.
        r = GlobalIdentityRegistry(topology=walk_topology(),
                                   threshold=0.62, margin=0.06)
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-A", 2, bank(LOOKALIKE), now=100.0)
        r.release("CAM-A", 1)
        r.release("CAM-A", 2)

        res = r.resolve("CAM-B", 9, bank(BETWEEN), now=140.0)
        self.assertFalse(res.matched)
        self.assertEqual(res.reason, "ambiguous_margin")
        self.assertEqual(res.candidates, 2)
        self.assertLess(res.score - res.runner_up, 0.06)
        self.assertEqual(r.stats["rejected_margin"], 1)
        self.assertEqual(len(r), 3)

    def test_clear_winner_is_accepted(self):
        # Same setup but the second candidate is plainly a different person,
        # so the margin is wide and matching proceeds.
        r = GlobalIdentityRegistry(topology=walk_topology(),
                                   threshold=0.62, margin=0.06)
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-A", 2, bank(PERSON_B), now=100.0)
        r.release("CAM-A", 1)
        r.release("CAM-A", 2)

        res = r.resolve("CAM-B", 9, bank(PERSON_A_ALT), now=140.0)
        self.assertTrue(res.matched)
        self.assertEqual(res.global_ref, first.global_ref)
        self.assertGreaterEqual(res.score - res.runner_up, 0.06)

    def test_single_candidate_bypasses_margin(self):
        r = GlobalIdentityRegistry(topology=walk_topology(), margin=0.9)
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        res = r.resolve("CAM-B", 9, bank(PERSON_A_ALT), now=140.0)
        self.assertTrue(res.matched)
        self.assertEqual(res.global_ref, first.global_ref)


class TestSameCameraConflict(unittest.TestCase):
    def test_two_live_tracks_on_one_camera_stay_separate(self):
        # One person cannot be in two places on the same camera at once, however
        # similar the crops. Prevents a bad merge collapsing two people.
        r = GlobalIdentityRegistry(topology=CameraTopology())
        a = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        b = r.resolve("CAM-A", 2, bank(PERSON_A), now=100.0)
        self.assertNotEqual(a.global_ref, b.global_ref)
        self.assertEqual(len(r), 2)

    def test_reappearance_on_same_camera_can_rematch(self):
        # After the first track ends, an ID break on one camera should re-link.
        r = GlobalIdentityRegistry(topology=CameraTopology())
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        second = r.resolve("CAM-A", 55, bank(PERSON_A_ALT), now=110.0)
        self.assertTrue(second.matched)
        self.assertEqual(second.global_ref, first.global_ref)


class TestExpiryAndEviction(unittest.TestCase):
    def test_ttl_expiry_removes_identity_and_clears_vectors(self):
        r = GlobalIdentityRegistry(topology=CameraTopology(), ttl_seconds=60.0)
        ref = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        ident = r.get(ref)
        r.release("CAM-A", 1)
        r.expire(now=200.0)
        self.assertIsNone(r.get(ref))
        # Privacy: the biometric template is actually gone, not just unreferenced.
        self.assertEqual(ident.bank.vectors, [])
        self.assertEqual(r.stats["expired"], 1)

    def test_expiry_spares_active_identities(self):
        r = GlobalIdentityRegistry(topology=CameraTopology(), ttl_seconds=1.0)
        ref = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        r.expire(now=500.0)          # still bound to a live track
        self.assertIsNotNone(r.get(ref))

    def test_gallery_cap_evicts_oldest_inactive(self):
        r = GlobalIdentityRegistry(topology=CameraTopology(), max_identities=2,
                                   ttl_seconds=10_000.0)
        refs = []
        for i in range(3):
            # Distinct-enough vectors so none of them match each other.
            v = [0.0] * 3
            v[i % 3] = 1.0
            refs.append(r.resolve(f"CAM-{i}", i, bank(v), now=100.0 + i).global_ref)
            r.release(f"CAM-{i}", i)
        self.assertLessEqual(len(r), 2)
        self.assertIsNone(r.get(refs[0]))     # oldest went first


class TestMerge(unittest.TestCase):
    def test_merge_folds_history(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        a = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0, zone_id="Z1").global_ref
        r.release("CAM-A", 1)
        b = r.resolve("CAM-B", 2, bank(PERSON_B), now=200.0, zone_id="Z2").global_ref

        self.assertTrue(r.merge(a, b))
        self.assertIsNone(r.get(b))
        kept = r.get(a)
        self.assertEqual(kept.first_seen, 100.0)
        self.assertEqual(kept.last_seen, 200.0)
        self.assertEqual(sorted(kept.cameras_seen), ["CAM-A", "CAM-B"])
        self.assertEqual([z["zone_id"] for z in kept.journey()], ["Z1", "Z2"])
        # The live binding follows the surviving ref.
        self.assertEqual(r.ref_for("CAM-B", 2), a)

    def test_merge_rejects_bad_input(self):
        r = GlobalIdentityRegistry()
        ref = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        self.assertFalse(r.merge(ref, ref))
        self.assertFalse(r.merge(ref, "gp_doesnotexist00"))


class TestSiteMetrics(unittest.TestCase):
    def test_overlapping_cameras_do_not_double_count(self):
        # The counting payoff: one person standing in the overlap is two local
        # tracks. Summing per-camera occupancy would say 2 people; it is 1.
        topo = CameraTopology(overlapping=[("CAM-A", "CAM-B")])
        r = GlobalIdentityRegistry(topology=topo)
        first = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        second = r.resolve("CAM-B", 4, bank(PERSON_A_ALT), now=100.0)
        self.assertTrue(second.matched)
        self.assertEqual(second.global_ref, first.global_ref)
        self.assertEqual(r.site_occupancy(), 1)

    def test_distinct_people_are_counted_separately(self):
        topo = CameraTopology(overlapping=[("CAM-A", "CAM-B")])
        r = GlobalIdentityRegistry(topology=topo)
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-B", 4, bank(PERSON_B), now=100.0)
        self.assertEqual(r.site_occupancy(), 2)

    def test_cross_camera_refs_lists_only_multi_camera_identities(self):
        topo = CameraTopology(overlapping=[("CAM-A", "CAM-B")])
        r = GlobalIdentityRegistry(topology=topo)
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-B", 4, bank(PERSON_A_ALT), now=100.0)
        r.resolve("CAM-A", 2, bank(PERSON_B), now=100.0)
        self.assertEqual(len(r.cross_camera_refs()), 1)


class TestUniqueCountingWithoutZones(unittest.TestCase):
    """Counting people must not depend on zone polygons being defined.

    Every resolve() below passes zone_id=None — the no-zones case.
    """

    def test_unique_total_counts_distinct_people(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-A", 2, bank(PERSON_B), now=100.0)
        self.assertEqual(r.unique_total(), 2)

    def test_same_person_on_two_cameras_counts_once(self):
        topo = CameraTopology(overlapping=[("CAM-A", "CAM-B")])
        r = GlobalIdentityRegistry(topology=topo)
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-B", 9, bank(PERSON_A_ALT), now=100.0)
        # Two local tracks, two cameras, ONE person.
        self.assertEqual(r.unique_total(), 1)
        self.assertEqual(r.site_occupancy(), 1)
        # Per-camera tallies each see them, so they sum to 2 — the gap versus
        # unique_total is exactly the double-count that was avoided.
        self.assertEqual(r.unique_by_camera(), {"CAM-A": 1, "CAM-B": 1})

    def test_unique_total_survives_the_person_leaving(self):
        # Footfall must not fall when someone walks out and the TTL expires.
        r = GlobalIdentityRegistry(topology=CameraTopology(), ttl_seconds=10.0)
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        r.expire(now=500.0)
        self.assertEqual(len(r), 0)              # gallery emptied
        self.assertEqual(r.site_occupancy(), 0)  # nobody here now
        self.assertEqual(r.unique_total(), 1)    # but one person WAS here

    def test_live_counts_drop_when_tracks_end(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.resolve("CAM-A", 2, bank(PERSON_B), now=100.0)
        self.assertEqual(r.live_by_camera(), {"CAM-A": 2})
        r.release("CAM-A", 1)
        self.assertEqual(r.live_by_camera(), {"CAM-A": 1})
        self.assertEqual(r.unique_total(), 2)    # cumulative unchanged

    def test_reappearing_person_is_not_counted_twice(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        r.release("CAM-A", 1)
        # Same person returns under a new ByteTrack id.
        res = r.resolve("CAM-A", 77, bank(PERSON_A_ALT), now=130.0)
        self.assertTrue(res.matched)
        self.assertEqual(r.unique_total(), 1)

    def test_merge_corrects_the_cumulative_tally(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        a = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0).global_ref
        b = r.resolve("CAM-B", 2, bank(PERSON_B), now=100.0).global_ref
        self.assertEqual(r.unique_total(), 2)
        r.merge(a, b)                            # they were one person after all
        self.assertEqual(r.unique_total(), 1)

    def test_snapshot_exposes_the_counts(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        snap = r.snapshot()
        self.assertEqual(snap["unique_total"], 1)
        self.assertEqual(snap["unique_by_camera"], {"CAM-A": 1})
        self.assertEqual(snap["live_by_camera"], {"CAM-A": 1})


class TestJourneyAndSummary(unittest.TestCase):
    def test_journey_records_zone_sequence(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        ref = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0, zone_id="Z1").global_ref
        r.resolve("CAM-A", 1, bank(PERSON_A), now=101.0, zone_id="Z1")   # no dup
        r.resolve("CAM-A", 1, bank(PERSON_A), now=102.0, zone_id="Z2")
        journey = r.get(ref).journey()
        self.assertEqual([j["zone_id"] for j in journey], ["Z1", "Z2"])

    def test_summary_contains_no_embedding(self):
        # What gets persisted must never carry a biometric template.
        r = GlobalIdentityRegistry(topology=CameraTopology())
        ref = r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0, zone_id="Z1").global_ref
        summary = r.get(ref).summary()
        flat = repr(summary)
        self.assertNotIn("vectors", flat)
        self.assertNotIn("0.98", flat)
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["camera_count"], 1)

    def test_snapshot_reports_counters(self):
        r = GlobalIdentityRegistry(topology=CameraTopology())
        r.resolve("CAM-A", 1, bank(PERSON_A), now=100.0)
        snap = r.snapshot()
        self.assertEqual(snap["identities"], 1)
        self.assertEqual(snap["active_bindings"], 1)
        self.assertEqual(snap["stats"]["created"], 1)


class TestConstructorValidation(unittest.TestCase):
    def test_invalid_threshold(self):
        for bad in (0.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                GlobalIdentityRegistry(threshold=bad)

    def test_invalid_margin(self):
        with self.assertRaises(ValueError):
            GlobalIdentityRegistry(margin=-0.1)


if __name__ == "__main__":
    unittest.main()
