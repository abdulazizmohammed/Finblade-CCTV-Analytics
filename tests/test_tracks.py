import unittest

from finblade.tracks import (
    TrackRegistry, STATE_TENTATIVE, STATE_TRACKED, STATE_COMPLETED,
)

BOX = (10.0, 20.0, 30.0, 80.0)


class TestTrackRegistry(unittest.TestCase):
    def test_new_track_outside_zone_is_tentative(self):
        r = TrackRegistry()
        t = r.observe(1, "CAM", BOX, 0.9, None, False, 0.0, "pr_1", now=100.0)
        self.assertEqual(t.state, STATE_TENTATIVE)
        self.assertEqual(t.first_seen, 100.0)
        self.assertIsNone(t.current_zone_id)
        self.assertEqual(len(r), 1)

    def test_new_track_in_zone_is_tracked(self):
        r = TrackRegistry()
        t = r.observe(1, "CAM", BOX, 0.9, "Z1", True, 0.0, "pr_1", now=100.0)
        self.assertEqual(t.state, STATE_TRACKED)
        self.assertEqual(t.current_zone_id, "Z1")
        self.assertEqual(t.confirmed_zone_entry_time, 100.0)
        self.assertEqual(t.zones_visited, ["Z1"])

    def test_age_and_last_seen_update(self):
        r = TrackRegistry()
        r.observe(1, "CAM", BOX, 0.9, "Z1", True, 0.0, "pr_1", now=100.0)
        t = r.observe(1, "CAM", (0, 0, 5, 5), 0.8, "Z1", False, 4.0, "pr_1", now=104.0)
        self.assertEqual(t.last_seen, 104.0)
        self.assertAlmostEqual(t.track_age, 4.0)
        self.assertEqual(t.bbox, (0, 0, 5, 5))     # bbox updated
        self.assertEqual(t.confidence, 0.8)
        self.assertEqual(t.dwell_time, 4.0)

    def test_zone_transition_records_previous(self):
        r = TrackRegistry()
        r.observe(1, "CAM", BOX, 0.9, "Z1", True, 0.0, "pr_1", now=100.0)
        t = r.observe(1, "CAM", BOX, 0.9, "Z2", True, 0.0, "pr_1", now=110.0)
        self.assertEqual(t.previous_zone_id, "Z1")
        self.assertEqual(t.current_zone_id, "Z2")
        self.assertEqual(t.confirmed_zone_entry_time, 110.0)
        self.assertEqual(t.zones_visited, ["Z1", "Z2"])

    def test_complete_returns_summary_and_removes(self):
        r = TrackRegistry()
        r.observe(1, "CAM", BOX, 0.9, "Z1", True, 0.0, "pr_1", now=100.0)
        r.observe(1, "CAM", BOX, 0.9, "Z1", False, 7.5, "pr_1", now=107.5)
        done = r.complete(1)
        self.assertEqual(done.state, STATE_COMPLETED)
        s = done.summary()
        self.assertEqual(s["track_id"], 1)
        self.assertEqual(s["camera_id"], "CAM")
        self.assertAlmostEqual(s["track_age"], 7.5)
        self.assertEqual(s["dwell_time"], 7.5)
        self.assertEqual(s["zones_visited"], ["Z1"])
        self.assertEqual(len(r), 0)                # removed -> bounded memory
        self.assertIsNone(r.complete(1))           # idempotent

    def test_multiple_tracks_independent(self):
        r = TrackRegistry()
        r.observe(1, "CAM", BOX, 0.9, "Z1", True, 0.0, "pr_1", now=0.0)
        r.observe(2, "CAM", BOX, 0.9, "Z2", True, 0.0, "pr_2", now=0.0)
        self.assertEqual(len(r), 2)
        r.complete(1)
        self.assertEqual(len(r), 1)
        self.assertEqual(r.get(2).current_zone_id, "Z2")


if __name__ == "__main__":
    unittest.main()
