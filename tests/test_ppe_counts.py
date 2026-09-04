"""The height gate, and per-zone compliance counts for the UI.

WHY THE GATE EXISTS. Observed live on media/PPEVideo.mp4: tracks with no PPE
detections of any kind — because the people were small and distant — drifted to
NONCOMPLIANT on absence alone. The system was convicting workers for standing
far from the camera. absence_weight slows that fourfold; it does not stop it.

WHY THE COUNTS MATTER. A tile reading "2 without hardhats" that is really
counting "2 people too small to assess" is worse than no tile, because it looks
authoritative. So there are three buckets, not two, and the unassessable one is
reported rather than folded into either.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.ppe import (COMPLIANT, EV_ABSENT, EV_NEGATIVE, EV_POSITIVE,
                          HARDHAT, NONCOMPLIANT, PPEThresholds, PPETracker,
                          VEST)


def tracker(**kw):
    kw.setdefault("min_person_height_px", 120.0)
    return PPETracker("CAM-01", PPEThresholds(**kw))


def feed(tr, track, ppe, evidence, seconds, t0, conf=0.9, dt=0.5):
    t = t0
    while t < t0 + seconds:
        tr.observe(track, ppe, evidence, conf, t, dt)
        t += dt


# person boxes: (x1, y1, x2, y2)
TALL = (100.0, 100.0, 180.0, 400.0)     # 300px — assessable
SHORT = (100.0, 100.0, 120.0, 160.0)    # 60px  — too far away


class TestHeightGate(unittest.TestCase):
    def test_a_close_worker_is_assessable(self):
        self.assertTrue(tracker().assessable(TALL))

    def test_a_distant_worker_is_not(self):
        self.assertFalse(tracker().assessable(SHORT))

    def test_exactly_at_the_threshold_counts_as_assessable(self):
        box = (0.0, 0.0, 50.0, 120.0)          # exactly 120px
        self.assertTrue(tracker(min_person_height_px=120.0).assessable(box))

    def test_a_missing_or_malformed_box_is_not_assessable(self):
        tr = tracker()
        self.assertFalse(tr.assessable(None))
        self.assertFalse(tr.assessable(()))
        self.assertFalse(tr.assessable((1.0, 2.0)))

    def test_the_threshold_is_configurable(self):
        self.assertTrue(tracker(min_person_height_px=50.0).assessable(SHORT))

    def test_the_gate_is_what_prevents_conviction_by_distance(self):
        """The bug this was written for: silence from a distant worker used to
        reach the state machine and convict them. The gate means the caller
        never feeds it, so no amount of elapsed time produces a violation."""
        tr = tracker(violation_confirm_s=8.0)
        # A caller honouring the gate skips this track entirely...
        self.assertFalse(tr.assessable(SHORT))
        # ...and with nothing fed, there is no state and no verdict.
        self.assertIsNone(tr.state_of(1, HARDHAT))
        # Whereas feeding absence — the old behaviour — DOES convict, which is
        # exactly why the gate has to sit in front of it.
        feed(tr, 2, HARDHAT, EV_ABSENT, 200.0, 100.0, conf=0.0)
        self.assertEqual(tr.verdict(2, HARDHAT), NONCOMPLIANT)


class TestZoneCounts(unittest.TestCase):
    def _tracker_with(self):
        tr = tracker(violation_confirm_s=8.0, recovery_confirm_s=5.0)
        feed(tr, 1, HARDHAT, EV_POSITIVE, 10.0, 100.0)      # 1: fully compliant
        feed(tr, 1, VEST, EV_POSITIVE, 10.0, 100.0)
        feed(tr, 2, HARDHAT, EV_NEGATIVE, 12.0, 100.0)      # 2: no hardhat
        feed(tr, 2, VEST, EV_POSITIVE, 10.0, 100.0)
        feed(tr, 3, HARDHAT, EV_NEGATIVE, 12.0, 100.0)      # 3: neither
        feed(tr, 3, VEST, EV_NEGATIVE, 12.0, 100.0)
        return tr

    def test_counts_are_people_not_violations(self):
        tr = self._tracker_with()
        got = tr.zone_summary([(1, "Z", True), (2, "Z", True), (3, "Z", True)])["Z"]
        self.assertEqual(got["compliant"], 1)
        self.assertEqual(got["non_compliant"], 2)     # tracks 2 and 3
        # ...but three ITEM violations across them.
        self.assertEqual(got["violations"], {HARDHAT: 2, VEST: 1})
        self.assertEqual(sum(got["violations"].values()), 3)
        self.assertNotEqual(sum(got["violations"].values()), got["non_compliant"],
                            "summing violations must not give a headcount")

    def test_the_headcount_invariant_holds(self):
        """compliant + non_compliant + not_assessable == occupancy. A UI that
        cannot rely on this will show numbers that do not add up."""
        tr = self._tracker_with()
        roll = [(1, "Z", True), (2, "Z", True), (3, "Z", True), (4, "Z", False)]
        got = tr.zone_summary(roll)["Z"]
        total = got["compliant"] + got["non_compliant"] + got["not_assessable"]
        self.assertEqual(total, len(roll))

    def test_unassessable_people_are_reported_not_buried(self):
        """Folding 'we could not tell' into either bucket is a worse lie than
        admitting the gap."""
        tr = self._tracker_with()
        got = tr.zone_summary([(1, "Z", True), (9, "Z", False),
                               (10, "Z", False)])["Z"]
        self.assertEqual(got["not_assessable"], 2)
        self.assertEqual(got["compliant"], 1)
        self.assertEqual(got["non_compliant"], 0)

    def test_zones_are_counted_separately(self):
        tr = self._tracker_with()
        got = tr.zone_summary([(1, "ZA", True), (2, "ZB", True)])
        self.assertEqual(got["ZA"]["compliant"], 1)
        self.assertEqual(got["ZA"]["non_compliant"], 0)
        self.assertEqual(got["ZB"]["compliant"], 0)
        self.assertEqual(got["ZB"]["non_compliant"], 1)

    def test_a_candidate_is_not_yet_a_violation(self):
        """Only a CONFIRMED verdict counts. Someone mid-timer is still
        compliant as far as the count is concerned, matching when the alert
        fires."""
        tr = tracker(violation_confirm_s=8.0)
        tr.observe(1, HARDHAT, EV_NEGATIVE, 0.9, 100.0, 0.5)   # candidate only
        got = tr.zone_summary([(1, "Z", True)])["Z"]
        self.assertEqual(got["non_compliant"], 0)
        self.assertEqual(got["compliant"], 1)

    def test_an_empty_zone_produces_nothing(self):
        self.assertEqual(tracker().zone_summary([]), {})

    def test_recovery_moves_a_person_back_to_compliant(self):
        tr = tracker(violation_confirm_s=8.0, recovery_confirm_s=5.0)
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        self.assertEqual(tr.zone_summary([(1, "Z", True)])["Z"]["non_compliant"], 1)
        feed(tr, 1, HARDHAT, EV_POSITIVE, 30.0, 120.0)
        self.assertEqual(tr.verdict(1, HARDHAT), COMPLIANT)
        self.assertEqual(tr.zone_summary([(1, "Z", True)])["Z"]["compliant"], 1)


if __name__ == "__main__":
    unittest.main()
