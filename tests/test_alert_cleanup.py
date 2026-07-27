import os
import shutil
import tempfile
import unittest

from services.api.service import IngestService
from services.api.store import InMemoryStore

BOOKMARKS = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "evidence", "bookmarks"))


def alert(rule="R-06", status="OPEN", frame=None, ts=100.0):
    return {"rule_id": rule, "severity": "RED", "message": "test",
            "zone_id": "Z1", "camera_id": "CAM-A", "ts": ts,
            "kind": "FIRE", "status": status, "frame": frame}


class TestDeleteAlerts(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.svc = IngestService(self.store)

    def _add(self, **kw):
        aid = self.store.save_alert(alert(**kw))
        return aid

    def test_closed_scope_spares_open_alerts(self):
        # The safe default: an operator clearing the feed must not silently
        # delete something still needing attention.
        self._add(status="OPEN")
        self._add(status="RESOLVED")
        self._add(status="DISMISSED")
        code, body = self.svc.clear_alerts(scope="closed", delete_frames=False)
        self.assertEqual(code, 200)
        self.assertEqual(body["alerts_deleted"], 2)
        self.assertEqual(len(self.store.list_alerts()), 1)

    def test_all_scope_deletes_everything(self):
        self._add(status="OPEN")
        self._add(status="RESOLVED")
        code, body = self.svc.clear_alerts(scope="all", delete_frames=False)
        self.assertEqual(body["alerts_deleted"], 2)
        self.assertEqual(self.store.list_alerts(), [])

    def test_bad_scope_rejected(self):
        code, body = self.svc.clear_alerts(scope="everything")
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_deleting_nothing_is_not_an_error(self):
        code, body = self.svc.clear_alerts(scope="closed")
        self.assertEqual(code, 200)
        self.assertEqual(body["alerts_deleted"], 0)


class TestFrameDeletion(unittest.TestCase):
    """Frame files must actually leave the disk — deleting only the rows would
    orphan the JPEGs, which are the bulk of the space."""

    def setUp(self):
        self.store = InMemoryStore()
        self.svc = IngestService(self.store)
        os.makedirs(BOOKMARKS, exist_ok=True)
        self.made = []

    def tearDown(self):
        for p in self.made:
            try:
                os.remove(p)
            except OSError:
                pass

    def _frame(self, name):
        path = os.path.join(BOOKMARKS, name)
        with open(path, "wb") as fh:
            fh.write(b"jpegdata")
        self.made.append(path)
        return "/bookmarks/" + name

    def test_frame_file_is_removed_with_its_alert(self):
        ref = self._frame("test_del_one.jpg")
        path = os.path.join(BOOKMARKS, "test_del_one.jpg")
        self.store.save_alert(alert(status="RESOLVED", frame=ref))
        self.assertTrue(os.path.exists(path))

        code, body = self.svc.clear_alerts(scope="closed")
        self.assertEqual(body["frames_deleted"], 1)
        self.assertFalse(os.path.exists(path))

    def test_alert_without_a_frame_is_fine(self):
        self.store.save_alert(alert(status="RESOLVED", frame=None))
        code, body = self.svc.clear_alerts(scope="closed")
        self.assertEqual(body["alerts_deleted"], 1)
        self.assertEqual(body["frames_deleted"], 0)

    def test_already_missing_file_is_not_a_failure(self):
        self.store.save_alert(
            alert(status="RESOLVED", frame="/bookmarks/never_existed.jpg"))
        code, body = self.svc.clear_alerts(scope="closed")
        self.assertEqual(body["frames_deleted"], 0)
        self.assertEqual(body["frames_failed"], 0)

    def test_path_traversal_ref_cannot_delete_outside_bookmarks(self):
        # A frame ref is data from the database; it must never be able to point
        # the delete at an arbitrary file.
        victim = tempfile.NamedTemporaryFile(delete=False)
        victim.write(b"important")
        victim.close()
        self.addCleanup(lambda: os.path.exists(victim.name) and os.remove(victim.name))

        rel = "../../../.." + victim.name
        self.store.save_alert(alert(status="RESOLVED", frame=rel))
        self.svc.clear_alerts(scope="closed")
        self.assertTrue(os.path.exists(victim.name),
                        "traversal ref deleted a file outside evidence/bookmarks")

    def test_frames_kept_when_delete_frames_false(self):
        ref = self._frame("test_keep.jpg")
        path = os.path.join(BOOKMARKS, "test_keep.jpg")
        self.store.save_alert(alert(status="RESOLVED", frame=ref))
        self.svc.clear_alerts(scope="closed", delete_frames=False)
        self.assertTrue(os.path.exists(path))


class TestSnapshotPolicy(unittest.TestCase):
    def test_only_critical_density_and_restricted_intrusion(self):
        # The whole point of the change: loitering fires continuously, and on a
        # looping clip it wrote 7,741 frames / 944 MB, burying the snapshots
        # that actually matter.
        from services.inference.run_cpu import SNAPSHOT_RULES
        self.assertEqual(SNAPSHOT_RULES, {"R-02", "R-06"})
        self.assertNotIn("R-05", SNAPSHOT_RULES)   # loitering
        self.assertNotIn("R-01", SNAPSHOT_RULES)   # density warning (amber)
        self.assertNotIn("R-03", SNAPSHOT_RULES)   # capacity
        self.assertNotIn("R-07", SNAPSHOT_RULES)   # camera offline


if __name__ == "__main__":
    unittest.main()
