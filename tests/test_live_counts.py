"""Live-count smoothing: the median window behind people_in_view.

Reported live: the dashboard showed 1 person while 2 were standing there. Two
causes, and the second is what this file pins down.

  1. the worker published only every 5s, so any change waited up to 5s
  2. the value published was the count from the SINGLE frame that happened to
     coincide with the tick

Cause 2 is the one that produced a WRONG number rather than a late one. A person
detected in ~80% of frames is missed by roughly one sample in five, and that
wrong value then sat on screen for the whole interval. Publishing the median of a
short window means one dropped frame cannot move the reported figure.

These tests exercise _median_int directly rather than the pipeline, so they run
headless with no camera, no model and no GPU.
"""

import unittest

from services.inference.run_cpu import _median_int


class TestMedianInt(unittest.TestCase):

    def test_single_frame_dropout_does_not_change_the_count(self):
        """The whole point. Two people tracked, one frame misses one of them —
        the reported count must stay 2."""
        window = [2, 2, 2, 2, 2, 1, 2, 2, 2, 2]
        self.assertEqual(_median_int(window), 2)

    def test_a_dropout_run_shorter_than_half_the_window_is_absorbed(self):
        """Four bad frames out of eleven still leaves the median on the truth."""
        window = [2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1]
        self.assertEqual(_median_int(window), 2)

    def test_a_sustained_change_DOES_move_the_count(self):
        """Smoothing must not become stickiness: once most of the window agrees
        that a second person arrived, the count has to follow. A filter that
        never changes is as useless as one that flickers."""
        window = [1, 1, 1, 2, 2, 2, 2, 2, 2]
        self.assertEqual(_median_int(window), 2)

    def test_a_single_spurious_detection_does_not_inflate_the_count(self):
        """The inverse failure, and the one that matters for a client demo: one
        frame of a phantom must not report an extra person."""
        window = [1, 1, 1, 1, 1, 3, 1, 1, 1]
        self.assertEqual(_median_int(window), 1)

    def test_empty_window_uses_the_fallback(self):
        """Before the window fills — the first frames after startup — the caller
        passes the instantaneous count, which must be used verbatim rather than
        reported as zero people."""
        self.assertEqual(_median_int([], 7), 7)
        self.assertEqual(_median_int([]), 0)

    def test_result_is_always_an_int(self):
        """An even-length window must not yield 1.5 people. Occupancy is a count
        and it is rendered directly into the UI and the event payloads."""
        for window in ([1, 2], [0, 3], [2, 2, 3, 3]):
            v = _median_int(window)
            self.assertIsInstance(v, int, f"{window} -> {v!r}")

    def test_zero_is_reported_when_the_room_is_actually_empty(self):
        """A count of 0 is a real measurement, not a missing value — the median
        must not confuse 'empty' with 'no data'."""
        self.assertEqual(_median_int([0, 0, 0, 0, 0]), 0)
        self.assertEqual(_median_int([0, 0, 0, 1, 0]), 0)


if __name__ == "__main__":
    unittest.main()
