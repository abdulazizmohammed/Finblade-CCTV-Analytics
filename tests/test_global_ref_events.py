"""Cross-camera identity reaches the event store (REQ-26, REQ-31).

person_ref is a hash of the tracker id, scoped to one camera process and one
session, so the same human on two cameras carries two unrelated refs and "where
did this person go" has no query at all. The identity service was already
resolving a cross-camera global_ref and dropping it at the process boundary.
These tests hold the column, the filter and the merge write-back.
"""

import os
import tempfile
import unittest

from finblade.events import ZONE_ENTRY, ZONE_TRANSITION, new_event, validate_event
from services.api.service import IngestService
from tests.pgfixture import store_for

SITE = "SITE-DXB-01"
GREF_A = "gp_aaaaaaaaaaaaaaaa"
GREF_B = "gp_bbbbbbbbbbbbbbbb"


def anon(label):
    import hashlib
    return "pr_" + hashlib.sha256(label.encode()).hexdigest()[:16]


class TestSchema(unittest.TestCase):
    def test_global_ref_is_accepted(self):
        e = new_event(ZONE_ENTRY, "CAM-03", SITE, 10.0, person_ref=anon("a"),
                      zone_to="LOBBY", confidence=0.8, global_ref=GREF_A)
        ok, errors = validate_event(e)
        self.assertTrue(ok, errors)

    def test_global_ref_must_be_a_string(self):
        e = new_event(ZONE_ENTRY, "CAM-03", SITE, 10.0, person_ref=anon("a"),
                      zone_to="LOBBY", confidence=0.8, global_ref=17)
        self.assertFalse(validate_event(e)[0])

    def test_absent_is_different_from_null(self):
        # "not resolved" is a different statement from "resolved to nothing",
        # so a null must not validate as a global ref.
        e = new_event(ZONE_ENTRY, "CAM-03", SITE, 10.0, person_ref=anon("a"),
                      zone_to="LOBBY", confidence=0.8)
        self.assertTrue(validate_event(e)[0])
        e["global_ref"] = None
        self.assertFalse(validate_event(e)[0])


class TestStoredAndQueryable(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = store_for(self.path)
        self.svc = IngestService(self.store)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def post(self, cam, ts, ref_label, gref, **payload):
        evt = new_event(ZONE_TRANSITION, cam, SITE, ts,
                        person_ref=anon(ref_label), global_ref=gref, **payload)
        code, body = self.svc.ingest_event(evt)
        self.assertEqual(code, 202, body)

    def test_one_person_across_three_cameras_is_one_query(self):
        # The same human, three cameras, three unrelated per-camera refs.
        self.post("CAM-01", 100.0, "cam1-track125", GREF_A,
                  zone_from="LOBBY", zone_to="CORRIDOR")
        self.post("CAM-03", 160.0, "cam3-track72", GREF_A,
                  zone_from="CORRIDOR", zone_to="ATRIUM")
        self.post("CAM-07", 220.0, "cam7-track281", GREF_A,
                  zone_from="ATRIUM", zone_to="RESTRICTED-01")
        # Somebody else entirely.
        self.post("CAM-03", 165.0, "cam3-track99", GREF_B,
                  zone_from="CORRIDOR", zone_to="ATRIUM")

        journey = self.store.list_events(0, 9e12, global_ref=GREF_A)
        self.assertEqual(len(journey), 3)
        self.assertEqual({e["camera_id"] for e in journey},
                         {"CAM-01", "CAM-03", "CAM-07"})
        # Per-camera refs really are all different — the point of the column.
        self.assertEqual(len({e["person_ref"] for e in journey}), 3)

    def test_events_without_reid_are_unaffected(self):
        evt = new_event(ZONE_TRANSITION, "CAM-01", SITE, 100.0,
                        person_ref=anon("x"), zone_from="A", zone_to="B")
        self.assertEqual(self.svc.ingest_event(evt)[0], 202)
        rows = self.store.list_events(0, 9e12)
        self.assertIsNone(rows[0]["global_ref"])
        self.assertEqual(self.store.list_events(0, 9e12, global_ref=GREF_A), [])

    def test_merge_rebinds_history_not_just_the_gallery(self):
        self.post("CAM-01", 100.0, "a", GREF_A, zone_from="A", zone_to="B")
        self.post("CAM-03", 200.0, "b", GREF_B, zone_from="B", zone_to="C")
        self.post("CAM-03", 260.0, "b2", GREF_B, zone_from="C", zone_to="D")

        moved = self.store.rebind_global_ref(GREF_B, GREF_A)
        self.assertEqual(moved, 2)
        self.assertEqual(len(self.store.list_events(0, 9e12, global_ref=GREF_A)), 3)
        self.assertEqual(self.store.list_events(0, 9e12, global_ref=GREF_B), [])

    def test_rebinding_is_a_no_op_for_nonsense_input(self):
        self.post("CAM-01", 100.0, "a", GREF_A, zone_from="A", zone_to="B")
        self.assertEqual(self.store.rebind_global_ref(GREF_A, GREF_A), 0)
        self.assertEqual(self.store.rebind_global_ref("", GREF_A), 0)
        self.assertEqual(self.store.rebind_global_ref(None, GREF_A), 0)
        self.assertEqual(len(self.store.list_events(0, 9e12, global_ref=GREF_A)), 1)

    def test_column_is_added_to_an_existing_database(self):
        # The migration path: a store opened again over the same file must not
        # lose the column or the rows.
        self.post("CAM-01", 100.0, "a", GREF_A, zone_from="A", zone_to="B")
        reopened = store_for(self.path)
        self.assertEqual(len(reopened.list_events(0, 9e12, global_ref=GREF_A)), 1)


if __name__ == "__main__":
    unittest.main()
