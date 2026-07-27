"""HTTP-level tests for the cross-camera identity endpoints.

These are the first real TestClient tests in the suite. MORNING.md recorded the
FastAPI HTTP layer as untestable because httpx was missing and could not be
fetched; httpx arrived as a boxmot dependency, so the endpoints are now covered
by unit tests rather than only by the live demo script.

FINBLADE_INMEMORY is set before importing the app so this never touches the
real SQLite database.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

try:
    from fastapi.testclient import TestClient

    from finblade.globalid import GlobalIdentityRegistry
    from finblade.topology import CameraTopology
    from services.api.app import app, id_svc
    HAVE_APP = True
except Exception as _exc:                      # noqa: BLE001
    HAVE_APP = False
    IMPORT_ERROR = _exc

DIM = 64


def vec(a=1.0, b=0.0):
    v = [0.0] * DIM
    v[0], v[1] = a, b
    return v


def body(camera_id="CAM-A", track=1, ts=100.0, embeddings=None, zone_id=None):
    return {"camera_id": camera_id, "local_track_id": track, "ts": ts,
            "embeddings": embeddings if embeddings is not None else [vec()],
            "zone_id": zone_id}


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestIdentityEndpoints(unittest.TestCase):
    def setUp(self):
        # Fresh registry per test; the app module holds a global service.
        topo = CameraTopology(transits={("CAM-A", "CAM-B"): (10.0, 90.0)},
                              allow_unknown_pairs=False)
        id_svc.registry = GlobalIdentityRegistry(topology=topo)
        # No context manager: skips lifespan so the background monitors and the
        # camera manager never start during tests.
        self.client = TestClient(app)

    def test_resolve_returns_a_ref(self):
        r = self.client.post("/api/v1/identity/resolve", json=body())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["resolved"])
        self.assertTrue(data["global_ref"].startswith("gp_"))

    def test_malformed_payload_is_422_not_500(self):
        r = self.client.post("/api/v1/identity/resolve",
                             json={"camera_id": "CAM-A"})
        self.assertEqual(r.status_code, 422)
        self.assertTrue(r.json()["errors"])

    def test_nan_embedding_rejected_over_http(self):
        # A NaN would poison every cosine in the gallery. Sent as raw content
        # because httpx refuses to encode NaN client-side — the point here is
        # that the SERVER rejects it, so the payload has to actually arrive.
        import json
        bad = vec()
        bad[3] = float("nan")
        raw = json.dumps(body(embeddings=[bad]))     # emits a bare NaN token
        self.assertIn("NaN", raw)
        r = self.client.post("/api/v1/identity/resolve", content=raw,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)
        self.assertTrue(any("NaN" in e for e in r.json()["errors"]))

    def test_cross_camera_handover_over_http(self):
        first = self.client.post("/api/v1/identity/resolve",
                                 json=body("CAM-A", 1, 100.0)).json()
        self.client.post("/api/v1/identity/release",
                         json={"camera_id": "CAM-A", "local_track_id": 1})
        second = self.client.post("/api/v1/identity/resolve",
                                  json=body("CAM-B", 7, 140.0)).json()
        self.assertTrue(second["matched"])
        self.assertEqual(second["global_ref"], first["global_ref"])

    def test_journey_endpoint(self):
        ref = self.client.post("/api/v1/identity/resolve",
                               json=body(zone_id="Z1")).json()["global_ref"]
        r = self.client.get(f"/api/v1/identity/{ref}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["found"])
        self.assertEqual([j["zone_id"] for j in data["journey"]], ["Z1"])

    def test_unknown_ref_is_404(self):
        r = self.client.get("/api/v1/identity/gp_nosuchref00000")
        self.assertEqual(r.status_code, 404)

    def test_static_routes_are_not_shadowed_by_the_ref_route(self):
        # /{global_ref} is declared last on purpose; if it were declared first
        # it would swallow /stats and /list and both would 404.
        self.assertEqual(self.client.get("/api/v1/identity/stats").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/identity/list").status_code, 200)

    def test_stats_shape(self):
        self.client.post("/api/v1/identity/resolve", json=body())
        data = self.client.get("/api/v1/identity/stats").json()
        self.assertEqual(data["identities"], 1)
        self.assertEqual(data["site_occupancy"], 1)
        self.assertIn("topology_source", data)

    def test_list_and_merge(self):
        a = self.client.post("/api/v1/identity/resolve",
                             json=body("CAM-A", 1, 100.0)).json()["global_ref"]
        b = self.client.post("/api/v1/identity/resolve",
                             json=body("CAM-B", 2, 100.0,
                                       embeddings=[vec(0.0, 1.0)])).json()["global_ref"]
        listed = self.client.get("/api/v1/identity/list").json()["identities"]
        self.assertEqual(len(listed), 2)

        r = self.client.post("/api/v1/identity/merge",
                             json={"keep_ref": a, "drop_ref": b})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/identity/{b}").status_code, 404)

    def test_merge_unknown_ref_is_409(self):
        r = self.client.post("/api/v1/identity/merge",
                             json={"keep_ref": "gp_a", "drop_ref": "gp_b"})
        self.assertEqual(r.status_code, 409)

    def test_no_endpoint_leaks_an_embedding(self):
        # The privacy contract, enforced at the HTTP boundary.
        ref = self.client.post("/api/v1/identity/resolve",
                               json=body(zone_id="Z1")).json()["global_ref"]
        for path in (f"/api/v1/identity/{ref}", "/api/v1/identity/list",
                     "/api/v1/identity/stats"):
            text = self.client.get(path).text
            self.assertNotIn("embedding", text.lower(), f"{path} leaked a vector")
            self.assertNotIn("vectors", text, f"{path} leaked a vector")


if __name__ == "__main__":
    unittest.main()
