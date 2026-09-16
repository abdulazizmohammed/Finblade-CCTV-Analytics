"""Outbound webhooks: subscriptions, signing, matching, the durable queue,
retries, and the hooks on the alert path.

  * finblade/webhooks.py      pure: validation, matching, envelope, HMAC, backoff
  * store contract            both backends keep the queue the same way
  * WebhookDispatcher         fake receiver: 2xx, 4xx, 5xx, connection errors
  * IngestService             raise/ack/resolve/clear and geofence crossings fan out
  * HTTP routes               secret shown once, test-fire, deliveries, retry
"""

import json
import os
import sys
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade import gps
from finblade import webhooks as W
from services.api.service import IngestService
from services.api.store import InMemoryStore
from services.api.webhooks import WebhookDispatcher
from tests import pgfixture

T0 = 1_800_000_000.0
RUH = (24.7136, 46.6753)
ORG = {"tenant": {"name": "Wareed Medical Laboratories", "short": "Wareed"},
       "regions": [{"region_id": "CENTRAL", "name": "Central", "cities": [{"city_id": "RUH", "name": "Riyadh", "branches": [
           {"branch_id": "RUH-01", "name": "Riyadh Main Lab", "lat": RUH[0], "lon": RUH[1]}]}]},
           {"region_id": "WESTERN", "name": "Western", "cities": [{"city_id": "JED", "name": "Jeddah", "branches": [
               {"branch_id": "JED-01", "name": "Jeddah Main Lab"}]}]}]}


def alert(**kw):
    a = {"alert_id": "al-1", "rule_id": "R-02", "severity": "RED", "message": "density critical",
         "zone_id": "LOBBY", "camera_id": "CAM-1", "site_id": "RUH-01", "ts": T0, "kind": "FIRE", "status": "OPEN"}
    a.update(kw)
    return a


# ---------------------------------------------------------------- pure -----
class TestSubscription(unittest.TestCase):
    def test_defaults_are_critical_alerts_only_with_a_generated_secret(self):
        row, errs = W.validate_subscription({"url": "https://ai.finblade.example/hooks/cctv"})
        self.assertEqual([], errs)
        self.assertEqual(["alert.raised"], row["events"])
        self.assertEqual(["RED", "CRITICAL"], row["severities"])
        self.assertTrue(row["webhook_id"].startswith("wh-"))
        self.assertGreaterEqual(len(row["secret"]), 32)
        self.assertTrue(row["enabled"])

    def test_validation(self):
        _, errs = W.validate_subscription({"url": "ftp://x", "events": ["alert.raised", "nope"], "secret": "short"})
        self.assertTrue(any("url" in e for e in errs))
        self.assertTrue(any("unknown events" in e for e in errs))
        self.assertTrue(any("secret" in e for e in errs))
        row, errs = W.validate_subscription({"url": "http://h/x", "events": ["alert.raised", "alert.resolved"],
                                             "severities": [], "rule_ids": ["r-06"], "branch_id": "RUH-01",
                                             "headers": {"X-Api-Key": "k"}})
        self.assertEqual([], errs)
        self.assertEqual([], row["severities"], "explicit empty = all severities")
        self.assertEqual(["R-06"], row["rule_ids"])
        self.assertEqual({"X-Api-Key": "k"}, row["headers"])

    def test_public_view_masks_the_secret(self):
        row, _ = W.validate_subscription({"url": "http://h/x", "secret": "0123456789abcdefXYZ9"})
        self.assertEqual("•••XYZ9", W.public_view(row)["secret"])


class TestMatching(unittest.TestCase):
    def sub(self, **kw):
        base = {"url": "http://h/x"}; base.update(kw)
        row, errs = W.validate_subscription(base)
        assert not errs, errs
        return row

    def test_severity_and_rule_filters_apply_to_the_raise_only(self):
        s = self.sub(events=["alert.raised", "alert.cleared", "alert.resolved"], rule_ids=["R-02"])
        self.assertTrue(W.matches(s, "alert.raised", alert=alert()))
        self.assertFalse(W.matches(s, "alert.raised", alert=alert(severity="AMBER")))
        self.assertFalse(W.matches(s, "alert.raised", alert=alert(rule_id="R-01")))
        # a CLEAR is INFO by construction and must still get through
        self.assertTrue(W.matches(s, "alert.cleared", alert=alert(severity="INFO", kind="CLEAR")))
        self.assertTrue(W.matches(s, "alert.resolved", alert=alert(severity="RED", status="RESOLVED")))
        self.assertFalse(W.matches(s, "alert.cleared", alert=alert(rule_id="R-01", severity="INFO")))
        self.assertFalse(W.matches(s, "alert.acknowledged", alert=alert()), "not subscribed")

    def test_scope_and_disabled(self):
        s = self.sub(events=["alert.raised"])
        self.assertTrue(W.matches(s, "alert.raised", alert=alert(), scope_sites={"RUH-01"}, site_id="RUH-01"))
        self.assertFalse(W.matches(s, "alert.raised", alert=alert(), scope_sites={"JED-01"}, site_id="RUH-01"))
        self.assertFalse(W.matches(s, "alert.raised", alert=alert(), scope_sites={"RUH-01"}, site_id=None),
                         "a scoped subscription never gets an unplaced camera's alert")
        self.assertTrue(W.matches(s, "alert.raised", alert=alert(), scope_sites=None, site_id=None))
        s["enabled"] = False
        self.assertFalse(W.matches(s, "alert.raised", alert=alert()))


class TestSigning(unittest.TestCase):
    def test_sign_and_verify_agree_and_reject_tampering_and_replay(self):
        body = W.serialize(W.envelope("alert.raised", alert=alert(), now=T0))
        header, ts = W.sign("s3cret-s3cret-s3cret", body, ts=1_800_000_100)
        self.assertTrue(header.startswith("t=1800000100,v1="))
        self.assertTrue(W.verify("s3cret-s3cret-s3cret", body, header, now=1_800_000_130))
        self.assertFalse(W.verify("wrong-secret-wrong-s", body, header, now=1_800_000_130))
        self.assertFalse(W.verify("s3cret-s3cret-s3cret", body + " ", header, now=1_800_000_130))
        self.assertFalse(W.verify("s3cret-s3cret-s3cret", body, header, now=1_800_000_100 + 3600), "replay window")
        self.assertFalse(W.verify("s3cret-s3cret-s3cret", body, "garbage"))

    def test_serialize_is_canonical(self):
        a = W.serialize({"b": 1, "a": [1, 2]}); b = W.serialize({"a": [1, 2], "b": 1})
        self.assertEqual(a, b)

    def test_headers_cannot_be_overridden_by_the_subscriber(self):
        h = W.headers_for("alert.raised", "dl-1", "t=1,v1=x", 1, {"X-Api-Key": "k", "x-finblade-signature": "forged"})
        self.assertEqual("t=1,v1=x", h["X-FinBlade-Signature"])
        self.assertEqual("k", h["X-Api-Key"])

    def test_envelope_carries_links_and_context(self):
        e = W.envelope("alert.raised", alert=alert(frame="f.jpg"), context={"branch": {"name": "Riyadh Main Lab"}},
                       tenant={"name": "Wareed"}, base_url="https://cctv.example", now=T0)
        self.assertEqual("1.0", e["schema_version"])
        self.assertEqual("https://cctv.example/api/v1/alerts/al-1/ack", e["links"]["acknowledge"])
        self.assertEqual("https://cctv.example/api/v1/incidents/al-1/frame", e["links"]["frame"])
        self.assertIsNone(W.envelope("alert.raised", alert=alert(), now=T0)["links"]["frame"])
        self.assertEqual("Riyadh Main Lab", e["context"]["branch"]["name"])

    def test_backoff_schedule_gives_up(self):
        self.assertEqual(T0 + 5, W.next_attempt(1, T0))
        self.assertEqual(T0 + 30, W.next_attempt(2, T0))
        self.assertEqual(T0 + 3600, W.next_attempt(7, T0))
        self.assertIsNone(W.next_attempt(W.MAX_ATTEMPTS, T0))


# ------------------------------------------------------------ store contract
class QueueContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        row, _ = W.validate_subscription({"webhook_id": "wh-a", "url": "http://h/a", "secret": "0123456789abcdef0123"})
        self.store.save_webhook(row)

    def test_subscription_round_trip_json_fields_and_delete_cascades(self):
        w = self.store.list_webhooks()[0]
        self.assertEqual(["alert.raised"], w["events"])
        self.assertEqual(["RED", "CRITICAL"], w["severities"])
        self.assertEqual({}, w["headers"])
        self.assertTrue(w["enabled"])
        self.store.enqueue_delivery({"delivery_id": "dl-1", "webhook_id": "wh-a", "event": "alert.raised",
                                     "alert_id": "al-1", "payload": "{}", "next_attempt_at": T0, "created_at": T0})
        self.assertTrue(self.store.delete_webhook("wh-a"))
        self.assertEqual([], self.store.list_deliveries())
        self.assertFalse(self.store.delete_webhook("wh-a"))

    def test_due_ordering_and_update(self):
        for i, at in enumerate((T0 + 50, T0 + 10, T0 + 999)):
            self.store.enqueue_delivery({"delivery_id": f"dl-{i}", "webhook_id": "wh-a", "event": "alert.raised",
                                         "alert_id": None, "payload": "{}", "next_attempt_at": at, "created_at": T0 + i})
        self.assertEqual(["dl-1", "dl-0"], [d["delivery_id"] for d in self.store.due_deliveries(T0 + 60)])
        self.store.update_delivery("dl-1", status="SENT", attempts=1, sent_at=T0 + 61, response_code=200)
        self.assertEqual(["dl-0"], [d["delivery_id"] for d in self.store.due_deliveries(T0 + 60)])
        d = self.store.get_delivery("dl-1")
        self.assertEqual(("SENT", 1, 200), (d["status"], int(d["attempts"]), int(d["response_code"])))
        w = self.store.list_webhooks()[0]
        self.assertEqual("SENT", w["last_status"])
        self.assertEqual(["dl-2", "dl-1", "dl-0"], [d["delivery_id"] for d in self.store.list_deliveries("wh-a")])
        self.assertIsNone(self.store.get_delivery("nope"))


class TestInMemoryQueue(QueueContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


@pgfixture.skip_without_pg
class TestPostgresQueue(QueueContract, unittest.TestCase):
    def make_store(self):
        store, teardown = pgfixture.make_store("wh")
        self.addCleanup(teardown)
        return store


# --------------------------------------------------------------- dispatcher
class FakeReceiver:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        r = self.responses.pop(0) if self.responses else (200, "ok")
        if isinstance(r, Exception):
            raise r
        return r


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.rx = FakeReceiver()
        self.d = WebhookDispatcher(self.store, post=self.rx, base_url="https://cctv.example")
        row, _ = W.validate_subscription({"webhook_id": "wh-a", "url": "https://ai.example/h", "secret": "0123456789abcdef0123"})
        self.store.save_webhook(row)

    def test_notify_writes_a_row_and_tick_sends_a_signed_body(self):
        self.assertEqual(1, self.d.notify("alert.raised", alert=alert(), now=T0))
        self.assertEqual([], self.rx.calls, "the alert path never touches the network")
        self.assertEqual({"SENT": 1, "FAILED": 0, "PENDING": 0}, self.d.tick(now=T0 + 1))
        url, body, headers = self.rx.calls[0]
        self.assertEqual("https://ai.example/h", url)
        self.assertEqual("alert.raised", headers["X-FinBlade-Event"])
        self.assertTrue(W.verify("0123456789abcdef0123", body, headers["X-FinBlade-Signature"], now=T0 + 1))
        p = json.loads(body)
        self.assertEqual("al-1", p["alert"]["alert_id"])
        self.assertEqual("https://cctv.example/api/v1/alerts/al-1/ack", p["links"]["acknowledge"])
        self.assertEqual(headers["X-FinBlade-Delivery"], p["delivery_id"])

    def test_filters_and_scope_at_enqueue(self):
        self.assertEqual(0, self.d.notify("alert.raised", alert=alert(severity="AMBER"), now=T0))
        self.assertEqual(0, self.d.notify("alert.resolved", alert=alert(), now=T0), "not subscribed")
        scoped = WebhookDispatcher(self.store, post=self.rx, scope_resolver=lambda **kw: {"JED-01"})
        row, _ = W.validate_subscription({"webhook_id": "wh-jed", "url": "https://x/j", "branch_id": "JED-01",
                                          "secret": "0123456789abcdef0123"})
        self.store.save_webhook(row)
        n = scoped.notify("alert.raised", alert=alert(site_id="RUH-01"), now=T0)
        self.assertEqual(1, n, "only the unscoped one")
        n = scoped.notify("alert.raised", alert=alert(site_id="JED-01"), now=T0)
        self.assertEqual(2, n)

    def test_4xx_is_permanent_5xx_retries_and_gives_up(self):
        self.rx.responses = [(400, "bad")]
        self.d.notify("alert.raised", alert=alert(), now=T0)
        self.assertEqual({"SENT": 0, "FAILED": 1, "PENDING": 0}, self.d.tick(now=T0))
        d = self.store.list_deliveries()[0]
        self.assertEqual(("FAILED", 1, 400), (d["status"], d["attempts"], d["response_code"]))
        self.assertIn("bad", d["last_error"])

        self.rx.responses = [(503, "down")] * 3 + [ConnectionError("refused")] + [(500, "x")] * 10
        self.d.notify("alert.raised", alert=alert(alert_id="al-2"), now=T0)
        t = T0
        for i in range(W.MAX_ATTEMPTS - 1):
            self.assertEqual({"SENT": 0, "FAILED": 0, "PENDING": 1}, self.d.tick(now=t))
            d = next(x for x in self.store.list_deliveries() if x["alert_id"] == "al-2")
            self.assertEqual(i + 1, d["attempts"])
            if i == 3:
                self.assertIn("refused", d["last_error"], "a connection error is recorded like an HTTP one")
            self.assertEqual({"SENT": 0, "FAILED": 0, "PENDING": 0}, self.d.tick(now=t + 1), "not due yet")
            t = d["next_attempt_at"]
        self.assertEqual({"SENT": 0, "FAILED": 1, "PENDING": 0}, self.d.tick(now=t))
        d = next(x for x in self.store.list_deliveries() if x["alert_id"] == "al-2")
        self.assertEqual(W.MAX_ATTEMPTS, d["attempts"])

    def test_retry_and_test_fire_and_payload_is_frozen(self):
        self.rx.responses = [(400, "bad"), (200, "ok")]
        self.d.notify("alert.raised", alert=alert(), now=T0)
        self.d.tick(now=T0)
        did = self.store.list_deliveries()[0]["delivery_id"]
        first_body = self.rx.calls[0][1]
        self.assertTrue(self.d.retry(did, now=T0 + 5))
        self.assertEqual({"SENT": 1, "FAILED": 0, "PENDING": 0}, self.d.tick(now=T0 + 5))
        self.assertEqual(first_body, self.rx.calls[1][1], "a retry sends the same bytes")
        self.assertFalse(self.d.retry("nope"))
        q = self.d.test_fire("wh-a", now=T0 + 9)
        self.assertTrue(q["delivery_id"].startswith("dl-"))
        self.d.tick(now=T0 + 9)
        p = json.loads(self.rx.calls[-1][1])
        self.assertTrue(p["alert"]["test"]); self.assertEqual("RED", p["alert"]["severity"])
        self.assertIsNone(self.d.test_fire("nope"))

    def test_disabled_subscription_fails_its_queue_instead_of_sending(self):
        self.d.notify("alert.raised", alert=alert(), now=T0)
        row, _ = W.validate_subscription({"webhook_id": "wh-a", "url": "https://ai.example/h", "enabled": False,
                                          "secret": "0123456789abcdef0123"})
        self.store.save_webhook(row)
        self.assertEqual({"SENT": 0, "FAILED": 1, "PENDING": 0}, self.d.tick(now=T0 + 1))
        self.assertEqual([], self.rx.calls)


# ------------------------------------------------------------------ service
class TestServiceHooks(unittest.TestCase):
    def setUp(self):
        self.svc = IngestService(InMemoryStore())
        self.svc.import_org(ORG)
        self.rx = FakeReceiver()
        self.svc.webhooks.post = self.rx
        self.svc.upsert_camera({"camera_id": "CAM-1", "site_id": "RUH-01", "name": "Lobby cam",
                                "source": "rtsp://admin:hunter2@10.0.0.1/s"})
        code, body = self.svc.save_webhook({"webhook_id": "wh-all", "url": "https://ai.example/h",
                                            "events": list(W.EVENTS), "severities": []})
        self.assertEqual(200, code, body)
        self.secret = body["webhook"]["secret"]

    def events_sent(self):
        self.svc.webhooks.tick()
        return [json.loads(b)["event"] for _, b, _ in self.rx.calls]

    def test_raise_ack_resolve_and_clear_fan_out_with_context(self):
        aid = self.svc.raise_alert({"rule_id": "R-02", "severity": "RED", "message": "m", "zone_id": "LOBBY",
                                    "camera_id": "CAM-1", "ts": T0, "kind": "FIRE"})
        self.svc.acknowledge(aid, "ops", T0 + 1)
        self.svc.resolve(aid, "RESOLVED", "ops", T0 + 2, note="handled")
        self.svc.raise_alert({"rule_id": "R-07", "severity": "INFO", "message": "camera CAM-1 recovered",
                              "camera_id": "CAM-1", "ts": T0 + 3, "kind": "CLEAR"})
        self.assertEqual(["alert.raised", "alert.acknowledged", "alert.resolved", "alert.cleared"], self.events_sent())
        first = json.loads(self.rx.calls[0][1])
        self.assertEqual("RUH-01", first["alert"]["site_id"], "site derived from the camera")
        self.assertEqual("Riyadh Main Lab", first["context"]["branch"]["name"])
        self.assertEqual("Central", first["context"]["branch"]["region"])
        self.assertEqual("Lobby cam", first["context"]["camera"]["name"])
        self.assertEqual({"name": "Wareed Medical Laboratories", "short": "Wareed"}, first["tenant"])
        third = json.loads(self.rx.calls[2][1])
        self.assertEqual(("RESOLVED", "handled"), (third["alert"]["status"], third["alert"]["note"]))
        self.assertNotIn("hunter2", "".join(b for _, b, _ in self.rx.calls), "no RTSP credential in any envelope")

    def test_geofence_crossings_fan_out(self):
        self.svc.register_tracker({"tracker_id": "VAN-1", "home_branch_id": "RUH-01"})
        for i in range(2):
            self.svc.ingest_position(gps.Position("VAN-1", T0 + i * 10, RUH[0], RUH[1]))
        for i in range(2):
            self.svc.ingest_position(gps.Position("VAN-1", T0 + 600 + i * 10, RUH[0] + 0.01, RUH[1]))
        self.assertEqual(["tracker.arrived", "tracker.departed"], self.events_sent())
        dep = json.loads(self.rx.calls[1][1])
        self.assertEqual("RUH-01", dep["tracker_event"]["branch_id"])
        self.assertEqual("Riyadh Main Lab", dep["context"]["branch"]["name"])
        self.assertIn("map.html?vehicle=VAN-1", dep["links"]["map"])

    def test_a_broken_subscription_row_never_blocks_the_alert(self):
        self.svc.store._webhooks["wh-bad"] = {"webhook_id": "wh-bad", "url": "http://x", "enabled": True,
                                              "events": None, "secret": None}
        aid = self.svc.raise_alert({"rule_id": "R-02", "severity": "RED", "message": "m",
                                    "camera_id": "CAM-1", "ts": T0, "kind": "FIRE"})
        self.assertTrue(aid)

    def test_scope_validation_and_secret_kept_on_edit(self):
        self.assertEqual(422, self.svc.save_webhook({"url": "http://x/y", "region_id": "NOPE"})[0])
        code, body = self.svc.save_webhook({"url": "http://x/z"}, existing_id="wh-all")
        self.assertEqual(200, code)
        self.assertEqual("•••" + self.secret[-4:], body["webhook"]["secret"])
        self.assertEqual(self.secret, self.svc.store.list_webhooks()[0]["secret"])


# --------------------------------------------------------------- HTTP routes
try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc as app_svc
    HAVE_APP = True
except Exception:                              # noqa: BLE001
    HAVE_APP = False


@unittest.skipUnless(HAVE_APP, "fastapi app not importable")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        for w in list(app_svc.store.list_webhooks()):
            app_svc.store.delete_webhook(w["webhook_id"])
        self.rx = FakeReceiver()
        app_svc.webhooks.post = self.rx

    def test_create_shows_secret_once_list_masks_test_fire_and_deliveries(self):
        r = self.c.post("/api/v1/webhooks", json={"name": "FinBlade AI", "url": "https://ai.example/cctv"})
        self.assertEqual(200, r.status_code, r.text)
        w = r.json()["webhook"]
        self.assertGreaterEqual(len(w["secret"]), 32)
        listed = self.c.get("/api/v1/webhooks").json()
        self.assertTrue(listed["webhooks"][0]["secret"].startswith("•••"))
        self.assertEqual(1, listed["dispatcher"]["subscriptions"])
        r = self.c.post(f"/api/v1/webhooks/{w['webhook_id']}/test")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("SENT", r.json()["status"])
        self.assertTrue(W.verify(w["secret"], self.rx.calls[0][1], self.rx.calls[0][2]["X-FinBlade-Signature"]))
        ds = self.c.get(f"/api/v1/webhooks/deliveries?webhook_id={w['webhook_id']}").json()["deliveries"]
        self.assertEqual("SENT", ds[0]["status"])
        self.assertEqual("R-02", ds[0]["summary"]["rule_id"])
        self.assertNotIn("payload", ds[0])
        one = self.c.get(f"/api/v1/webhooks/deliveries/{ds[0]['delivery_id']}").json()
        self.assertEqual("alert.raised", one["payload"]["event"])
        self.assertEqual(422, self.c.put(f"/api/v1/webhooks/{w['webhook_id']}", json={"url": "nope"}).status_code)
        self.assertEqual(200, self.c.delete(f"/api/v1/webhooks/{w['webhook_id']}").status_code)
        self.assertEqual(404, self.c.delete(f"/api/v1/webhooks/{w['webhook_id']}").status_code)

    def test_retry_route(self):
        self.rx.responses = [(500, "down"), (200, "ok")]
        w = self.c.post("/api/v1/webhooks", json={"url": "https://ai.example/cctv"}).json()["webhook"]
        r = self.c.post(f"/api/v1/webhooks/{w['webhook_id']}/test").json()
        self.assertEqual("PENDING", r["status"])
        r = self.c.post(f"/api/v1/webhooks/deliveries/{r['delivery_id']}/retry").json()
        self.assertEqual("SENT", r["status"])
        self.assertEqual(404, self.c.post("/api/v1/webhooks/deliveries/nope/retry").status_code)


if __name__ == "__main__":
    unittest.main()
