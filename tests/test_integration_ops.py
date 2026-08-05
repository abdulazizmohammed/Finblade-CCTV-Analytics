"""Operational contract: health endpoints and forwarder durability.

A FinBlade integration has to distinguish "CCTV analytics is down" from "one
camera dropped" from "the push to FinBlade is failing" — three problems with
three different owners. A single up/down flag makes them the same event.
"""

import os
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from services.api.forwarder import SCHEMA_VERSION, FinBladeForwarder   # noqa: E402
from services.api.store import InMemoryStore                           # noqa: E402

try:
    from fastapi.testclient import TestClient
    from services.api.app import app
    HAVE_APP = True
except Exception:                                  # noqa: BLE001
    HAVE_APP = False

FULL = {"Authorization": "Bearer full-key"}


class CursorStore(InMemoryStore):
    """InMemoryStore plus cursor durability, standing in for SQLite."""

    def __init__(self):
        super().__init__()
        self.saved = {}

    def load_cursors(self):
        return dict(self.saved)

    def save_cursors(self, cursors):
        self.saved.update(cursors)


def _ok_post(sent):
    def post(path, payload):
        sent.append((path, payload))
        return {"accepted": True}
    return post


class TestForwarderDurability(unittest.TestCase):
    def setUp(self):
        self.store = CursorStore()
        self.sent = []
        self.now = time.time()

    def event(self, eid, ts):
        self.store.save_event({"event_id": eid, "event_type": "ZONE_ENTRY",
                               "camera_id": "C1", "site_id": "SITE-1",
                               "ts": ts, "timestamp": ts})

    def test_cursor_is_persisted_after_a_successful_send(self):
        fw = FinBladeForwarder(self.store, base_url="http://fb.test",
                               post=_ok_post(self.sent))
        self.event("e1", self.now)
        fw.tick()
        self.assertIn("events", self.store.saved)
        self.assertGreaterEqual(self.store.saved["events"], self.now)

    def test_a_restart_resumes_instead_of_skipping_the_backlog(self):
        """The bug this closes: the cursor lived only in the process, so an API
        restart during a FinBlade outage skipped everything written meanwhile."""
        failing = FinBladeForwarder(
            self.store, base_url="http://fb.test",
            post=lambda p, b: (_ for _ in ()).throw(RuntimeError("FinBlade down")))
        self.event("e1", self.now)
        failing.tick()
        self.assertEqual({}, self.store.saved, "nothing sent, nothing to persist")

        # Process restarts while FinBlade is still down, then it recovers.
        restarted = FinBladeForwarder(self.store, base_url="http://fb.test",
                                      post=_ok_post(self.sent))
        restarted.tick()
        forwarded = [e["event_id"] for _p, body in self.sent
                     for e in body.get("batch", [])]
        self.assertIn("e1", forwarded,
                      "the event written before the restart must still be sent")

    def test_a_first_run_does_not_backfill_the_whole_database(self):
        """Still true: an empty cursor store starts from now."""
        self.event("old", self.now - 86400)
        fw = FinBladeForwarder(self.store, base_url="http://fb.test",
                               post=_ok_post(self.sent))
        fw.tick()
        self.assertEqual([], self.sent, "history before startup is not backfilled")

    def test_a_store_that_cannot_persist_still_forwards(self):
        """Durability is an enhancement; losing it must not stop the stream."""
        class Broken(InMemoryStore):
            def load_cursors(self):
                raise RuntimeError("no table")

            def save_cursors(self, cursors):
                raise RuntimeError("read only")

        store = Broken()
        store.save_event({"event_id": "e1", "event_type": "ZONE_ENTRY",
                          "camera_id": "C1", "ts": self.now, "timestamp": self.now})
        fw = FinBladeForwarder(store, base_url="http://fb.test",
                               post=_ok_post(self.sent))
        fw.tick()
        self.assertTrue(self.sent, "forwarding must survive a cursor-store fault")


class TestOutboundEnvelope(unittest.TestCase):
    def test_every_payload_is_stamped_with_version_source_and_site(self):
        sent = []
        store = CursorStore()
        store.save_event({"event_id": "e1", "event_type": "ZONE_ENTRY",
                          "camera_id": "C1", "ts": time.time(),
                          "timestamp": time.time()})
        fw = FinBladeForwarder(store, base_url="http://fb.test",
                               post=_ok_post(sent), site_id="SITE-9")
        fw.tick()
        self.assertTrue(sent)
        for _path, body in sent:
            self.assertEqual(SCHEMA_VERSION, body["schema_version"])
            self.assertEqual("cctv", body["source"])
            self.assertEqual("SITE-9", body["site_id"])

    def test_no_site_configured_means_no_site_key_rather_than_null(self):
        sent = []
        store = CursorStore()
        store.save_event({"event_id": "e1", "event_type": "ZONE_ENTRY",
                          "camera_id": "C1", "ts": time.time(),
                          "timestamp": time.time()})
        FinBladeForwarder(store, base_url="http://fb.test",
                          post=_ok_post(sent)).tick()
        self.assertNotIn("site_id", sent[0][1])


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestHealthEndpoints(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.client = TestClient(app)

    def test_healthz_is_open_so_a_load_balancer_can_reach_it(self):
        r = self.client.get("/healthz")
        self.assertEqual(200, r.status_code)
        self.assertEqual("ok", r.json()["status"])

    def test_readyz_is_open_and_terse(self):
        r = self.client.get("/readyz")
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.json()["ready"])
        self.assertEqual({"ready", "store", "ts"}, set(r.json()))

    def test_open_health_probes_leak_no_operational_detail(self):
        """They are unauthenticated, so they must not name cameras, counts or
        failure modes — that is what /api/v1/health is for."""
        body = self.client.get("/healthz").text + self.client.get("/readyz").text
        for leak in ("camera", "forwarder", "error", "site"):
            self.assertNotIn(leak, body.lower())

    def test_detailed_health_requires_a_key(self):
        self.assertEqual(401, self.client.get("/api/v1/health").status_code)

    def test_detailed_health_separates_the_failure_modes(self):
        body = self.client.get("/api/v1/health", headers=FULL).json()
        self.assertIn("healthy", body)
        for component in ("store", "cameras", "forwarder",
                          "report_scheduler", "offline_monitor"):
            self.assertIn(component, body["checks"], component)
        self.assertIn("ok", body["checks"]["store"])

    def test_a_disabled_forwarder_is_not_reported_as_unhealthy(self):
        """Most deployments do not push. 'Off' is not 'broken'."""
        body = self.client.get("/api/v1/health", headers=FULL).json()
        self.assertFalse(body["checks"]["forwarder"]["enabled"])
        self.assertTrue(body["checks"]["forwarder"]["ok"])


if __name__ == "__main__":
    unittest.main()
