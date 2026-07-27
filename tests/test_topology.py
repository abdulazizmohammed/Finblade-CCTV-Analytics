import unittest

from finblade.topology import CameraTopology


class TestSameCamera(unittest.TestCase):
    def test_same_camera_always_feasible(self):
        t = CameraTopology()
        ok, reason = t.feasible("CAM-A", "CAM-A", 0.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "same_camera")
        # Even a long gap: a person can leave and come back into one view.
        self.assertTrue(t.feasible("CAM-A", "CAM-A", 9999.0)[0])


class TestOverlappingPairs(unittest.TestCase):
    def setUp(self):
        self.t = CameraTopology(overlapping=[("CAM-A", "CAM-B")],
                                default_transit=(2.0, 120.0),
                                overlap_tolerance_s=5.0)

    def test_simultaneous_is_the_expected_case(self):
        # Overlapping views see the same person at the same instant.
        ok, reason = self.t.feasible("CAM-A", "CAM-B", 0.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "overlapping")

    def test_small_negative_dt_tolerated_as_clock_skew(self):
        # Independent camera processes drift; -3s is skew, not time travel.
        self.assertTrue(self.t.feasible("CAM-A", "CAM-B", -3.0)[0])

    def test_large_negative_dt_rejected(self):
        ok, reason = self.t.feasible("CAM-A", "CAM-B", -30.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "overlap_clock_skew")

    def test_stale_overlap_rejected(self):
        # Beyond the window the person plainly left; that is a new visit.
        ok, reason = self.t.feasible("CAM-A", "CAM-B", 500.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "overlap_stale")

    def test_overlap_is_undirected(self):
        self.assertTrue(self.t.is_overlapping("CAM-B", "CAM-A"))
        self.assertTrue(self.t.feasible("CAM-B", "CAM-A", 0.0)[0])


class TestNonOverlappingPairs(unittest.TestCase):
    def setUp(self):
        self.t = CameraTopology(
            transits={("CAM-A", "CAM-B"): (10.0, 90.0)},
            allow_unknown_pairs=True,
        )

    def test_too_fast_is_rejected(self):
        # THE decisive gate: two similar-looking people at opposite ends of a
        # site, 3s apart. Appearance would accept; physics must not.
        ok, reason = self.t.feasible("CAM-A", "CAM-B", 3.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_fast")

    def test_within_window_ok(self):
        ok, reason = self.t.feasible("CAM-A", "CAM-B", 30.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "transit_ok")

    def test_boundaries_are_inclusive(self):
        self.assertTrue(self.t.feasible("CAM-A", "CAM-B", 10.0)[0])
        self.assertTrue(self.t.feasible("CAM-A", "CAM-B", 90.0)[0])

    def test_too_slow_is_rejected(self):
        ok, reason = self.t.feasible("CAM-A", "CAM-B", 200.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_slow")

    def test_negative_dt_rejected_for_non_overlapping(self):
        # Unlike an overlapping pair, being seen at B before leaving A is
        # not skew tolerance — it is a contradiction.
        ok, reason = self.t.feasible("CAM-A", "CAM-B", -1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "negative_dt")

    def test_transit_config_is_undirected(self):
        self.assertEqual(self.t.transit_window("CAM-B", "CAM-A"), (10.0, 90.0))
        self.assertFalse(self.t.feasible("CAM-B", "CAM-A", 3.0)[0])


class TestUnknownPairs(unittest.TestCase):
    def test_unknown_pair_uses_default_and_is_flagged(self):
        t = CameraTopology(default_transit=(2.0, 60.0), allow_unknown_pairs=True)
        ok, reason = t.feasible("CAM-X", "CAM-Y", 30.0)
        self.assertTrue(ok)
        # Flagged so the caller can report that topology config is incomplete.
        self.assertEqual(reason, "unknown_pair")

    def test_unknown_pair_allows_simultaneous_sighting(self):
        # Regression: the default minimum used to be 2s, so an unconfigured pair
        # of OVERLAPPING cameras had every candidate rejected as "too_fast" —
        # cross-camera matching silently did nothing. Cameras added from the UI
        # are never in a topology file, so this was the default experience.
        t = CameraTopology()
        ok, _ = t.feasible("CAM-06", "Cam-07", 0.0)
        self.assertTrue(ok)
        self.assertTrue(t.feasible("CAM-06", "Cam-07", 0.4)[0])

    def test_unknown_pair_can_be_rejected_outright(self):
        t = CameraTopology(allow_unknown_pairs=False)
        ok, reason = t.feasible("CAM-X", "CAM-Y", 30.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "unknown_pair_rejected")

    def test_unknown_pair_still_obeys_default_window(self):
        t = CameraTopology(default_transit=(5.0, 60.0))
        self.assertFalse(t.feasible("CAM-X", "CAM-Y", 1.0)[0])


class TestConfigLoading(unittest.TestCase):
    def test_from_dict_full(self):
        t = CameraTopology.from_dict({
            "overlapping_pairs": [{"a": "CAM-A", "b": "CAM-B"}],
            "transits": [{"a": "CAM-B", "b": "CAM-C",
                          "min_seconds": 8, "max_seconds": 45}],
            "default_transit": {"min_seconds": 3, "max_seconds": 100},
            "overlap_tolerance_seconds": 2,
            "allow_unknown_pairs": False,
        })
        self.assertTrue(t.is_overlapping("CAM-A", "CAM-B"))
        self.assertEqual(t.transit_window("CAM-C", "CAM-B"), (8.0, 45.0))
        self.assertEqual(t.default_transit, (3.0, 100.0))
        self.assertFalse(t.allow_unknown_pairs)
        self.assertFalse(t.feasible("CAM-A", "CAM-B", -10.0)[0])  # tolerance 2s

    def test_from_dict_accepts_pair_lists(self):
        t = CameraTopology.from_dict({"overlapping_pairs": [["CAM-A", "CAM-B"]]})
        self.assertTrue(t.is_overlapping("CAM-A", "CAM-B"))

    def test_from_dict_empty_is_permissive(self):
        t = CameraTopology.from_dict({})
        self.assertTrue(t.allow_unknown_pairs)
        self.assertTrue(t.feasible("CAM-A", "CAM-B", 30.0)[0])

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            CameraTopology(transits={("A", "B"): (50.0, 10.0)})
        with self.assertRaises(ValueError):
            CameraTopology(transits={("A", "B"): (-1.0, 10.0)})

    def test_is_known_pair(self):
        t = CameraTopology(overlapping=[("A", "B")], transits={("C", "D"): (1.0, 2.0)})
        self.assertTrue(t.is_known_pair("B", "A"))
        self.assertTrue(t.is_known_pair("D", "C"))
        self.assertFalse(t.is_known_pair("A", "D"))


if __name__ == "__main__":
    unittest.main()
