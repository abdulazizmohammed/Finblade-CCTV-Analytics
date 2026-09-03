"""Extended ReID retention: off by default, and identical in what it decides.

The transform is validated in isolation by tests/test_cancelable.py. These
tests are the level that actually matters operationally: run the SAME resolve
sequence through a default registry and an extended one, and assert the
matcher reaches the same conclusions. If the two ever diverge, the accuracy
argument for this feature is gone whatever the maths says.

Also asserts the properties the privacy claim rests on:
  * default construction touches nothing — no keyring, no projection
  * a stored template is NOT the raw vector once the mode is on
  * an identity stranded by a key rotation is dropped, not kept as dead weight
  * erasure is immediate and total
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.appearance import TrackFeatureBank, cosine_similarity
from finblade.globalid import GlobalIdentityRegistry
from finblade.topology import CameraTopology

try:
    import numpy as np                                       # noqa: F401
    HAVE_NUMPY = True
except ImportError:                                          # pragma: no cover
    HAVE_NUMPY = False

needs_numpy = unittest.skipUnless(HAVE_NUMPY, "numpy not installed")

DIM = 128          # smaller than production 512; the property is dimension-free
DAY = 86400.0


def bank_of(vectors, cap=5):
    b = TrackFeatureBank(capacity=cap)
    for v in vectors:
        b.add(v)
    return b


def person(rng, dim=DIM):
    """A base signature, plus a way to make another view of the same person."""
    base = [rng.gauss(0, 1) for _ in range(dim)]

    def view(noise=0.05):
        return [x + rng.gauss(0, noise) for x in base]
    return view


def wide_topology(max_s=DAY):
    """A topology whose transit windows admit long gaps.

    NOT a test convenience — it is the configuration extended retention
    REQUIRES. The default default_transit is (0.0, 120.0), so the physics gate
    refuses any candidate last seen more than two minutes ago, and holding a
    template for 24 hours buys exactly nothing. See
    GlobalIdentityRegistry.extended_retention_warnings().
    """
    return CameraTopology(default_transit=(0.0, max_s), same_camera_max_s=max_s)


def make_registry(extended=False, topology=None, **kw):
    args = dict(topology=topology or CameraTopology.empty(), threshold=0.70,
                margin=0.06, ttl_seconds=300.0, bank_capacity=5,
                embedding_dim=DIM)
    if extended:
        args["extended_max_retention_seconds"] = DAY
        args["epoch_seconds"] = kw.pop("epoch_seconds", DAY / 2)
    args.update(kw)
    return GlobalIdentityRegistry(**args)


# --------------------------------------------------------------------------
# Default state
# --------------------------------------------------------------------------
class TestDefaultIsUntouched(unittest.TestCase):
    def test_extended_retention_is_off_by_default(self):
        r = make_registry()
        self.assertFalse(r.extended_retention)
        self.assertIsNone(r.extended_max_retention_seconds)
        self.assertIsNone(r._keyring)

    def test_default_ceiling_is_unchanged(self):
        r = make_registry()
        self.assertEqual(r.retention_for("CAM-01"), 300.0)
        self.assertEqual(r.max_retention_seconds, 1800.0)

    def test_default_stores_raw_vectors(self):
        r = make_registry()
        rng = random.Random(1)
        v = [rng.gauss(0, 1) for _ in range(DIM)]
        r.resolve("CAM-01", 1, bank_of([v, v]), now=100.0)
        ident = r.get(r.all_refs()[0])
        self.assertIsNone(ident.epoch_id)
        # L2-normalised on the way in, so compare by direction not by value.
        self.assertAlmostEqual(cosine_similarity(ident.bank.vectors[0], v),
                               1.0, places=9)

    def test_snapshot_reports_the_mode_either_way(self):
        r = make_registry()
        self.assertEqual(r.snapshot()["retention"]["mode"], "ram_short_ttl")


# --------------------------------------------------------------------------
# The mode, on
# --------------------------------------------------------------------------
@needs_numpy
class TestExtendedMode(unittest.TestCase):
    def test_ceiling_moves_but_only_the_ceiling(self):
        r = make_registry(extended=True)
        self.assertTrue(r.extended_retention)
        self.assertEqual(r.extended_max_retention_seconds, DAY)
        # max_retention_seconds — the field whose whole purpose is to stop a
        # five-minute bound becoming an all-day one — must be untouched.
        self.assertEqual(r.max_retention_seconds, 1800.0)
        self.assertEqual(r.ttl_seconds, 300.0)

    def test_ttl_is_clamped_to_24h(self):
        r = make_registry(extended=True, extended_max_retention_seconds=DAY * 10)
        self.assertEqual(r.extended_max_retention_seconds, DAY)

    def test_ceiling_cannot_go_below_the_ordinary_one(self):
        r = make_registry(extended=True, extended_max_retention_seconds=60.0)
        self.assertGreaterEqual(r.extended_max_retention_seconds,
                                r.max_retention_seconds)

    def test_stored_template_is_not_the_raw_vector(self):
        """The property the confidentiality claim rests on."""
        r = make_registry(extended=True)
        rng = random.Random(2)
        v = [rng.gauss(0, 1) for _ in range(DIM)]
        r.resolve("CAM-01", 1, bank_of([v, v]), now=100.0)
        ident = r.get(r.all_refs()[0])
        self.assertIsNotNone(ident.epoch_id)
        sim = abs(cosine_similarity(ident.bank.vectors[0], v))
        self.assertLess(sim, 0.5, "stored vector still resembles the raw one")

    def test_snapshot_states_the_guarantee_without_overclaiming(self):
        r = make_registry(extended=True)
        r.resolve("CAM-01", 1, bank_of([[0.1] * DIM] * 2), now=100.0)
        snap = r.snapshot()["retention"]
        self.assertEqual(snap["mode"], "ram_extended")
        self.assertIn("NOT non-invertible", snap["guarantee"])
        self.assertEqual(len(snap["keyring"]["live_epochs"]), 1)


# --------------------------------------------------------------------------
# Equivalence — the test that matters
# --------------------------------------------------------------------------
@needs_numpy
class TestDecisionEquivalence(unittest.TestCase):
    def _run(self, reg, script):
        out = []
        for cam, tid, vecs, now in script:
            res = reg.resolve(cam, tid, bank_of(vecs), now=now)
            out.append((res.matched, res.reason, round(res.score, 6)))
        return out

    def _script(self, seed):
        """Two people, two cameras, a handover each — enough to exercise
        match, create, and the margin path."""
        rng = random.Random(seed)
        a, b = person(rng), person(rng)
        return [
            ("CAM-01", 1, [a(), a(), a()], 100.0),
            ("CAM-01", 2, [b(), b(), b()], 101.0),
            ("CAM-02", 1, [a(), a(), a()], 400.0),
            ("CAM-02", 2, [b(), b(), b()], 402.0),
            ("CAM-01", 3, [a(), a(), a()], 700.0),
        ]

    def test_same_decisions_with_and_without_the_transform(self):
        for seed in (11, 22, 33, 44):
            with self.subTest(seed=seed):
                script = self._script(seed)
                plain = self._run(make_registry(), script)
                extended = self._run(make_registry(extended=True), script)
                self.assertEqual([(m, r) for m, r, _ in plain],
                                 [(m, r) for m, r, _ in extended])
                for (_, _, s1), (_, _, s2) in zip(plain, extended):
                    self.assertAlmostEqual(s1, s2, places=6)

    def test_same_identity_count(self):
        for seed in (5, 6, 7):
            with self.subTest(seed=seed):
                script = self._script(seed)
                p, e = make_registry(), make_registry(extended=True)
                self._run(p, script)
                self._run(e, script)
                self.assertEqual(len(p), len(e))


# --------------------------------------------------------------------------
# Epoch boundaries
# --------------------------------------------------------------------------
@needs_numpy
class TestEpochBoundary(unittest.TestCase):
    def test_someone_present_across_a_boundary_still_matches(self):
        """The failure a naive midnight reset would cause."""
        r = make_registry(extended=True, epoch_seconds=1000.0,
                          extended_max_retention_seconds=4000.0,
                          topology=wide_topology(4000.0))
        rng = random.Random(3)
        a = person(rng)
        first = r.resolve("CAM-01", 1, bank_of([a(), a(), a()]), now=100.0)
        r.release("CAM-01", 1)
        # Past the boundary: a new key is current, the old one still live.
        r.expire(1500.0)
        again = r.resolve("CAM-02", 9, bank_of([a(), a(), a()]), now=1500.0)
        self.assertTrue(again.matched)
        self.assertEqual(first.global_ref, again.global_ref)

    def test_identity_is_carried_into_the_current_epoch_when_seen(self):
        r = make_registry(extended=True, epoch_seconds=1000.0,
                          extended_max_retention_seconds=4000.0,
                          topology=wide_topology(4000.0))
        rng = random.Random(31)
        a = person(rng)
        res = r.resolve("CAM-01", 1, bank_of([a(), a(), a()]), now=100.0)
        e0 = r.get(res.global_ref).epoch_id
        r.release("CAM-01", 1)
        r.expire(1500.0)
        r.resolve("CAM-02", 9, bank_of([a(), a(), a()]), now=1500.0)
        self.assertNotEqual(r.get(res.global_ref).epoch_id, e0)
        self.assertEqual(r.stats.get("epoch_reprojected", 0), 1)

    def test_an_identity_stranded_by_two_rotations_is_dropped(self):
        """Unseen for a whole epoch: its key is destroyed, so its templates are
        unrecoverable. Keeping the record would be dead weight that can never
        match again."""
        r = make_registry(extended=True, epoch_seconds=1000.0,
                          extended_max_retention_seconds=100000.0)
        rng = random.Random(4)
        a = person(rng)
        res = r.resolve("CAM-01", 1, bank_of([a(), a(), a()]), now=100.0)
        r.release("CAM-01", 1)
        r.expire(1500.0)                      # epoch 0 -> 1, key 0 still live
        self.assertIsNotNone(r.get(res.global_ref))
        r.expire(2600.0)                      # epoch 1 -> 2, key 0 destroyed
        self.assertIsNone(r.get(res.global_ref))
        self.assertGreaterEqual(r.stats.get("epoch_stranded", 0), 1)

    def test_retention_still_bounded_by_two_epochs(self):
        r = make_registry(extended=True, epoch_seconds=1000.0,
                          extended_max_retention_seconds=100000.0)
        rng = random.Random(41)
        a = person(rng)
        r.resolve("CAM-01", 1, bank_of([a(), a(), a()]), now=0.0)
        r.release("CAM-01", 1)
        for t in (1100.0, 2200.0, 3300.0):
            r.expire(t)
        self.assertEqual(len(r), 0,
                         "nothing may outlive two epochs, whatever the ceiling")


# --------------------------------------------------------------------------
# The inertness guard
# --------------------------------------------------------------------------
@needs_numpy
class TestWarnings(unittest.TestCase):
    def test_default_topology_is_reported_as_making_the_mode_inert(self):
        """Enabled + default transit windows = holds templates for a day and
        refuses every candidate over 120s old. Must be visible, not silent."""
        r = make_registry(extended=True)          # CameraTopology.empty()
        warnings = r.extended_retention_warnings()
        self.assertTrue(warnings)
        self.assertTrue(any("no effect" in w for w in warnings))
        self.assertIn("warnings", r.snapshot()["retention"])

    def test_no_warning_when_the_topology_supports_the_window(self):
        r = make_registry(extended=True, topology=wide_topology(DAY))
        self.assertEqual(r.extended_retention_warnings(), [])

    def test_short_epoch_is_reported_as_the_real_bound(self):
        r = make_registry(extended=True, topology=wide_topology(DAY),
                          epoch_seconds=600.0)
        self.assertTrue(any("key bound wins" in w
                            for w in r.extended_retention_warnings()))

    def test_mode_off_never_warns(self):
        self.assertEqual(make_registry().extended_retention_warnings(), [])


# --------------------------------------------------------------------------
# Erasure
# --------------------------------------------------------------------------
@needs_numpy
class TestErasure(unittest.TestCase):
    def test_erase_is_immediate_and_total(self):
        r = make_registry(extended=True)
        rng = random.Random(6)
        for i in range(4):
            p = person(rng)
            r.resolve("CAM-01", i, bank_of([p(), p(), p()]), now=100.0 + i)
        self.assertGreater(len(r), 0)
        out = r.erase_templates()
        self.assertEqual(len(r), 0)
        self.assertGreaterEqual(out["identities_dropped"], 1)
        self.assertGreaterEqual(out["epoch_keys_destroyed"], 1)

    def test_erase_works_with_the_mode_off_too(self):
        r = make_registry()
        rng = random.Random(7)
        p = person(rng)
        r.resolve("CAM-01", 1, bank_of([p(), p(), p()]), now=100.0)
        out = r.erase_templates()
        self.assertEqual(len(r), 0)
        self.assertEqual(out["epoch_keys_destroyed"], 0)


if __name__ == "__main__":
    unittest.main()
