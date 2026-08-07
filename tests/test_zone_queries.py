"""The three chatbot query endpoints, through the service — Part B.

test_series.py covers the arithmetic. This covers what sits around it: which
zone a bare zone_id means, where the outage windows come from, how a bad
argument is refused, and the bucket cap.
"""

import unittest

from finblade.emission import StateWriteGate
from services.api.service import IngestService
from services.api.store import InMemoryStore

T0 = 1_700_000_000.0


def state(ts, occupancy, zone="ZONE-01", camera="CAM-01", status="NORMAL"):
    return {"zone_id": zone, "camera_id": camera, "ts": ts,
            "occupancy": occupancy, "density": round(occupancy / 10.0, 4),
            "capacity_pct": occupancy * 5.0, "status": status,
            "inflow_per_min": 0.0, "outflow_per_min": 0.0}


def camera_event(etype, ts, camera="CAM-01"):
    evt = {"event_id": f"{etype}-{ts}", "event_type": etype,
           "camera_id": camera, "site_id": "SITE-01", "timestamp": ts}
    if etype == "CAMERA_OFFLINE":
        evt["last_seen"] = ts - 31.0        # required by the event schema
    return evt


class Base(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        # always-mode: these tests post exactly the samples they mean, and a
        # gate suppressing some of them would obscure what is being asserted.
        self.svc = IngestService(self.store, state_gate=StateWriteGate("always"))

    def post(self, *args, **kwargs):
        code, body = self.svc.record_zone_state(state(*args, **kwargs))
        self.assertEqual(202, code, body)

    def emit(self, etype, ts, camera="CAM-01"):
        # Asserted, because a silently rejected CAMERA_OFFLINE would make the
        # outage tests pass for the wrong reason — an unlogged gap also
        # produces gaps, just labelled no_data.
        code, body = self.svc.ingest_event(camera_event(etype, ts, camera))
        self.assertEqual(202, code, body)

    def steady(self, t_from, t_to, occupancy, step=300.0, **kwargs):
        """What the writer actually produces: the change, then keepalives.

        Posting a single row and expecting it to describe an hour is not what
        write-on-change does — the keepalive fires every 300s precisely so a
        long unchanged stretch stays distinguishable from a dead worker. A
        fixture without them tests a state the system never reaches.
        """
        t = t_from
        while t < t_to:
            self.post(t, occupancy, **kwargs)
            t += step

    def zones(self, camera, *zone_ids):
        self.store.save_zones(camera, [
            {"zone_id": z, "zone_name": z.title(), "area_sqm": 10.0,
             "polygon": [[0, 0], [1, 0], [1, 1]]} for z in zone_ids])


class TestResolveZone(Base):
    def test_a_zone_id_present_on_two_cameras_is_ambiguous(self):
        """ZONE-01 is the lobby on one camera and the loading bay on another.
        Answering for whichever sorted first is wrong in a way that never
        surfaces downstream."""
        self.zones("CAM-01", "ZONE-01")
        self.zones("CAM-02", "ZONE-01")
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 300.0)
        self.assertEqual("ambiguous", out["error"])
        self.assertEqual(409, out["status"])
        self.assertEqual({"CAM-01", "CAM-02"},
                         {c["camera_id"] for c in out["candidates"]})

    def test_camera_id_disambiguates(self):
        self.zones("CAM-01", "ZONE-01")
        self.zones("CAM-02", "ZONE-01")
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 300.0,
                                   camera_id="CAM-02")
        self.assertNotIn("error", out)
        self.assertEqual("CAM-02", out["camera_id"])

    def test_an_unknown_zone_is_404(self):
        out = self.svc.zone_series("ZONE-99", T0, T0 + 3600, 300.0)
        self.assertEqual(404, out["status"])

    def test_ambiguity_is_seen_when_only_one_camera_is_configured(self):
        """Found by the live check, not by these tests, because every fixture
        here configured its cameras.

        Resolution used to try the config and only look at history if the
        config was empty. With ZONE-01 configured on CAM-02 and merely
        REPORTING on CAM-01, the config found exactly one match and the query
        was silently answered for the wrong physical area.
        """
        self.post(T0, 3, camera="CAM-01")      # reports, never configured
        self.zones("CAM-02", "ZONE-01")        # configured, never reports
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 300.0)
        self.assertEqual("ambiguous", out.get("error"))
        self.assertEqual({"CAM-01", "CAM-02"},
                         {c["camera_id"] for c in out["candidates"]})

    def test_a_configured_zone_with_no_history_is_still_found(self):
        """The other direction: a zone drawn this morning has no rows yet, and
        asking about it must be an empty series rather than a 404."""
        self.zones("CAM-01", "ZONE-07")
        out = self.svc.zone_series("ZONE-07", T0, T0 + 3600, 300.0)
        self.assertNotIn("error", out)
        self.assertEqual(0, out["rows_in_window"])

    def test_a_zone_deleted_from_config_is_still_queryable(self):
        """The history outlives the polygon. "Show me last week" must keep
        working after someone redraws the zones."""
        self.post(T0, 3)                      # reported, never configured
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 300.0)
        self.assertNotIn("error", out)
        self.assertEqual("CAM-01", out["camera_id"])


class TestZoneSeries(Base):
    def setUp(self):
        super().setUp()
        self.zones("CAM-01", "ZONE-01")

    def test_sparse_rows_fill_every_bucket_they_cover(self):
        """The reason the endpoint exists.

        An hour of four people is 720 posts but only 12 stored rows — one
        change and eleven keepalives. A plain SELECT bucketed by ts leaves most
        buckets empty; holding each reading forward fills them.
        """
        self.steady(T0, T0 + 3600, 4)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 600.0)
        self.assertEqual(12, out["rows_in_window"], "12 rows for 720 posts")
        # Seven buckets, not six: they sit on epoch multiples, so the first
        # starts before the window and a seventh is needed to reach the end.
        self.assertEqual(7, len(out["points"]))
        self.assertTrue(all(p["occupancy"] == 4.0 for p in out["points"][1:]))
        self.assertEqual(1.0, out["coverage"])

    def test_a_single_row_only_speaks_for_max_hold(self):
        """The other side of the same coin. Without keepalives one row is
        indistinguishable from a worker that died right after writing it, so
        the buckets past max_hold are holes rather than a flat line."""
        self.post(T0, 4)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 600.0)
        filled = [p["occupancy"] for p in out["points"] if p["occupancy"] is not None]
        self.assertEqual([4.0, 4.0], filled)
        self.assertEqual(["no_data"], [g["reason"] for g in out["gaps"]])

    def test_coverage_is_full_when_the_camera_kept_reporting(self):
        for i in range(13):
            self.post(T0 + i * 300, i % 3)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 600.0)
        self.assertEqual(1.0, out["coverage"])
        self.assertEqual([], out["gaps"])

    def test_a_logged_outage_becomes_null_buckets_and_a_gap(self):
        self.steady(T0, T0 + 1200, 4)
        self.emit("CAMERA_OFFLINE", T0 + 1200)
        self.emit("CAMERA_ONLINE", T0 + 2400)
        self.steady(T0 + 2400, T0 + 3600, 4)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 600.0)
        self.assertEqual(["camera_offline"], [g["reason"] for g in out["gaps"]])
        self.assertEqual(1200.0, out["gaps"][0]["seconds"])
        self.assertAlmostEqual(2400.0 / 3600.0, out["coverage"], places=3)
        blank = [p for p in out["points"] if p["occupancy"] is None]
        self.assertTrue(blank, "the outage must leave holes, not zeros")
        self.assertTrue(all(p["coverage"] == 0.0 for p in blank))

    def test_a_dead_worker_becomes_a_no_data_gap(self):
        """No CAMERA_OFFLINE is emitted when a process is killed, so this is
        the only trace. Reported separately from a logged outage because they
        need different responses from whoever is on call."""
        self.post(T0, 4)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 7200, 600.0)
        self.assertEqual(["no_data"], [g["reason"] for g in out["gaps"]])
        self.assertLess(out["coverage"], 0.2)

    def test_max_hold_follows_the_keepalive(self):
        """Two keepalive intervals, so one late or dropped keepalive is not an
        outage. With the keepalive off there is nothing to measure against."""
        self.assertEqual(600.0,
                         IngestService(InMemoryStore(),
                                       state_gate=StateWriteGate(keepalive_s=300.0)
                                       ).max_hold())
        self.assertIsNone(IngestService(InMemoryStore(),
                                        state_gate=StateWriteGate(keepalive_s=0)
                                        ).max_hold())

    def test_buckets_are_coarsened_rather_than_the_request_refused(self):
        """A model choosing its own arguments will ask for a week of 1-second
        buckets. A 400 costs a round trip and it usually retries with a guess."""
        self.post(T0, 1)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 604800, 1.0)
        self.assertTrue(out["bucket_adjusted"])
        self.assertEqual(1.0, out["requested_bucket_seconds"])
        self.assertLessEqual(len(out["points"]), IngestService.MAX_BUCKETS)
        self.assertGreater(out["bucket_seconds"], 1.0)

    def test_the_bucket_cap_matches_the_chart_point_cap(self):
        """Finer would make the JSON and the chart tag disagree, and the chart
        is trimmed to its FIRST N points — for a time series that silently
        drops the most recent data."""
        from services.api.charts import MAX_POINTS
        self.assertEqual(MAX_POINTS, IngestService.MAX_BUCKETS)

    def test_an_honoured_bucket_is_not_marked_adjusted(self):
        self.post(T0, 1)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 300.0)
        self.assertFalse(out["bucket_adjusted"])
        self.assertEqual(300.0, out["bucket_seconds"])

    def test_a_zero_bucket_is_chosen_for_the_caller(self):
        self.post(T0, 1)
        out = self.svc.zone_series("ZONE-01", T0, T0 + 3600, 0)
        self.assertTrue(out["bucket_adjusted"])
        self.assertGreater(out["bucket_seconds"], 0)


class TestZoneAt(Base):
    def setUp(self):
        super().setUp()
        self.zones("CAM-01", "ZONE-01")

    def test_the_governing_reading_is_returned(self):
        self.post(T0, 2)
        self.post(T0 + 600, 7)
        out = self.svc.zone_at("ZONE-01", T0 + 900)
        self.assertEqual(7, out["state"]["occupancy"])
        self.assertEqual(300.0, out["state"]["age_seconds"])

    def test_a_reading_older_than_max_hold_is_not_trustworthy(self):
        self.post(T0, 4)
        out = self.svc.zone_at("ZONE-01", T0 + 3000)
        self.assertEqual(4, out["state"]["occupancy"])
        self.assertTrue(out["state"]["stale"])
        self.assertFalse(out["trustworthy"])

    def test_a_fresh_reading_is_trustworthy(self):
        self.post(T0, 4)
        out = self.svc.zone_at("ZONE-01", T0 + 30)
        self.assertTrue(out["trustworthy"])
        self.assertFalse(out["camera_offline"])

    def test_a_logged_outage_marks_the_reading_untrustworthy(self):
        """Fresh by age, but the event log says the camera was down."""
        self.post(T0, 4)
        self.emit("CAMERA_OFFLINE", T0 + 10)
        out = self.svc.zone_at("ZONE-01", T0 + 60)
        self.assertTrue(out["camera_offline"])
        self.assertFalse(out["trustworthy"])

    def test_nothing_before_the_instant_returns_no_state(self):
        """Not the next sample. Under write-on-change it can be hours later,
        and using it reports a reading from a time that had not happened."""
        self.post(T0 + 600, 9)
        out = self.svc.zone_at("ZONE-01", T0)
        self.assertIsNone(out["state"])
        self.assertIn("no reading", out["reason"])

    def test_ambiguity_is_refused_here_too(self):
        self.zones("CAM-02", "ZONE-01")
        self.assertEqual(409, self.svc.zone_at("ZONE-01", T0)["status"])


class TestZoneDuration(Base):
    def setUp(self):
        super().setUp()
        self.zones("CAM-01", "ZONE-01")

    def test_occupied_time_from_two_rows(self):
        self.post(T0, 0)
        self.post(T0 + 600, 3)
        self.post(T0 + 1200, 0)
        out = self.svc.zone_duration("ZONE-01", T0, T0 + 1800,
                                     field="occupancy", op="gt", value=0)
        self.assertEqual(600.0, out["total_seconds"])
        self.assertEqual(1, out["episode_count"])

    def test_a_status_condition(self):
        self.post(T0, 9, status="WARNING")
        self.post(T0 + 300, 1, status="NORMAL")
        out = self.svc.zone_duration("ZONE-01", T0, T0 + 600, status="warning")
        self.assertEqual(300.0, out["total_seconds"])
        self.assertEqual({"status": "WARNING"}, out["condition"])

    def test_an_outage_is_not_reported_as_a_breach(self):
        """The failure that matters. A camera down for four hours must never
        be reported as four hours over capacity."""
        self.post(T0, 12, status="CRITICAL")
        self.emit("CAMERA_OFFLINE", T0 + 300)
        out = self.svc.zone_duration("ZONE-01", T0, T0 + 14400,
                                     field="occupancy", op="gt", value=0)
        self.assertEqual(300.0, out["total_seconds"])
        self.assertEqual(14100.0, out["unobserved_seconds"])

    def test_a_misspelt_field_is_refused_not_answered_with_zero(self):
        """Zero seconds from a typo is indistinguishable from a real answer of
        never, which is how a chatbot confidently reports the wrong thing."""
        out = self.svc.zone_duration("ZONE-01", T0, T0 + 600,
                                     field="occupanci", op="gt", value=0)
        self.assertEqual(422, out["status"])
        self.assertIn("occupancy", out["message"])

    def test_a_bad_operator_is_refused(self):
        out = self.svc.zone_duration("ZONE-01", T0, T0 + 600,
                                     field="occupancy", op="=~", value=0)
        self.assertEqual(422, out["status"])

    def test_the_condition_is_echoed_back(self):
        self.post(T0, 1)
        out = self.svc.zone_duration("ZONE-01", T0, T0 + 600,
                                     field="density", op="gte", value=0.5)
        self.assertEqual({"field": "density", "op": "gte", "value": 0.5},
                         out["condition"])


class TestReportGapsAndCoverage(Base):
    def test_the_report_carries_gaps_per_zone(self):
        """`coverage: 0.4` does not say whether the camera was down for one
        stretch overnight or flapping all day, and those support different
        conclusions from the same average."""
        self.zones("CAM-01", "ZONE-01")
        self.post(T0, 3)
        self.emit("CAMERA_OFFLINE", T0 + 600)
        self.emit("CAMERA_ONLINE", T0 + 1200)
        self.post(T0 + 1200, 3)
        report = self.svc.occupancy_report(T0, T0 + 1800)
        zone = report["zones"][0]
        self.assertEqual(["camera_offline"], [g["reason"] for g in zone["gaps"]])
        self.assertEqual(600.0, zone["gaps"][0]["seconds"])
        self.assertLess(zone["coverage"], 1.0)

    def test_totals_carry_the_worst_coverage_not_the_mean(self):
        """A report whose zones range from 1.00 to 0.05 is not "52% observed"
        — one camera was down and anything drawn from it is unsafe."""
        self.zones("CAM-01", "ZONE-01")
        self.zones("CAM-02", "ZONE-02")
        for i in range(13):
            self.post(T0 + i * 300, 1, zone="ZONE-01", camera="CAM-01")
        self.post(T0, 1, zone="ZONE-02", camera="CAM-02")
        report = self.svc.occupancy_report(T0, T0 + 3600)
        coverages = [z["coverage"] for z in report["zones"]]
        self.assertEqual(min(coverages), report["totals"]["min_coverage"])
        self.assertLess(report["totals"]["min_coverage"], 1.0)

    def test_a_fully_covered_report_says_so(self):
        self.zones("CAM-01", "ZONE-01")
        for i in range(13):
            self.post(T0 + i * 300, 2)
        report = self.svc.occupancy_report(T0, T0 + 3600)
        self.assertEqual(1.0, report["totals"]["min_coverage"])
        self.assertEqual([], report["zones"][0]["gaps"])

    def test_two_cameras_sharing_a_zone_id_are_two_report_rows(self):
        """A real defect this found: zone_state_stats grouped on zone_id alone,
        so the lobby on one camera and the loading bay on another collapsed
        into ONE row whose averages were computed across both physical areas
        and whose camera_id was whichever id sorted last. Nothing in the output
        showed it had happened.
        """
        for i in range(4):
            self.post(T0 + i * 300, 10, zone="ZONE-01", camera="CAM-01")
            self.post(T0 + i * 300, 0, zone="ZONE-01", camera="CAM-02")
        report = self.svc.occupancy_report(T0, T0 + 1800)
        rows = {z["camera_id"]: z for z in report["zones"]}
        self.assertEqual({"CAM-01", "CAM-02"}, set(rows))
        self.assertEqual(10, rows["CAM-01"]["peak_occupancy"])
        self.assertEqual(0, rows["CAM-02"]["peak_occupancy"],
                         "the busy camera must not leak into the quiet one")

    def test_an_empty_report_has_no_coverage_rather_than_zero(self):
        report = self.svc.occupancy_report(T0, T0 + 3600)
        self.assertEqual([], report["zones"])
        self.assertIsNone(report["totals"]["min_coverage"])


class TestSeriesCharts(unittest.TestCase):
    def chart(self, series):
        from services.api.charts import series_charts
        return series_charts(series)

    def test_a_null_bucket_stays_null_in_the_chart(self):
        """The one place the "never substitute 0" rule has teeth: a break in
        the line, not an empty room."""
        charts = self.chart({"zone_id": "ZONE-01", "camera_id": "CAM-01",
                             "coverage": 0.5,
                             "points": [{"from": 0, "occupancy": 4.0},
                                        {"from": 300, "occupancy": None}]})
        data = charts[0]["datasets"][0]["data"]
        self.assertEqual([4.0, None], data)

    def test_coverage_is_shown_only_when_incomplete(self):
        full = self.chart({"zone_id": "Z", "coverage": 1.0,
                           "points": [{"from": 0, "occupancy": 1.0}]})
        part = self.chart({"zone_id": "Z", "coverage": 0.31,
                           "points": [{"from": 0, "occupancy": 1.0}]})
        self.assertEqual(["zone_occupancy_series"], [c["id"] for c in full])
        self.assertIn("series_coverage", [c["id"] for c in part])
        self.assertEqual(31.0, part[1]["value"])

    def test_an_entirely_unobserved_series_produces_no_chart(self):
        """A chart of nothing looks configured and renders empty, which the
        tile owner cannot diagnose."""
        self.assertEqual([], self.chart({"zone_id": "Z", "coverage": 0.0,
                                         "points": [{"from": 0, "occupancy": None}]}))

    def test_no_points_produces_no_chart(self):
        self.assertEqual([], self.chart({"zone_id": "Z", "points": []}))
        self.assertEqual([], self.chart({}))

    def test_the_chart_survives_validation(self):
        """_valid() drops anything FinBlade would discard, and a chart that is
        dropped at the far side looks configured and renders nothing."""
        from services.api.charts import tag
        block = tag(self.chart({
            "zone_id": "ZONE-01", "camera_id": "CAM-01", "coverage": 0.5,
            "points": [{"from": 0, "occupancy": 1.0, "peak_occupancy": 3},
                       {"from": 300, "occupancy": None, "peak_occupancy": None}]}))
        self.assertIsNotNone(block)
        self.assertEqual(2, len(block["charts"]))


if __name__ == "__main__":
    unittest.main()
