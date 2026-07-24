import unittest

from finblade.debounce import BoundaryDebouncer


class TestBoundaryDebounce(unittest.TestCase):
    def test_n_minus_1_does_not_trigger_n_does(self):
        d = BoundaryDebouncer(n=3)
        # Track enters ZONE-01: need 3 consecutive frames to confirm.
        self.assertEqual(d.update(1, "ZONE-01"), (None, False))   # 1
        self.assertEqual(d.update(1, "ZONE-01"), (None, False))   # 2 (N-1)
        self.assertEqual(d.update(1, "ZONE-01"), ("ZONE-01", True))  # 3 (N) -> commit

    def test_flicker_does_not_flip(self):
        d = BoundaryDebouncer(n=3)
        for _ in range(3):
            d.update(1, "ZONE-01")
        self.assertEqual(d.confirmed_zone(1), "ZONE-01")
        # Straddling boundary: single frames in ZONE-02 interleaved with ZONE-01.
        self.assertEqual(d.update(1, "ZONE-02"), ("ZONE-01", False))
        self.assertEqual(d.update(1, "ZONE-01"), ("ZONE-01", False))  # resets candidate
        self.assertEqual(d.update(1, "ZONE-02"), ("ZONE-01", False))
        self.assertEqual(d.update(1, "ZONE-02"), ("ZONE-01", False))  # only 2 consecutive
        self.assertEqual(d.confirmed_zone(1), "ZONE-01")

    def test_sustained_change_commits(self):
        d = BoundaryDebouncer(n=3)
        for _ in range(3):
            d.update(1, "ZONE-01")
        d.update(1, "ZONE-02")
        d.update(1, "ZONE-02")
        self.assertEqual(d.update(1, "ZONE-02"), ("ZONE-02", True))

    def test_transition_to_none_debounced(self):
        d = BoundaryDebouncer(n=2)
        for _ in range(2):
            d.update(1, "ZONE-01")
        self.assertEqual(d.update(1, None), ("ZONE-01", False))
        self.assertEqual(d.update(1, None), (None, True))

    def test_tracks_are_independent(self):
        d = BoundaryDebouncer(n=2)
        d.update(1, "ZONE-01")
        d.update(2, "ZONE-02")
        self.assertEqual(d.update(1, "ZONE-01"), ("ZONE-01", True))
        self.assertEqual(d.update(2, "ZONE-02"), ("ZONE-02", True))

    def test_n_equals_one_is_immediate(self):
        d = BoundaryDebouncer(n=1)
        self.assertEqual(d.update(1, "ZONE-01"), ("ZONE-01", True))

    def test_drop_removes_track_state(self):
        d = BoundaryDebouncer(n=1)
        d.update(5, "ZONE-01")
        self.assertEqual(d.confirmed_zone(5), "ZONE-01")
        self.assertIn(5, d._state)
        d.drop(5)
        self.assertNotIn(5, d._state)             # freed -> bounded memory
        self.assertIsNone(d.confirmed_zone(5))


if __name__ == "__main__":
    unittest.main()
