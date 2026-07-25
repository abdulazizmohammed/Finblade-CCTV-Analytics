import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.api.sqlite_store import SQLiteStore
from finblade.events import RESTRICTED_ZONE_EXIT, new_event
from finblade.identity import PersonRefHasher

PR = PersonRefHasher(session_salt="fixed").ref(1)


class TestSQLiteCameraHealth(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_health_roundtrip(self):
        s = SQLiteStore(self.db)
        s.record_camera_health("CAM-A-01", {
            "state": "DEGRADED", "input_fps": 12.5, "resolution": (640, 480),
            "dropped_frames": 4, "reconnects": 2, "loops": 1, "frozen": True,
            "enabled": True, "stream_url": "http://h:8080/stream"}, ts=100.0,
            site_id="SITE-1")
        cams = {c["camera_id"]: c for c in s.list_cameras()}
        c = cams["CAM-A-01"]
        self.assertEqual(c["state"], "DEGRADED")
        self.assertEqual(c["input_fps"], 12.5)
        self.assertEqual(c["resolution"], "640x480")   # tuple flattened
        self.assertIs(c["frozen"], True)                # stored 1 -> bool
        self.assertEqual(c["site_id"], "SITE-1")

    def test_sim_flag_persists(self):
        s = SQLiteStore(self.db)
        s.record_camera_health("CAM-A-01", {"state": "ONLINE"}, ts=1.0)
        s.set_camera_sim("CAM-A-01", True)
        c = next(c for c in s.list_cameras() if c["camera_id"] == "CAM-A-01")
        self.assertIs(c["sim_failure"], True)
        s.set_camera_sim("CAM-A-01", False)
        c = next(c for c in s.list_cameras() if c["camera_id"] == "CAM-A-01")
        self.assertIs(c["sim_failure"], False)

    def test_upsert_and_delete(self):
        s = SQLiteStore(self.db)
        s.upsert_camera("CAM-B-02", name="Dock", site_id="SITE-1")
        ids = {c["camera_id"] for c in s.list_cameras()}
        self.assertIn("CAM-B-02", ids)
        self.assertTrue(s.delete_camera("CAM-B-02"))
        self.assertFalse(s.delete_camera("CAM-B-02"))

    def test_migration_adds_columns_to_old_db(self):
        # Simulate a DB created before the health columns existed.
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE cameras(camera_id TEXT PRIMARY KEY, site_id TEXT, last_seen REAL)")
        conn.execute("INSERT INTO cameras(camera_id,site_id,last_seen) VALUES('CAM-OLD','S',1.0)")
        conn.commit(); conn.close()
        s = SQLiteStore(self.db)                        # opens + migrates
        s.record_camera_health("CAM-OLD", {"state": "ONLINE", "input_fps": 9.0}, ts=5.0)
        c = next(c for c in s.list_cameras() if c["camera_id"] == "CAM-OLD")
        self.assertEqual(c["state"], "ONLINE")

    def test_event_payload_extra_survives_roundtrip(self):
        # Phase 7 regression: duration lives only in the payload JSON.
        s = SQLiteStore(self.db)
        evt = new_event(RESTRICTED_ZONE_EXIT, "CAM-A-01", "SITE-1", 10.0,
                        zone_id="Z2", person_ref=PR, duration=7.5)
        s.save_event(evt)
        rows = s.list_events(0, 1e12)
        self.assertEqual(rows[0]["duration"], 7.5)


if __name__ == "__main__":
    unittest.main()
