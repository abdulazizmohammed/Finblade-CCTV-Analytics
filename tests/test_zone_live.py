"""zone_live — Part A step 2.

Current state moves out of the history table into one row per zone, overwritten
in place. Reading "what is happening now" used to mean
SELECT ... WHERE id IN (SELECT MAX(id) ... GROUP BY zone_id, camera_id) across
the whole of zone_state_ts — 1.9s against 1.6M rows before a covering index was
added, on a query the dashboard runs twice a second.

Run against both stores. A divergence between them makes the suite green while
production behaves differently, which is exactly what had happened here: the
SQLite path took the last row INSERTED, the in-memory path took the newest by
timestamp, and nothing noticed.
"""

import os
import tempfile
import time
import unittest

from services.api.sqlite_store import SQLiteStore
from services.api.store import InMemoryStore

NOW = time.time()


def state(zone_id="ZONE-01", camera_id="CAM-01", ts=None, occupancy=3, **over):
    row = {"zone_id": zone_id, "camera_id": camera_id,
           "ts": NOW if ts is None else ts, "occupancy": occupancy,
           "density": 0.06, "capacity_pct": 7.5, "inflow_per_min": 1.0,
           "outflow_per_min": 2.0, "status": "NORMAL", "zone_name": "Lobby"}
    row.update(over)
    return row


class ZoneLiveContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def live(self):
        return {(z.get("camera_id"), z["zone_id"]): z
                for z in self.store.latest_zone_states()}

    def test_one_row_per_zone_however_many_writes(self):
        for i in range(50):
            self.store.save_zone_state(state(ts=NOW - 50 + i, occupancy=i))
        rows = self.store.latest_zone_states()
        self.assertEqual(1, len(rows))
        self.assertEqual(49, rows[0]["occupancy"], "must be the newest write")

    def test_zone_ids_are_scoped_to_their_camera(self):
        """ZONE-01 exists on several cameras as different areas. Keying on
        zone_id alone collapsed them and returned whichever wrote last."""
        self.store.save_zone_state(state(camera_id="CAM-01", occupancy=4))
        self.store.save_zone_state(state(camera_id="CAM-02", occupancy=9))
        live = self.live()
        self.assertEqual(2, len(live))
        self.assertEqual(4, live[("CAM-01", "ZONE-01")]["occupancy"])
        self.assertEqual(9, live[("CAM-02", "ZONE-01")]["occupancy"])

    def test_an_out_of_order_write_does_not_move_state_backwards(self):
        """A delayed post from a slow camera arriving after a newer one."""
        self.store.save_zone_state(state(ts=NOW, occupancy=7))
        self.store.save_zone_state(state(ts=NOW - 30, occupancy=1))   # stale
        self.assertEqual(7, self.store.latest_zone_states()[0]["occupancy"])

    def test_history_is_still_written(self):
        """zone_live is in addition to the series, not instead of it."""
        for i in range(3):
            self.store.save_zone_state(state(ts=NOW - 10 + i))
        self.assertEqual(3, len(self.store.zone_state_range("ZONE-01", 0, NOW + 1)))

    def test_stale_zones_are_dropped_from_live(self):
        """A zone removed or renamed in the editor stops reporting; the 30s
        freshness window is what makes it disappear."""
        self.store.save_zone_state(state(ts=NOW - 3600))
        self.assertEqual([], self.store.latest_zone_states())

    def test_pruning_history_does_not_remove_current_state(self):
        """The point of splitting the tables: retention deletes the series, the
        live reading survives. Under the old query it was the same row."""
        self.store.save_zone_state(state(ts=NOW))
        self.store.delete_before(NOW + 86400)          # delete everything older
        self.assertEqual(1, len(self.store.latest_zone_states()))

    def test_extra_fields_survive_the_round_trip(self):
        self.store.save_zone_state(state(net_flow=-1.0, capacity_max=40,
                                         area_sqm=50.0))
        row = self.store.latest_zone_states()[0]
        self.assertEqual(-1.0, row["net_flow"])
        self.assertEqual(40, row["capacity_max"])

    def test_site_id_and_flags_survive(self):
        self.store.save_zone_state(state(site_id="SITE-9", restricted=True,
                                         zone_type="RESTRICTED"))
        row = self.store.latest_zone_states()[0]
        self.assertEqual("SITE-9", row["site_id"])
        self.assertTrue(row["restricted"])


class TestInMemoryZoneLive(ZoneLiveContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()

    def test_matches_the_pre_zone_live_implementation(self):
        """Same data, same answer as the MAX-by-timestamp scan it replaced."""
        for cam, zone, occ in (("CAM-01", "ZONE-01", 2), ("CAM-01", "ZONE-02", 5),
                               ("CAM-02", "ZONE-01", 8)):
            self.store.save_zone_state(state(zone_id=zone, camera_id=cam,
                                             occupancy=occ))
        by_key = lambda rows: sorted(  # noqa: E731
            (r.get("camera_id"), r["zone_id"], r["occupancy"]) for r in rows)
        self.assertEqual(by_key(self.store._latest_from_history()),
                         by_key(self.store.latest_zone_states()))


class TestSQLiteZoneLive(ZoneLiveContract, unittest.TestCase):
    def make_store(self):
        return SQLiteStore(self.path())

    def path(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_existing_history_is_backfilled_on_upgrade(self):
        """A deployment upgrading with zone_state_ts already populated must not
        show an empty dashboard until every camera next reports — and on a box
        with the workers stopped, that would be indefinitely."""
        path = self.path()
        store = SQLiteStore(path)
        store.save_zone_state(state(occupancy=6))
        # Simulate the pre-zone_live state: history present, live table empty.
        with store._lock:
            store._conn.execute("DELETE FROM zone_live")
            store._conn.commit()
        store._zone_cache = None
        self.assertEqual([], store.latest_zone_states(), "precondition")

        reopened = SQLiteStore(path)                   # runs _migrate()
        rows = reopened.latest_zone_states()
        self.assertEqual(1, len(rows))
        self.assertEqual(6, rows[0]["occupancy"])

    def test_backfill_does_not_clobber_a_populated_table(self):
        path = self.path()
        store = SQLiteStore(path)
        store.save_zone_state(state(ts=NOW - 100, occupancy=1))
        store.save_zone_state(state(ts=NOW, occupancy=9))
        reopened = SQLiteStore(path)
        self.assertEqual(9, reopened.latest_zone_states()[0]["occupancy"])


if __name__ == "__main__":
    unittest.main()
