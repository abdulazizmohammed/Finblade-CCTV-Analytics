"""HTTP tests for the pull integration — GET /api/v1/summary + the scoped key.

This is the path a consuming platform (FinBlade) uses to render its own tiles
from our data: its backend polls, its browsers never touch this host. The unit
tests in test_auth.py cover the scope rules; these assert the wiring — that the
middleware really returns 403 rather than 401, and that /summary agrees with the
individual endpoints it replaces.

FINBLADE_INMEMORY is set before importing the app so this never touches the real
SQLite database.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

try:
    from fastapi.testclient import TestClient

    from services.api.app import app, svc
    HAVE_APP = True
except Exception as _exc:                      # noqa: BLE001
    HAVE_APP = False
    IMPORT_ERROR = _exc


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestSummaryEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_summary_carries_every_section_a_tile_board_needs(self):
        r = self.client.get("/api/v1/summary")
        self.assertEqual(200, r.status_code)
        body = r.json()
        for key in ("cameras", "zones", "alerts", "counts", "ts"):
            self.assertIn(key, body)
        self.assertIsInstance(body["cameras"], list)
        self.assertIsInstance(body["zones"], list)
        self.assertIsInstance(body["alerts"], list)

    def test_summary_agrees_with_the_endpoints_it_replaces(self):
        """One call must not become a second source of truth."""
        summary = self.client.get("/api/v1/summary").json()
        self.assertEqual(
            [c["camera_id"] for c in self.client.get("/api/v1/cameras").json()["cameras"]],
            [c["camera_id"] for c in summary["cameras"]])
        self.assertEqual(self.client.get("/api/v1/zones/state").json()["zones"],
                         summary["zones"])
        self.assertEqual(self.client.get("/api/v1/alerts").json()["alerts"],
                         summary["alerts"])

    def test_internal_stream_url_is_not_exposed(self):
        """stream_url points at a per-camera MJPEG port (8090+) that is
        reassigned on restart and unreachable wherever only 8000 is open. A
        remote consumer reading it gets a URL that works in testing and fails
        in production."""
        svc.record_camera_health({
            "camera_id": "CAM-SUM", "site_id": "SITE-1", "ts": 1.0,
            "health": {"state": "ONLINE", "stream_url": "http://127.0.0.1:8090/stream"}})
        # Guard against a vacuous assertion: /cameras must still carry it, or
        # this test would pass even if the field were never set at all.
        plain = self.client.get("/api/v1/cameras").json()["cameras"]
        self.assertIn("stream_url",
                      next(c for c in plain if c["camera_id"] == "CAM-SUM"))

        cams = self.client.get("/api/v1/summary").json()["cameras"]
        row = next((c for c in cams if c["camera_id"] == "CAM-SUM"), None)
        self.assertIsNotNone(row, "camera missing from summary")
        self.assertNotIn("stream_url", row)
        self.assertEqual("/api/v1/cameras/CAM-SUM/snapshot", row["snapshot_path"])
        self.assertEqual("/api/v1/cameras/CAM-SUM/stream", row["stream_path"])

    def test_camera_ids_are_url_encoded_in_the_paths(self):
        """camera_id is operator-chosen and specified as opaque. An id with a
        space produces a path a consumer cannot request unless we encode it."""
        svc.record_camera_health({
            "camera_id": "CAM 01/A", "site_id": "SITE-1", "ts": 1.0,
            "health": {"state": "ONLINE"}})
        cams = self.client.get("/api/v1/summary").json()["cameras"]
        row = next((c for c in cams if c["camera_id"] == "CAM 01/A"), None)
        self.assertIsNotNone(row, "camera missing from summary")
        self.assertEqual("/api/v1/cameras/CAM%2001%2FA/snapshot", row["snapshot_path"])


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestScopedKeyOverHttp(unittest.TestCase):
    """End-to-end: the middleware, not just the auth helper."""

    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        os.environ["FINBLADE_INTEGRATION_KEY"] = "scoped-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.addCleanup(os.environ.pop, "FINBLADE_INTEGRATION_KEY", None)
        self.client = TestClient(app)
        self.scoped = {"Authorization": "Bearer scoped-key"}

    def test_scoped_key_reads_the_summary(self):
        self.assertEqual(200, self.client.get("/api/v1/summary",
                                              headers=self.scoped).status_code)

    def test_no_key_is_401(self):
        self.assertEqual(401, self.client.get("/api/v1/summary").status_code)

    def test_scoped_key_cannot_delete_alert_history(self):
        r = self.client.delete("/api/v1/alerts", headers=self.scoped)
        self.assertEqual(403, r.status_code)
        self.assertEqual("forbidden", r.json()["error"])

    def test_scoped_key_cannot_overwrite_zone_polygons(self):
        r = self.client.post("/api/v1/zones", headers=self.scoped,
                             json={"camera_id": "CAM-01", "zones": []})
        self.assertEqual(403, r.status_code)

    def test_scoped_key_may_acknowledge_an_alert(self):
        """The one write the integration needs — and it carries attribution."""
        alert_id = str(svc.raise_alert({
            "rule_id": "R-01", "severity": "AMBER", "message": "test",
            "zone_id": "ZONE-01", "camera_id": "CAM-01", "ts": 1.0}))
        r = self.client.post(f"/api/v1/alerts/{alert_id}/ack", headers=self.scoped,
                             json={"acknowledged_by": "operator@finblade"})
        self.assertEqual(200, r.status_code)
        self.assertEqual("operator@finblade", r.json()["acknowledged_by"])

    def test_repeated_ack_is_409_and_terminal(self):
        """Documented as terminal; a client that retries non-2xx retries forever."""
        alert_id = str(svc.raise_alert({
            "rule_id": "R-01", "severity": "AMBER", "message": "test2",
            "zone_id": "ZONE-01", "camera_id": "CAM-01", "ts": 1.0}))
        body = {"acknowledged_by": "operator@finblade"}
        first = self.client.post(f"/api/v1/alerts/{alert_id}/ack",
                                 headers=self.scoped, json=body)
        second = self.client.post(f"/api/v1/alerts/{alert_id}/ack",
                                  headers=self.scoped, json=body)
        self.assertEqual(200, first.status_code)
        self.assertEqual(409, second.status_code)


if __name__ == "__main__":
    unittest.main()
