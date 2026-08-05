"""Saved frames are images of people. They must not be public.

/bookmarks and /media were in auth._OPEN_PREFIXES, so incident snapshots and
reference stills were served to anyone who could reach the port, with no
credential, while every JSON route was gated. Filenames are guessable
(bm_<camera>_<seq>.jpg), so this was enumerable, not merely reachable.

Also covers GET /api/v1/incidents/{alert_id}/frame, the route integrations
should use: it takes an alert id instead of a filesystem-shaped path, and it
returns the frame from WHEN the incident happened rather than a live snapshot
of the room now.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from services.api import auth                                   # noqa: E402

try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc, _BOOKMARKS_DIR
    HAVE_APP = True
except Exception:                                  # noqa: BLE001
    HAVE_APP = False

JPEG = b"\xff\xd8\xff\xe0INCIDENT-FRAME"


class Headers(dict):
    def get(self, k, default=None):
        return dict.get(self, k.lower(), default)


class TestPrefixesAreGated(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "s3cret"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)

    def test_saved_frames_are_not_open_paths(self):
        for path in ("/bookmarks/bm_CAM-01_00012.jpg",
                     "/media/CAM-01_frame.jpg"):
            self.assertFalse(auth.path_is_open(path), f"{path} must require a key")
            self.assertFalse(auth.request_is_authorised(path, Headers(), {}), path)

    def test_the_dashboard_itself_still_bootstraps_without_a_key(self):
        """The pages are what ask the user for the key; gating them deadlocks."""
        for path in ("/web/dashboard.html", "/web/apikey.js",
                     "/tools/zone-editor.html", "/openapi.json", "/"):
            self.assertTrue(auth.request_is_authorised(path, Headers(), {}), path)

    def test_query_key_works_on_saved_frames(self):
        """<img src> cannot send a header — same reason as the MJPEG stream."""
        for path in ("/bookmarks/x.jpg", "/media/CAM-01_frame.jpg"):
            self.assertTrue(
                auth.request_is_authorised(path, Headers(), {"key": "s3cret"}), path)
            self.assertFalse(
                auth.request_is_authorised(path, Headers(), {"key": "wrong"}), path)

    def test_query_key_is_still_refused_on_json_routes(self):
        """Widening ?key= to two prefixes must not widen it generally."""
        for path in ("/api/v1/alerts", "/api/v1/cameras", "/api/v1/summary"):
            self.assertFalse(
                auth.request_is_authorised(path, Headers(), {"key": "s3cret"}), path)


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestIncidentFrameRoute(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        os.environ["FINBLADE_INTEGRATION_KEY"] = "scoped-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.addCleanup(os.environ.pop, "FINBLADE_INTEGRATION_KEY", None)
        self.client = TestClient(app)
        self.scoped = {"Authorization": "Bearer scoped-key"}
        os.makedirs(_BOOKMARKS_DIR, exist_ok=True)
        self.frame_path = os.path.join(_BOOKMARKS_DIR, "test_incident.jpg")
        with open(self.frame_path, "wb") as fh:
            fh.write(JPEG)
        self.addCleanup(lambda: os.path.exists(self.frame_path)
                        and os.remove(self.frame_path))
        self.alert_id = str(svc.raise_alert({
            "rule_id": "R-06", "severity": "RED", "message": "intrusion",
            "zone_id": "ZONE-02", "camera_id": "CAM-01", "ts": 100.0,
            "kind": "FIRE", "frame": "/bookmarks/test_incident.jpg"}))

    def test_served_to_an_integration_key_with_the_right_content_type(self):
        r = self.client.get(f"/api/v1/incidents/{self.alert_id}/frame",
                            headers=self.scoped)
        self.assertEqual(200, r.status_code)
        self.assertEqual("image/jpeg", r.headers["content-type"])
        self.assertEqual(JPEG, r.content)

    def test_requires_a_key(self):
        self.assertEqual(
            401, self.client.get(f"/api/v1/incidents/{self.alert_id}/frame").status_code)

    def test_unknown_alert_is_404(self):
        r = self.client.get("/api/v1/incidents/no-such-alert/frame",
                            headers=self.scoped)
        self.assertEqual(404, r.status_code)
        self.assertEqual("unknown alert", r.json()["error"])

    def test_alert_without_a_frame_explains_itself(self):
        aid = str(svc.raise_alert({"rule_id": "R-01", "severity": "AMBER",
                                   "message": "density", "zone_id": "ZONE-01",
                                   "camera_id": "CAM-01", "ts": 100.0,
                                   "kind": "FIRE"}))
        r = self.client.get(f"/api/v1/incidents/{aid}/frame", headers=self.scoped)
        self.assertEqual(404, r.status_code)
        self.assertIn("R-02", r.json()["detail"], "should name which rules carry frames")

    def test_traversal_in_a_stored_reference_cannot_read_other_files(self):
        """The ref comes from the database. A traversal in it must not become
        an arbitrary file read."""
        aid = str(svc.raise_alert({
            "rule_id": "R-06", "severity": "RED", "message": "x",
            "camera_id": "CAM-01", "ts": 100.0, "kind": "FIRE",
            "frame": "/bookmarks/../../../services/api/auth.py"}))
        r = self.client.get(f"/api/v1/incidents/{aid}/frame", headers=self.scoped)
        self.assertIn(r.status_code, (400, 404))
        self.assertNotIn(b"FINBLADE_API_KEY", r.content)

    def test_missing_file_is_404_not_500(self):
        os.remove(self.frame_path)
        r = self.client.get(f"/api/v1/incidents/{self.alert_id}/frame",
                            headers=self.scoped)
        self.assertEqual(404, r.status_code)


if __name__ == "__main__":
    unittest.main()
