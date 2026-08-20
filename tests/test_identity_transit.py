"""Staying the same person through a lift, and never being two people at once.

Two defects, both found by reviewing the identity layer against the intended
design rather than by a failing test:

  1. RETENTION AND THE TRANSIT WINDOWS WERE COUPLED, IN DIFFERENT FILES. How
     long a record is kept has to be at least as long as the slowest journey the
     gate will accept, or the record is deleted before the arrival can be
     scored — not rejected, not out-scored, absent, with no counter recording
     that anything happened.

     TO BE ACCURATE ABOUT SEVERITY: on the config as it stands this was NOT
     firing. The widest surveyed window is 270s and the flat TTL was 300s, so
     the gate refused a slow arrival before expiry could lose it. The defect was
     that nothing enforced that ordering. Widen one window in
     config/topology.yaml past 300s — a slow lift, a longer route — and matches
     begin failing silently, in a file that says nothing about retention.
     Retention is now derived from the topology, so the two cannot disagree.

  2. SIMULTANEOUS PRESENCE WAS ONLY IMPLICIT ACROSS CAMERAS. Two live tracks on
     one camera were refused explicitly, but two live tracks on DIFFERENT
     cameras were only refused because dt near zero fails the transit minimum.
     That reasoning holds only while the minimum is above zero. With
     allow_unknown_pairs the fallback minimum is zero, so any camera missing
     from the topology silently lost the rule.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.appearance import TrackFeatureBank
from finblade.globalid import GlobalIdentityRegistry
from finblade.topology import CameraTopology


def bank(*vectors):
    b = TrackFeatureBank(capacity=5)
    for v in vectors:
        b.add(list(v))
    return b


PERSON = (1.0, 0.02, 0.0)
PERSON_AGAIN = (1.0, 0.03, 0.0)
STRANGER = (0.0, 1.0, 0.0)

# The real site, reduced to the two facts that matter: a lift between the
# ground floor and the second that takes up to 270s, and a pair that overlaps.
SITE = CameraTopology(
    overlapping=[("CAM-03", "CAM-04")],
    transits={("CAM-03", "CAM-05"): (27.0, 270.0),
              ("CAM-01", "CAM-02"): (10.0, 150.0)},
    allow_unknown_pairs=False,
)


class TestRetentionCoversTheJourney(unittest.TestCase):
    def test_retention_outlasts_the_slowest_route_from_that_camera(self):
        r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        # 270s lift x 1.5 grace = 405s, which is longer than the flat TTL.
        self.assertEqual(405.0, r.retention_for("CAM-03"))
        # CAM-01's slowest route is the 150s walk, under the TTL, so the TTL wins.
        self.assertEqual(300.0, r.retention_for("CAM-01"))

    def test_someone_in_a_slow_lift_outlives_the_flat_ttl(self):
        """A window WIDER than the TTL — the case the old code got wrong.

        Enters the lift on CAM-03, invisible to every camera, and steps out on
        CAM-05 after 450s. The gate accepts that: the window is 600s. The flat
        300s TTL did not, so the record was already deleted and the arrival
        became a new visitor. Retention now follows the window."""
        slow = CameraTopology(transits={("CAM-03", "CAM-05"): (27.0, 600.0)},
                              allow_unknown_pairs=False)
        r = GlobalIdentityRegistry(topology=slow, ttl_seconds=300.0)
        self.assertEqual(900.0, r.retention_for("CAM-03"))

        first = r.resolve("CAM-03", 18, bank(PERSON, PERSON), now=1000.0)
        r.release("CAM-03", 18)

        r.expire(now=1450.0)
        self.assertIsNotNone(r.get(first.global_ref),
                             "deleted mid-journey; the arrival can never match")

        second = r.resolve("CAM-05", 31, bank(PERSON_AGAIN, PERSON_AGAIN),
                           now=1450.0)
        self.assertEqual(first.global_ref, second.global_ref)
        self.assertTrue(second.matched)

    def test_retention_is_never_shorter_than_the_gate_will_accept(self):
        """The invariant, stated directly. Whatever anyone puts in
        config/topology.yaml, a record must outlive the slowest arrival the gate
        would say yes to — otherwise the match fails for a reason no counter
        reports and no log line mentions."""
        for widest in (60.0, 270.0, 600.0, 1200.0):
            topo = CameraTopology(transits={("A", "B"): (0.0, widest)},
                                  allow_unknown_pairs=False)
            r = GlobalIdentityRegistry(topology=topo, ttl_seconds=300.0,
                                       max_retention_seconds=100_000.0)
            self.assertGreaterEqual(
                r.retention_for("A"), widest,
                "a %ss journey outlives the record that has to match it" % widest)

    def test_retention_still_ends(self):
        """Not indefinite. Past the window they are a new visitor, which is the
        safe error — a wrong merge is the harmful one."""
        r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        ref = r.resolve("CAM-03", 18, bank(PERSON, PERSON), now=1000.0).global_ref
        r.release("CAM-03", 18)
        r.expire(now=1000.0 + 406.0)
        self.assertIsNone(r.get(ref))

    def test_retention_is_bounded_whatever_the_topology_claims(self):
        """A mistyped max_seconds must not stretch a privacy bound. Templates
        live in RAM and the ceiling is what says for how long."""
        silly = CameraTopology(transits={("A", "B"): (0.0, 86400.0)},
                               allow_unknown_pairs=False)
        r = GlobalIdentityRegistry(topology=silly, max_retention_seconds=1800.0)
        self.assertEqual(1800.0, r.retention_for("A"))

    def test_the_ttl_is_a_floor_not_a_suggestion(self):
        r = GlobalIdentityRegistry(
            topology=CameraTopology(allow_unknown_pairs=False), ttl_seconds=60.0)
        self.assertEqual(60.0, r.retention_for("CAM-NOWHERE"))


class TestUnobservedIsAState(unittest.TestCase):
    """A person between cameras is neither tracked nor gone, and until now the
    difference was not expressible — so nothing could report on it."""

    def setUp(self):
        self.r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        self.ref = self.r.resolve("CAM-03", 18, bank(PERSON, PERSON),
                                  now=1000.0).global_ref

    def test_on_a_camera_is_tracked(self):
        self.assertEqual("TRACKED", self.r.state_of(self.ref, 1000.0))
        self.assertEqual([], self.r.in_transit(1000.0))

    def test_between_cameras_is_unobserved(self):
        self.r.release("CAM-03", 18)
        self.assertEqual("UNOBSERVED", self.r.state_of(self.ref, 1200.0))
        self.assertEqual([self.ref], self.r.in_transit(1200.0))

    def test_past_retention_is_unknown(self):
        self.r.release("CAM-03", 18)
        self.assertEqual("UNKNOWN", self.r.state_of(self.ref, 1000.0 + 500.0))
        self.assertEqual([], self.r.in_transit(1000.0 + 500.0))

    def test_a_ref_nobody_ever_saw_is_unknown(self):
        self.assertEqual("UNKNOWN", self.r.state_of("gp_neverexisted", 1000.0))


class TestNoOneIsInTwoPlacesAtOnce(unittest.TestCase):
    def test_a_live_track_elsewhere_blocks_the_match(self):
        """Identical appearance, and still refused: the first person is visibly
        standing in front of CAM-01 at that instant."""
        r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        a = r.resolve("CAM-01", 1, bank(PERSON, PERSON), now=1000.0)
        b = r.resolve("CAM-02", 9, bank(PERSON, PERSON), now=1000.0)
        self.assertNotEqual(a.global_ref, b.global_ref)
        self.assertGreaterEqual(r.stats["rejected_simultaneous"], 1)

    def test_a_zero_minimum_buys_no_exclusion(self):
        """WHERE THE GUARANTEE COMES FROM, and where it does not.

        An earlier version excluded here too, reasoning that a camera missing
        from the topology should not be able to steal an identity from someone
        plainly standing in front of another camera. The intent was right; the
        evidence was not there to act on. A zero transit minimum is not a claim
        that two cameras are adjacent - it is the topology declining to claim
        anything, which is the default for every unsurveyed pair.

        Turning "unknown" into "impossible" refused real handovers. Measured on
        the recorded rig: rejected_simultaneous 7 against matched 7, blocking as
        many links as the matcher managed to make, because a track that has not
        been reaped yet still counts as live on the previous camera.

        So the rule now rests on the same evidence as the rest of the physics.
        Survey the pair and it bites (see the test above). Leave it unsurveyed
        and the system declines to invent a distance it was never told."""
        permissive = CameraTopology(allow_unknown_pairs=True,
                                    default_transit=(0.0, 120.0))
        r = GlobalIdentityRegistry(topology=permissive, ttl_seconds=300.0)
        a = r.resolve("CAM-01", 1, bank(PERSON, PERSON), now=1000.0)
        b = r.resolve("CAM-NEW", 2, bank(PERSON, PERSON), now=1000.0)
        self.assertEqual(a.global_ref, b.global_ref)
        self.assertEqual(0, r.stats["rejected_simultaneous"])

    def test_a_surveyed_minimum_does(self):
        """The same two sightings, on a pair somebody measured. Now the
        topology genuinely says they are apart, so simultaneous is refused."""
        surveyed = CameraTopology(transits={("CAM-01", "CAM-NEW"): (10.0, 200.0)},
                                  allow_unknown_pairs=True)
        r = GlobalIdentityRegistry(topology=surveyed, ttl_seconds=300.0)
        a = r.resolve("CAM-01", 1, bank(PERSON, PERSON), now=1000.0)
        b = r.resolve("CAM-NEW", 2, bank(PERSON, PERSON), now=1000.0)
        self.assertNotEqual(a.global_ref, b.global_ref)
        self.assertGreaterEqual(r.stats["rejected_simultaneous"], 1)

    def test_overlapping_cameras_are_exempt_because_both_can_see_them(self):
        """The exemption that makes overlap dedup work at all. CAM-03 and CAM-04
        watch the same floor, so simultaneous IS the expected case — refusing it
        would count one person in the shared area twice."""
        r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        a = r.resolve("CAM-03", 1, bank(PERSON, PERSON), now=1000.0)
        b = r.resolve("CAM-04", 2, bank(PERSON_AGAIN, PERSON_AGAIN), now=1000.2)
        self.assertEqual(a.global_ref, b.global_ref)
        self.assertEqual(1, r.site_occupancy())

    def test_a_released_track_is_not_a_conflict(self):
        """Gone from CAM-01 and later seen on CAM-02 is the normal walk, not a
        conflict. Only a LIVE binding excludes."""
        r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        a = r.resolve("CAM-01", 1, bank(PERSON, PERSON), now=1000.0)
        r.release("CAM-01", 1)
        b = r.resolve("CAM-02", 9, bank(PERSON_AGAIN, PERSON_AGAIN), now=1060.0)
        self.assertEqual(a.global_ref, b.global_ref)

    def test_a_genuine_new_visitor_still_gets_their_own_id(self):
        """The exclusion must not become a reason to refuse everybody. A
        stranger arriving is a new person, and that is the correct answer."""
        r = GlobalIdentityRegistry(topology=SITE, ttl_seconds=300.0)
        a = r.resolve("CAM-01", 1, bank(PERSON, PERSON), now=1000.0)
        b = r.resolve("CAM-01", 2, bank(STRANGER, STRANGER), now=1000.0)
        self.assertNotEqual(a.global_ref, b.global_ref)
        self.assertEqual(2, r.stats["created"])


if __name__ == "__main__":
    unittest.main()
