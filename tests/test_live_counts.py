"""Live-count smoothing: the window behind people_in_view.

Reported live, twice, and the second report is why this file exists in its
current form.

First: the dashboard showed 1 person while 2 were standing there, and looked
frozen. The worker published every 5s and sent the count from the SINGLE frame
that coincided with the tick, so a momentary dropout was held on screen for the
whole interval.

Then, after switching to a MEDIAN over a short window: the dashboard showed 0
people while a man sat in the reception seats. Measured from the worker log, the
reason was unarguable - over 1749 frames he was tracked in only 52% of them:

    tracked=0 : 848 frames (48.5%)
    tracked=1 : 801 frames (45.8%)
    tracked=2 :  95 frames  (5.4%)

A median sits exactly on that knife-edge and reports ZERO. The lesson is that
the errors are ASYMMETRIC: a detection is positive evidence somebody is there,
while a non-detection is weak evidence of nothing, because at 640x360 on a
seated subject the detector simply misses about half the frames. So the
estimator has to lean toward presence, which is what _presence_count does.

It is a quantile and not max() because max lets a single phantom frame invent a
person - this same camera once reported three people from one real one.

These tests drive the function directly, so they run headless with no camera, no
model and no GPU.
"""

import unittest

from services.inference.run_cpu import _presence_count


class TestPresenceCount(unittest.TestCase):

    def test_the_measured_52_percent_case_reports_one_person(self):
        """THE regression test. This distribution is copied from the real worker
        log for a man sitting in the reception seats, and the previous median
        implementation reported 0 for it while he was plainly visible."""
        window = [0] * 48 + [1] * 46 + [2] * 6
        self.assertEqual(_presence_count(window), 1)

    def test_a_subject_seen_in_only_a_third_of_frames_still_counts(self):
        """Detection on a low-resolution substream is poor but not absent.
        Someone present in a third of frames is present."""
        window = [0] * 14 + [1] * 8
        self.assertEqual(_presence_count(window), 1)

    def test_single_frame_dropout_does_not_change_the_count(self):
        """The original complaint: two people tracked, one frame misses one of
        them, the reported count must stay 2."""
        window = [2, 2, 2, 2, 2, 1, 2, 2, 2, 2]
        self.assertEqual(_presence_count(window), 2)

    def test_a_single_spurious_detection_does_not_inflate_the_count(self):
        """The opposite failure, and the one that matters in front of a client:
        one frame of a phantom must not add a person. This camera reported three
        people from one real one on detections occupying 0.3% of frames."""
        window = [1] * 21 + [3]
        self.assertEqual(_presence_count(window), 1)

    def test_a_brief_phantom_run_still_does_not_inflate_the_count(self):
        """Two phantom frames out of twenty-two is under the quarter-of-window
        bar, so it stays out."""
        window = [1] * 20 + [2, 2]
        self.assertEqual(_presence_count(window), 1)

    def test_a_sustained_second_person_IS_counted(self):
        """Leaning toward presence must not become deafness: once a second
        person holds across the window, the count has to follow."""
        window = [1] * 5 + [2] * 17
        self.assertEqual(_presence_count(window), 2)

    def test_an_empty_room_reports_zero(self):
        """0 is a real measurement, not missing data. A presence-biased
        estimator must still be able to say 'nobody is here'."""
        self.assertEqual(_presence_count([0] * 22), 0)

    def test_almost_empty_room_stays_zero(self):
        """One or two stray frames in an empty room must not conjure a person,
        or every unattended camera reports phantom footfall overnight."""
        window = [0] * 20 + [1, 1]
        self.assertEqual(_presence_count(window), 0)

    def test_empty_window_uses_the_fallback(self):
        """Before the window fills - the first frames after a worker starts -
        the caller passes the instantaneous count, which must be used rather
        than reporting an empty room."""
        self.assertEqual(_presence_count([], 7), 7)
        self.assertEqual(_presence_count([]), 0)

    def test_result_is_always_an_int(self):
        """An even-length window must not yield 1.5 people. This value is
        rendered directly into the UI and into event payloads."""
        for window in ([1, 2], [0, 3], [2, 2, 3, 3]):
            v = _presence_count(window)
            self.assertIsInstance(v, int, f"{window} -> {v!r}")

    def test_quantile_is_tunable_and_max_is_reachable(self):
        """FINBLADE_PRESENCE_QUANTILE exists so a site with a worse camera can
        lean further toward presence. At 1.0 it degrades to max(), which is
        deliberately NOT the default."""
        window = [0] * 20 + [1, 1]
        self.assertEqual(_presence_count(window, quantile=1.0), 1)
        self.assertEqual(_presence_count(window, quantile=0.5), 0)


if __name__ == "__main__":
    unittest.main()
