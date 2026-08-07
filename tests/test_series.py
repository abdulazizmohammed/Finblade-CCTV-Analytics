"""Reading a sparse zone history — Part B.

These are the questions a chatbot asks. Step 4 made the history sparse, so
every one of them is now a "hold the last value forward" problem rather than a
SELECT, and the interesting cases are all about not answering confidently when
the camera was not watching.
"""

import unittest

from finblade.series import (COMPARABLE_FIELDS, OPERATORS, align_down,
                             bucket_bounds, bucket_series, duration_where,
                             field_predicate, find_gaps, state_at,
                             status_predicate)

FIELDS = ("occupancy",)


def s(ts, occupancy, status="NORMAL"):
    return {"ts": ts, "occupancy": occupancy, "density": occupancy / 10.0,
            "status": status}


class TestBucketBounds(unittest.TestCase):
    def test_buckets_align_to_the_epoch_not_the_query(self):
        """Two turns asking for "the last hour" a minute apart must not return
        buckets covering different spans — the second answer would contradict
        the first for no reason the user can see."""
        a = bucket_bounds(1000.0, 1600.0, 300.0)
        b = bucket_bounds(1060.0, 1660.0, 300.0)
        self.assertEqual(900.0, a[0][0])
        self.assertEqual(900.0, b[0][0])
        self.assertEqual(a[:2], b[:2])

    def test_every_bucket_covers_the_same_span(self):
        """Clipping the edges to the window makes the first and last buckets
        cover less time while looking identical, which draws as a dip at each
        end that is an artifact of the query."""
        for b0, b1 in bucket_bounds(1000.0, 2000.0, 300.0):
            self.assertAlmostEqual(300.0, b1 - b0)

    def test_the_span_covers_the_whole_window(self):
        bounds = bucket_bounds(1000.0, 2000.0, 300.0)
        self.assertLessEqual(bounds[0][0], 1000.0)
        self.assertGreaterEqual(bounds[-1][1], 2000.0)

    def test_degenerate_inputs_return_nothing_rather_than_looping(self):
        self.assertEqual([], bucket_bounds(0.0, 100.0, 0.0))
        self.assertEqual([], bucket_bounds(100.0, 100.0, 10.0))
        self.assertEqual([], bucket_bounds(200.0, 100.0, 10.0))

    def test_align_down(self):
        self.assertEqual(900.0, align_down(1050.0, 300.0))
        self.assertEqual(1200.0, align_down(1200.0, 300.0))


class TestBucketSeries(unittest.TestCase):
    def test_a_bucket_with_no_row_is_described_by_the_row_before_it(self):
        """The core of it. Under write-on-change most buckets contain no row at
        all — a zone holding four people for an hour writes once."""
        series = bucket_series([s(0.0, 4)], 0.0, 900.0, 300.0, FIELDS)
        self.assertEqual(3, len(series))
        self.assertEqual([4.0, 4.0, 4.0], [b["occupancy"] for b in series])
        self.assertEqual([1, 0, 0], [b["samples"] for b in series])

    def test_a_prior_sample_describes_the_opening_buckets(self):
        series = bucket_series([s(600.0, 0)], 0.0, 900.0, 300.0, FIELDS,
                               prior=s(-5000.0, 7))
        self.assertEqual([7.0, 7.0, 0.0], [b["occupancy"] for b in series])

    def test_a_bucket_is_weighted_within_itself(self):
        """Two people for the first half, six for the second: four, not the
        mean of the two rows by accident."""
        series = bucket_series([s(0.0, 2), s(150.0, 6)], 0.0, 300.0, 300.0, FIELDS)
        self.assertAlmostEqual(4.0, series[0]["occupancy"])

    def test_an_unobserved_bucket_is_none_not_zero(self):
        """A chart that draws an outage as an empty room is worse than a chart
        with a hole in it."""
        series = bucket_series([s(0.0, 4)], 0.0, 900.0, 300.0, FIELDS,
                               offline=[(300.0, 600.0)])
        self.assertIsNone(series[1]["occupancy"])
        self.assertEqual(0.0, series[1]["coverage"])
        self.assertEqual(4.0, series[0]["occupancy"])
        self.assertEqual(4.0, series[2]["occupancy"])

    def test_a_partly_observed_bucket_reports_partial_coverage(self):
        series = bucket_series([s(0.0, 4)], 0.0, 300.0, 300.0, FIELDS,
                               offline=[(150.0, 300.0)])
        self.assertEqual(0.5, series[0]["coverage"])
        self.assertAlmostEqual(4.0, series[0]["occupancy"],
                               msg="the half it saw, not half the value")

    def test_max_hold_makes_a_dead_worker_a_hole(self):
        """No CAMERA_OFFLINE is emitted when a worker is killed, so without
        max_hold the last reading holds for hours and every bucket is filled
        with a value nobody measured."""
        held = bucket_series([s(0.0, 9)], 0.0, 900.0, 300.0, FIELDS)
        capped = bucket_series([s(0.0, 9)], 0.0, 900.0, 300.0, FIELDS, max_hold=60.0)
        self.assertEqual([9.0, 9.0, 9.0], [b["occupancy"] for b in held])
        self.assertEqual([9.0, None, None], [b["occupancy"] for b in capped])

    def test_peak_is_carried_alongside_the_mean(self):
        """A five-minute mean of 0.2 hides that someone was in a restricted
        zone. The peak is what an operator actually reacts to."""
        series = bucket_series([s(0.0, 0), s(280.0, 3)], 0.0, 300.0, 300.0, FIELDS)
        self.assertEqual(3, series[0]["peak_occupancy"])
        self.assertLess(series[0]["occupancy"], 0.3)

    def test_multiple_fields(self):
        series = bucket_series([s(0.0, 4)], 0.0, 300.0, 300.0,
                               ("occupancy", "density"))
        self.assertEqual(4.0, series[0]["occupancy"])
        self.assertAlmostEqual(0.4, series[0]["density"])

    def test_no_samples_and_no_prior_gives_empty_buckets_not_zeros(self):
        series = bucket_series([], 0.0, 600.0, 300.0, FIELDS)
        self.assertEqual(2, len(series))
        self.assertTrue(all(b["occupancy"] is None for b in series))

    def test_a_long_span_of_small_buckets_stays_linear(self):
        """A day of 1-minute buckets is 1,440 of them against a sparse history.
        The naive implementation rescans every sample per bucket."""
        samples = [s(i * 3600.0, i % 5) for i in range(24)]
        series = bucket_series(samples, 0.0, 86400.0, 60.0, FIELDS)
        self.assertEqual(1440, len(series))
        self.assertEqual(0.0, series[0]["occupancy"])
        self.assertEqual(23 % 5, series[-1]["occupancy"], "the 23:00 sample, held")


class TestFindGaps(unittest.TestCase):
    def test_a_known_outage_is_reported_as_such(self):
        gaps = find_gaps([s(0.0, 1)], 0.0, 900.0, offline=[(300.0, 600.0)])
        self.assertEqual(1, len(gaps))
        self.assertEqual("camera_offline", gaps[0]["reason"])
        self.assertEqual(300.0, gaps[0]["seconds"])

    def test_silence_beyond_max_hold_is_reported_as_no_data(self):
        """A killed worker announces nothing, so this is the only trace it
        leaves. Distinguished from a known outage because they need different
        responses from whoever is on call."""
        gaps = find_gaps([s(0.0, 1)], 0.0, 900.0, max_hold=60.0)
        self.assertEqual(["no_data"], [g["reason"] for g in gaps])
        self.assertEqual(840.0, gaps[0]["seconds"])

    def test_an_outage_wins_where_the_two_overlap(self):
        """Otherwise the same seconds are reported twice and the gap totals
        exceed the length of the window."""
        gaps = find_gaps([s(0.0, 1)], 0.0, 900.0, offline=[(60.0, 900.0)],
                         max_hold=30.0)
        total = sum(g["seconds"] for g in gaps)
        self.assertLessEqual(total, 900.0)
        self.assertEqual(30.0, sum(g["seconds"] for g in gaps
                                   if g["reason"] == "no_data"))
        self.assertEqual(840.0, sum(g["seconds"] for g in gaps
                                    if g["reason"] == "camera_offline"))

    def test_time_before_the_first_sample_is_a_gap(self):
        gaps = find_gaps([s(800.0, 1)], 0.0, 900.0, max_hold=60.0)
        self.assertEqual(800.0, gaps[0]["seconds"])
        self.assertEqual(0.0, gaps[0]["from"])

    def test_a_healthy_window_has_no_gaps(self):
        samples = [s(i * 60.0, 0) for i in range(15)]
        self.assertEqual([], find_gaps(samples, 0.0, 900.0, max_hold=300.0))

    def test_an_empty_window_with_no_data_is_one_whole_gap(self):
        gaps = find_gaps([], 0.0, 900.0, max_hold=60.0)
        self.assertEqual([{"from": 0.0, "to": 900.0, "seconds": 900.0,
                           "reason": "no_data"}], gaps)

    def test_without_max_hold_only_a_totally_empty_window_is_a_gap(self):
        self.assertEqual(1, len(find_gaps([], 0.0, 900.0)))
        self.assertEqual([], find_gaps([s(0.0, 1)], 0.0, 900.0))

    def test_gaps_are_ordered(self):
        gaps = find_gaps([s(0.0, 1), s(500.0, 1)], 0.0, 900.0,
                         offline=[(700.0, 800.0)], max_hold=60.0)
        self.assertEqual(sorted(g["from"] for g in gaps),
                         [g["from"] for g in gaps])


class TestStateAt(unittest.TestCase):
    def test_the_last_sample_at_or_before_the_instant(self):
        got = state_at([s(0.0, 1), s(100.0, 5), s(200.0, 9)], 150.0)
        self.assertEqual(5, got["occupancy"])

    def test_a_sample_exactly_on_the_instant_counts(self):
        self.assertEqual(5, state_at([s(100.0, 5)], 100.0)["occupancy"])

    def test_it_never_reaches_forward(self):
        """Interpolating, or taking the nearest row in either direction, invents
        a reading for a time nobody observed — and the next sample can be hours
        later under write-on-change."""
        self.assertIsNone(state_at([s(500.0, 9)], 100.0))

    def test_a_prior_covers_an_instant_with_no_row_before_it_in_range(self):
        got = state_at([s(500.0, 9)], 100.0, prior=s(-9000.0, 2))
        self.assertEqual(2, got["occupancy"])

    def test_the_age_of_the_reading_is_reported(self):
        """So a caller can say "4 people, from a reading 3 hours old" rather
        than just "4 people"."""
        got = state_at([s(0.0, 4)], 10800.0)
        self.assertEqual(10800.0, got["age_seconds"])

    def test_stale_is_flagged_against_max_hold(self):
        fresh = state_at([s(0.0, 4)], 30.0, max_hold=300.0)
        stale = state_at([s(0.0, 4)], 3000.0, max_hold=300.0)
        self.assertFalse(fresh["stale"])
        self.assertTrue(stale["stale"])

    def test_nothing_at_all_returns_none(self):
        self.assertIsNone(state_at([], 100.0))


class TestDurationWhere(unittest.TestCase):
    def occupied(self):
        return field_predicate("occupancy", "gt", 0)

    def test_a_single_row_can_stand_for_hours(self):
        """The whole reason this is not a row count: one row means "and it
        stayed that way"."""
        got = duration_where([s(0.0, 3)], 0.0, 3600.0, self.occupied())
        self.assertEqual(3600.0, got["total_seconds"])
        self.assertEqual(1, got["episode_count"])

    def test_separate_episodes_are_kept_apart(self):
        samples = [s(0.0, 0), s(100.0, 2), s(200.0, 0), s(300.0, 5), s(400.0, 0)]
        got = duration_where(samples, 0.0, 500.0, self.occupied())
        self.assertEqual(200.0, got["total_seconds"])
        self.assertEqual(2, got["episode_count"])
        self.assertEqual(100.0, got["longest_seconds"])

    def test_adjacent_matching_rows_merge_into_one_episode(self):
        """3 people then 4 people is one continuous occupancy, not two."""
        samples = [s(0.0, 3), s(100.0, 4), s(200.0, 0)]
        got = duration_where(samples, 0.0, 300.0, self.occupied())
        self.assertEqual(1, got["episode_count"])
        self.assertEqual(200.0, got["total_seconds"])

    def test_an_outage_is_not_counted_as_a_breach(self):
        """The failure that matters: a camera down for four hours must not be
        reported as four hours over capacity."""
        got = duration_where([s(0.0, 5)], 0.0, 3600.0, self.occupied(),
                             offline=[(600.0, 3600.0)])
        self.assertEqual(600.0, got["total_seconds"])
        self.assertEqual(3000.0, got["unobserved_seconds"])
        self.assertAlmostEqual(1 / 6, got["coverage"], places=4)

    def test_a_dead_worker_is_not_counted_either(self):
        got = duration_where([s(0.0, 5)], 0.0, 3600.0, self.occupied(),
                             max_hold=300.0)
        self.assertEqual(300.0, got["total_seconds"])
        self.assertEqual(3300.0, got["unobserved_seconds"])

    def test_an_episode_split_by_an_outage_is_two_episodes(self):
        """Reporting one continuous 100-minute episode across a gap asserts
        something the camera did not see."""
        got = duration_where([s(0.0, 5)], 0.0, 900.0, self.occupied(),
                             offline=[(300.0, 600.0)])
        self.assertEqual(2, got["episode_count"])
        self.assertEqual(600.0, got["total_seconds"])

    def test_a_prior_row_starts_an_episode_at_the_window_edge(self):
        got = duration_where([s(600.0, 0)], 0.0, 900.0, self.occupied(),
                             prior=s(-100.0, 4))
        self.assertEqual(600.0, got["total_seconds"])
        self.assertEqual(0.0, got["episodes"][0]["from"])

    def test_never_matching_is_zero_seconds_and_no_episodes(self):
        got = duration_where([s(0.0, 0)], 0.0, 900.0, self.occupied())
        self.assertEqual(0.0, got["total_seconds"])
        self.assertEqual([], got["episodes"])
        self.assertEqual(1.0, got["coverage"], "zero seconds, fully observed")

    def test_status_predicate(self):
        samples = [s(0.0, 8, "WARNING"), s(100.0, 2, "NORMAL")]
        got = duration_where(samples, 0.0, 200.0, status_predicate("warning"))
        self.assertEqual(100.0, got["total_seconds"])


class TestPredicateSafety(unittest.TestCase):
    """This is reachable from a model choosing its own arguments."""

    def test_an_unknown_field_raises_rather_than_matching_nothing(self):
        """Zero seconds from a typo is indistinguishable from a real answer of
        zero, which is how a chatbot confidently reports "never"."""
        with self.assertRaises(ValueError):
            field_predicate("occupanci", "gt", 0)

    def test_an_unknown_operator_raises(self):
        with self.assertRaises(ValueError):
            field_predicate("occupancy", "=~", 0)

    def test_the_operator_set_is_closed(self):
        self.assertEqual({"gt", "gte", "lt", "lte", "eq"}, set(OPERATORS))
        self.assertEqual(("occupancy", "density", "capacity_pct"), COMPARABLE_FIELDS)

    def test_a_missing_value_does_not_match(self):
        check = field_predicate("density", "gt", 0)
        self.assertFalse(check({"ts": 0.0}))

    def test_a_non_numeric_value_does_not_explode(self):
        check = field_predicate("occupancy", "gt", 0)
        self.assertFalse(check({"ts": 0.0, "occupancy": "many"}))


if __name__ == "__main__":
    unittest.main()
