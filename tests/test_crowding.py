"""Detection-quality degradation and the crowd-model seam (REQ-27, REQ-28)."""

import unittest

from finblade.crowding import (
    RELIABLE, SATURATED, STRAINED, CrowdEstimator, TrackingQualityMonitor,
    select_mode,
)


def feed(m, frames, ids_per_frame, conf, t0=100.0, step=0.1, max_det=None,
         stable_ids=True):
    """Drive the monitor for `frames` frames.

    stable_ids models a clean scene — the same people keep the same ids.
    Setting it False mints fresh ids every frame, which is what fragmentation
    looks like from here.
    """
    nxt = [0]
    for i in range(frames):
        if stable_ids:
            ids = list(range(ids_per_frame))
        else:
            ids = [nxt[0] + j for j in range(ids_per_frame)]
            nxt[0] += ids_per_frame
        m.observe(t0 + i * step, ids, [conf] * ids_per_frame, max_det=max_det)


class TestQualityAssessment(unittest.TestCase):
    def test_a_clean_scene_is_reliable(self):
        m = TrackingQualityMonitor()
        feed(m, 50, 4, 0.85)
        self.assertEqual(m.assess(), RELIABLE)
        self.assertTrue(m.snapshot()["counts_reliable"])

    def test_startup_does_not_raise_an_alarm(self):
        m = TrackingQualityMonitor()
        feed(m, 2, 30, 0.30)
        self.assertEqual(m.assess(), RELIABLE,
                         "two frames is not evidence of anything")

    def test_falling_confidence_is_detected(self):
        m = TrackingQualityMonitor()
        feed(m, 40, 12, 0.50)
        self.assertEqual(m.assess(), STRAINED)

    def test_collapsing_confidence_reads_saturated(self):
        m = TrackingQualityMonitor()
        feed(m, 40, 25, 0.40)
        self.assertEqual(m.assess(), SATURATED)
        self.assertFalse(m.snapshot()["counts_reliable"])

    def test_id_churn_is_detected_even_at_good_confidence(self):
        # The failure mode confidence alone misses: boxes still look sharp, but
        # the tracker cannot hold anyone across frames.
        m = TrackingQualityMonitor()
        feed(m, 60, 6, 0.90, stable_ids=False)
        self.assertIn(m.assess(), (STRAINED, SATURATED))
        self.assertGreater(m.snapshot()["track_churn_per_min"], 0.0)

    def test_stable_ids_produce_low_churn(self):
        m = TrackingQualityMonitor()
        feed(m, 60, 6, 0.90, stable_ids=True)
        self.assertLess(m.snapshot()["track_churn_per_min"],
                        m.churn_strained)

    def test_hitting_the_detector_cap_is_reported(self):
        # People are being dropped before any of this code sees them, which no
        # downstream measure can recover.
        m = TrackingQualityMonitor()
        feed(m, 40, 100, 0.80, max_det=100)
        self.assertEqual(m.saturation_fraction(), 1.0)
        self.assertEqual(m.assess(), SATURATED)

    def test_below_the_cap_is_not_saturation(self):
        m = TrackingQualityMonitor()
        feed(m, 40, 10, 0.80, max_det=100)
        self.assertEqual(m.saturation_fraction(), 0.0)
        self.assertEqual(m.assess(), RELIABLE)

    def test_old_frames_leave_the_window(self):
        m = TrackingQualityMonitor(window_s=5.0)
        feed(m, 40, 20, 0.40)                      # bad
        self.assertEqual(m.assess(), SATURATED)
        feed(m, 40, 3, 0.95, t0=200.0)             # much later, and clean
        self.assertEqual(m.assess(), RELIABLE, "the bad window must age out")

    def test_snapshot_shape(self):
        m = TrackingQualityMonitor()
        feed(m, 20, 5, 0.9)
        s = m.snapshot()
        for key in ("tracking_quality", "mean_confidence", "track_churn_per_min",
                    "detector_saturation", "mean_tracks", "counts_reliable"):
            self.assertIn(key, s)


class TestCrowdEstimatorSeam(unittest.TestCase):
    def test_no_model_registered_returns_no_estimate(self):
        est = CrowdEstimator()
        self.assertFalse(est.available)
        self.assertIsNone(est.estimate(object()))
        self.assertEqual(est.describe(), {"crowd_model": None, "available": False})

    def test_a_registered_backend_is_used(self):
        class Fake:
            name = "fake-density-net"

            def estimate(self, frame, zone=None):
                return 87.5

        est = CrowdEstimator(Fake())
        self.assertTrue(est.available)
        self.assertEqual(est.estimate(object()), 87.5)
        self.assertEqual(est.describe()["crowd_model"], "fake-density-net")


class TestModeSelection(unittest.TestCase):
    def test_reliable_scenes_use_tracking(self):
        self.assertEqual(select_mode(RELIABLE), "track")
        self.assertEqual(select_mode(RELIABLE, CrowdEstimator()), "track")

    def test_saturated_without_a_model_says_so_rather_than_pretending(self):
        # The important negative: no silent switch to a method that does not
        # exist. The count is still produced, and is flagged as degraded.
        self.assertEqual(select_mode(SATURATED), "track_degraded")
        self.assertEqual(select_mode(STRAINED, CrowdEstimator()), "track_degraded")

    def test_saturated_with_a_model_hands_over(self):
        class Fake:
            name = "x"

            def estimate(self, frame, zone=None):
                return 1.0

        self.assertEqual(select_mode(SATURATED, CrowdEstimator(Fake())),
                         "crowd_model")


if __name__ == "__main__":
    unittest.main()
