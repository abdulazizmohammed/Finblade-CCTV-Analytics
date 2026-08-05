"""Opening an EXISTING database with the new schema must not lose it.

The suite runs against InMemoryStore, so nothing else exercises _migrate().
site_id was added to alerts and zone_state_ts after deployments already had a
data/finblade.db — CREATE TABLE IF NOT EXISTS silently does nothing to an
existing table, so without the ALTER the first INSERT would fail with
"table alerts has no column named site_id" and every alert would stop being
recorded. On a live site that is silent data loss, not a crash anyone notices.
"""

import os
import sqlite3
import tempfile
import unittest

from services.api.sqlite_store import SQLiteStore

# The alerts/zone_state_ts tables exactly as they were before site_id existed.
_OLD_SCHEMA = """
CREATE TABLE alerts(
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, severity TEXT, message TEXT,
  zone_id TEXT, camera_id TEXT, person_ref TEXT, ts REAL, frame TEXT, kind TEXT,
  acknowledged_by TEXT, acknowledged_at REAL,
  status TEXT DEFAULT 'OPEN', note TEXT, resolved_by TEXT, resolved_at REAL);
CREATE TABLE zone_state_ts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT, camera_id TEXT, zone_name TEXT,
  zone_type TEXT, restricted INTEGER, ts REAL, occupancy INTEGER, density REAL,
  capacity_pct REAL, peak_occupancy INTEGER, avg_occupancy REAL, trend TEXT,
  extra TEXT, inflow REAL, outflow REAL, status TEXT);
"""


class TestMigrationFromAPreSiteIdDatabase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)                     # let sqlite create it
        conn = sqlite3.connect(self.path)
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO alerts(rule_id,severity,message,camera_id,ts,kind,status) "
            "VALUES ('R-01','AMBER','legacy row','CAM-OLD',100.0,'FIRE','OPEN')")
        conn.execute(
            "INSERT INTO zone_state_ts(zone_id,camera_id,ts,occupancy,density,"
            "capacity_pct,inflow,outflow,status) "
            "VALUES ('ZONE-OLD','CAM-OLD',100.0,3,0.5,10.0,1.0,1.0,'NORMAL')")
        conn.commit()
        conn.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_existing_rows_survive_and_read_back(self):
        store = SQLiteStore(self.path)
        rows = store.list_alerts_history(0, 1e12, limit=10)
        self.assertEqual(1, len(rows))
        self.assertEqual("legacy row", rows[0]["message"])
        self.assertIsNone(rows[0]["site_id"], "pre-existing rows have no site")

    def test_new_writes_carry_site_id_on_a_migrated_file(self):
        store = SQLiteStore(self.path)
        aid = store.save_alert({"rule_id": "R-06", "severity": "RED",
                                "message": "new row", "camera_id": "CAM-NEW",
                                "site_id": "SITE-NEW", "ts": 200.0, "kind": "FIRE"})
        row = next(a for a in store.list_alerts(unacked_only=False)
                   if str(a["alert_id"]) == str(aid))
        self.assertEqual("SITE-NEW", row["site_id"])

    def test_zone_state_migrates_and_accepts_a_site(self):
        store = SQLiteStore(self.path)
        # Current timestamp: latest_zone_states drops stale rows, so a 1970
        # timestamp would be filtered out and prove nothing about the column.
        store.save_zone_state({"zone_id": "ZONE-NEW", "camera_id": "CAM-NEW",
                               "site_id": "SITE-NEW", "ts": __import__("time").time(),
                               "occupancy": 5,
                               "density": 1.0, "capacity_pct": 12.5,
                               "inflow_per_min": 0.0, "outflow_per_min": 0.0,
                               "status": "NORMAL"})
        row = next(z for z in store.latest_zone_states()
                   if z["zone_id"] == "ZONE-NEW")
        self.assertEqual("SITE-NEW", row["site_id"])

    def test_migration_is_idempotent(self):
        """The API opens the store on every start; a second ALTER would raise
        'duplicate column name' and the process would not boot."""
        SQLiteStore(self.path)
        SQLiteStore(self.path)
        store = SQLiteStore(self.path)
        self.assertEqual(1, len(store.list_alerts_history(0, 1e12, limit=10)))

    def test_a_fresh_database_has_the_columns_without_migrating(self):
        fresh = os.path.join(tempfile.mkdtemp(), "fresh.db")
        store = SQLiteStore(fresh)
        aid = store.save_alert({"rule_id": "R-01", "severity": "AMBER",
                                "message": "m", "camera_id": "C", "ts": 1.0,
                                "site_id": "SITE-F", "kind": "FIRE"})
        row = next(a for a in store.list_alerts(unacked_only=False)
                   if str(a["alert_id"]) == str(aid))
        self.assertEqual("SITE-F", row["site_id"])


if __name__ == "__main__":
    unittest.main()
