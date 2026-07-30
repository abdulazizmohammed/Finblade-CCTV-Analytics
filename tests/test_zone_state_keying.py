"""Zone state must be keyed on (camera_id, zone_id), never zone_id alone.

Zone ids are unique only WITHIN a camera. The zone editor numbers each camera's
zones from ZONE-01, so a six-camera site routinely has several zones called
ZONE-01, all different areas.

Both stores keyed their "latest state per zone" on zone_id alone, so those zones
overwrote each other and only the last writer survived. Measured on a live
six-camera site: seven zones existed, every response returned exactly five, and
the pairs sharing an id alternated between requests -

    CAM-02/ZONE-03 '1F Lobby'   appeared in 13 of 25 responses
    CAM-06/ZONE-03 '2F Lobby'   appeared in 12 of 25 responses   (13 + 12 = 25)
    CAM-03/ZONE-01 'Lobby'      appeared in 20 of 25 responses
    CAM-04/ZONE-01 'Lobby'      appeared in  5 of 25 responses   (20 +  5 = 25)

Consequences, in order of seriousness: site occupancy silently omitted whole
zones and the total was simply wrong; rules evaluated against whichever camera
posted last; and the dashboard reshuffled cards as zones appeared and vanished.

It is also invisible on any single-camera site, and on any site where zone ids
happen not to collide - which is why it survived this long.
"""

import os
import tempfile
import unittest

from services.api.sqlite_store import SQLiteStore
from services.api.store import InMemoryStore


def _state(camera_id, zone_id, name, occupancy, ts):
    return {"camera_id": camera_id, "zone_id": zone_id, "zone_name": name,
            "zone_type": "MONITORED", "restricted": False, "ts": ts,
            "occupancy": occupancy, "density": 0.0, "capacity_pct": 0.0,
            "peak_occupancy": occupancy, "avg_occupancy": float(occupancy),
            "trend": "flat", "inflow_per_min": 0.0, "outflow_per_min": 0.0,
            "status": "NORMAL"}


class _ZoneKeyingContract:
    """Applied to both backends - they must agree, or behaviour changes with
    FINBLADE_INMEMORY and tests stop predicting production."""

    def _store(self):
        raise NotImplementedError

    def test_same_zone_id_on_two_cameras_both_survive(self):
        """THE regression. Two cameras each with a ZONE-01 must both appear."""
        s = self._store()
        import time
        now = time.time()
        s.save_zone_state(_state("CAM-03", "ZONE-01", "Lobby", 2, now))
        s.save_zone_state(_state("CAM-04", "ZONE-01", "Reception", 3, now + 0.1))
        got = s.latest_zone_states()
        keys = sorted((z["camera_id"], z["zone_id"]) for z in got)
        self.assertEqual(keys, [("CAM-03", "ZONE-01"), ("CAM-04", "ZONE-01")],
                         "a zone was dropped: zone ids collide across cameras")

    def test_site_occupancy_counts_every_zone(self):
        """The consequence that reached the operator: the headline total was
        summed over whatever survived the collision."""
        s = self._store()
        import time
        now = time.time()
        s.save_zone_state(_state("CAM-03", "ZONE-01", "Lobby", 2, now))
        s.save_zone_state(_state("CAM-04", "ZONE-01", "Reception", 3, now + 0.1))
        s.save_zone_state(_state("CAM-05", "ZONE-07", "Passage", 1, now + 0.2))
        total = sum(z["occupancy"] for z in s.latest_zone_states())
        self.assertEqual(total, 6, "site occupancy omitted a colliding zone")

    def test_latest_state_still_wins_for_the_same_zone(self):
        """Fixing the collision must not break the actual purpose: the newest
        sample for a given camera+zone is the one reported."""
        s = self._store()
        import time
        now = time.time()
        s.save_zone_state(_state("CAM-03", "ZONE-01", "Lobby", 2, now))
        s.save_zone_state(_state("CAM-03", "ZONE-01", "Lobby", 9, now + 1))
        got = [z for z in s.latest_zone_states() if z["camera_id"] == "CAM-03"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["occupancy"], 9)

    def test_zone_count_is_stable_across_repeated_reads(self):
        """The symptom that exposed it: identical data returning a different
        set of zones from one request to the next."""
        s = self._store()
        import time
        now = time.time()
        for cam, zid in (("CAM-02", "ZONE-03"), ("CAM-06", "ZONE-03"),
                         ("CAM-01", "ZONE-02"), ("CAM-04", "ZONE-06")):
            s.save_zone_state(_state(cam, zid, cam + zid, 1, now))
        counts = {len(s.latest_zone_states()) for _ in range(5)}
        self.assertEqual(counts, {4},
                         f"zone count varied between identical reads: {counts}")


class TestInMemoryZoneKeying(_ZoneKeyingContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class TestSQLiteZoneKeying(_ZoneKeyingContract, unittest.TestCase):
    def _store(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return SQLiteStore(path)


if __name__ == "__main__":
    unittest.main()
