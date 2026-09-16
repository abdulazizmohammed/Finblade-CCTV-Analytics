"""Outbound webhooks: push alerts to FinBlade AI workflows (or anything
with a URL) the moment they happen.

A webhook is a subscription: a URL, a shared secret, which events it wants,
and filters (severities, rules, Region/City/Branch scope). When an alert is
raised, acknowledged, resolved or cleared — or a vehicle arrives at / departs
a branch — every matching subscription gets a DELIVERY row. A dispatcher
posts due deliveries, signs them, and retries with backoff. Nothing in the
alert path waits on the network.

THE SIGNATURE. Stripe-style:  X-FinBlade-Signature: t=<epoch>,v1=<hex>
where hex = HMAC-SHA256(secret, f"{t}.{body}"). Signing the timestamp with
the body lets the receiver reject replays older than a few minutes. verify()
is here so a receiver written in Python can import it, and so the tests
prove sign() and verify() agree.

This module is pure: subscriptions, envelopes, matching, signing, backoff.
No HTTP, no clock it does not receive. services/api/webhooks.py drives it.
"""

import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Dict, Iterable, List, Optional, Set, Tuple

SCHEMA_VERSION = "1.0"

EVENTS = (
    "alert.raised",        # a new alert (kind FIRE) — the one workflows mostly want
    "alert.cleared",       # the INFO/CLEAR companion when a condition recovers
    "alert.acknowledged",
    "alert.resolved",      # RESOLVED or DISMISSED, with the note
    "tracker.arrived",     # a vehicle entered a branch geofence
    "tracker.departed",
)
ALERT_EVENTS = ("alert.raised", "alert.cleared", "alert.acknowledged", "alert.resolved")

# What a new subscription gets when it does not say: critical alerts only.
DEFAULT_EVENTS = ("alert.raised",)
DEFAULT_SEVERITIES = ("RED", "CRITICAL")

# Retry schedule, seconds after each failed attempt. Eight tries over ~3h,
# then FAILED and left for a human (or a manual retry). Long enough to ride
# out a FinBlade deploy, short enough that a dead URL does not queue forever.
BACKOFF_S = (5, 30, 120, 300, 900, 1800, 3600, 3600)
MAX_ATTEMPTS = len(BACKOFF_S)

# Receiver's replay window: reject a signature whose t is older than this.
REPLAY_WINDOW_S = 300

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)


# ------------------------------------------------------------ subscription --
def validate_subscription(payload: dict, existing_id: Optional[str] = None) -> Tuple[Optional[dict], List[str]]:
    """(row, errors). A row only when errors is empty."""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return None, ["object expected"]
    wid = str(payload.get("webhook_id") or existing_id or "").strip() or ("wh-" + uuid.uuid4().hex[:10])
    if not _ID_RE.match(wid):
        errors.append("webhook_id: letters, digits, '_', '-' or '.' only")
    url = str(payload.get("url") or "").strip()
    if not _URL_RE.match(url):
        errors.append("url must be http(s)://...")
    events = payload.get("events")
    if events is None or events == []:
        events = list(DEFAULT_EVENTS)
    if not isinstance(events, list) or not events:
        errors.append("events must be a non-empty list")
        events = []
    bad = [e for e in events if e not in EVENTS]
    if bad:
        errors.append(f"unknown events {bad}; choose from {list(EVENTS)}")
    sev = payload.get("severities")
    if sev is None:
        sev = list(DEFAULT_SEVERITIES) if "alert.raised" in events else []
    if not isinstance(sev, list):
        errors.append("severities must be a list (empty = all)")
        sev = []
    sev = [str(x).upper() for x in sev]
    rules = payload.get("rule_ids") or []
    if not isinstance(rules, list):
        errors.append("rule_ids must be a list (empty = all)")
        rules = []
    rules = [str(x).upper() for x in rules]
    secret = payload.get("secret")
    if secret is None or secret == "":
        secret = uuid.uuid4().hex + uuid.uuid4().hex     # 64 hex chars, generated for them
    secret = str(secret)
    if len(secret) < 16:
        errors.append("secret must be at least 16 characters (or omit it to have one generated)")
    if errors:
        return None, errors
    return {
        "webhook_id": wid,
        "name": str(payload.get("name") or wid).strip(),
        "url": url,
        "secret": secret,
        "enabled": payload.get("enabled", True) is not False,
        "events": events,
        "severities": sev,
        "rule_ids": rules,
        "region_id": payload.get("region_id") or None,
        "city_id": payload.get("city_id") or None,
        "branch_id": payload.get("branch_id") or None,
        "headers": {str(k): str(v) for k, v in (payload.get("headers") or {}).items()} if isinstance(payload.get("headers"), dict) else {},
    }, []


def public_view(row: dict) -> dict:
    """A subscription as the API returns it: the secret is shown ONCE at
    creation and never again — the same rule every webhook platform uses."""
    out = dict(row)
    if out.get("secret"):
        out["secret"] = "•••" + out["secret"][-4:]
    return out


# ---------------------------------------------------------------- matching --
def matches(sub: dict, event: str, alert: Optional[dict] = None,
            scope_sites: Optional[Set[str]] = None, site_id: Optional[str] = None) -> bool:
    """Does this subscription want this event?

    scope_sites is the branch set the subscription's region/city/branch
    resolves to (None = no scope). site_id is the record's branch. A scoped
    subscription never receives a record with no branch — an unassigned
    camera is nobody's business until it is placed.
    """
    if not sub.get("enabled", True):
        return False
    if event not in (sub.get("events") or ()):
        return False
    if scope_sites is not None:
        if not site_id or site_id not in scope_sites:
            return False
    if event in ALERT_EVENTS and alert is not None:
        rules = sub.get("rule_ids") or []
        if rules and str(alert.get("rule_id") or "").upper() not in rules:
            return False
        # Severity filters the RAISE only. An acknowledge/resolve/clear of an
        # alert you were told about is always relevant; and a CLEAR is INFO
        # by construction, so filtering it on severity would drop every one.
        if event == "alert.raised":
            sevs = sub.get("severities") or []
            if sevs and str(alert.get("severity") or "").upper() not in sevs:
                return False
    return True


# ---------------------------------------------------------------- envelope --
def envelope(event: str, alert: Optional[dict] = None, tracker_event: Optional[dict] = None,
             context: Optional[dict] = None, tenant: Optional[dict] = None,
             base_url: str = "", now: Optional[float] = None) -> dict:
    """The body a receiver gets. Stable shape, versioned; everything a
    workflow needs to act without a second call, plus the links for when it
    wants one."""
    now = time.time() if now is None else now
    body = {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "delivery_id": "dl-" + uuid.uuid4().hex,
        "sent_at": now,
        "tenant": tenant or {},
        "context": context or {},
    }
    if alert is not None:
        a = dict(alert)
        body["alert"] = a
        aid = a.get("alert_id")
        body["links"] = {
            "alert": f"{base_url}/api/v1/alerts/{aid}",
            "frame": f"{base_url}/api/v1/incidents/{aid}/frame" if a.get("frame") else None,
            "acknowledge": f"{base_url}/api/v1/alerts/{aid}/ack",
            "resolve": f"{base_url}/api/v1/alerts/{aid}/resolve",
            "console": f"{base_url}/web/ops.html?camera={a.get('camera_id') or ''}",
        }
    if tracker_event is not None:
        body["tracker_event"] = dict(tracker_event)
        body["links"] = {"map": f"{base_url}/web/map.html?vehicle={tracker_event.get('tracker_id') or ''}"}
    return body


def serialize(body: dict) -> str:
    """Canonical bytes: what gets signed is exactly what gets sent."""
    return json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- signing ---
def sign(secret: str, body: str, ts: Optional[int] = None) -> Tuple[str, int]:
    """(header value, ts). Header: 't=<ts>,v1=<hex>'."""
    ts = int(time.time()) if ts is None else int(ts)
    mac = hmac.new(secret.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}", ts


def verify(secret: str, body: str, header: str, now: Optional[float] = None,
           window_s: int = REPLAY_WINDOW_S) -> bool:
    """For the receiver. Constant-time compare, replay window enforced."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError, AttributeError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > window_s:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def headers_for(event: str, delivery_id: str, signature: str, ts: int,
                extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "User-Agent": "FinBlade-CCTV-Webhooks/1.0",
        "X-FinBlade-Event": event,
        "X-FinBlade-Delivery": delivery_id,
        "X-FinBlade-Timestamp": str(ts),
        "X-FinBlade-Signature": signature,
    }
    for k, v in (extra or {}).items():
        # A subscriber may add their own auth header; they may not overwrite ours.
        if k.lower() not in {x.lower() for x in h}:
            h[k] = v
    return h


# ----------------------------------------------------------------- retries --
def next_attempt(attempts: int, now: float) -> Optional[float]:
    """When to try again after `attempts` failures, or None to give up."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return now + BACKOFF_S[attempts - 1] if attempts >= 1 else now
