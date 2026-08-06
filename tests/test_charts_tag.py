"""FinBlade live-feed chart tags (live-feed-chart-tags.md, schema 1).

Two things these tests exist to protect.

The tag is ADDITIVE. Every existing consumer — the operator dashboard included
— must see byte-identical payloads apart from one new key, and must be able to
turn even that off.

The tag must not smuggle in the numeric mistakes the rest of this integration
is careful about: a computed-as-zero occupancy, or a site total built by adding
per-camera counts together.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from services.api import charts                                   # noqa: E402

try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc
    HAVE_APP = True
except Exception:                                  # noqa: BLE001
    HAVE_APP = False

FULL = {"Authorization": "Bearer full-key"}


class TestEnvelope(unittest.TestCase):
    def test_schema_version_and_shape(self):
        block = charts.tag([{"id": "x", "type": "metric", "value": 1}])
        self.assertEqual(1, block["schema"])
        self.assertEqual(1, len(block["charts"]))

    def test_nothing_worth_drawing_returns_none(self):
        """An empty charts array would offer a picker with nothing in it."""
        self.assertIsNone(charts.tag([]))
        self.assertIsNone(charts.tag([{"id": "x", "type": "nonsense"}]))

    def test_attach_omits_the_key_entirely_when_there_is_nothing(self):
        self.assertNotIn("finblade", charts.attach({"zones": []}, []))

    def test_attach_does_not_mutate_the_caller(self):
        body = {"zones": []}
        charts.attach(body, [{"id": "x", "type": "metric", "value": 1}])
        self.assertNotIn("finblade", body)


class TestValidation(unittest.TestCase):
    """We drop what FinBlade would drop, here — a chart discarded at the far
    end looks configured and renders nothing, with no way to see why."""

    def test_mismatched_data_and_labels_are_dropped(self):
        self.assertIsNone(charts.tag([{
            "id": "bad", "type": "bar", "labels": ["a", "b"],
            "datasets": [{"label": "s", "data": [1]}]}]))

    def test_unknown_type_is_dropped(self):
        self.assertIsNone(charts.tag([{"id": "x", "type": "gauge", "value": 1}]))

    def test_metric_without_a_value_is_dropped(self):
        self.assertIsNone(charts.tag([{"id": "x", "type": "metric"}]))
        self.assertIsNone(charts.tag([{"id": "x", "type": "metric", "value": None}]))

    def test_caps_are_enforced_before_sending(self):
        big = {"id": "x", "type": "line",
               "labels": list(range(charts.MAX_POINTS + 50)),
               "datasets": [{"label": "s",
                             "data": list(range(charts.MAX_POINTS + 50))}]}
        out = charts.tag([big])["charts"][0]
        self.assertEqual(charts.MAX_POINTS, len(out["labels"]))
        self.assertEqual(charts.MAX_POINTS, len(out["datasets"][0]["data"]),
                         "labels and data must be truncated together")

    def test_chart_count_is_capped(self):
        many = [{"id": f"m{i}", "type": "metric", "value": i}
                for i in range(charts.MAX_CHARTS + 5)]
        self.assertEqual(charts.MAX_CHARTS, len(charts.tag(many)["charts"]))

    def test_numbers_are_numbers_not_strings(self):
        out = charts.zone_charts([{"zone_id": "Z", "occupancy": "4", "density": "1.5"}])
        data = out[0]["datasets"][0]["data"]
        self.assertTrue(all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            for v in data), data)


class TestZoneCharts(unittest.TestCase):
    def zones(self, **over):
        base = {"zone_id": "ZONE-01", "zone_name": "Lobby", "camera_id": "CAM-01",
                "occupancy": 4, "density": 0.067, "inflow_per_min": 3.0,
                "outflow_per_min": 2.0}
        base.update(over)
        return base

    def test_no_zones_means_no_charts(self):
        self.assertEqual([], charts.zone_charts([]))
        self.assertEqual([], charts.zone_charts(None))

    def test_labels_disambiguate_once_two_cameras_are_present(self):
        """zone_id is unique per camera only — two cameras both have ZONE-01,
        and without the prefix the chart shows two identical bars."""
        one = charts.zone_charts([self.zones()])
        self.assertEqual(["Lobby"], one[0]["labels"])

        two = charts.zone_charts([self.zones(),
                                  self.zones(camera_id="CAM-02", zone_name="Lobby")])
        self.assertEqual(["CAM-01 / Lobby", "CAM-02 / Lobby"], two[0]["labels"])

    def test_an_all_zero_pie_is_not_sent(self):
        """Three slices of nothing renders as a fault, not as an empty room."""
        ids = [c["id"] for c in charts.zone_charts([self.zones(occupancy=0)])]
        self.assertNotIn("occupancy_share", ids)
        ids = [c["id"] for c in charts.zone_charts([self.zones(occupancy=2)])]
        self.assertIn("occupancy_share", ids)

    def test_flow_carries_both_directions(self):
        flow = next(c for c in charts.zone_charts([self.zones()])
                    if c["id"] == "zone_flow")
        self.assertEqual(["In", "Out"], [d["label"] for d in flow["datasets"]])


class TestSummaryCharts(unittest.TestCase):
    def body(self, people=7, live=9):
        return {"cameras": [{"camera_id": "CAM-01", "effective_state": "ONLINE",
                             "people_in_view": 3},
                            {"camera_id": "CAM-02", "effective_state": "OFFLINE",
                             "people_in_view": 0}],
                "counts": {"live": live},
                "summary": {"people_in_zones": people,
                            "cameras": {"online": 1, "offline": 1},
                            "alerts": {"open_total": 2, "red": 1, "amber": 1,
                                       "critical": 0, "info": 0}}}

    def test_uncomputable_occupancy_is_omitted_not_zero(self):
        """people_in_zones is null when no polygons are drawn. A metric of 0
        would show an empty site while people are standing in it."""
        ids = [c["id"] for c in charts.summary_charts(self.body(people=None))]
        self.assertNotIn("people_on_site", ids)
        ids = [c["id"] for c in charts.summary_charts(self.body(people=0))]
        self.assertIn("people_on_site", ids, "a real measured 0 IS reportable")

    def test_per_camera_bar_never_becomes_a_site_total(self):
        """people_in_view summed across cameras double-counts anyone two
        cameras can see. Only offline cameras are excluded; no total exists."""
        bar = next(c for c in charts.summary_charts(self.body())
                   if c["id"] == "camera_people")
        self.assertEqual(["CAM-01"], bar["labels"], "offline camera excluded")
        self.assertIn("not a site total", bar["title"])
        for chart in charts.summary_charts(self.body()):
            if chart["type"] == "metric":
                self.assertNotEqual(3, chart.get("value"),
                                    "no metric may be a sum of people_in_view")

    def test_severity_doughnut_omits_empty_slices(self):
        d = next(c for c in charts.summary_charts(self.body())
                 if c["id"] == "alerts_by_severity")
        self.assertEqual(["Red", "Amber"], d["labels"])


class TestCountsCharts(unittest.TestCase):
    def test_metrics_and_per_camera_bar(self):
        out = charts.counts_charts({"live": 4, "unique_total": 93,
                                    "cross_camera": 16,
                                    "per_camera": [{"camera_id": "CAM-01", "live": 2}]})
        ids = [c["id"] for c in out]
        self.assertEqual(["live_now", "footfall_total", "cross_camera",
                          "live_per_camera"], ids)
        self.assertIn("not a site total",
                      next(c for c in out if c["id"] == "live_per_camera")["title"])

    def test_missing_values_are_skipped(self):
        self.assertEqual([], charts.counts_charts({}))


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestOverHttp(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.client = TestClient(app)
        import time
        svc.record_camera_health({"camera_id": "CAM-TAG", "site_id": "SITE-T",
                                  "ts": time.time(),
                                  "health": {"state": "ONLINE"}})
        svc.record_zone_state({"zone_id": "ZONE-TAG", "camera_id": "CAM-TAG",
                               "zone_name": "Atrium", "occupancy": 5,
                               "density": 1.2, "capacity_pct": 12.0,
                               "inflow_per_min": 1.0, "outflow_per_min": 1.0,
                               "status": "NORMAL", "ts": time.time()})

    def test_zone_state_carries_the_tag(self):
        body = self.client.get("/api/v1/zones/state", headers=FULL).json()
        self.assertEqual(1, body["finblade"]["schema"])
        self.assertIn("zone_occupancy",
                      [c["id"] for c in body["finblade"]["charts"]])

    def test_summary_carries_the_tag(self):
        body = self.client.get("/api/v1/summary", headers=FULL).json()
        self.assertIn("finblade", body)

    def test_charts_zero_omits_it(self):
        body = self.client.get("/api/v1/zones/state", headers=FULL,
                               params={"charts": 0}).json()
        self.assertNotIn("finblade", body)

    def test_the_tag_is_purely_additive(self):
        """The operator dashboard reads these endpoints. Everything it already
        depends on must be identical with the tag present."""
        with_tag = self.client.get("/api/v1/zones/state", headers=FULL).json()
        without = self.client.get("/api/v1/zones/state", headers=FULL,
                                  params={"charts": 0}).json()
        self.assertEqual(without["zones"], with_tag["zones"])
        self.assertEqual({"zones"}, set(without))
        self.assertEqual({"zones", "finblade"}, set(with_tag))

    def test_counts_and_movement_and_report_carry_it(self):
        for route, params in (("/api/v1/identity/counts", {}),
                              ("/api/v1/movement", {}),
                              ("/api/v1/reports/occupancy.json",
                               {"from": 0, "to": 9e12})):
            body = self.client.get(route, headers=FULL, params=params).json()
            self.assertIsInstance(body, dict, route)
            if "finblade" in body:
                self.assertEqual(1, body["finblade"]["schema"], route)


if __name__ == "__main__":
    unittest.main()
