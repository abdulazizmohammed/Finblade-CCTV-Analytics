import unittest

from finblade.appearance import (CropQualityGate, EmbeddingSampler,
                                 TrackFeatureBank, cosine_similarity)

FRAME_W, FRAME_H = 1280, 720


def good_box():
    """A comfortably valid person box, away from every frame edge."""
    return (500.0, 200.0, 580.0, 500.0)   # 80x300, aspect 0.27


class TestCropQualityGate(unittest.TestCase):
    def setUp(self):
        self.gate = CropQualityGate()

    def test_accepts_a_good_crop(self):
        ok, reason = self.gate.check(good_box(), 0.9, FRAME_W, FRAME_H)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_rejects_low_confidence(self):
        # Detector threshold is 0.35; the embedder demands more than that.
        ok, reason = self.gate.check(good_box(), 0.4, FRAME_W, FRAME_H)
        self.assertFalse(ok)
        self.assertEqual(reason, "low_confidence")

    def test_rejects_too_small(self):
        ok, reason = self.gate.check((500.0, 200.0, 520.0, 260.0), 0.9,
                                     FRAME_W, FRAME_H)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_small")

    def test_rejects_wide_aspect(self):
        # Wider than tall => merged detections or a person on the floor.
        ok, reason = self.gate.check((400.0, 200.0, 900.0, 500.0), 0.9,
                                     FRAME_W, FRAME_H)
        self.assertFalse(ok)
        self.assertEqual(reason, "aspect_wide")

    def test_rejects_thin_sliver(self):
        ok, reason = self.gate.check((500.0, 100.0, 533.0, 600.0), 0.9,
                                     FRAME_W, FRAME_H)
        self.assertFalse(ok)
        self.assertEqual(reason, "aspect_thin")

    def test_rejects_truncated_at_each_edge(self):
        # Each box is otherwise valid (size + aspect) and fails only on the edge.
        for box in [(2.0, 200.0, 90.0, 500.0),                    # left
                    (500.0, 2.0, 580.0, 400.0),                   # top
                    (1180.0, 200.0, FRAME_W - 1.0, 500.0),        # right
                    (500.0, 300.0, 580.0, FRAME_H - 2.0)]:        # bottom
            ok, reason = self.gate.check(box, 0.9, FRAME_W, FRAME_H)
            self.assertFalse(ok, f"expected rejection for {box}")
            self.assertEqual(reason, "truncated_at_edge")

    def test_edge_margin_scales_with_frame_size(self):
        # A fixed pixel margin is proportionally ~3x stricter on a 416px-tall
        # clip than on a 1440px one. The margin scales so the rule means the
        # same thing at every resolution the system ingests.
        box = (500.0, 200.0, 580.0, 500.0)
        # 1% of 1440 = 14.4px, so a box 10px from the bottom is truncated there...
        tall = (500.0, 200.0, 580.0, 1440.0 - 10.0)
        self.assertFalse(self.gate.check(tall, 0.9, 1920, 1440)[0])
        # ...while the same 10px gap on a small frame (floor 6px) is fine.
        small = (300.0, 100.0, 360.0, 416.0 - 10.0)
        ok, reason = self.gate.check(small, 0.9, 752, 416)
        self.assertTrue(ok, reason)
        self.assertTrue(self.gate.check(box, 0.9, FRAME_W, FRAME_H)[0])

    def test_rejects_degenerate_box(self):
        ok, reason = self.gate.check((500.0, 200.0, 500.0, 200.0), 0.9,
                                     FRAME_W, FRAME_H)
        self.assertFalse(ok)
        self.assertEqual(reason, "degenerate")


class TestOcclusion(unittest.TestCase):
    def setUp(self):
        self.gate = CropQualityGate()

    def test_heavy_overlap_is_occluded(self):
        box = (500.0, 200.0, 580.0, 500.0)
        other = (520.0, 200.0, 700.0, 500.0)   # covers ~75% of box
        self.assertTrue(self.gate.occluded_by(box, [other]))

    def test_light_overlap_is_not_occluded(self):
        box = (500.0, 200.0, 580.0, 500.0)
        other = (570.0, 200.0, 700.0, 500.0)   # ~12% of box
        self.assertFalse(self.gate.occluded_by(box, [other]))

    def test_no_overlap(self):
        box = (500.0, 200.0, 580.0, 500.0)
        self.assertFalse(self.gate.occluded_by(box, [(900.0, 200.0, 1000.0, 500.0)]))

    def test_box_does_not_occlude_itself(self):
        box = (500.0, 200.0, 580.0, 500.0)
        self.assertFalse(self.gate.occluded_by(box, [box]))

    def test_large_occluder_swallowing_small_box(self):
        # Low IoU but the crop is ruined — intersection-over-self catches it.
        box = (500.0, 200.0, 540.0, 300.0)
        other = (400.0, 100.0, 900.0, 600.0)
        self.assertTrue(self.gate.occluded_by(box, [other]))


class TestEmbeddingSampler(unittest.TestCase):
    def test_first_sample_always_wanted(self):
        s = EmbeddingSampler(interval_s=1.0, max_samples=5)
        self.assertTrue(s.wants_sample(1, 100.0))

    def test_not_resampled_within_interval(self):
        s = EmbeddingSampler(interval_s=1.0, max_samples=5)
        s.record(1, 100.0)
        self.assertFalse(s.wants_sample(1, 100.5))
        self.assertTrue(s.wants_sample(1, 101.0))

    def test_stops_at_max_samples(self):
        s = EmbeddingSampler(interval_s=1.0, max_samples=3)
        for i in range(3):
            self.assertTrue(s.wants_sample(1, 100.0 + i))
            s.record(1, 100.0 + i)
        self.assertFalse(s.wants_sample(1, 200.0))
        self.assertEqual(s.sample_count(1), 3)

    def test_budget_caps_crops_per_frame(self):
        # The perf guard: a sudden crowd must degrade sampling, not frame rate.
        s = EmbeddingSampler(interval_s=1.0, max_samples=5, budget_per_frame=3)
        chosen = s.select(list(range(20)), 100.0)
        self.assertEqual(len(chosen), 3)

    def test_fewest_samples_win_the_budget(self):
        s = EmbeddingSampler(interval_s=1.0, max_samples=5, budget_per_frame=2)
        for _ in range(3):
            s.record(1, 100.0)
        s.record(2, 100.0)
        # Track 3 is new and unmatchable across cameras; it must not be starved.
        chosen = s.select([1, 2, 3], 102.0)
        self.assertIn(3, chosen)
        self.assertIn(2, chosen)
        self.assertNotIn(1, chosen)

    def test_select_is_deterministic(self):
        s = EmbeddingSampler(budget_per_frame=2)
        self.assertEqual(s.select([5, 3, 9, 1], 100.0), [1, 3])

    def test_drop_clears_state(self):
        s = EmbeddingSampler(interval_s=1.0, max_samples=1)
        s.record(1, 100.0)
        self.assertFalse(s.wants_sample(1, 200.0))
        s.drop(1)
        self.assertTrue(s.wants_sample(1, 200.0))
        self.assertEqual(s.tracked(), 0)

    def test_invalid_params_rejected(self):
        for kwargs in ({"interval_s": 0}, {"max_samples": 0}, {"budget_per_frame": 0}):
            with self.assertRaises(ValueError):
                EmbeddingSampler(**kwargs)


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertAlmostEqual(cosine_similarity([1, 2, 3], [1, 2, 3]), 1.0, places=6)

    def test_orthogonal_is_zero(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0, places=6)

    def test_opposite_is_minus_one(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0, places=6)

    def test_magnitude_does_not_matter(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [7, 0]), 1.0, places=6)

    def test_zero_vector_is_zero_not_nan(self):
        self.assertEqual(cosine_similarity([0, 0], [1, 1]), 0.0)

    def test_dimension_mismatch_raises(self):
        with self.assertRaises(ValueError):
            cosine_similarity([1, 0], [1, 0, 0])


class TestTrackFeatureBank(unittest.TestCase):
    def test_add_normalizes(self):
        b = TrackFeatureBank()
        b.add([3.0, 4.0])          # norm 5
        self.assertAlmostEqual(b.vectors[0][0], 0.6, places=6)
        self.assertAlmostEqual(b.vectors[0][1], 0.8, places=6)

    def test_capacity_evicts_oldest(self):
        b = TrackFeatureBank(capacity=2)
        b.add([1.0, 0.0])
        b.add([0.0, 1.0])
        b.add([1.0, 1.0])
        self.assertEqual(b.n, 2)
        self.assertAlmostEqual(b.vectors[0][1], 1.0, places=6)   # first one gone

    def test_mean_vector_is_normalized(self):
        b = TrackFeatureBank()
        b.add([1.0, 0.0])
        b.add([0.0, 1.0])
        m = b.mean_vector()
        self.assertAlmostEqual(sum(v * v for v in m) ** 0.5, 1.0, places=6)

    def test_empty_bank_has_no_mean(self):
        self.assertIsNone(TrackFeatureBank().mean_vector())

    def test_identical_banks_are_similar(self):
        a, b = TrackFeatureBank(), TrackFeatureBank()
        a.add([1.0, 0.0, 0.0])
        b.add([1.0, 0.0, 0.0])
        self.assertAlmostEqual(a.similarity(b), 1.0, places=6)

    def test_orthogonal_banks_are_dissimilar(self):
        a, b = TrackFeatureBank(), TrackFeatureBank()
        a.add([1.0, 0.0])
        b.add([0.0, 1.0])
        self.assertAlmostEqual(a.similarity(b), 0.0, places=6)

    def test_empty_bank_similarity_is_zero(self):
        a = TrackFeatureBank()
        a.add([1.0, 0.0])
        self.assertEqual(a.similarity(TrackFeatureBank()), 0.0)

    def test_one_strong_view_survives_averaging(self):
        # Two cameras seeing opposite sides: the means diverge, but one pair of
        # views matches well. That must still register as similar.
        a, b = TrackFeatureBank(), TrackFeatureBank()
        a.add([1.0, 0.0, 0.0])
        a.add([0.0, 1.0, 0.0])
        b.add([1.0, 0.0, 0.0])
        b.add([0.0, 0.0, 1.0])
        # mean-to-mean is ~0.5; the shared view carries it to 0.9.
        self.assertAlmostEqual(a.similarity(b), 0.9, places=6)


if __name__ == "__main__":
    unittest.main()
