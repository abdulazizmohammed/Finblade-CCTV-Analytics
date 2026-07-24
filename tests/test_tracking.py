import unittest

from finblade.tracking import TrackReaper


class TestTrackReaper(unittest.TestCase):
    def test_keeps_recently_seen(self):
        r = TrackReaper(ttl_seconds=5.0)
        r.see(1, now=100.0)
        r.see(2, now=100.0)
        self.assertEqual(r.reap(now=103.0), [])   # within TTL
        self.assertEqual(r.active_count(), 2)

    def test_evicts_stale(self):
        r = TrackReaper(ttl_seconds=5.0)
        r.see(1, now=100.0)
        r.see(2, now=104.0)
        stale = r.reap(now=106.0)                  # track 1 unseen 6s > 5s
        self.assertEqual(stale, [1])
        self.assertEqual(r.active_count(), 1)      # track 2 retained

    def test_reap_forgets_returned_ids(self):
        r = TrackReaper(ttl_seconds=2.0)
        r.see(7, now=0.0)
        self.assertEqual(r.reap(now=5.0), [7])
        self.assertEqual(r.reap(now=6.0), [])      # already forgotten
        self.assertEqual(r.active_count(), 0)

    def test_reappearing_track_is_fresh(self):
        r = TrackReaper(ttl_seconds=2.0)
        r.see(3, now=0.0)
        self.assertEqual(r.reap(now=5.0), [3])     # evicted
        r.see(3, now=5.0)                          # comes back
        self.assertEqual(r.reap(now=6.0), [])      # fresh, retained
        self.assertEqual(r.active_count(), 1)

    def test_bounded_over_many_transient_tracks(self):
        r = TrackReaper(ttl_seconds=1.0)
        # 1000 one-frame tracks over time; active set must not grow unbounded.
        for i in range(1000):
            r.see(i, now=float(i))
            r.reap(now=float(i))
        self.assertLessEqual(r.active_count(), 2)

    def test_bad_ttl(self):
        with self.assertRaises(ValueError):
            TrackReaper(ttl_seconds=0)


if __name__ == "__main__":
    unittest.main()
