"""The reporting day: 06:00-18:00 site-local, and the counts taken over it.

Two things these protect.

The BEFORE-OPENING rule. At 01:00 the day being reported is yesterday, because
today's business day has not started. Getting this wrong shows a night operator
a near-empty building and reads as an outage, which is the failure that made the
rule worth writing down.

The COUNTS are of people, not of identity records. The live gallery evicts, and
an evicted person who returns is minted again, so its cumulative tally drifts
above the number of real people. A windowed count over stored history does not.
"""

import datetime as _dt
import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from finblade import window                                       # noqa: E402
from services.api.store import InMemoryStore                      # noqa: E402

KSA = "Asia/Riyadh"


def at(y, m, d, hh, mm=0, tz=KSA):
    """Epoch seconds for a wall-clock time in the site's timezone."""
    return _dt.datetime(y, m, d, hh, mm, tzinfo=window._zone(tz)).timestamp()


def local(ts, tz=KSA):
    return _dt.datetime.fromtimestamp(ts, window._zone(tz))


class TestBusinessWindow(unittest.TestCase):
    def test_after_closing_reports_the_whole_day_just_finished(self):
        w = window.business_window(at(2026, 8, 26, 19, 59))
        self.assertEqual("2026-08-26", w["date"])
        self.assertEqual(6, local(w["from"]).hour)
        self.assertEqual(18, local(w["to"]).hour)
        self.assertTrue(w["complete"])

    def test_before_opening_reports_yesterday(self):
        """01:00 is the case the rule exists for: 'today' has not begun, and a
        00:00-01:00 window would show an empty building."""
        w = window.business_window(at(2026, 8, 26, 1, 0))
        self.assertEqual("2026-08-25", w["date"])
        self.assertEqual(at(2026, 8, 25, 6), w["from"])
        self.assertEqual(at(2026, 8, 25, 18), w["to"])
        self.assertTrue(w["complete"])

    def test_exactly_at_opening_switches_to_today(self):
        self.assertEqual("2026-08-26",
                         window.business_window(at(2026, 8, 26, 6, 0))["date"])
        self.assertEqual("2026-08-25",
                         window.business_window(at(2026, 8, 26, 5, 59))["date"])

    def test_inside_opening_hours_is_clipped_to_now_and_incomplete(self):
        now = at(2026, 8, 26, 13, 30)
        w = window.business_window(now)
        self.assertEqual("2026-08-26", w["date"])
        self.assertEqual(now, w["to"], "the window covers time that has happened")
        self.assertFalse(w["complete"])

    def test_before_opening_on_the_first_of_a_month_rolls_the_date_back(self):
        w = window.business_window(at(2026, 9, 1, 2, 0))
        self.assertEqual("2026-08-31", w["date"])

    def test_the_window_is_site_local_not_utc(self):
        """A UTC day splits a Riyadh afternoon across two dates and puts the
        06:00 boundary at 09:00 local."""
        w = window.business_window(at(2026, 8, 26, 19, 0))
        self.assertEqual(3 * 3600, local(w["from"]).utcoffset().total_seconds())
        self.assertEqual(6, local(w["from"]).hour)

    def test_hours_are_configurable(self):
        w = window.business_window(at(2026, 8, 26, 12, 0), start_hour=8,
                                   end_hour=20)
        self.assertEqual(8, local(w["from"]).hour)
        self.assertEqual(20, local(w["closes_at"]).hour)

    def test_a_backwards_window_is_refused(self):
        with self.assertRaises(ValueError):
            window.business_window(at(2026, 8, 26, 12), start_hour=18,
                                   end_hour=6)

    def test_an_explicit_range_wins_and_claims_no_date(self):
        w = window.resolve_window(at(2026, 8, 26, 19), frm=100.0, to=200.0)
        self.assertEqual((100.0, 200.0), (w["from"], w["to"]))
        self.assertIsNone(w["date"], "an arbitrary range is not a business day")

    def test_no_explicit_range_falls_back_to_the_business_day(self):
        self.assertEqual(window.business_window(at(2026, 8, 26, 19)),
                         window.resolve_window(at(2026, 8, 26, 19)))


class TestWindowedCounts(unittest.TestCase):
    """Distinct PEOPLE in a window, from stored history."""

    def setUp(self):
        self.store = InMemoryStore()
        self.n = 0

    def seen(self, ref, camera, ts):
        self.n += 1
        self.store.save_event({"event_id": f"e{self.n}", "event_type": "ZONE_ENTRY",
                               "camera_id": camera, "global_ref": ref,
                               "timestamp": ts})

    def test_counts_distinct_people_not_sightings(self):
        for ts in (10.0, 11.0, 12.0):
            self.seen("gp_a", "CAM-01", ts)
        out = self.store.identity_window_counts(0.0, 100.0)
        self.assertEqual(1, out["unique_total"])

    def test_someone_on_two_cameras_is_one_visitor_and_one_crossing(self):
        self.seen("gp_a", "CAM-01", 10.0)
        self.seen("gp_a", "CAM-02", 20.0)
        self.seen("gp_b", "CAM-01", 30.0)
        out = self.store.identity_window_counts(0.0, 100.0)
        self.assertEqual(2, out["unique_total"])
        self.assertEqual(1, out["cross_camera"])

    def test_cross_camera_counts_people_not_extra_sightings(self):
        """A person on THREE cameras is one person seen by 2+, not two. This is
        where the old sum(per_camera) - unique_total identity broke."""
        for cam in ("CAM-01", "CAM-02", "CAM-03"):
            self.seen("gp_a", cam, 10.0)
        out = self.store.identity_window_counts(0.0, 100.0)
        self.assertEqual(1, out["cross_camera"])
        summed = sum(c["unique"] for c in out["per_camera"])
        self.assertEqual(3, summed)
        self.assertNotEqual(summed - out["unique_total"], out["cross_camera"])

    def test_the_window_bounds_the_count(self):
        self.seen("gp_old", "CAM-01", 5.0)
        self.seen("gp_in", "CAM-01", 50.0)
        self.seen("gp_late", "CAM-01", 500.0)
        self.assertEqual(1, self.store.identity_window_counts(10.0, 100.0)["unique_total"])

    def test_events_without_an_identity_are_not_counted(self):
        """person_ref is a per-camera, per-session hash of a tracker id.
        Counting it would count one person once per camera, then again after
        every restart."""
        self.store.save_event({"event_id": "x", "camera_id": "CAM-01",
                               "person_ref": "pr_local", "timestamp": 10.0})
        self.assertEqual(0, self.store.identity_window_counts(0.0, 100.0)["unique_total"])

    def test_per_camera_never_sums_to_the_site_total(self):
        self.seen("gp_a", "CAM-01", 10.0)
        self.seen("gp_a", "CAM-02", 11.0)
        out = self.store.identity_window_counts(0.0, 100.0)
        self.assertEqual([{"camera_id": "CAM-01", "unique": 1},
                          {"camera_id": "CAM-02", "unique": 1}], out["per_camera"])
        self.assertEqual(1, out["unique_total"], "two bars, one person")

    def test_an_empty_window_is_zero_not_an_error(self):
        out = self.store.identity_window_counts(0.0, 100.0)
        self.assertEqual({"unique_total": 0, "cross_camera": 0, "per_camera": []},
                         out)


if __name__ == "__main__":
    unittest.main()
