import unittest

from finblade.zones import Zone, zone_of, zone_from_dict, ZONE_TYPES


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

    def test_disabled_zone_ignored(self):
        z = _zone("ZD", [(0, 0), (10, 0), (10, 10), (0, 10)])
        z.enabled = False
        self.assertIsNone(zone_of((5, 5), [z]))


class TestZoneModel(unittest.TestCase):
    def test_restricted_derives_zone_type(self):
        z = zone_from_dict({"zone_id": "Z", "restricted": True, "polygon": [[0, 0], [1, 0], [1, 1]]})
        self.assertEqual(z.zone_type, "RESTRICTED")

    def test_zone_type_restricted_sets_restricted(self):
        z = zone_from_dict({"zone_id": "Z", "zone_type": "RESTRICTED",
                            "polygon": [[0, 0], [1, 0], [1, 1]]})
        self.assertTrue(z.restricted)

    def test_new_defaults(self):
        z = zone_from_dict({"zone_id": "Z", "polygon": [[0, 0], [1, 0], [1, 1]]})
        self.assertEqual(z.zone_type, "MONITORED")
        self.assertFalse(z.restricted)
        self.assertEqual(z.warning_density, 2.0)
        self.assertEqual(z.critical_density, 4.0)
        self.assertEqual(z.loitering_threshold_sec, 30.0)
        self.assertTrue(z.enabled)

    def test_extended_fields(self):
        z = zone_from_dict({
            "zone_id": "Z", "zone_name": "Gate", "zone_type": "ENTRANCE",
            "warning_density": 1.5, "critical_density": 3.0,
            "loitering_threshold_sec": 45, "adjacency_list": ["Z2", "Z3"],
            "colour": "#e0479e", "enabled": False, "camera_id": "CAM-A",
            "polygon": [[0, 0], [2, 0], [2, 2]],
        })
        self.assertEqual(z.zone_type, "ENTRANCE")
        self.assertEqual(z.warning_density, 1.5)
        self.assertEqual(z.critical_density, 3.0)
        self.assertEqual(z.loitering_threshold_sec, 45.0)
        self.assertEqual(z.adjacency_list, ["Z2", "Z3"])
        self.assertEqual(z.colour, "#e0479e")
        self.assertFalse(z.enabled)
        self.assertEqual(z.camera_id, "CAM-A")

    def test_unknown_zone_type_falls_back(self):
        z = zone_from_dict({"zone_id": "Z", "zone_type": "BOGUS", "polygon": [[0, 0], [1, 0], [1, 1]]})
        self.assertEqual(z.zone_type, "MONITORED")
        self.assertIn(z.zone_type, ZONE_TYPES)

    def test_normalized_polygon_load_and_export_roundtrip(self):
        # Load from normalized coords with a known frame size -> pixel polygon.
        z = zone_from_dict({"zone_id": "Z", "zone_type": "MONITORED",
                            "normalized_polygon": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.8]]},
                           frame_width=1000, frame_height=500)
        self.assertEqual(z.polygon, [(100.0, 100.0), (500.0, 100.0), (500.0, 400.0)])
        # Export back to normalized -> same fractions.
        d = z.to_dict(1000, 500)
        self.assertEqual(d["normalized_polygon"], [[0.1, 0.2], [0.5, 0.2], [0.5, 0.8]])


if __name__ == "__main__":
    unittest.main()
