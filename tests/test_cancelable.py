"""The epoch transform must not change what the matcher decides.

TIER 1 (property) and TIER 2 (float precision) of the validation plan. Tier 3
is the end-to-end run through scripts/eval_cross_camera.py, which needs
footage and a GPU and so is not part of the unit suite.

The claim under test is exact, not statistical: for orthogonal Q,
cos(Qa, Qb) = cos(a, b) identically. So these assert a NULL RESULT. If any of
them starts failing, the transform is not orthogonal any more and the accuracy
argument for this whole feature has gone with it.

Precision matters more than it looks. The matcher thresholds at 0.70 with a
margin of 0.06; an error of 1e-9 is irrelevant, an error of 1e-2 would move
pairs across the line invisibly. So we bound the error AND assert directly
that no pair changes side of the threshold.
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.appearance import TrackFeatureBank, cosine_similarity
from finblade.cancelable import (EpochKeyring, MAX_EXTENDED_RETENTION_SECONDS,
                                 random_orthogonal)

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:                                          # pragma: no cover
    HAVE_NUMPY = False

needs_numpy = unittest.skipUnless(HAVE_NUMPY, "numpy not installed")

DIM = 512
THRESHOLD = 0.70


def rand_vec(dim=DIM, rng=None):
    r = rng or random
    return [r.gauss(0.0, 1.0) for _ in range(dim)]


# --------------------------------------------------------------------------
# TIER 1 — the property itself
# --------------------------------------------------------------------------
@needs_numpy
class TestOrthogonality(unittest.TestCase):
    def test_matrix_is_actually_orthogonal(self):
        q = random_orthogonal(64, seed=b"0" * 32)
        err = float(np.abs(q.T @ q - np.eye(64)).max())
        self.assertLess(err, 1e-10, "QᵀQ is not the identity: %g" % err)

    def test_cosine_is_preserved_on_random_vectors(self):
        ring = EpochKeyring(DIM, now=0.0)
        rng = random.Random(1234)
        worst = 0.0
        for _ in range(200):
            a, b = rand_vec(rng=rng), rand_vec(rng=rng)
            before = cosine_similarity(a, b)
            after = cosine_similarity(ring.project(a), ring.project(b))
            worst = max(worst, abs(before - after))
        self.assertLess(worst, 1e-9, "worst cosine drift %g" % worst)

    def test_cosine_is_preserved_on_near_identical_pairs(self):
        """The case that decides a MATCH. A tiny perturbation of one vector —
        the same person from two angles — must score the same after."""
        ring = EpochKeyring(DIM, now=0.0)
        rng = random.Random(7)
        worst = 0.0
        for _ in range(100):
            a = rand_vec(rng=rng)
            b = [x + rng.gauss(0, 0.01) for x in a]
            worst = max(worst, abs(cosine_similarity(a, b)
                                   - cosine_similarity(ring.project(a),
                                                       ring.project(b))))
        self.assertLess(worst, 1e-9)

    def test_cosine_is_preserved_on_near_orthogonal_pairs(self):
        """The case that decides a NON-match."""
        ring = EpochKeyring(DIM, now=0.0)
        rng = random.Random(8)
        worst = 0.0
        for _ in range(100):
            a, b = rand_vec(rng=rng), rand_vec(rng=rng)
            worst = max(worst, abs(cosine_similarity(a, b)
                                   - cosine_similarity(ring.project(a),
                                                       ring.project(b))))
        self.assertLess(worst, 1e-9)

    def test_bank_similarity_is_preserved(self):
        """The function the matcher ACTUALLY calls — max-over-pairs blended
        with mean-to-mean, not a bare cosine."""
        ring = EpochKeyring(DIM, now=0.0)
        rng = random.Random(99)
        for _ in range(25):
            raw_a, raw_b = TrackFeatureBank(capacity=5), TrackFeatureBank(capacity=5)
            prj_a, prj_b = TrackFeatureBank(capacity=5), TrackFeatureBank(capacity=5)
            for _ in range(5):
                va, vb = rand_vec(rng=rng), rand_vec(rng=rng)
                raw_a.add(va); raw_b.add(vb)
                prj_a.add(ring.project(va)); prj_b.add(ring.project(vb))
            self.assertAlmostEqual(raw_a.similarity(raw_b),
                                   prj_a.similarity(prj_b), places=9)


# --------------------------------------------------------------------------
# TIER 2 — precision against the decision boundary
# --------------------------------------------------------------------------
@needs_numpy
class TestThresholdSafety(unittest.TestCase):
    def test_no_pair_changes_side_of_the_threshold(self):
        """The bound that matters operationally. Drift is only harmless if it
        never moves a pair across 0.70 — that is what would silently change a
        match into a split with nothing in the logs to say why."""
        ring = EpochKeyring(DIM, now=0.0)
        rng = random.Random(4242)
        flipped = 0
        for _ in range(400):
            a = rand_vec(rng=rng)
            # Spread similarities across the whole range, concentrating near
            # the threshold where a flip would actually be possible.
            b = [x * rng.uniform(0.2, 1.0) + rng.gauss(0, rng.uniform(0.05, 1.2))
                 for x in a]
            before = cosine_similarity(a, b)
            after = cosine_similarity(ring.project(a), ring.project(b))
            if (before >= THRESHOLD) != (after >= THRESHOLD):
                flipped += 1
        self.assertEqual(flipped, 0, "%d pairs crossed the threshold" % flipped)

    def test_projection_preserves_norm(self):
        ring = EpochKeyring(DIM, now=0.0)
        v = rand_vec(rng=random.Random(11))
        n0 = math.sqrt(sum(x * x for x in v))
        n1 = math.sqrt(sum(x * x for x in ring.project(v)))
        self.assertAlmostEqual(n0, n1, places=9)

    def test_reprojection_across_epochs_is_exact(self):
        """Moving a template from the previous window into the current one must
        not degrade it — otherwise anyone present at a boundary slowly decays."""
        ring = EpochKeyring(DIM, epoch_seconds=100.0, now=0.0)
        rng = random.Random(5)
        a, b = rand_vec(rng=rng), rand_vec(rng=rng)
        e0 = ring.current_epoch
        pa, pb = ring.project(a, e0), ring.project(b, e0)
        ring.maybe_roll(now=150.0)
        e1 = ring.current_epoch
        self.assertNotEqual(e0, e1)
        ra = ring.reproject(pa, e0, e1)
        rb = ring.reproject(pb, e0, e1)
        self.assertAlmostEqual(cosine_similarity(a, b),
                               cosine_similarity(ra, rb), places=9)
        # And it really is the same template, in the new basis.
        self.assertAlmostEqual(cosine_similarity(ring.project(a, e1), ra),
                               1.0, places=9)


# --------------------------------------------------------------------------
# Rotation behaviour and erasure
# --------------------------------------------------------------------------
@needs_numpy
class TestEpochRotation(unittest.TestCase):
    def test_two_keys_live_after_a_roll_not_three(self):
        ring = EpochKeyring(DIM, epoch_seconds=100.0, now=0.0)
        self.assertEqual(len(ring.live_epochs()), 1)
        ring.maybe_roll(now=150.0)
        self.assertEqual(len(ring.live_epochs()), 2)
        ring.maybe_roll(now=300.0)
        self.assertEqual(len(ring.live_epochs()), 2,
                         "a third key would widen the unlinkability window")

    def test_roll_is_a_no_op_before_the_window_elapses(self):
        ring = EpochKeyring(DIM, epoch_seconds=100.0, now=0.0)
        self.assertIsNone(ring.maybe_roll(now=99.0))
        self.assertEqual(ring.stats["rolled"], 1)

    def test_a_retired_epoch_can_no_longer_be_projected(self):
        """The unlinkability boundary. Once the key is destroyed the templates
        written under it are unrecoverable — which is the point, and is why
        callers must drop those identities rather than keep dead weight."""
        ring = EpochKeyring(DIM, epoch_seconds=100.0, now=0.0)
        e0 = ring.current_epoch
        ring.maybe_roll(now=150.0)
        ring.maybe_roll(now=300.0)
        self.assertFalse(ring.has(e0))
        with self.assertRaises(KeyError):
            ring.project([0.0] * DIM, e0)

    def test_maybe_roll_reports_the_retired_epoch(self):
        ring = EpochKeyring(DIM, epoch_seconds=100.0, now=0.0)
        e0 = ring.current_epoch
        self.assertIsNone(ring.maybe_roll(now=50.0))
        ring.maybe_roll(now=150.0)
        retired = ring.maybe_roll(now=300.0)
        self.assertEqual(retired, e0)

    def test_destroy_all_is_immediate_erasure(self):
        ring = EpochKeyring(DIM, epoch_seconds=100.0, now=0.0)
        ring.maybe_roll(now=150.0)
        self.assertEqual(ring.destroy_all(), 2)
        self.assertEqual(ring.live_epochs(), [])

    def test_ceiling_constant_is_24h(self):
        """A mistyped env var must not be able to widen this."""
        self.assertEqual(MAX_EXTENDED_RETENTION_SECONDS, 86400.0)


@needs_numpy
class TestKeysDiffer(unittest.TestCase):
    def test_two_epochs_use_different_bases(self):
        """If two epochs shared a key there would be no unlinkability at all."""
        ring = EpochKeyring(64, epoch_seconds=10.0, now=0.0)
        v = rand_vec(64, rng=random.Random(3))
        a = ring.project(v)
        ring.maybe_roll(now=20.0)
        b = ring.project(v)
        self.assertLess(abs(cosine_similarity(a, b)), 0.9,
                        "epoch keys are suspiciously similar")


if __name__ == "__main__":
    unittest.main()
