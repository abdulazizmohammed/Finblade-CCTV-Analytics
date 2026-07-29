import os
import tempfile
import unittest

from finblade.globalid import GlobalIdentityRegistry
from finblade.topology import CameraTopology
from services.api.identity import IdentityService


def write_yaml(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    fh.write(text)
    fh.close()
    return fh.name


class TestTuningFromConfig(unittest.TestCase):
    def test_defaults_when_no_matching_block(self):
        svc = IdentityService(topology_path="/nonexistent.yaml")
        t = svc.get_tuning()
        self.assertEqual(t["threshold"], 0.70)
        self.assertEqual(t["margin"], 0.06)
        self.assertEqual(t["source"], "defaults")

    def test_matching_block_is_applied(self):
        path = write_yaml(
            "overlapping_pairs: []\n"
            "matching:\n"
            "  threshold: 0.55\n"
            "  margin: 0.02\n"
            "  ttl_seconds: 90\n"
            "  bank_capacity: 9\n")
        self.addCleanup(os.remove, path)
        svc = IdentityService(topology_path=path)
        t = svc.get_tuning()
        self.assertEqual(t["threshold"], 0.55)
        self.assertEqual(t["margin"], 0.02)
        self.assertEqual(t["ttl_seconds"], 90)
        self.assertEqual(t["bank_capacity"], 9)
        self.assertEqual(svc.registry.bank_capacity, 9)

    def test_topology_still_loads_alongside_matching(self):
        path = write_yaml(
            "overlapping_pairs:\n"
            "  - a: CAM-A\n"
            "    b: CAM-B\n"
            "matching:\n"
            "  threshold: 0.6\n")
        self.addCleanup(os.remove, path)
        svc = IdentityService(topology_path=path)
        self.assertTrue(svc.registry.topology.is_overlapping("CAM-A", "CAM-B"))
        self.assertEqual(svc.get_tuning()["threshold"], 0.6)

    def test_env_overrides_the_file(self):
        path = write_yaml("matching:\n  threshold: 0.55\n")
        self.addCleanup(os.remove, path)
        os.environ["FINBLADE_REID_THRESHOLD"] = "0.81"
        self.addCleanup(os.environ.pop, "FINBLADE_REID_THRESHOLD", None)
        svc = IdentityService(topology_path=path)
        self.assertEqual(svc.get_tuning()["threshold"], 0.81)
        self.assertEqual(svc.get_tuning()["source"], "env")

    def test_a_broken_file_does_not_kill_startup(self):
        path = write_yaml("overlapping_pairs: [[[[\n")
        self.addCleanup(os.remove, path)
        svc = IdentityService(topology_path=path)
        self.assertIn("failed", svc.topology_source)
        self.assertEqual(svc.get_tuning()["threshold"], 0.70)   # safe defaults


class TestRuntimeTuning(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(registry=GlobalIdentityRegistry(
            topology=CameraTopology()))

    def test_change_applies_immediately(self):
        code, body = self.svc.set_tuning({"threshold": 0.55, "margin": 0.10})
        self.assertEqual(code, 200)
        self.assertEqual(self.svc.registry.threshold, 0.55)
        self.assertEqual(self.svc.registry.margin, 0.10)
        self.assertEqual(body["applied"], {"threshold": 0.55, "margin": 0.10})

    def test_gallery_survives_a_tuning_change(self):
        # The whole point: tuning needs live footage, and a restart would empty
        # the gallery and force re-measuring from scratch on every nudge.
        from finblade.appearance import TrackFeatureBank
        b = TrackFeatureBank()
        b.add([1.0] + [0.0] * 63)
        self.svc.registry.resolve("CAM-A", 1, b, now=100.0)
        self.assertEqual(len(self.svc.registry), 1)
        self.svc.set_tuning({"threshold": 0.9})
        self.assertEqual(len(self.svc.registry), 1)

    def test_rejects_out_of_range(self):
        for bad in ({"threshold": 0}, {"threshold": 1.5}, {"threshold": "x"},
                    {"margin": -1}, {"ttl_seconds": 0}):
            code, body = self.svc.set_tuning(bad)
            self.assertEqual(code, 422, bad)
            self.assertTrue(body["errors"])

    def test_bad_value_applies_nothing_at_all(self):
        # Transactional. A good threshold alongside a bad margin must leave the
        # registry completely untouched — returning 422 while having silently
        # moved the threshold is the worst of both outcomes.
        before_t = self.svc.registry.threshold
        before_m = self.svc.registry.margin
        code, _ = self.svc.set_tuning({"threshold": 0.55, "margin": -5})
        self.assertEqual(code, 422)
        self.assertEqual(self.svc.registry.threshold, before_t)
        self.assertEqual(self.svc.registry.margin, before_m)

    def test_nan_is_rejected(self):
        code, _ = self.svc.set_tuning({"threshold": float("nan")})
        self.assertEqual(code, 422)
        code, _ = self.svc.set_tuning({"margin": float("inf")})
        self.assertEqual(code, 422)

    def test_empty_payload_rejected(self):
        code, body = self.svc.set_tuning({})
        self.assertEqual(code, 422)

    def test_response_warns_it_is_not_persisted(self):
        _, body = self.svc.set_tuning({"threshold": 0.55})
        self.assertIn("not persisted", body["note"])


if __name__ == "__main__":
    unittest.main()
