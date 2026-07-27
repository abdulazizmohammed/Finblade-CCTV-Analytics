import unittest

from finblade.globalid import GlobalIdentityRegistry
from finblade.topology import CameraTopology
from services.api.identity import (MAX_VECTORS, IdentityService,
                                   validate_resolve)

DIM = 64


def vec(lead=1.0, second=0.0):
    """A DIM-length unit-ish vector; only the first two components vary."""
    v = [0.0] * DIM
    v[0], v[1] = lead, second
    return v


def payload(camera_id="CAM-A", track=1, ts=100.0, embeddings=None, zone_id=None):
    return {
        "camera_id": camera_id,
        "local_track_id": track,
        "ts": ts,
        "embeddings": embeddings if embeddings is not None else [vec()],
        "zone_id": zone_id,
    }


class TestValidateResolve(unittest.TestCase):
    def test_valid_payload(self):
        ok, errors = validate_resolve(payload())
        self.assertTrue(ok, errors)

    def test_rejects_non_object(self):
        ok, errors = validate_resolve("nope")
        self.assertFalse(ok)

    def test_requires_camera_id(self):
        p = payload()
        p["camera_id"] = ""
        ok, errors = validate_resolve(p)
        self.assertFalse(ok)
        self.assertIn("camera_id must be a non-empty string", errors)

    def test_rejects_bool_as_track_id(self):
        # bool is an int subclass in Python; a True here would silently become 1.
        p = payload()
        p["local_track_id"] = True
        self.assertFalse(validate_resolve(p)[0])

    def test_requires_numeric_ts(self):
        p = payload()
        p["ts"] = "now"
        self.assertFalse(validate_resolve(p)[0])

    def test_rejects_empty_embeddings(self):
        self.assertFalse(validate_resolve(payload(embeddings=[]))[0])

    def test_rejects_too_many_vectors(self):
        p = payload(embeddings=[vec()] * (MAX_VECTORS + 1))
        ok, errors = validate_resolve(p)
        self.assertFalse(ok)
        self.assertTrue(any("at most" in e for e in errors))

    def test_rejects_undersized_vector(self):
        ok, errors = validate_resolve(payload(embeddings=[[1.0, 2.0]]))
        self.assertFalse(ok)
        self.assertTrue(any("outside" in e for e in errors))

    def test_rejects_oversized_vector(self):
        ok, errors = validate_resolve(payload(embeddings=[[0.1] * 5000]))
        self.assertFalse(ok)

    def test_rejects_mixed_dimensions(self):
        ok, errors = validate_resolve(payload(embeddings=[vec(), [0.1] * (DIM + 1)]))
        self.assertFalse(ok)
        self.assertTrue(any("dimension" in e for e in errors))

    def test_rejects_nan(self):
        bad = vec()
        bad[5] = float("nan")
        ok, errors = validate_resolve(payload(embeddings=[bad]))
        self.assertFalse(ok)
        self.assertTrue(any("NaN" in e for e in errors))

    def test_rejects_infinity(self):
        bad = vec()
        bad[5] = float("inf")
        self.assertFalse(validate_resolve(payload(embeddings=[bad]))[0])

    def test_rejects_non_numeric_component(self):
        bad = vec()
        bad[5] = "x"
        self.assertFalse(validate_resolve(payload(embeddings=[bad]))[0])

    def test_zone_id_must_be_string_or_null(self):
        self.assertTrue(validate_resolve(payload(zone_id=None))[0])
        self.assertTrue(validate_resolve(payload(zone_id="Z1"))[0])
        p = payload()
        p["zone_id"] = 7
        self.assertFalse(validate_resolve(p)[0])


class TestIdentityServiceResolve(unittest.TestCase):
    def setUp(self):
        topo = CameraTopology(transits={("CAM-A", "CAM-B"): (10.0, 90.0)},
                              allow_unknown_pairs=False)
        self.svc = IdentityService(registry=GlobalIdentityRegistry(topology=topo))

    def test_resolve_returns_a_ref(self):
        status, body = self.svc.resolve(payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["resolved"])
        self.assertTrue(body["global_ref"].startswith("gp_"))
        self.assertFalse(body["matched"])

    def test_invalid_payload_is_422(self):
        status, body = self.svc.resolve({"camera_id": "CAM-A"})
        self.assertEqual(status, 422)
        self.assertFalse(body["resolved"])
        self.assertTrue(body["errors"])

    def test_response_never_contains_an_embedding(self):
        # The privacy contract: vectors go in, only an opaque ref comes out.
        _, body = self.svc.resolve(payload())
        self.assertNotIn("embeddings", body)
        self.assertNotIn("1.0", repr(body))

    def test_cross_camera_match_through_the_service(self):
        _, first = self.svc.resolve(payload("CAM-A", 1, 100.0))
        self.svc.release({"camera_id": "CAM-A", "local_track_id": 1})
        _, second = self.svc.resolve(payload("CAM-B", 7, 140.0))
        self.assertTrue(second["matched"])
        self.assertEqual(second["global_ref"], first["global_ref"])

    def test_service_honours_the_physics_gate(self):
        _, first = self.svc.resolve(payload("CAM-A", 1, 100.0))
        self.svc.release({"camera_id": "CAM-A", "local_track_id": 1})
        _, second = self.svc.resolve(payload("CAM-B", 7, 102.0))   # 2s: impossible
        self.assertFalse(second["matched"])
        self.assertNotEqual(second["global_ref"], first["global_ref"])


class TestReleaseAndJourney(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(registry=GlobalIdentityRegistry(
            topology=CameraTopology()))

    def test_release_returns_the_ref(self):
        _, res = self.svc.resolve(payload())
        status, body = self.svc.release({"camera_id": "CAM-A", "local_track_id": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["global_ref"], res["global_ref"])

    def test_release_validates_input(self):
        status, _ = self.svc.release({"camera_id": "", "local_track_id": 1})
        self.assertEqual(status, 422)
        status, _ = self.svc.release({"camera_id": "CAM-A", "local_track_id": "x"})
        self.assertEqual(status, 422)

    def test_journey_for_unknown_ref_is_404(self):
        status, body = self.svc.journey("gp_nope")
        self.assertEqual(status, 404)
        self.assertFalse(body["found"])

    def test_journey_returns_zone_sequence_without_vectors(self):
        _, res = self.svc.resolve(payload(zone_id="Z1"))
        self.svc.resolve(payload(ts=101.0, zone_id="Z2"))
        status, body = self.svc.journey(res["global_ref"])
        self.assertEqual(status, 200)
        self.assertEqual([j["zone_id"] for j in body["journey"]], ["Z1", "Z2"])
        self.assertNotIn("bank", body)
        self.assertNotIn("vectors", repr(body))


class TestStatsAndListing(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(registry=GlobalIdentityRegistry(
            topology=CameraTopology()))

    def test_stats_reports_config_and_counts(self):
        self.svc.resolve(payload())
        stats = self.svc.stats(now=100.0)
        self.assertEqual(stats["identities"], 1)
        self.assertEqual(stats["site_occupancy"], 1)
        self.assertIn("topology_source", stats)
        self.assertIn("threshold", stats)

    def test_list_identities_excludes_vectors(self):
        self.svc.resolve(payload(zone_id="Z1"))
        items = self.svc.list_identities()
        self.assertEqual(len(items), 1)
        self.assertNotIn("vectors", repr(items))

    def test_cross_camera_only_filter(self):
        self.svc.resolve(payload("CAM-A", 1, 100.0))
        self.svc.resolve(payload("CAM-B", 2, 100.0, embeddings=[vec(0.0, 1.0)]))
        self.assertEqual(len(self.svc.list_identities()), 2)
        self.assertEqual(len(self.svc.list_identities(cross_camera_only=True)), 0)


class TestCountsWithoutZones(unittest.TestCase):
    def setUp(self):
        topo = CameraTopology(overlapping=[("CAM-A", "CAM-B")])
        self.svc = IdentityService(registry=GlobalIdentityRegistry(topology=topo))

    def test_counts_work_with_zone_id_absent(self):
        # No zone_id anywhere — the no-zones deployment.
        self.svc.resolve(payload("CAM-A", 1, 100.0))
        self.svc.resolve(payload("CAM-A", 2, 100.0, embeddings=[vec(0.0, 1.0)]))
        c = self.svc.counts(now=100.0)
        self.assertEqual(c["live"], 2)
        self.assertEqual(c["unique_total"], 2)
        self.assertEqual(c["per_camera"], [{"camera_id": "CAM-A", "live": 2, "unique": 2}])

    def test_person_on_both_cameras_counted_once_site_wide(self):
        self.svc.resolve(payload("CAM-A", 1, 100.0))
        self.svc.resolve(payload("CAM-B", 9, 100.0))
        c = self.svc.counts(now=100.0)
        self.assertEqual(c["unique_total"], 1)
        self.assertEqual(c["live"], 1)
        self.assertEqual(c["cross_camera"], 1)
        # Each camera still saw one person; the sum exceeding the total is the
        # double-count that de-duplication removed.
        self.assertEqual(sum(p["unique"] for p in c["per_camera"]), 2)

    def test_unique_total_persists_after_expiry(self):
        svc = IdentityService(registry=GlobalIdentityRegistry(
            topology=CameraTopology(), ttl_seconds=10.0))
        svc.resolve(payload("CAM-A", 1, 100.0))
        svc.release({"camera_id": "CAM-A", "local_track_id": 1})
        c = svc.counts(now=1000.0)          # long past the TTL
        self.assertEqual(c["live"], 0)
        self.assertEqual(c["unique_total"], 1)


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(registry=GlobalIdentityRegistry(
            topology=CameraTopology()))

    def test_merge_two_identities(self):
        _, a = self.svc.resolve(payload("CAM-A", 1, 100.0))
        _, b = self.svc.resolve(payload("CAM-B", 2, 100.0, embeddings=[vec(0.0, 1.0)]))
        status, body = self.svc.merge({"keep_ref": a["global_ref"],
                                       "drop_ref": b["global_ref"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.svc.journey(b["global_ref"])[0], 404)

    def test_merge_validates(self):
        self.assertEqual(self.svc.merge({"keep_ref": "a"})[0], 422)
        self.assertEqual(self.svc.merge({"keep_ref": "gp_x", "drop_ref": "gp_y"})[0], 409)


class TestTopologyLoading(unittest.TestCase):
    def test_missing_topology_file_degrades_to_permissive(self):
        svc = IdentityService(topology_path="/nonexistent/topology.yaml")
        self.assertEqual(svc.topology_source, "default(permissive)")
        # Still functional rather than dead.
        self.assertEqual(svc.resolve(payload())[0], 200)

    def test_real_topology_file_is_loaded(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "config", "topology.yaml")
        if not os.path.exists(path):
            self.skipTest("config/topology.yaml not present")
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        svc = IdentityService(topology_path=path)
        self.assertTrue(svc.topology_source.endswith("topology.yaml"))


if __name__ == "__main__":
    unittest.main()
