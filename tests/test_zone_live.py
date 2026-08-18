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

from tests.pgfixture import store_for
from services.api.store import InMemoryStore

def NOW():
    """Evaluated per call, NOT once at import.

    latest_zone_states() drops readings older than 30 seconds of wall clock, so
    a module-level constant is a time bomb: it is fine while the suite finishes
    inside 30s and fails everywhere the moment it does not. It did not, once
    the Postgres conformance tests joined and the run went from 22s to 50s —
    eight tests in this file started failing on a stale fixture with nothing
    wrong in the code they cover.
    """
    return time.time()


def state(zone_id="ZONE-01", camera_id="CAM-01", ts=None, occupancy=3, **over):
    row = {"zone_id": zone_id, "camera_id": camera_id,
           "ts": NOW() if ts is None else ts, "occupancy": occupancy,
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
            self.store.save_zone_state(state(ts=NOW() - 50 + i, occupancy=i))
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
        self.store.save_zone_state(state(ts=NOW(), occupancy=7))
        self.store.save_zone_state(state(ts=NOW() - 30, occupancy=1))   # stale
        self.assertEqual(7, self.store.latest_zone_states()[0]["occupancy"])

    def test_history_is_still_written(self):
        """zone_live is in addition to the series, not instead of it."""
        for i in range(3):
            self.store.save_zone_state(state(ts=NOW() - 10 + i))
        self.assertEqual(3, len(self.store.zone_state_range("ZONE-01", 0, NOW() + 1)))

    def test_stale_zones_are_dropped_from_live(self):
        """A zone removed or renamed in the editor stops reporting; the 30s
        freshness window is what makes it disappear."""
        self.store.save_zone_state(state(ts=NOW() - 3600))
        self.assertEqual([], self.store.latest_zone_states())

    def test_pruning_history_does_not_remove_current_state(self):
        """The point of splitting the tables: retention deletes the series, the
        live reading survives. Under the old query it was the same row."""
        self.store.save_zone_state(state(ts=NOW()))
        self.store.delete_before(NOW() + 86400)          # delete everything older
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


class TestPostgresZoneLive(ZoneLiveContract, unittest.TestCase):
    """The durable backend must satisfy the same contract as the in-memory one."""

    def setUp(self):
        self._path = tempfile.mkdtemp() + "/zone_live"
        self.store = store_for(self._path)

    def make_store(self):
        return self.store

    def test_state_survives_a_new_store_on_the_same_database(self):
        """What the old SQLite case proved by reopening a file path."""
        self.store.save_zone_state(state(occupancy=4))
        reopened = store_for(self._path)
        live = reopened.latest_zone_states()
        self.assertEqual(1, len(live))
        self.assertEqual(4, live[0]["occupancy"])

    # REMOVED WITH SQLITE: test_existing_history_is_backfilled_on_upgrade and
    # test_backfill_does_not_clobber_a_populated_table.
    #
    # Both drove SQLiteStore._migrate(), which seeded zone_live from
    # zone_state_ts for deployments upgrading from a schema that predated the
    # table. They reached into store._lock and store._conn to empty zone_live
    # and simulate that older shape.
    #
    # Postgres has no equivalent to reproduce. zone_live has been in ddl_pg.sql
    # since that file existed, so no Postgres deployment has ever reached the
    # state the backfill repaired, and there is no migration to test. Writing a
    # Postgres version would be testing a code path that does not exist.


if __name__ == "__main__":
    unittest.main()
