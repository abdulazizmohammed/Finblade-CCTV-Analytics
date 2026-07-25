import unittest

from finblade.events import (
    CAMERA_HEARTBEAT,
    CAMERA_OFFLINE,
    DENSITY_UPDATE,
    ZONE_ENTRY,
    ZONE_EXIT,
    ZONE_TRANSITION,
    new_event,
    validate_event,
)
from finblade.identity import PersonRefHasher

PR = PersonRefHasher(session_salt="fixed").ref(1)


class TestEventBuild(unittest.TestCase):
    def test_envelope(self):
        e = new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", 1000.0,
                      zone_to="Z1", person_ref=PR, confidence=0.9)
        self.assertEqual(e["event_type"], ZONE_ENTRY)
        self.assertIn("event_id", e)
        self.assertEqual(e["zone_to"], "Z1")


class TestValidation(unittest.TestCase):
    def test_valid_entry(self):
        e = new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", 1000.0,
                      zone_to="Z1", person_ref=PR, confidence=0.9)
        ok, errs = validate_event(e)
        self.assertTrue(ok, errs)

    def test_valid_transition(self):
        e = new_event(ZONE_TRANSITION, "CAM-A-01", "SITE-1", 1.0,
                      zone_from="Z1", zone_to="Z2", person_ref=PR)
        self.assertTrue(validate_event(e)[0])

    def test_valid_density_update(self):
        e = new_event(DENSITY_UPDATE, "CAM-A-01", "SITE-1", 1.0,
                      zone_id="Z1", occupancy=5, density=0.4)
        self.assertTrue(validate_event(e)[0])

    def test_valid_heartbeat_and_offline(self):
        self.assertTrue(validate_event(
            new_event(CAMERA_HEARTBEAT, "CAM-A-01", "SITE-1", 1.0))[0])
        self.assertTrue(validate_event(
            new_event(CAMERA_OFFLINE, "CAM-A-01", "SITE-1", 40.0, last_seen=5.0))[0])

    def test_valid_new_event_types(self):
        from finblade.events import (
            CAPACITY_WARNING, RESTRICTED_ZONE_ENTRY, RESTRICTED_ZONE_EXIT,
            LOITERING_START, LOITERING_END, CAMERA_ONLINE, CAMERA_RECOVERED,
        )
        ok = lambda e: validate_event(e)[0]
        self.assertTrue(ok(new_event(CAPACITY_WARNING, "C", "S", 1.0,
                                     zone_id="Z1", occupancy=36, capacity_pct=90.0)))
        self.assertTrue(ok(new_event(RESTRICTED_ZONE_ENTRY, "C", "S", 1.0,
                                     zone_id="Z2", person_ref=PR)))
        self.assertTrue(ok(new_event(RESTRICTED_ZONE_EXIT, "C", "S", 1.0,
                                     zone_id="Z2", person_ref=PR)))
        self.assertTrue(ok(new_event(LOITERING_START, "C", "S", 1.0,
                                     zone_id="Z1", person_ref=PR, dwell_time=42.0)))
        self.assertTrue(ok(new_event(LOITERING_END, "C", "S", 1.0,
                                     zone_id="Z1", person_ref=PR, dwell_time=99.0)))
        self.assertTrue(ok(new_event(CAMERA_ONLINE, "C", "S", 1.0)))
        self.assertTrue(ok(new_event(CAMERA_RECOVERED, "C", "S", 1.0)))

    def test_new_event_missing_field_rejected(self):
        from finblade.events import LOITERING_START
        ok, errs = validate_event(new_event(LOITERING_START, "C", "S", 1.0,
                                            zone_id="Z1", person_ref=PR))  # no dwell_time
        self.assertFalse(ok)
        self.assertTrue(any("dwell_time" in x for x in errs))

    def test_unknown_type_rejected(self):
        e = new_event("BOGUS", "CAM-A-01", "SITE-1", 1.0)
        ok, errs = validate_event(e)
        self.assertFalse(ok)
        self.assertTrue(any("event_type" in x for x in errs))

    def test_missing_required_field_rejected(self):
        e = new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", 1.0, person_ref=PR, confidence=0.5)
        # zone_to missing
        ok, errs = validate_event(e)
        self.assertFalse(ok)
        self.assertTrue(any("zone_to" in x for x in errs))

    def test_wrong_type_rejected(self):
        e = new_event(DENSITY_UPDATE, "CAM-A-01", "SITE-1", 1.0,
                      zone_id="Z1", occupancy="five", density=0.4)
        self.assertFalse(validate_event(e)[0])

    def test_negative_occupancy_rejected(self):
        e = new_event(DENSITY_UPDATE, "CAM-A-01", "SITE-1", 1.0,
                      zone_id="Z1", occupancy=-1, density=0.4)
        self.assertFalse(validate_event(e)[0])

    def test_confidence_out_of_range_rejected(self):
        e = new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", 1.0,
                      zone_to="Z1", person_ref=PR, confidence=1.5)
        self.assertFalse(validate_event(e)[0])

    def test_bad_timestamp_rejected(self):
        e = new_event(CAMERA_HEARTBEAT, "CAM-A-01", "SITE-1", -1.0)
        self.assertFalse(validate_event(e)[0])

    def test_pii_person_ref_rejected(self):
        e = new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", 1.0,
                      zone_to="Z1", person_ref="john.smith", confidence=0.9)
        ok, errs = validate_event(e)
        self.assertFalse(ok)
        self.assertTrue(any("PII" in x or "anonymous" in x for x in errs))

    def test_bool_not_accepted_as_number(self):
        e = new_event(DENSITY_UPDATE, "CAM-A-01", "SITE-1", 1.0,
                      zone_id="Z1", occupancy=True, density=0.4)
        self.assertFalse(validate_event(e)[0])


if __name__ == "__main__":
    unittest.main()
