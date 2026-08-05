"""Capability gaps the Option 1 audit found, and the contract that closes them.

Covers the site summary block, site_id propagation, alert detail by id, the new
filters, and pagination. Every one of these is additive: the assertions at the
bottom pin that a caller passing no new parameter sees exactly what it saw
before, because the operator dashboard is such a caller.
"""

import os
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc
    HAVE_APP = True
except Exception:                                  # noqa: BLE001
    HAVE_APP = False

FULL = {"Authorization": "Bearer full-key"}


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class Base(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.addCleanup(os.environ.pop, "FINBLADE_SITE_ID", None)
        self.client = TestClient(app)
        self.now = time.time()
        svc.record_camera_health({"camera_id": "CAM-CAP", "site_id": "SITE-CAP",
                                  "ts": self.now,
                                  "health": {"state": "ONLINE", "enabled": True}})


class TestSiteSummary(Base):
    def test_summary_block_pre_tallies_what_a_tile_needs(self):
        svc.record_zone_state({
            "zone_id": "ZONE-01", "camera_id": "CAM-CAP", "occupancy": 7,
            "density": 2.4, "capacity_pct": 30.0, "inflow_per_min": 1.0,
            "outflow_per_min": 1.0, "status": "WARNING", "ts": self.now})
        svc.raise_alert({"rule_id": "R-01", "severity": "AMBER", "message": "m",
                         "camera_id": "CAM-CAP", "zone_id": "ZONE-01",
                         "ts": self.now, "kind": "FIRE"})
        body = self.client.get("/api/v1/summary", headers=FULL).json()
        s = body["summary"]
        self.assertIn("site_id", body)
        self.assertGreaterEqual(s["cameras"]["online"], 1)
        self.assertGreaterEqual(s["zones"]["warning"], 1)
        self.assertGreaterEqual(s["alerts"]["amber"], 1)
        self.assertGreaterEqual(s["alerts"]["open_total"], 1)
        # The tally must equal the array it summarises, whatever else the
        # shared in-process store is holding from other tests.
        self.assertEqual(sum(int(z["occupancy"]) for z in body["zones"]),
                         s["people_in_zones"])
        self.assertEqual(len(body["alerts"]), s["alerts"]["open_total"])
        self.assertEqual(len(body["cameras"]), sum(s["cameras"].values()))

    def test_people_in_zones_is_none_when_no_polygons_are_drawn(self):
        """Occupancy that cannot be computed is not zero. Rendering 0 shows an
        empty site while people are standing in it."""
        from services.api.app import svc as service
        original = service.zone_states
        service.zone_states = lambda: []
        self.addCleanup(setattr, service, "zone_states", original)
        s = self.client.get("/api/v1/summary", headers=FULL).json()["summary"]
        self.assertIsNone(s["people_in_zones"])

    def test_configured_site_id_reaches_the_response(self):
        os.environ["FINBLADE_SITE_ID"] = "SITE-OVERRIDE"
        self.assertEqual("SITE-OVERRIDE",
                         self.client.get("/api/v1/summary", headers=FULL)
                         .json()["site_id"])


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestSiteIdDerivation(unittest.TestCase):
    """Tested against the helper directly: the module-level store is shared by
    the whole suite, so the derived value depends on what other tests
    registered — which is precisely the ambiguity this helper handles."""

    def setUp(self):
        self.addCleanup(os.environ.pop, "FINBLADE_SITE_ID", None)
        os.environ.pop("FINBLADE_SITE_ID", None)

    def test_one_site_is_derived_from_the_cameras(self):
        from services.api.app import _site_id
        self.assertEqual("SITE-A", _site_id([{"site_id": "SITE-A"},
                                             {"site_id": "SITE-A"}]))

    def test_disagreeing_cameras_yield_none_not_a_guess(self):
        """A wrong site label is worse than an absent one for a platform that
        routes records by it."""
        from services.api.app import _site_id
        self.assertIsNone(_site_id([{"site_id": "SITE-A"}, {"site_id": "SITE-B"}]))

    def test_no_cameras_yields_none(self):
        from services.api.app import _site_id
        self.assertIsNone(_site_id([]))

    def test_configuration_overrides_derivation(self):
        from services.api.app import _site_id
        os.environ["FINBLADE_SITE_ID"] = "SITE-CONFIGURED"
        self.assertEqual("SITE-CONFIGURED",
                         _site_id([{"site_id": "SITE-A"}, {"site_id": "SITE-B"}]))


class TestSiteIdPropagation(Base):
    def test_an_alert_inherits_its_camera_site(self):
        """Workers post a camera id, not a site. A platform routing records by
        site should not have to join camera data to attribute one."""
        aid = str(svc.raise_alert({"rule_id": "R-06", "severity": "RED",
                                   "message": "intrusion", "camera_id": "CAM-CAP",
                                   "zone_id": "ZONE-02", "ts": self.now,
                                   "kind": "FIRE"}))
        alert = self.client.get(f"/api/v1/alerts/{aid}", headers=FULL).json()
        self.assertEqual("SITE-CAP", alert["site_id"])

    def test_an_explicit_site_is_not_overwritten(self):
        aid = str(svc.raise_alert({"rule_id": "R-01", "severity": "AMBER",
                                   "message": "m", "camera_id": "CAM-CAP",
                                   "site_id": "SITE-EXPLICIT", "ts": self.now,
                                   "kind": "FIRE"}))
        self.assertEqual("SITE-EXPLICIT",
                         self.client.get(f"/api/v1/alerts/{aid}",
                                         headers=FULL).json()["site_id"])

    def test_zone_state_inherits_the_site_too(self):
        svc.record_zone_state({
            "zone_id": "ZONE-SITE", "camera_id": "CAM-CAP", "occupancy": 1,
            "density": 0.1, "capacity_pct": 1.0, "inflow_per_min": 0.0,
            "outflow_per_min": 0.0, "status": "NORMAL", "ts": self.now})
        row = next(z for z in self.client.get("/api/v1/zones/state", headers=FULL)
                   .json()["zones"] if z["zone_id"] == "ZONE-SITE")
        self.assertEqual("SITE-CAP", row.get("site_id"))


class TestAlertDetail(Base):
    def test_detail_by_id_and_a_frame_url_that_is_not_a_file_path(self):
        aid = str(svc.raise_alert({"rule_id": "R-02", "severity": "RED",
                                   "message": "density", "camera_id": "CAM-CAP",
                                   "ts": self.now, "kind": "FIRE",
                                   "frame": "/bookmarks/x.jpg"}))
        body = self.client.get(f"/api/v1/alerts/{aid}", headers=FULL).json()
        self.assertEqual(aid, str(body["alert_id"]))
        self.assertEqual(f"/api/v1/incidents/{aid}/frame", body["frame_url"])

    def test_a_resolved_alert_is_still_retrievable(self):
        """It has left the active feed; a consumer following up on a pushed
        alert must still be able to read it."""
        aid = str(svc.raise_alert({"rule_id": "R-01", "severity": "AMBER",
                                   "message": "m", "camera_id": "CAM-CAP",
                                   "ts": self.now, "kind": "FIRE"}))
        svc.resolve(aid, "RESOLVED", "op", self.now)
        self.assertEqual(200, self.client.get(f"/api/v1/alerts/{aid}",
                                              headers=FULL).status_code)

    def test_unknown_alert_is_404(self):
        self.assertEqual(404, self.client.get("/api/v1/alerts/nope",
                                              headers=FULL).status_code)


class TestFilters(Base):
    def setUp(self):
        super().setUp()
        for rule, sev, zone in (("R-01", "AMBER", "ZONE-A"),
                                ("R-02", "RED", "ZONE-B"),
                                ("R-06", "RED", "ZONE-A")):
            svc.raise_alert({"rule_id": rule, "severity": sev, "message": "m",
                             "camera_id": "CAM-CAP", "zone_id": zone,
                             "ts": self.now, "kind": "FIRE"})

    def get(self, **params):
        return self.client.get("/api/v1/alerts", headers=FULL,
                               params=params).json()["alerts"]

    def test_filter_by_severity(self):
        self.assertTrue(all(a["severity"] == "RED" for a in self.get(severity="RED")))
        self.assertGreaterEqual(len(self.get(severity="RED")), 2)

    def test_filter_is_case_insensitive(self):
        self.assertEqual(len(self.get(severity="RED")), len(self.get(severity="red")))

    def test_filter_by_zone_and_rule(self):
        self.assertTrue(all(a["zone_id"] == "ZONE-A" for a in self.get(zone_id="ZONE-A")))
        self.assertTrue(all(a["rule_id"] == "R-06" for a in self.get(rule_id="R-06")))

    def test_unmatched_filter_returns_empty_not_everything(self):
        self.assertEqual([], self.get(severity="NO-SUCH-SEVERITY"))

    def test_zone_state_filters(self):
        for zid in ("ZONE-F1", "ZONE-F2"):
            svc.record_zone_state({
                "zone_id": zid, "camera_id": "CAM-CAP", "occupancy": 1,
                "density": 0.1, "capacity_pct": 1.0, "inflow_per_min": 0.0,
                "outflow_per_min": 0.0, "status": "NORMAL", "ts": self.now})
        rows = self.client.get("/api/v1/zones/state", headers=FULL,
                               params={"zone_id": "ZONE-F1"}).json()["zones"]
        self.assertTrue(rows and all(z["zone_id"] == "ZONE-F1" for z in rows))


class TestPagination(Base):
    def setUp(self):
        super().setUp()
        for i in range(7):
            svc.raise_alert({"rule_id": "R-01", "severity": "AMBER",
                             "message": f"page-{i}", "camera_id": "CAM-PAGE",
                             "ts": self.now + i, "kind": "FIRE"})

    def page(self, **params):
        return self.client.get("/api/v1/history/alerts", headers=FULL,
                               params=dict(params, **{"from": 0, "to": 9e12})).json()

    def test_has_more_is_a_fact_not_a_guess(self):
        first = self.page(limit=3, offset=0)
        self.assertEqual(3, len(first["alerts"]))
        self.assertTrue(first["page"]["has_more"])

    def test_last_page_reports_no_more(self):
        self.assertFalse(self.page(limit=100, offset=0)["page"]["has_more"])

    def test_offset_advances_without_repeating_rows(self):
        ids = lambda p: [a["alert_id"] for a in p["alerts"]]           # noqa: E731
        first, second = self.page(limit=3, offset=0), self.page(limit=3, offset=3)
        self.assertFalse(set(ids(first)) & set(ids(second)), "pages must not overlap")

    def test_page_metadata_shape(self):
        page = self.page(limit=2, offset=1)["page"]
        self.assertEqual({"limit": 2, "offset": 1, "returned": 2, "has_more": True},
                         page)


class TestBackwardCompatibility(Base):
    """The operator dashboard calls these with no new parameters. It must see
    exactly what it saw before."""

    def test_alerts_without_filters_is_the_full_active_feed(self):
        svc.raise_alert({"rule_id": "R-01", "severity": "AMBER", "message": "m",
                         "camera_id": "CAM-CAP", "ts": self.now, "kind": "FIRE"})
        self.assertEqual(svc.list_alerts(unacked_only=False),
                         self.client.get("/api/v1/alerts", headers=FULL)
                         .json()["alerts"])

    def test_zone_state_without_filters_is_unchanged(self):
        self.assertEqual(svc.zone_states(),
                         self.client.get("/api/v1/zones/state", headers=FULL)
                         .json()["zones"])

    def test_history_still_returns_its_array_under_the_same_key(self):
        body = self.client.get("/api/v1/history/alerts", headers=FULL,
                               params={"from": 0, "to": 9e12}).json()
        self.assertIn("alerts", body)
        self.assertIsInstance(body["alerts"], list)

    def test_movement_still_defaults_to_minutes_back_from_now(self):
        body = self.client.get("/api/v1/movement", headers=FULL).json()
        self.assertEqual(15.0, body["minutes"])
        self.assertIn("flows", body)

    def test_summary_keeps_its_original_top_level_arrays(self):
        body = self.client.get("/api/v1/summary", headers=FULL).json()
        for key in ("cameras", "zones", "alerts", "counts", "ts"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main()
