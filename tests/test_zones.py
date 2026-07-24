import unittest

from finblade.zones import Zone, zone_of, zone_from_dict


def _zone(zid, poly, restricted=False):
    return Zone(zone_id=zid, zone_name=zid, restricted=restricted,
                capacity_max=10, area_sqm=20.0, polygon=poly)


class TestZoneAssignment(unittest.TestCase):
    def setUp(self):
        self.lobby = _zone("ZONE-01", [(0, 0), (10, 0), (10, 10), (0, 10)])
        # ZONE-02 overlaps the lobby's top-right and is restricted.
        self.restricted = _zone("ZONE-02", [(5, 5), (10, 5), (10, 10), (5, 10)],
                                restricted=True)
        self.zones = [self.lobby, self.restricted]

    def test_point_in_single_zone(self):
        self.assertEqual(zone_of((2, 2), self.zones), "ZONE-01")

    def test_point_in_no_zone(self):
        self.assertIsNone(zone_of((50, 50), self.zones))

    def test_restricted_wins_overlap(self):
        # (7,7) is inside BOTH; restricted must win.
        self.assertEqual(zone_of((7, 7), self.zones), "ZONE-02")

    def test_restricted_wins_regardless_of_list_order(self):
        reversed_zones = [self.restricted, self.lobby]
        self.assertEqual(zone_of((7, 7), reversed_zones), "ZONE-02")

    def test_from_dict(self):
        z = zone_from_dict({
            "zone_id": "Z", "zone_name": "n", "restricted": True,
            "capacity_max": 5, "area_sqm": 12.0, "polygon": [[0, 0], [1, 0], [1, 1]],
        })
        self.assertTrue(z.restricted)
        self.assertEqual(z.polygon, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])


if __name__ == "__main__":
    unittest.main()
