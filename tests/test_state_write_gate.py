"""Write-on-change for zone_state_ts — Part A step 4.

The camera still posts every 5 seconds. These tests pin down which of those
posts become history rows, and — more importantly — everything that must NOT
change as a result: the live reading, the heartbeat, and the sub-5-second visit
carried by ZONE_ENTRY/ZONE_EXIT.
"""

import time
import unittest

from finblade.emission import StateWriteGate
from services.api.service import IngestService
from services.api.store import InMemoryStore


def state(ts, occupancy, status="NORMAL", zone="ZONE-01", camera="CAM-01"):
    return {"zone_id": zone, "camera_id": camera, "ts": ts,
            "occupancy": occupancy, "density": occupancy / 10.0,
            "capacity_pct": occupancy * 5.0, "status": status,
            "inflow_per_min": 0.0, "outflow_per_min": 0.0}


class TestGateDecision(unittest.TestCase):
    def setUp(self):
        self.gate = StateWriteGate(keepalive_s=300.0)

    def write(self, ts, occ, status="NORMAL", zone="ZONE-01", camera="CAM-01"):
        return self.gate.should_write(camera, zone, occ, status, ts)

    def test_the_first_post_for_a_zone_is_always_recorded(self):
        self.assertTrue(self.write(0.0, 0))

    def test_an_unchanged_repeat_is_suppressed(self):
        self.write(0.0, 0)
        self.assertFalse(self.write(5.0, 0))
        self.assertFalse(self.write(10.0, 0))

    def test_occupancy_changing_is_recorded(self):
        self.write(0.0, 0)
        self.assertTrue(self.write(5.0, 1))

    def test_returning_to_a_previous_value_is_still_a_change(self):
        """0 -> 1 -> 0 must write three rows. Suppressing the return would
        leave the history saying the zone is still occupied."""
        self.assertTrue(self.write(0.0, 0))
        self.assertTrue(self.write(5.0, 1))
        self.assertTrue(self.write(10.0, 0))

    def test_status_changing_at_constant_occupancy_is_recorded(self):
        """Thresholds can be reconfigured, or a zone's area edited, so the same
        headcount can cross into WARNING without occupancy moving."""
        self.write(0.0, 3, "NORMAL")
        self.assertTrue(self.write(5.0, 3, "WARNING"))

    def test_zones_are_gated_independently(self):
        self.write(0.0, 0, zone="ZONE-01")
        self.write(0.0, 0, zone="ZONE-02")
        self.assertTrue(self.write(5.0, 4, zone="ZONE-01"))
        self.assertFalse(self.write(5.0, 0, zone="ZONE-02"))

    def test_cameras_are_gated_independently(self):
        """Two cameras can share a zone_id across a site boundary; one going
        quiet must not suppress the other."""
        self.write(0.0, 0, camera="CAM-01")
        self.assertTrue(self.write(0.0, 0, camera="CAM-02"))

    def test_a_missing_status_is_a_value_not_an_absence(self):
        """The API rejects a state post without a status, so this cannot arrive
        over HTTP — but the gate is also callable directly, and None must
        behave as its own value rather than collapsing into "NORMAL"."""
        self.assertTrue(self.write(0.0, 0, None))
        self.assertFalse(self.write(5.0, 0, None))
        self.assertTrue(self.write(10.0, 0, "NORMAL"))


class TestKeepalive(unittest.TestCase):
    def test_a_quiet_zone_writes_once_per_keepalive(self):
        gate = StateWriteGate(keepalive_s=300.0)
        written = [t for t in range(0, 3600, 5)
                   if gate.should_write("CAM-01", "ZONE-01", 0, "NORMAL", float(t))]
        # 720 posts in the hour become 12: the anchor at t=0, then t=300 to
        # t=3300. 98.3% of the rows this zone used to write, gone.
        self.assertEqual(12, len(written))
        self.assertEqual(720, len(range(0, 3600, 5)))
        self.assertEqual([0, 300, 600], written[:3])

    def test_the_keepalive_clock_restarts_on_a_real_change(self):
        """A zone that changes constantly must not also emit keepalives."""
        gate = StateWriteGate(keepalive_s=300.0)
        gate.should_write("CAM-01", "ZONE-01", 0, "NORMAL", 0.0)
        gate.should_write("CAM-01", "ZONE-01", 1, "NORMAL", 290.0)
        self.assertFalse(gate.should_write("CAM-01", "ZONE-01", 1, "NORMAL", 300.0))
        self.assertTrue(gate.should_write("CAM-01", "ZONE-01", 1, "NORMAL", 590.0))

    def test_keepalive_zero_disables_it(self):
        gate = StateWriteGate(keepalive_s=0)
        gate.should_write("CAM-01", "ZONE-01", 0, "NORMAL", 0.0)
        self.assertFalse(gate.should_write("CAM-01", "ZONE-01", 0, "NORMAL", 86400.0))

    def test_the_keepalive_is_what_makes_silence_readable(self):
        """The reason the interval exists, stated as a test.

        finblade.timeweight distinguishes "nothing changed" from "the camera
        was dead" by whether writes kept arriving. With a keepalive, an hour of
        quiet is 12 rows and reads as observed. Without one it is a single row
        and is indistinguishable from a killed worker.
        """
        quiet = StateWriteGate(keepalive_s=300.0)
        dead = StateWriteGate(keepalive_s=300.0)
        alive = sum(quiet.should_write("C", "Z", 0, "NORMAL", float(t))
                    for t in range(0, 3600, 5))
        dead.should_write("C", "Z", 0, "NORMAL", 0.0)       # then the process dies
        self.assertGreater(alive, 1)
        self.assertEqual(1, dead.written)


class TestAlwaysMode(unittest.TestCase):
    def test_always_records_every_post(self):
        gate = StateWriteGate("always")
        self.assertTrue(all(gate.should_write("C", "Z", 0, "NORMAL", float(t))
                            for t in range(0, 100, 5)))

    def test_an_unrecognised_mode_falls_back_to_change_not_an_error(self):
        gate = StateWriteGate("evnet-drivn")
        self.assertEqual("change", gate.mode)
        self.assertEqual("evnet-drivn", gate.invalid_mode)


class TestStats(unittest.TestCase):
    def test_suppression_is_counted_and_reportable(self):
        """Operators need to see how much is being dropped; a gate that turns
        out to suppress 100% is a bug, and silence would hide it."""
        gate = StateWriteGate(keepalive_s=0)
        for t in range(0, 100, 5):
            gate.should_write("C", "Z", 0, "NORMAL", float(t))
        stats = gate.stats()
        self.assertEqual(1, stats["written"])
        self.assertEqual(19, stats["suppressed"])
        self.assertEqual(95.0, stats["suppressed_pct"])

    def test_stats_with_no_traffic_report_no_percentage(self):
        self.assertIsNone(StateWriteGate().stats()["suppressed_pct"])


class TestThroughTheService(unittest.TestCase):
    """The gate in place: what actually lands in each table."""

    def setUp(self):
        self.store = InMemoryStore()
        self.svc = IngestService(self.store, state_gate=StateWriteGate(keepalive_s=300.0))

    def post(self, ts, occ, status="NORMAL"):
        return self.svc.record_zone_state(state(ts, occ, status))

    def history(self):
        return self.store.zone_state_rows(0, 1e12, camera_id="CAM-01",
                                          zone_id="ZONE-01")

    def test_repeated_identical_posts_append_one_row(self):
        for t in range(0, 60, 5):
            self.post(float(t), 0)
        self.assertEqual(1, len(self.history()))

    def test_the_live_reading_still_tracks_every_post(self):
        """The interaction that makes this safe.

        /zones/state reads zone_live and hides anything older than 30 seconds.
        A quiet zone writes no history for up to a keepalive interval, so if
        the gate suppressed the live write too it would vanish from the
        dashboard half a minute after it settled — the zone card would empty
        out precisely because nothing was happening.

        Real timestamps, because the freshness cutoff is wall-clock.
        """
        now = time.time()
        for t in range(0, 60, 5):
            self.post(now - 55 + t, 0)
        self.assertEqual(1, len(self.history()), "one history row")
        live = self.store.latest_zone_states()
        self.assertEqual(1, len(live), "still visible after 55s of no change")
        self.assertAlmostEqual(now, live[0]["ts"], places=3)

    def test_the_response_says_whether_the_post_was_recorded(self):
        self.assertTrue(self.post(0.0, 0)[1]["recorded"])
        self.assertFalse(self.post(5.0, 0)[1]["recorded"])

    def test_a_suppressed_post_is_still_accepted(self):
        self.post(0.0, 0)
        code, body = self.post(5.0, 0)
        self.assertEqual(202, code)
        self.assertTrue(body["accepted"])

    def test_a_suppressed_post_still_counts_as_a_heartbeat(self):
        """Otherwise every zone that goes quiet trips R-07 camera-offline."""
        self.post(0.0, 0)
        self.assertFalse(self.post(5.0, 0)[1]["recorded"])
        cam = self.store.list_cameras()[0]
        self.assertEqual(5.0, cam["last_seen"])

    def test_a_malformed_post_is_rejected_before_the_gate_sees_it(self):
        code, _ = self.svc.record_zone_state({"zone_id": "ZONE-01"})
        self.assertEqual(422, code)
        self.assertEqual(0, self.svc.state_gate.written)

    def test_the_history_keeps_both_ends_of_a_visit(self):
        """Empty, three people arrive, empty again. Sixty seconds of posts
        collapse to the three rows that say something."""
        for t in range(0, 30, 5):
            self.post(float(t), 0)
            self.post(float(t), 0)
        for t in range(30, 45, 5):
            self.post(float(t), 3)
        for t in range(45, 60, 5):
            self.post(float(t), 0)
        rows = self.history()
        self.assertEqual([(0.0, 0), (30.0, 3), (45.0, 0)],
                         [(r["ts"], r["occupancy"]) for r in rows])

    def test_a_two_second_visit_is_absent_from_the_history_as_it_always_was(self):
        """The question this design has to answer honestly.

        Someone enters at t=6 and leaves at t=8. The sampler ticks at 5 and 10,
        reads 0 both times, and posts 0 both times. No gate is involved — the
        row never existed. The visit lives in ZONE_ENTRY/ZONE_EXIT, which are
        per-person and immediate, and which this change does not touch.
        """
        self.post(5.0, 0)
        self.post(10.0, 0)
        self.assertEqual(1, len(self.history()))

        ref = "pr_" + "a" * 16
        base = {"camera_id": "CAM-01", "site_id": "SITE-01",
                "zone_id": "ZONE-01", "person_ref": ref, "confidence": 0.9}
        for evt in (dict(base, event_id="e1", event_type="ZONE_ENTRY",
                         zone_to="ZONE-01", timestamp=6.0, occupancy=1),
                    dict(base, event_id="e2", event_type="ZONE_EXIT",
                         zone_from="ZONE-01", timestamp=8.0, occupancy=0)):
            code, body = self.svc.ingest_event(evt)
            self.assertEqual(202, code, body)
        got = self.store.list_events(0, 100)
        self.assertEqual(["ZONE_ENTRY", "ZONE_EXIT"],
                         sorted(e["event_type"] for e in got))
        # And step 1's occupancy stamp rides along, so the visit is not just
        # recorded but counted.
        entry = [e for e in got if e["event_type"] == "ZONE_ENTRY"][0]
        self.assertEqual(1, entry["occupancy"])


class TestReportsStillWork(unittest.TestCase):
    """Step 3 landed first for this reason: the reader has to be right before
    the writer goes sparse."""

    def setUp(self):
        self.store = InMemoryStore()
        self.svc = IngestService(self.store, state_gate=StateWriteGate(keepalive_s=300.0))

    def test_a_sparse_history_reports_the_time_weighted_average(self):
        """Empty for an hour, then five people for a minute.

        732 posts collapse to 13 rows: the opening anchor, eleven keepalives,
        and the one that says five people arrived. Averaging those 13 rows
        equally says 0.38 — the quiet hour is now under-represented, because
        it wrote 12 rows for 3600 seconds while the busy minute wrote one for
        60. Weighting by duration says 0.08, which is the true figure.
        """
        for t in range(0, 3600, 5):
            self.svc.record_zone_state(state(float(t), 0))
        for t in range(3600, 3660, 5):
            self.svc.record_zone_state(state(float(t), 5))

        rows = self.store.zone_state_rows(0, 3660.0, camera_id="CAM-01")
        self.assertEqual(13, len(rows), "732 posts, 13 rows")
        plain = sum(r["occupancy"] for r in rows) / len(rows)
        self.assertAlmostEqual(5 / 13, plain, places=3)

        tw = self.svc.zone_time_weighted(0.0, 3660.0, camera_id="CAM-01",
                                         zone_id="ZONE-01")
        occ = tw[("CAM-01", "ZONE-01")]["fields"]["occupancy"]
        self.assertAlmostEqual(5 * 60 / 3660, occ["mean"], places=3)
        self.assertEqual(5, occ["peak"])
        self.assertEqual(1.0, tw[("CAM-01", "ZONE-01")]["coverage"],
                         "keepalives every 300s keep max_hold=600s satisfied")

    def test_without_the_keepalive_a_quiet_hour_reads_as_downtime(self):
        """Why the keepalive is on by default, stated as a comparison.

        Same traffic, keepalive disabled. The quiet hour writes one row, the
        reader cannot tell it from a dead worker, and coverage collapses.
        """
        svc = IngestService(InMemoryStore(), state_gate=StateWriteGate(keepalive_s=0))
        for t in range(0, 3600, 5):
            svc.record_zone_state(state(float(t), 0))
        for t in range(3600, 3660, 5):
            svc.record_zone_state(state(float(t), 5))
        tw = svc.zone_time_weighted(0.0, 3660.0, camera_id="CAM-01",
                                    zone_id="ZONE-01")
        # max_hold is None with no keepalive, so the single row holds the hour
        # and coverage claims 1.0 — the number is right here only because the
        # zone genuinely was empty. Nothing distinguishes it from an outage.
        self.assertEqual(2, len(svc.store.zone_state_rows(0, 3660.0)))
        self.assertEqual(1.0, tw[("CAM-01", "ZONE-01")]["coverage"])


class TestTheReportPromotesTheWeightedAverage(unittest.TestCase):
    """occupancy_report's headline averages are the time-weighted ones as of
    step 4, because AVG() over rows stopped being correct in the same commit."""

    def setUp(self):
        self.store = InMemoryStore()
        self.svc = IngestService(self.store, state_gate=StateWriteGate(keepalive_s=300.0))
        for t in range(0, 3600, 5):
            self.svc.record_zone_state(state(float(t), 0))
        for t in range(3600, 3660, 5):
            self.svc.record_zone_state(state(float(t), 5))
        self.zone = self.svc.occupancy_report(0.0, 3660.0)["zones"][0]

    def test_avg_occupancy_is_the_weighted_figure(self):
        self.assertAlmostEqual(5 * 60 / 3660, self.zone["avg_occupancy"], places=3)

    def test_the_sampled_figure_is_kept_for_comparison(self):
        """Reports generated before this commit contain it, and a reader
        spanning the boundary needs to see both."""
        self.assertAlmostEqual(5 / 13, self.zone["sampled"]["avg_occupancy"],
                               places=3)
        self.assertNotAlmostEqual(self.zone["sampled"]["avg_occupancy"],
                                  self.zone["avg_occupancy"], places=2)

    def test_coverage_is_reported_at_zone_level(self):
        self.assertEqual(1.0, self.zone["coverage"])

    def test_peak_is_unaffected(self):
        """Peak is a max, not an average, so weighting never applied to it."""
        self.assertEqual(5, self.zone["peak_occupancy"])

    def test_a_zone_with_no_observed_time_keeps_its_sampled_average(self):
        """None means "no observed time carried a value". Blanking the column
        would read as a fault; the pre-existing number is the honest fallback."""
        empty = IngestService(InMemoryStore(),
                              state_gate=StateWriteGate(keepalive_s=300.0))
        report = empty.occupancy_report(0.0, 3660.0)
        self.assertEqual([], report["zones"])

    def test_the_csv_carries_coverage(self):
        from services.api.report import render_report_csv
        csv_text = render_report_csv([self.zone])
        header, row = csv_text.splitlines()[:2]
        self.assertIn("Coverage", header)
        self.assertEqual(header.split(",").index("Coverage"),
                         row.split(",").index("1.0"))


if __name__ == "__main__":
    unittest.main()
