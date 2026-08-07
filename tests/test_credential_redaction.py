"""No credential may leave this process, by any route.

The audit found rtsp://user:password@host returned verbatim by GET /cameras,
GET /summary and the WebSocket frame — to the read-only integration key. That
secret unlocks the CAMERA, not this API, so a holder could pull video directly
and bypass this service entirely.

These tests are the guard. They assert on the serialised body rather than on
individual fields, so a new field carrying a source cannot reintroduce the leak
without failing here.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from services.api.redact import (contains_credentials, mask_credentials,   # noqa: E402
                                 redact_camera)

try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc
    HAVE_APP = True
except Exception:                                  # noqa: BLE001
    HAVE_APP = False

SECRET = "hunter2"
RTSP = f"rtsp://admin:{SECRET}@192.168.1.50:554/Streaming/Channels/101"


class TestMasking(unittest.TestCase):
    def test_user_and_password_both_masked(self):
        out = mask_credentials(RTSP)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("admin", out)
        self.assertIn("192.168.1.50:554", out, "host must stay readable")

    def test_password_free_url_with_a_user_is_still_masked(self):
        """rtsp://admin@host names an account; that is half a credential."""
        self.assertNotIn("admin", mask_credentials("rtsp://admin@host/s"))

    def test_at_sign_inside_the_password_does_not_leak_the_tail(self):
        """Found on live data. The first version stopped at the FIRST '@', so
            rtsp://operator:p@ssw0rd@10.0.0.5:554/Streaming/Channels/102
        came back as
            rtsp://***:***@ssw0rd@10.0.0.5:554/Streaming/Channels/102
        with the tail of the password still in the response. Masked output
        that still contains part of the secret is worse than none, because it
        looks handled.

        The URL here is INVENTED. It was the real one until a repository audit
        noticed that this file — the test for not leaking credentials — had
        committed a live camera's address, username and password tail to a
        public repo. A synthetic fixture exercises the same bug.
        """
        url = "rtsp://operator:p@ssw0rd@10.0.0.5:554/Streaming/Channels/102"
        out = mask_credentials(url)
        self.assertNotIn("ssw0rd", out)
        self.assertNotIn("operator", out)
        self.assertEqual(
            "rtsp://***:***@10.0.0.5:554/Streaming/Channels/102", out)
        self.assertFalse(contains_credentials(out))

    def test_multiple_at_signs_in_the_password(self):
        out = mask_credentials("rtsp://u:a@b@c@10.0.0.1/s")
        self.assertEqual("rtsp://***:***@10.0.0.1/s", out)
        for fragment in ("a@b", "b@c", ":a", "u:"):
            self.assertNotIn(fragment, out)

    def test_an_at_sign_in_the_path_is_not_treated_as_a_credential(self):
        """http://host/a@b has no '@' before the first '/', so there is no
        userinfo to mask and the URL must survive intact."""
        for url in ("http://host/a@b", "http://127.0.0.1:8090/stream@2",
                    "file:///media/clip@2x.mp4"):
            self.assertEqual(url, mask_credentials(url))

    def test_detector_catches_an_at_sign_password(self):
        self.assertTrue(
            contains_credentials("rtsp://admin:Secret@2030@10.0.0.1:554/x"))

    def test_urls_without_credentials_are_untouched(self):
        for url in ("rtsp://127.0.0.1:8554/cam01", "http://127.0.0.1:8090/stream",
                    "media/clip.mp4", ""):
            self.assertEqual(url, mask_credentials(url))

    def test_non_strings_pass_through(self):
        for value in (None, 42, True, ["a"], {"b": 1}):
            self.assertEqual(value, mask_credentials(value))

    def test_detector_agrees_with_the_masker(self):
        self.assertTrue(contains_credentials(RTSP))
        self.assertFalse(contains_credentials(mask_credentials(RTSP)))

    def test_redact_camera_does_not_mutate_the_input(self):
        row = {"camera_id": "C1", "source": RTSP}
        redact_camera(row)
        self.assertEqual(RTSP, row["source"], "caller's dict must be untouched")

    def test_drop_source_removes_the_field(self):
        out = redact_camera({"camera_id": "C1", "source": RTSP}, drop_source=True)
        self.assertNotIn("source", out)


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestNoCredentialInAnyResponse(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        os.environ["FINBLADE_INTEGRATION_KEY"] = "scoped-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.addCleanup(os.environ.pop, "FINBLADE_INTEGRATION_KEY", None)
        self.client = TestClient(app)
        self.full = {"Authorization": "Bearer full-key"}
        self.scoped = {"Authorization": "Bearer scoped-key"}
        svc.store.upsert_camera("CAM-SECRET", site_id="SITE-01", source=RTSP,
                                state="ONLINE")
        svc.record_camera_health({"camera_id": "CAM-SECRET", "site_id": "SITE-01",
                                  "ts": 1.0, "health": {"state": "ONLINE"}})

    def test_no_route_leaks_the_password(self):
        for route in ("/api/v1/cameras", "/api/v1/summary"):
            for label, headers in (("full", self.full), ("scoped", self.scoped)):
                body = self.client.get(route, headers=headers).text
                self.assertNotIn(SECRET, body, f"{route} leaked to the {label} key")
                self.assertFalse(contains_credentials(body),
                                 f"{route} leaked credentials to the {label} key")

    def test_websocket_frame_carries_no_credential(self):
        with self.client.websocket_connect("/ws?key=scoped-key") as ws:
            frame = ws.receive_json()
        import json
        self.assertNotIn(SECRET, json.dumps(frame))

    def test_operator_key_still_sees_the_source_field(self):
        """web/cameras.html tests `camera.source` for truthiness to decide
        whether to offer 'Start pipeline'. Dropping it for the operator would
        silently disable that button."""
        row = next(c for c in self.client.get("/api/v1/cameras", headers=self.full)
                   .json()["cameras"] if c["camera_id"] == "CAM-SECRET")
        self.assertIn("source", row)
        self.assertTrue(row["source"], "must stay truthy for the operator UI")
        self.assertNotIn(SECRET, row["source"])

    def test_integration_key_gets_no_source_at_all(self):
        row = next(c for c in self.client.get("/api/v1/cameras", headers=self.scoped)
                   .json()["cameras"] if c["camera_id"] == "CAM-SECRET")
        self.assertNotIn("source", row)

    def test_restarting_a_pipeline_still_uses_the_real_source(self):
        """Redaction is a projection. The stored value must be intact, or a
        masked URL would be handed to the camera worker and never connect."""
        cam = next(c for c in svc.cameras() if c["camera_id"] == "CAM-SECRET")
        self.assertEqual(RTSP, cam["source"])


if __name__ == "__main__":
    unittest.main()
