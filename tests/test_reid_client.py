import unittest

from services.inference.reid_client import ReIDResolver

FRAME_W, FRAME_H = 1280, 720


class FakeEmbedder:
    """Stands in for OSNet so this tests the plumbing, not the model.

    NOTE: this is a test double for the transport layer only. Nothing like it
    ships in the runtime path — a stub embedder in production would emit
    confident wrong identities (CLAUDE.md rule 1). ReIDResolver.load() disables
    ReID outright when the real weights are absent.
    """

    def __init__(self):
        self.loaded = True
        self.weights = "fake"
        self.calls = []

    def embed(self, frame, bboxes):
        self.calls.append(list(bboxes))
        # One distinct 64-d unit vector per box, keyed on its x1.
        out = []
        for box in bboxes:
            v = [0.0] * 64
            v[int(box[0]) % 64] = 1.0
            out.append(v)
        return out


class FakePoster:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"global_ref": "gp_abc0123456789012",
                                     "matched": False, "score": 0.0}

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path.endswith("/release"):
            return {"ok": True}
        return self.response


def resolver(poster=None, **kwargs):
    r = ReIDResolver("CAM-A", poster or FakePoster(), **kwargs)
    r.embedder = FakeEmbedder()
    r.status = "ready"
    return r


def track(tid, x1=500.0, y1=200.0, x2=580.0, y2=500.0):
    return (tid, x1, y1, x2, y2)


class TestObserve(unittest.TestCase):
    def test_disabled_resolver_does_nothing(self):
        r = ReIDResolver("CAM-A", FakePoster(), enabled=False)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 0)

    def test_embeds_a_good_track(self):
        r = resolver()
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 1)
        self.assertEqual(r._banks[1].n, 1)

    def test_gates_out_a_tiny_crop(self):
        r = resolver()
        r.observe(None, [track(1, 500.0, 200.0, 515.0, 240.0)], {1: 0.9},
                  100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 0)
        self.assertEqual(r.stats["gated_out"], 1)

    def test_gates_out_low_confidence(self):
        r = resolver()
        r.observe(None, [track(1)], {1: 0.2}, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 0)

    def test_gates_out_occluded_track(self):
        r = resolver()
        # A big box overlapping most of track 1.
        tracks = [track(1), track(2, 510.0, 200.0, 900.0, 500.0)]
        r.observe(None, tracks, {1: 0.9, 2: 0.9}, 100.0, FRAME_W, FRAME_H)
        self.assertNotIn(1, r._banks)

    def test_respects_the_per_frame_budget(self):
        r = resolver(budget_per_frame=2)
        tracks = [track(t, 300.0 + t * 100, 200.0, 360.0 + t * 100, 500.0)
                  for t in range(1, 6)]
        confs = {t: 0.9 for t in range(1, 6)}
        r.observe(None, tracks, confs, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 2)

    def test_does_not_resample_within_the_interval(self):
        r = resolver(interval_s=1.0)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        r.observe(None, [track(1)], {1: 0.9}, 100.2, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 1)
        r.observe(None, [track(1)], {1: 0.9}, 101.5, FRAME_W, FRAME_H)
        self.assertEqual(r.stats["embedded"], 2)

    def test_embedding_failure_does_not_raise(self):
        r = resolver()

        def boom(frame, boxes):
            raise RuntimeError("cuda oom")

        r.embedder.embed = boom
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)   # no raise
        self.assertEqual(r.stats["embedded"], 0)


class TestResolvePending(unittest.TestCase):
    def test_waits_for_enough_samples(self):
        poster = FakePoster()
        r = resolver(poster, min_samples_to_resolve=2, interval_s=0.0001)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.resolve_pending(100.0, {}), {})
        self.assertEqual(poster.calls, [])           # nothing sent yet

        r.observe(None, [track(1)], {1: 0.9}, 101.0, FRAME_W, FRAME_H)
        assigned = r.resolve_pending(101.0, {1: "Z1"})
        self.assertEqual(assigned, {1: "gp_abc0123456789012"})
        self.assertEqual(poster.calls[0][0], "/api/v1/identity/resolve")
        self.assertEqual(poster.calls[0][1]["zone_id"], "Z1")
        self.assertEqual(poster.calls[0][1]["camera_id"], "CAM-A")

    def test_resolves_only_once_per_track(self):
        poster = FakePoster()
        r = resolver(poster, min_samples_to_resolve=1)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        r.resolve_pending(100.0, {})
        r.resolve_pending(101.0, {})
        self.assertEqual(len(poster.calls), 1)

    def test_counts_a_match(self):
        poster = FakePoster({"global_ref": "gp_1111111111111111",
                             "matched": True, "score": 0.8, "reason": "appearance_match"})
        r = resolver(poster, min_samples_to_resolve=1)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        r.resolve_pending(100.0, {})
        self.assertEqual(r.stats["matched"], 1)
        self.assertEqual(r.global_ref(1), "gp_1111111111111111")

    def test_api_failure_is_counted_and_retried(self):
        class DeadPoster(FakePoster):
            def __call__(self, path, payload):
                self.calls.append((path, payload))
                return None

        poster = DeadPoster()
        r = resolver(poster, min_samples_to_resolve=1, retry_interval_s=2.0)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.resolve_pending(100.0, {}), {})
        self.assertEqual(r.stats["resolve_failed"], 1)
        self.assertIsNone(r.global_ref(1))
        # Unresolved, so it tries again — but only after the backoff.
        r.resolve_pending(103.0, {})
        self.assertEqual(r.stats["resolve_failed"], 2)

    def test_failed_resolve_backs_off_instead_of_retrying_every_frame(self):
        # A 30s benchmark with the API down produced 1877 failed POSTs before
        # this backoff existed — one per track per frame.
        class DeadPoster(FakePoster):
            def __call__(self, path, payload):
                self.calls.append((path, payload))
                return None

        poster = DeadPoster()
        r = resolver(poster, min_samples_to_resolve=1, retry_interval_s=2.0)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        for i in range(40):                     # 40 frames inside the window
            r.resolve_pending(100.0 + i * 0.04, {})
        self.assertEqual(len(poster.calls), 1)
        r.resolve_pending(103.0, {})            # past the backoff
        self.assertEqual(len(poster.calls), 2)


class TestDropAndSnapshot(unittest.TestCase):
    def test_drop_clears_vectors_and_releases(self):
        poster = FakePoster()
        r = resolver(poster, min_samples_to_resolve=1)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        r.resolve_pending(100.0, {})
        bank = r._banks[1]

        r.drop(1)
        self.assertEqual(bank.vectors, [])          # templates actually gone
        self.assertNotIn(1, r._banks)
        self.assertIsNone(r.global_ref(1))
        self.assertEqual(poster.calls[-1][0], "/api/v1/identity/release")

    def test_drop_of_unknown_track_is_safe(self):
        r = resolver()
        r.drop(999)

    def test_snapshot_has_counts_but_no_vectors(self):
        r = resolver(min_samples_to_resolve=1)
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        snap = r.snapshot()
        self.assertEqual(snap["tracks_with_bank"], 1)
        self.assertEqual(snap["embedded"], 1)
        self.assertNotIn("vectors", repr(snap))
        self.assertNotIn("banks", repr(snap))


class TestLoadFailure(unittest.TestCase):
    def test_missing_weights_disables_rather_than_faking(self):
        r = ReIDResolver("CAM-A", FakePoster(),
                         weights="/nonexistent/osnet.pt")
        self.assertFalse(r.load())
        self.assertFalse(r.enabled)
        self.assertFalse(r.ready)
        self.assertIn("unavailable", r.status)
        # And it stays inert rather than emitting made-up identities.
        r.observe(None, [track(1)], {1: 0.9}, 100.0, FRAME_W, FRAME_H)
        self.assertEqual(r.resolve_pending(100.0, {}), {})
        self.assertEqual(r.stats["embedded"], 0)


if __name__ == "__main__":
    unittest.main()
