"""Deleting or stopping a camera must release its identity bindings.

Regression: a deleted camera's people were counted toward site occupancy
forever. Deletion is worse than a crash — the offline monitor only walks
REGISTERED cameras, so once the row is gone nothing would ever release them.
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
except Exception:
    HAVE_APP = False

DIM = 64


def vec(a=1.0, b=0.0):
    v = [0.0] * DIM
    v[0], v[1] = a, b
    return v


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestDeleteReleasesIdentities(unittest.TestCase):
    def setUp(self):
        id_svc.registry = GlobalIdentityRegistry(topology=CameraTopology())
        self.client = TestClient(app)

    def _resolve(self, cam, track, lead=1.0):
        return self.client.post("/api/v1/identity/resolve", json={
            "camera_id": cam, "local_track_id": track, "ts": 100.0,
            "embeddings": [vec(lead)], "zone_id": None})

    def _register(self, cam):
        # No source, so no pipeline is launched — we only need the row to exist.
        return self.client.post("/api/v1/cameras",
                                json={"camera_id": cam, "site_id": "t"})

    def test_delete_releases_bindings(self):
        self._register("CAM-GONE")
        self._resolve("CAM-GONE", 1)
        self._resolve("CAM-GONE", 2, lead=0.0)
        self.assertEqual(
            self.client.get("/api/v1/identity/counts").json()["live"], 2)

        r = self.client.delete("/api/v1/cameras/CAM-GONE")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["identity_bindings_released"], 2)

        # The people it was tracking are no longer "on site".
        self.assertEqual(
            self.client.get("/api/v1/identity/counts").json()["live"], 0)

    def test_bindings_released_even_if_the_camera_row_is_unknown(self):
        # Defensive: release must happen before the row lookup, so an identity
        # bound to a camera that was never registered (or already removed) is
        # still freed rather than stranded forever.
        self._resolve("CAM-PHANTOM", 1)
        self.assertEqual(
            self.client.get("/api/v1/identity/counts").json()["live"], 1)
        r = self.client.delete("/api/v1/cameras/CAM-PHANTOM")
        self.assertEqual(r.status_code, 404)          # no such camera row
        self.assertEqual(
            self.client.get("/api/v1/identity/counts").json()["live"], 0)

    def test_delete_keeps_cumulative_footfall(self):
        self._resolve("CAM-GONE", 1)
        before = self.client.get("/api/v1/identity/counts").json()["unique_total"]
        self.client.delete("/api/v1/cameras/CAM-GONE")
        after = self.client.get("/api/v1/identity/counts").json()["unique_total"]
        self.assertEqual(before, after)   # they were still here earlier

    def test_stop_also_releases(self):
        self._resolve("CAM-STOP", 1)
        self.assertEqual(
            self.client.get("/api/v1/identity/counts").json()["live"], 1)
        r = self.client.post("/api/v1/cameras/CAM-STOP/stop")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["identity_bindings_released"], 1)
        self.assertEqual(
            self.client.get("/api/v1/identity/counts").json()["live"], 0)

    def test_deleting_one_camera_leaves_others_counted(self):
        self._resolve("CAM-KEEP", 1)
        self._resolve("CAM-GONE", 2, lead=0.0)
        self.client.delete("/api/v1/cameras/CAM-GONE")
        counts = self.client.get("/api/v1/identity/counts").json()
        self.assertEqual(counts["live"], 1)
        live = {p["camera_id"]: p["live"] for p in counts["per_camera"]}
        self.assertEqual(live.get("CAM-KEEP"), 1)


if __name__ == "__main__":
    unittest.main()
