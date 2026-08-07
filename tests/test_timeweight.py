"""Time-weighted aggregation — Part A step 3.

Must be right before step 4 makes writes sparse. Today's rows arrive every 5
seconds, so AVG() across rows happens to be correct; once one row can stand for
four seconds and the next for four hours, it is not.
"""

import unittest

from finblade.timeweight import (merge_intervals, offline_intervals,
                                 time_weighted)

FIELDS = ("occupancy",)


def s(ts, occupancy):
    return {"ts": ts, "occupancy": occupancy}


def mean(result, field="occupancy"):
    return result["fields"][field]["mean"]


class TestAgreesWithAverageOnEvenlySpacedData(unittest.TestCase):
    """The migration safety net: on today's dense 5-second data the new maths
    must reproduce the old number, or the change is not a refactor."""

    def test_matches_plain_average_when_samples_are_evenly_spaced(self):
        samples = [s(i * 5.0, v) for i, v in enumerate([2, 4, 6, 8])]
        # Window ends one interval after the last sample, so every sample
        # covers exactly 5 seconds — the assumption AVG() silently makes.
        result = time_weighted(samples, 0.0, 20.0, FIELDS)
        self.assertAlmostEqual(5.0, mean(result))       # (2+4+6+8)/4

    def test_diverges_once_samples_are_not_evenly_spaced(self):
        """Empty all night, briefly busy in the morning. Averaging rows says 5;
        weighting by duration says almost 0 — which is what happened."""
        samples = [s(0.0, 0), s(3600.0, 10)]
        result = time_weighted(samples, 0.0, 3610.0, FIELDS)
        self.assertAlmostEqual(5.0, sum([0, 10]) / 2, msg="the old answer")
        self.assertLess(mean(result), 0.1)
        self.assertAlmostEqual(10 * 10 / 3610, mean(result), places=4)


class TestWindowing(unittest.TestCase):
    def test_a_sample_holds_until_the_next_one(self):
        result = time_weighted([s(0.0, 4), s(10.0, 0)], 0.0, 20.0, FIELDS)
        self.assertAlmostEqual(2.0, mean(result))       # 4 for 10s, 0 for 10s

    def test_the_last_sample_holds_to_the_end_of_the_window(self):
        result = time_weighted([s(0.0, 6)], 0.0, 100.0, FIELDS)
        self.assertAlmostEqual(6.0, mean(result))

    def test_a_prior_sample_sets_the_opening_state(self):
        """A zone whose last write was yesterday was not unknown at midnight."""
        without = time_weighted([s(90.0, 0)], 0.0, 100.0, FIELDS)
        self.assertAlmostEqual(0.0, mean(without))
        with_prior = time_weighted([s(90.0, 0)], 0.0, 100.0, FIELDS,
                                   prior=s(-500.0, 8))
        self.assertAlmostEqual(8 * 90 / 100, mean(with_prior))

    def test_samples_after_the_window_do_not_contribute(self):
        result = time_weighted([s(0.0, 2), s(500.0, 99)], 0.0, 10.0, FIELDS)
        self.assertAlmostEqual(2.0, mean(result))

    def test_out_of_order_input_is_sorted(self):
        result = time_weighted([s(10.0, 0), s(0.0, 4)], 0.0, 20.0, FIELDS)
        self.assertAlmostEqual(2.0, mean(result))

    def test_empty_window_is_not_an_error(self):
        result = time_weighted([], 5.0, 5.0, FIELDS)
        self.assertIsNone(mean(result))
        self.assertEqual(0, result["samples"])

    def test_no_samples_means_no_mean_not_zero(self):
        """Zero is a measurement; 'we never saw this zone' is not."""
        result = time_weighted([], 0.0, 100.0, FIELDS)
        self.assertIsNone(mean(result))


class TestPeakAndMin(unittest.TestCase):
    def test_peak_and_min_track_values_not_durations(self):
        result = time_weighted([s(0.0, 1), s(1.0, 9), s(2.0, 3)], 0.0, 10.0, FIELDS)
        self.assertEqual(9, result["fields"]["occupancy"]["peak"])
        self.assertEqual(1, result["fields"]["occupancy"]["min"])

    def test_a_zero_duration_spike_still_counts_toward_peak(self):
        """Two writes at the same instant: the first covered no time, but it
        happened."""
        result = time_weighted([s(5.0, 12), s(5.0, 0)], 0.0, 10.0, FIELDS)
        self.assertEqual(12, result["fields"]["occupancy"]["peak"])
        self.assertAlmostEqual(0.0, mean(result))


class TestCoverage(unittest.TestCase):
    def test_full_coverage_when_the_camera_never_dropped(self):
        result = time_weighted([s(0.0, 4)], 0.0, 100.0, FIELDS)
        self.assertEqual(1.0, result["coverage"])

    def test_downtime_leaves_the_denominator(self):
        """Camera down for the second half. The average must describe the half
        it saw, not be halved by hours it could not observe."""
        result = time_weighted([s(0.0, 4)], 0.0, 100.0, FIELDS,
                               unknown=[(50.0, 100.0)])
        self.assertAlmostEqual(4.0, mean(result))
        self.assertEqual(0.5, result["coverage"])
        self.assertAlmostEqual(50.0, result["observed_seconds"])

    def test_a_window_entirely_inside_an_outage_reports_no_mean(self):
        result = time_weighted([s(0.0, 4)], 0.0, 100.0, FIELDS,
                               unknown=[(0.0, 100.0)])
        self.assertIsNone(mean(result))
        self.assertEqual(0.0, result["coverage"])

    def test_overlapping_outages_are_not_double_counted(self):
        result = time_weighted([s(0.0, 4)], 0.0, 100.0, FIELDS,
                               unknown=[(10.0, 50.0), (40.0, 60.0)])
        self.assertEqual(0.5, result["coverage"])       # 50s down, not 60s


class TestMaxHold(unittest.TestCase):
    """A killed camera worker emits no CAMERA_OFFLINE, so the gap it leaves is
    invisible to `unknown` and the last sample "holds" for hours.

    Measured on the live database: CAM-03/ZONE-01 read 0.0676 plain against
    0.0079 weighted, an 8.5x disagreement caused entirely by such gaps.
    """

    def test_a_sample_is_not_trusted_beyond_max_hold(self):
        result = time_weighted([s(0.0, 10)], 0.0, 1000.0, FIELDS, max_hold=10.0)
        self.assertAlmostEqual(10.0, mean(result), msg="the 10s it did observe")
        self.assertAlmostEqual(0.01, result["coverage"])

    def test_without_max_hold_the_sample_holds_the_whole_window(self):
        result = time_weighted([s(0.0, 10)], 0.0, 1000.0, FIELDS)
        self.assertAlmostEqual(10.0, mean(result))
        self.assertEqual(1.0, result["coverage"], "and claims full coverage")

    def test_a_long_outage_between_samples_becomes_unknown(self):
        """Two bursts of activity with the worker stopped in between."""
        samples = [s(0.0, 4), s(10_000.0, 8)]
        result = time_weighted(samples, 0.0, 10_010.0, FIELDS, max_hold=10.0)
        self.assertLess(result["coverage"], 0.01)
        # 4 for 10s then 8 for 10s — the 10,000s hole contributes nothing.
        self.assertAlmostEqual(6.0, mean(result))

    def test_time_before_the_first_sample_is_unknown_not_zero(self):
        result = time_weighted([s(900.0, 2)], 0.0, 1000.0, FIELDS, max_hold=100.0)
        self.assertAlmostEqual(2.0, mean(result))
        self.assertAlmostEqual(0.1, result["coverage"])

    def test_no_samples_at_all_is_zero_coverage(self):
        result = time_weighted([], 0.0, 1000.0, FIELDS, max_hold=100.0)
        self.assertIsNone(mean(result))
        self.assertEqual(0.0, result["coverage"])

    def test_dense_data_is_unaffected(self):
        """max_hold well above the write interval must change nothing."""
        samples = [s(i * 5.0, i % 3) for i in range(100)]
        loose = time_weighted(samples, 0.0, 500.0, FIELDS, max_hold=600.0)
        none_set = time_weighted(samples, 0.0, 500.0, FIELDS)
        self.assertAlmostEqual(mean(none_set), mean(loose))
        self.assertEqual(1.0, loose["coverage"])

    def test_a_keepalive_keeps_a_quiet_zone_fully_covered(self):
        """The point of the keepalive in step 4: a zone where nothing changes
        still writes periodically, so its silence stays interpretable."""
        keepalive = 300.0
        samples = [s(i * keepalive, 0) for i in range(12)]      # one hour
        result = time_weighted(samples, 0.0, 12 * keepalive, FIELDS,
                               max_hold=2 * keepalive)
        self.assertEqual(1.0, result["coverage"])
        self.assertAlmostEqual(0.0, mean(result))

    def test_explicit_outages_and_max_hold_combine_without_double_counting(self):
        result = time_weighted([s(0.0, 4)], 0.0, 100.0, FIELDS,
                               unknown=[(10.0, 60.0)], max_hold=20.0)
        # Observed: 0-10 real, 10-20 already unknown, 20-100 stale. 10s of 100.
        self.assertAlmostEqual(0.1, result["coverage"])
        self.assertAlmostEqual(4.0, mean(result))


class TestMergeIntervals(unittest.TestCase):
    def test_overlaps_coalesce(self):
        self.assertEqual([(0.0, 10.0)], merge_intervals([(0, 6), (4, 10)]))

    def test_touching_intervals_coalesce(self):
        self.assertEqual([(0.0, 10.0)], merge_intervals([(0, 5), (5, 10)]))

    def test_disjoint_intervals_are_kept_apart(self):
        self.assertEqual([(0.0, 1.0), (5.0, 6.0)],
                         merge_intervals([(5, 6), (0, 1)]))

    def test_zero_length_intervals_are_dropped(self):
        self.assertEqual([], merge_intervals([(3, 3)]))


class TestOfflineIntervals(unittest.TestCase):
    def evt(self, etype, ts):
        return {"event_type": etype, "ts": ts}

    def test_an_offline_online_pair_becomes_an_interval(self):
        got = offline_intervals(
            [self.evt("CAMERA_OFFLINE", 10), self.evt("CAMERA_ONLINE", 40)],
            0, 100)
        self.assertEqual([(10.0, 40.0)], got)

    def test_recovered_closes_an_outage_too(self):
        got = offline_intervals(
            [self.evt("CAMERA_OFFLINE", 10), self.evt("CAMERA_RECOVERED", 20)],
            0, 100)
        self.assertEqual([(10.0, 20.0)], got)

    def test_still_offline_at_the_end_runs_to_the_window_close(self):
        got = offline_intervals([self.evt("CAMERA_OFFLINE", 60)], 0, 100)
        self.assertEqual([(60.0, 100.0)], got)

    def test_a_window_with_no_camera_events_is_fully_covered(self):
        self.assertEqual([], offline_intervals([], 0, 100))

    def test_duplicate_offline_events_do_not_restart_the_clock(self):
        got = offline_intervals(
            [self.evt("CAMERA_OFFLINE", 10), self.evt("CAMERA_OFFLINE", 20),
             self.evt("CAMERA_ONLINE", 30)], 0, 100)
        self.assertEqual([(10.0, 30.0)], got)

    def test_an_online_with_no_preceding_offline_is_ignored(self):
        self.assertEqual([], offline_intervals([self.evt("CAMERA_ONLINE", 5)], 0, 100))


class TestMultipleFields(unittest.TestCase):
    def test_fields_are_weighted_independently(self):
        samples = [{"ts": 0.0, "occupancy": 4, "density": 0.4},
                   {"ts": 10.0, "occupancy": 0, "density": 0.0}]
        result = time_weighted(samples, 0.0, 20.0, ("occupancy", "density"))
        self.assertAlmostEqual(2.0, result["fields"]["occupancy"]["mean"])
        self.assertAlmostEqual(0.2, result["fields"]["density"]["mean"])

    def test_a_missing_field_yields_none_not_zero(self):
        result = time_weighted([s(0.0, 4)], 0.0, 10.0, ("capacity_pct",))
        self.assertIsNone(result["fields"]["capacity_pct"]["mean"])


if __name__ == "__main__":
    unittest.main()
