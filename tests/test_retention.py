"""Retention — Part A step 0.

A nine-day database reached 1.1 GB and 3.35M rows, growing ~130 MB/day, with
nothing pruning it. Beyond the disk, indefinite retention is not a defensible
position for a system that records people.

Covers both stores because the SQLite one is the default and the in-memory one
is what every other test runs against — a divergence between them would make
the suite green while production behaved differently.
"""

import os
import tempfile
import time
import unittest

from services.api.sqlite_store import SQLiteStore
from services.api.store import InMemoryStore

NOW = time.time()
DAY = 86400.0


def event(eid, ts):
    return {"event_id": eid, "event_type": "ZONE_ENTRY", "camera_id": "CAM-01",
            "site_id": "SITE-01", "ts": ts, "timestamp": ts,
            "zone_to": "ZONE-01", "person_ref": "pr_" + "a" * 16}


def zone_state(ts, zone_id="ZONE-01"):
    return {"zone_id": zone_id, "camera_id": "CAM-01", "ts": ts, "occupancy": 3,
            "density": 0.06, "capacity_pct": 7.5, "inflow_per_min": 1.0,
            "outflow_per_min": 1.0, "status": "NORMAL"}


class RetentionContract:
    """Applied to both stores — see module docstring."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        for age_days, eid in ((30, "old-1"), (10, "old-2"), (1, "recent")):
            self.store.save_event(event(eid, NOW - age_days * DAY))
            self.store.save_zone_state(zone_state(NOW - age_days * DAY))

    def test_deletes_only_what_is_older_than_the_cutoff(self):
        deleted = self.store.delete_before(NOW - 7 * DAY)
        self.assertEqual(2, deleted["events"])
        self.assertEqual(2, deleted["zone_state_ts"])
        remaining = [e["event_id"]
                     for e in self.store.list_events(0, NOW + DAY, limit=100)]
        self.assertEqual(["recent"], remaining)

    def test_is_idempotent(self):
        self.store.delete_before(NOW - 7 * DAY)
        again = self.store.delete_before(NOW - 7 * DAY)
        self.assertEqual(0, again["events"])
        self.assertEqual(0, again["zone_state_ts"])

    def test_a_cutoff_before_everything_deletes_nothing(self):
        deleted = self.store.delete_before(NOW - 365 * DAY)
        self.assertEqual({"zone_state_ts": 0, "events": 0}, deleted)

    def test_alerts_are_never_touched(self):
        """Alerts are the operator audit trail and each may own a snapshot JPEG
        on disk. Deleting the row here would orphan the image — that is what
        DELETE /api/v1/alerts is for."""
        self.store.save_alert({"rule_id": "R-06", "severity": "RED",
                               "message": "intrusion", "camera_id": "CAM-01",
                               "ts": NOW - 90 * DAY, "kind": "FIRE",
                               "frame": "/bookmarks/old.jpg"})
        self.store.delete_before(NOW - 7 * DAY)
        self.assertEqual(
            1, len(self.store.list_alerts_history(0, NOW + DAY, limit=100)))


class TestInMemoryRetention(RetentionContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class TestSQLiteRetention(RetentionContract, unittest.TestCase):
    def make_store(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return SQLiteStore(path)

    def test_live_zone_cache_is_dropped_after_a_delete(self):
        """latest_zone_states() caches for 1s. Pruning underneath a warm cache
        would keep serving rows that no longer exist."""
        self.store.save_zone_state(zone_state(NOW, zone_id="ZONE-LIVE"))
        self.store.latest_zone_states()                 # warm it
        self.store.delete_before(NOW - 7 * DAY)
        self.assertIsNone(self.store._zone_cache)


class TestBaseStoreDefault(unittest.TestCase):
    def test_a_backend_that_does_not_implement_it_is_a_no_op(self):
        """Postgres has no implementation yet. It must not raise."""
        from services.api.store import Store
        self.assertEqual({}, Store().delete_before(NOW))


class TestConfiguration(unittest.TestCase):
    """Deliberately does NOT importlib.reload the app module.

    The first version of this test did, and it broke two unrelated tests in
    test_snapshot_access: a reload rebinds `app`, `svc` and the store, while
    every other test module still holds references to the originals. An alert
    raised through the old service was then looked up in the new one's store and
    came back 404. Patch the attribute instead.
    """

    def test_retention_is_opt_in(self):
        """Deleting a deployment's history because a default said so is not a
        decision this code gets to make silently."""
        from services.api import app as app_module
        self.assertEqual(0, app_module.RETENTION_DAYS,
                         "default must be off; enable with FINBLADE_RETENTION_DAYS")

    def test_the_loop_returns_immediately_when_disabled(self):
        import asyncio
        from services.api import app as app_module
        original = app_module.RETENTION_DAYS
        app_module.RETENTION_DAYS = 0
        self.addCleanup(setattr, app_module, "RETENTION_DAYS", original)
        # Would otherwise sleep for RETENTION_INTERVAL before its first pass.
        asyncio.run(asyncio.wait_for(app_module._retention_loop(), timeout=2))

    def test_a_configured_window_is_read_in_days(self):
        from services.api import app as app_module
        original = app_module.RETENTION_DAYS
        app_module.RETENTION_DAYS = 7
        self.addCleanup(setattr, app_module, "RETENTION_DAYS", original)
        self.assertGreater(app_module.RETENTION_DAYS, 0)


if __name__ == "__main__":
    unittest.main()
