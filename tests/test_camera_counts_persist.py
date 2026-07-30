"""People counts must survive an event being ingested.

The bug this pins down was live for a long time and was invisible in every way
that mattered. mark_camera_seen ended with:

    ON CONFLICT(camera_id) DO UPDATE SET ...
        people_in_view=excluded.people_in_view,
        people_in_zones=excluded.people_in_zones

but its INSERT supplies only camera_id, site_id and last_seen — so
`excluded.people_in_view` was the column DEFAULT of 0. Every ingested event
therefore RESET the counts, and mark_camera_seen runs on every event and every
zone-state post, i.e. far more often than the health snapshot that actually
carries the numbers.

It presented as "the dashboard shows 0 people with a man sitting in frame", and
it hid behind a healthy-looking camera: seconds_since_seen stayed fresh, because
last_seen was updated by the very statement doing the wiping. It also survived a
long hunt through the detector, the confidence threshold and the smoothing
window, none of which were at fault.
"""

import os
import tempfile
import unittest

from services.api.sqlite_store import SQLiteStore


class TestCameraCountsPersist(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SQLiteStore(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _counts(self, camera_id="CAM-A"):
        row = [c for c in self.store.list_cameras() if c["camera_id"] == camera_id][0]
        return row.get("people_in_view"), row.get("people_in_zones")

    def _record_health(self, in_view, in_zones, ts=1000.0):
        self.store.record_camera_health(
            "CAM-A",
            {"state": "ONLINE", "input_fps": 30.0, "people_in_view": in_view,
             "people_in_zones": in_zones},
            ts, site_id="SITE-A")

    def test_health_counts_are_stored(self):
        self._record_health(2, 1)
        self.assertEqual(self._counts(), (2, 1))

    def test_mark_camera_seen_does_not_wipe_the_counts(self):
        """THE regression test. An event arriving must not zero the people
        counts — it carries no count of its own, so it has nothing to say about
        them."""
        self._record_health(2, 1)
        self.store.mark_camera_seen("CAM-A", 1001.0)
        self.assertEqual(self._counts(), (2, 1),
                         "ingesting an event reset the people counts to 0")

    def test_counts_survive_a_burst_of_events(self):
        """Events arrive far more often than health snapshots, so a single
        surviving wipe is enough to keep the dashboard pinned at 0."""
        self._record_health(3, 2)
        for i in range(50):
            self.store.mark_camera_seen("CAM-A", 1000.0 + i)
        self.assertEqual(self._counts(), (3, 2))

    def test_mark_camera_seen_still_updates_last_seen(self):
        """The wipe must be removed without breaking what the call is FOR —
        camera-offline detection depends on last_seen advancing."""
        self._record_health(1, 0, ts=1000.0)
        self.store.mark_camera_seen("CAM-A", 1234.5)
        row = [c for c in self.store.list_cameras() if c["camera_id"] == "CAM-A"][0]
        self.assertEqual(row.get("last_seen"), 1234.5)

    def test_a_later_health_snapshot_still_updates_the_counts(self):
        """Counts must be sticky against events but NOT against the health
        snapshot, which is the one thing that legitimately reports them."""
        self._record_health(2, 1, ts=1000.0)
        self.store.mark_camera_seen("CAM-A", 1001.0)
        self._record_health(0, 0, ts=1002.0)
        self.assertEqual(self._counts(), (0, 0),
                         "an empty room must still be reportable as empty")

    def test_mark_camera_seen_can_still_create_an_unknown_camera(self):
        """It is also the path that registers a camera first seen via an event."""
        self.store.mark_camera_seen("CAM-NEW", 500.0, site_id="SITE-A")
        ids = [c["camera_id"] for c in self.store.list_cameras()]
        self.assertIn("CAM-NEW", ids)


if __name__ == "__main__":
    unittest.main()
