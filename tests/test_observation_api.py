"""HTTP-level tests for the observation endpoints.

tests/test_fusion_service.py covers FusionService directly; this covers the four
routes over it. The gap mattered: the service was tested and the wiring was not,
so a route registered at the wrong path, or one returning the service's status
code as a 200, would have passed the whole suite.

FINBLADE_INMEMORY is set before importing the app so this never touches a real
database.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

try:
    from fastapi.testclient import TestClient

    from services.api.app import app, fusion_svc
    from services.api.fusion import FusionService
    HAVE_APP = True
except Exception as _exc:                      # noqa: BLE001
    HAVE_APP = False
    IMPORT_ERROR = _exc

CAM = {
    "observation_id": "obs-cam-1",
    "source_type": "CAMERA",
    "source_id": "CAM-03",
    "site_id": "SITE-1",
    "ts": 1000.0,
    "object_class": "PERSON",
    "confidence": 0.87,
    "position": {"frame": "IMAGE", "x": 640.0, "y": 712.0, "z": None,
                 "accuracy_m": None},
}

RADAR = {
    "observation_id": "obs-rad-1",
    "source_type": "RADAR",
    "source_id": "RAD-01",
    "site_id": "SITE-1",
    "ts": 1000.0,
    "object_class": "PERSON",
    "confidence": 0.9,
    "position": {"frame": "SITE", "x": 12.5, "y": 3.25, "z": None,
                 "accuracy_m": 0.4},
    "velocity": {"vx": 1.2, "vy": -0.4},
}


def obs(base, **over):
    o = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    o.update(over)
    return o


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestObservationIngest(unittest.TestCase):
    def setUp(self):
        # Fresh service per test; the app module holds a global.
        import services.api.app as app_mod
        app_mod.fusion_svc = FusionService()
        self.svc = app_mod.fusion_svc
        # No context manager: skips lifespan so background loops never start.
        self.client = TestClient(app)

    def test_camera_observation_accepted(self):
        r = self.client.post("/api/v1/observations/ingest", json=CAM)
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.json()["accepted"])

    def test_radar_observation_accepted_with_no_camera_involved(self):
        # The point of the whole schema: a source with no zones, no bbox and no
        # appearance channel can publish.
        r = self.client.post("/api/v1/observations/ingest", json=RADAR)
        self.assertEqual(r.status_code, 202)
        self.assertFalse(r.json()["appearance_capable"])

    def test_malformed_observation_is_422_not_500(self):
        r = self.client.post("/api/v1/observations/ingest",
                             json=obs(CAM, confidence=9.0))
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.json()["accepted"])
        self.assertTrue(r.json()["errors"])

    def test_service_status_code_reaches_the_client(self):
        # Guards the wiring specifically: a handler that returned the body but
        # dropped the code would answer 200 to a rejected payload.
        bad = self.client.post("/api/v1/observations/ingest", json={"nope": 1})
        self.assertEqual(bad.status_code, 422)

    def test_radar_claiming_an_appearance_is_refused_over_http(self):
        r = self.client.post("/api/v1/observations/ingest",
                             json=obs(RADAR, signature={"kind": "OSNET", "dim": 512}))
        self.assertEqual(r.status_code, 422)

    def test_nothing_is_fusable_yet_and_the_response_says_why(self):
        r = self.client.post("/api/v1/observations/ingest", json=RADAR)
        self.assertFalse(r.json()["fusable"])
        self.assertIn("calibration", r.json()["fusable_reason"])

    def test_events_ingest_is_untouched_by_the_new_route(self):
        # The whole design rests on observations being additive.
        r = self.client.post("/api/v1/events/ingest", json={"event_type": "BOGUS"})
        self.assertEqual(r.status_code, 422)


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestObservationViews(unittest.TestCase):
    def setUp(self):
        import services.api.app as app_mod
        app_mod.fusion_svc = FusionService()
        self.client = TestClient(app)
        self.client.post("/api/v1/observations/ingest", json=CAM)
        self.client.post("/api/v1/observations/ingest", json=RADAR)

    def test_stats_counts_both_sources(self):
        b = self.client.get("/api/v1/observations/stats").json()
        self.assertEqual(b["accepted"], 2)
        self.assertEqual(b["source_count"], 2)
        self.assertEqual(b["rejected"], 0)

    def test_stats_declares_fusion_off_and_why(self):
        b = self.client.get("/api/v1/observations/stats").json()
        self.assertFalse(b["fusion"]["geometric"])
        self.assertIn("CAMERA", b["appearance_capable_types"])
        self.assertNotIn("RADAR", b["appearance_capable_types"])

    def test_sources_lists_each_publisher(self):
        rows = self.client.get("/api/v1/observations/sources").json()["sources"]
        self.assertEqual({r["source_id"] for r in rows}, {"CAM-03", "RAD-01"})

    def test_recent_returns_what_arrived(self):
        b = self.client.get("/api/v1/observations/recent").json()
        self.assertEqual(len(b["observations"]), 2)

    def test_recent_filters_by_source(self):
        b = self.client.get("/api/v1/observations/recent",
                            params={"source_id": "RAD-01"}).json()
        self.assertEqual(len(b["observations"]), 1)
        self.assertEqual(b["observations"][0]["source_type"], "RADAR")

    def test_recent_honours_limit(self):
        b = self.client.get("/api/v1/observations/recent",
                            params={"limit": 1}).json()
        self.assertEqual(len(b["observations"]), 1)

    def test_recent_for_an_unknown_source_is_empty_not_404(self):
        r = self.client.get("/api/v1/observations/recent",
                            params={"source_id": "NOPE"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["observations"], [])


if __name__ == "__main__":
    unittest.main()
