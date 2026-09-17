"""Webhook dispatcher: enqueue on the alert path, deliver on a loop.

Two halves, deliberately separate:

  notify(event, alert=…)   called by IngestService the moment an alert is
                           raised / acknowledged / resolved / cleared, or a
                           vehicle crosses a geofence. It evaluates every
                           subscription, builds the signed envelope, and
                           writes a DELIVERY ROW. No network. Microseconds.

  tick()                   called by a loop in app.py every few seconds. It
                           posts due deliveries, marks SENT, or schedules the
                           retry. A receiver that is down, slow or wrong can
                           only ever slow this loop — never the alert.

WHY THE ENVELOPE IS FROZEN AT ENQUEUE TIME. The payload a receiver gets on a
retry an hour later is byte-identical to the first attempt, and its signature
still verifies. Re-rendering at send time would make a retry describe a
different world (the alert may be resolved by then) under the same
delivery_id, which is exactly the ambiguity an idempotent receiver cannot
resolve.

4xx is permanent (the receiver rejected the payload; resending the same
bytes cannot fix it) — the delivery is FAILED at once with the response body
kept. 5xx and connection errors retry on the backoff schedule.
"""

import json
import logging
import os
import time
import uuid
from typing import Callable, Dict, List, Optional

from finblade import webhooks as W

log = logging.getLogger("finblade.webhooks")

DEFAULT_TIMEOUT_S = 8.0


def _http_post(url: str, body: str, headers: Dict[str, str], timeout: float = DEFAULT_TIMEOUT_S):
    """(status_code, response_text). Raises on connection failure."""
    import requests
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=timeout)
    return r.status_code, r.text[:500]


class WebhookDispatcher:
    def __init__(self, store, scope_resolver: Optional[Callable] = None,
                 context_provider: Optional[Callable] = None,
                 tenant_provider: Optional[Callable] = None,
                 post: Optional[Callable] = None, base_url: Optional[str] = None):
        # A store, or a zero-arg callable returning the current one — the
        # service passes the latter so a swapped store (tests, a reconnect)
        # cannot leave the dispatcher queueing into a store nobody reads.
        self._store = store
        # branches_in_scope(region_id, city_id, branch_id) -> set | None
        self.scope_resolver = scope_resolver or (lambda **kw: None)
        # (alert) -> {"branch": ..., "camera": ..., "zone": ...}
        self.context_provider = context_provider or (lambda a: {})
        self.tenant_provider = tenant_provider or (lambda: {})
        self.post = post or _http_post
        self.base_url = (base_url or os.environ.get("FINBLADE_PUBLIC_URL")
                         or os.environ.get("FINBLADE_SELF_URL") or "").rstrip("/")
        self.enqueued = 0
        self.sent = 0
        self.failed = 0
        self.last_error: Optional[str] = None

    @property
    def store(self):
        return self._store() if callable(self._store) else self._store

    # ---- enqueue -------------------------------------------------------------
    def notify(self, event: str, alert: Optional[dict] = None,
               tracker_event: Optional[dict] = None, now: Optional[float] = None) -> int:
        """Fan one event out to every matching subscription. Returns rows written."""
        now = time.time() if now is None else now
        subs = [s for s in self.store.list_webhooks() if s.get("enabled", True)]
        if not subs:
            return 0
        site_id = (alert or tracker_event or {}).get("site_id") or (tracker_event or {}).get("branch_id")
        context = None
        n = 0
        for sub in subs:
            scope = None
            if sub.get("region_id") or sub.get("city_id") or sub.get("branch_id"):
                scope = self.scope_resolver(region_id=sub.get("region_id"), city_id=sub.get("city_id"),
                                            branch_id=sub.get("branch_id"))
                if scope is None:
                    scope = set()
            if not W.matches(sub, event, alert=alert, scope_sites=scope, site_id=site_id):
                continue
            if context is None:
                try:
                    context = self.context_provider(alert or tracker_event or {})
                except Exception:                           # noqa: BLE001
                    log.exception("webhook context lookup failed")
                    context = {}
            body = W.envelope(event, alert=alert, tracker_event=tracker_event, context=context,
                              tenant=self.tenant_provider(), base_url=self.base_url, now=now)
            self.store.enqueue_delivery({
                "delivery_id": body["delivery_id"], "webhook_id": sub["webhook_id"],
                "event": event, "alert_id": (alert or {}).get("alert_id"),
                "payload": W.serialize(body), "status": "PENDING", "attempts": 0,
                "next_attempt_at": now, "created_at": now})
            n += 1
        self.enqueued += n
        return n

    # ---- deliver -------------------------------------------------------------
    def deliver(self, d: dict, now: Optional[float] = None) -> str:
        """Attempt one delivery. Returns the new status."""
        now = time.time() if now is None else now
        sub = next((s for s in self.store.list_webhooks() if s["webhook_id"] == d["webhook_id"]), None)
        attempts = int(d.get("attempts") or 0) + 1
        if sub is None or not sub.get("enabled", True):
            self.store.update_delivery(d["delivery_id"], status="FAILED", attempts=attempts,
                                       last_error="subscription removed or disabled")
            return "FAILED"
        sig, ts = W.sign(sub["secret"], d["payload"], int(now))
        headers = W.headers_for(d["event"], d["delivery_id"], sig, ts, sub.get("headers") or {})
        try:
            code, text = self.post(sub["url"], d["payload"], headers)
        except Exception as e:                              # noqa: BLE001
            code, text = None, f"{type(e).__name__}: {e}"[:300]
        if code is not None and 200 <= code < 300:
            self.store.update_delivery(d["delivery_id"], status="SENT", attempts=attempts,
                                       sent_at=now, response_code=code, last_error=None)
            self.sent += 1
            return "SENT"
        if code is not None and 400 <= code < 500:
            self.store.update_delivery(d["delivery_id"], status="FAILED", attempts=attempts,
                                       response_code=code, last_error=f"HTTP {code}: {text}"[:500])
            self.failed += 1
            self.last_error = f"{sub['webhook_id']}: HTTP {code}"
            return "FAILED"
        nxt = W.next_attempt(attempts, now)
        err = (f"HTTP {code}: {text}" if code is not None else text)[:500]
        if nxt is None:
            self.store.update_delivery(d["delivery_id"], status="FAILED", attempts=attempts,
                                       response_code=code, last_error=err)
            self.failed += 1
            self.last_error = f"{sub['webhook_id']}: gave up after {attempts} attempts"
            return "FAILED"
        self.store.update_delivery(d["delivery_id"], status="PENDING", attempts=attempts,
                                   next_attempt_at=nxt, response_code=code, last_error=err)
        self.last_error = f"{sub['webhook_id']}: {err[:120]} (retry in {int(nxt - now)}s)"
        return "PENDING"

    def tick(self, now: Optional[float] = None, limit: int = 50) -> Dict[str, int]:
        now = time.time() if now is None else now
        out = {"SENT": 0, "FAILED": 0, "PENDING": 0}
        for d in self.store.due_deliveries(now, limit=limit):
            try:
                out[self.deliver(d, now)] += 1
            except Exception:                               # noqa: BLE001
                log.exception("webhook delivery %s crashed", d.get("delivery_id"))
        return out

    def retry(self, delivery_id: str, now: Optional[float] = None) -> bool:
        """Put a FAILED delivery back in the queue for an immediate attempt."""
        now = time.time() if now is None else now
        d = self.store.get_delivery(delivery_id)
        if not d:
            return False
        self.store.update_delivery(delivery_id, status="PENDING", attempts=0,
                                   next_attempt_at=now, last_error=None)
        return True

    def test_fire(self, webhook_id: str, now: Optional[float] = None) -> Optional[dict]:
        """Queue a synthetic alert.raised so a receiver can be checked end to end."""
        now = time.time() if now is None else now
        sub = next((s for s in self.store.list_webhooks() if s["webhook_id"] == webhook_id), None)
        if not sub:
            return None
        alert = {"alert_id": "test-" + uuid.uuid4().hex[:8], "rule_id": "R-02", "severity": "RED",
                 "message": "TEST — density critical in Reception (synthetic, sent from the "
                            "Webhooks page)", "zone_id": "TEST-ZONE", "camera_id": "TEST-CAM",
                 "site_id": sub.get("branch_id"), "ts": now, "kind": "FIRE", "status": "OPEN",
                 "test": True}
        body = W.envelope("alert.raised", alert=alert, context={"test": True},
                          tenant=self.tenant_provider(), base_url=self.base_url, now=now)
        self.store.enqueue_delivery({
            "delivery_id": body["delivery_id"], "webhook_id": webhook_id, "event": "alert.raised",
            "alert_id": alert["alert_id"], "payload": W.serialize(body), "status": "PENDING",
            "attempts": 0, "next_attempt_at": now, "created_at": now})
        return {"delivery_id": body["delivery_id"]}

    def status(self) -> dict:
        return {"enqueued": self.enqueued, "sent": self.sent, "failed": self.failed,
                "last_error": self.last_error,
                "subscriptions": len(self.store.list_webhooks())}
