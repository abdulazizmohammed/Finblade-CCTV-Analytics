import time
import unittest

from services.api.forwarder import FinBladeForwarder
from services.api.store import InMemoryStore


class FakePoster:
    """Records posts. Can be told to fail, to test the outage path."""

    def __init__(self, response=None):
        self.calls = []
        self.fail = False
        self.response = response or {}

    def __call__(self, path, payload):
        if self.fail:
            raise RuntimeError("finblade unreachable")
        self.calls.append((path, payload))
        return self.response

    def paths(self):
        return [p for p, _ in self.calls]

    def batch_for(self, path):
        for p, payload in self.calls:
            if p.endswith(path):
                return payload.get("batch", [])
        return []


def event(eid, ts, etype="ZONE_ENTRY"):
    return {"event_id": eid, "event_type": etype, "camera_id": "CAM-A",
            "site_id": "S", "timestamp": ts, "zone_to": "Z1",
            "person_ref": "pr_0123456789abcdef", "confidence": 0.9}


def alert(ts, rule="R-06"):
    return {"rule_id": rule, "severity": "RED", "message": "m",
            "camera_id": "CAM-A", "zone_id": "Z1", "ts": ts, "kind": "FIRE"}


class TestDisabledByDefault(unittest.TestCase):
    def test_no_url_means_no_traffic(self):
        poster = FakePoster()
        f = FinBladeForwarder(InMemoryStore(), post=poster)
        self.assertFalse(f.enabled)
        f.tick()
        self.assertEqual(poster.calls, [])

    def test_status_reports_disabled(self):
        f = FinBladeForwarder(InMemoryStore())
        self.assertFalse(f.status()["enabled"])
        self.assertIsNone(f.status()["target"])


class TestForwarding(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.poster = FakePoster()
        self.f = FinBladeForwarder(self.store, base_url="http://fb.test",
                                   post=self.poster)

    def test_events_are_forwarded(self):
        self.store.save_event(event("e1", time.time()))
        self.f.tick()
        self.assertIn("/api/v1/cctv/events", self.poster.paths())
        self.assertEqual(len(self.poster.batch_for("events")), 1)
        self.assertEqual(self.f.stats["events"], 1)

    def test_alerts_are_forwarded(self):
        self.store.save_alert(alert(time.time()))
        self.f.tick()
        self.assertIn("/api/v1/cctv/alerts", self.poster.paths())

    def test_zone_state_and_health_are_forwarded(self):
        self.store.save_zone_state({
            "zone_id": "Z1", "camera_id": "CAM-A", "ts": time.time(),
            "occupancy": 3, "density": 0.1, "capacity_pct": 5.0,
            "inflow_per_min": 1, "outflow_per_min": 0, "status": "NORMAL"})
        self.store.mark_camera_seen("CAM-A", time.time(), "S")
        self.f.tick()
        self.assertIn("/api/v1/cctv/zone-state", self.poster.paths())
        self.assertIn("/api/v1/cctv/camera-health", self.poster.paths())

    def test_nothing_new_sends_no_batch(self):
        self.f.tick()
        self.assertNotIn("/api/v1/cctv/events", self.poster.paths())

    def test_history_before_startup_is_not_backfilled(self):
        # Starting the forwarder must not dump the site's entire history into
        # FinBlade on first connect.
        self.store.save_event(event("old", time.time() - 86400))
        f = FinBladeForwarder(self.store, base_url="http://fb.test",
                              post=self.poster)
        f.tick()
        self.assertNotIn("/api/v1/cctv/events", self.poster.paths())


class TestOutageBehaviour(unittest.TestCase):
    """The store is the queue: a failed post must not lose data."""

    def setUp(self):
        self.store = InMemoryStore()
        self.poster = FakePoster()
        self.f = FinBladeForwarder(self.store, base_url="http://fb.test",
                                   post=self.poster)

    def test_cursor_holds_while_finblade_is_down(self):
        self.store.save_event(event("e1", time.time()))
        before = self.f.cursors["events"]
        self.poster.fail = True
        self.f.tick()
        self.assertEqual(self.f.cursors["events"], before)
        self.assertGreater(self.f.stats["failures"], 0)

    def test_backlog_replays_when_it_returns(self):
        self.store.save_event(event("e1", time.time()))
        self.poster.fail = True
        self.f.tick()                       # lost nothing, just not sent
        self.poster.fail = False
        self.f.tick()
        self.assertEqual(len(self.poster.batch_for("events")), 1)
        self.assertEqual(self.f.stats["events"], 1)

    def test_an_outage_never_raises(self):
        self.store.save_event(event("e1", time.time()))   # something to send
        self.poster.fail = True
        self.f.tick()                       # must not propagate
        self.assertIsNotNone(self.f.status()["last_error"])

    def test_a_quiet_site_records_no_error(self):
        # Nothing to send means nothing was attempted, so there is no failure
        # to report. In production the people-counts stream posts every tick,
        # so an outage is still detected while the site is quiet.
        self.poster.fail = True
        self.f.tick()
        self.assertIsNone(self.f.status()["last_error"])

    def test_the_cursor_advances_past_sent_events(self):
        self.store.save_event(event("e1", time.time()))
        before = self.f.cursors["events"]
        self.f.tick()
        self.assertGreater(self.f.cursors["events"], before)

    def test_a_recent_event_may_be_resent_within_the_overlap(self):
        # Deliberate. The cursor re-sends a couple of seconds of already-sent
        # records so that two events sharing a timestamp cannot be silently
        # dropped. The receiver is required to deduplicate on event_id, so a
        # duplicate costs nothing while a gap would be unrecoverable.
        self.store.save_event(event("e1", time.time()))
        self.f.tick()
        self.poster.calls.clear()
        self.f.tick()
        self.assertEqual(len(self.poster.batch_for("events")), 1)


class TestOperatorAcks(unittest.TestCase):
    def test_acks_in_the_response_are_applied(self):
        applied = []
        poster = FakePoster(response={"pending_acks": [
            {"alert_id": "42", "action": "ACK", "by": "op@finblade"}]})
        store = InMemoryStore()
        f = FinBladeForwarder(store, base_url="http://fb.test", post=poster,
                              apply_ack=applied.append)
        store.save_event(event("e1", time.time()))
        f.tick()
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["alert_id"], "42")
        self.assertEqual(f.stats["acks_applied"], 1)

    def test_a_failing_ack_does_not_break_forwarding(self):
        def boom(_):
            raise RuntimeError("no such alert")

        poster = FakePoster(response={"pending_acks": [{"alert_id": "42"}]})
        store = InMemoryStore()
        f = FinBladeForwarder(store, base_url="http://fb.test", post=poster,
                              apply_ack=boom)
        store.save_event(event("e1", time.time()))
        f.tick()                             # must not raise
        self.assertEqual(f.stats["events"], 1)

    def test_malformed_acks_are_ignored(self):
        applied = []
        poster = FakePoster(response={"pending_acks": [{}, "nonsense", None]})
        store = InMemoryStore()
        f = FinBladeForwarder(store, base_url="http://fb.test", post=poster,
                              apply_ack=applied.append)
        store.save_event(event("e1", time.time()))
        f.tick()
        self.assertEqual(applied, [])


class TestSnapshots(unittest.TestCase):
    """Periodic JPEG thumbnails — explicitly NOT continuous video.

    Streaming live video would be 20-40 Mbit/s per viewer; one frame per camera
    every 30s is ~2 KB/s. Four orders of magnitude, and enough for a dashboard
    tile or incident context.
    """

    def setUp(self):
        self.store = InMemoryStore()
        self.poster = FakePoster()
        self.jpeg = b"\xff\xd8\xff" + b"x" * 500        # plausible JPEG bytes
        self.store.mark_camera_seen("CAM-A", time.time(), "S")
        self.store.record_camera_health(
            "CAM-A", {"state": "ONLINE", "resolution": [640, 360]},
            time.time(), "S")

    def _fwd(self, interval=30.0, fetch=None):
        return FinBladeForwarder(
            self.store, base_url="http://fb.test", post=self.poster,
            snapshot_interval=interval,
            fetch_snapshot=fetch if fetch is not None else (lambda cid: self.jpeg))

    def test_snapshot_is_sent_base64(self):
        import base64
        f = self._fwd()
        f.tick()
        sent = [p for path, p in self.poster.calls if path.endswith("/snapshots")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(base64.b64decode(sent[0]["image_base64"]), self.jpeg)
        self.assertEqual(sent[0]["format"], "jpeg")
        self.assertEqual(sent[0]["camera_id"], "CAM-A")

    def test_disabled_by_zero_interval(self):
        f = self._fwd(interval=0)
        f.tick()
        self.assertNotIn("/api/v1/cctv/snapshots", self.poster.paths())

    def test_rate_limited_independently_of_the_tick(self):
        # Telemetry ticks every 5s; images must not ride every tick.
        f = self._fwd(interval=3600)
        f.tick()
        f.tick()
        f.tick()
        sent = [p for p, _ in self.poster.calls if p.endswith("/snapshots")]
        self.assertEqual(len(sent), 1)

    def test_offline_cameras_are_skipped(self):
        # A snapshot from an offline camera is a stale frame from before it
        # dropped — worse than none, because it looks live.
        self.store.record_camera_health(
            "CAM-A", {"state": "OFFLINE"}, time.time() - 600, "S")
        f = self._fwd()
        f.tick()
        self.assertNotIn("/api/v1/cctv/snapshots", self.poster.paths())

    def test_a_camera_that_cannot_be_grabbed_is_skipped_quietly(self):
        def boom(cid):
            raise RuntimeError("camera busy")

        f = self._fwd(fetch=boom)
        f.tick()                                        # must not raise
        self.assertNotIn("/api/v1/cctv/snapshots", self.poster.paths())

    def test_bytes_are_accounted(self):
        f = self._fwd()
        f.tick()
        self.assertEqual(f.stats["snapshots"], 1)
        self.assertEqual(f.stats["snapshot_bytes"], len(self.jpeg))


class TestStatus(unittest.TestCase):
    def test_status_shape(self):
        store = InMemoryStore()
        poster = FakePoster()
        f = FinBladeForwarder(store, base_url="http://fb.test",
                              api_key="k", post=poster)
        store.save_event(event("e1", time.time()))
        f.tick()
        s = f.status()
        self.assertTrue(s["enabled"])
        self.assertTrue(s["authenticated"])
        self.assertEqual(s["target"], "http://fb.test")
        self.assertIsNotNone(s["last_success"])
        self.assertIsNone(s["last_error"])
        self.assertIn("events", s["cursors"])


if __name__ == "__main__":
    unittest.main()
