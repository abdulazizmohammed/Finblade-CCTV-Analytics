# FinBlade CCTV — outbound webhooks

**For:** the FinBlade AI developer wiring a workflow trigger.
**Companion to:** `MCP.md` (the chatbot reads), this (the workflow is told).

When an alert fires — density critical, restricted-zone intrusion, fire,
PPE, camera or tracker offline — the CCTV system POSTs a signed JSON body to
your URL within a few seconds. Also on acknowledge, resolve and clear, and
when a vehicle arrives at or leaves a branch, if you subscribe to those.

---

## 1. Register

On the **Webhooks** page (`/web/webhooks.html`) or:

```bash
curl -X POST http://<cctv-host>:8000/api/v1/webhooks \
  -H "Authorization: Bearer <full api key>" -H "Content-Type: application/json" \
  -d '{"name":"FinBlade AI — critical","url":"https://ai.finblade.example/hooks/cctv",
       "events":["alert.raised","alert.resolved"],
       "severities":["RED","CRITICAL"],
       "region_id":"WESTERN",
       "headers":{"X-Api-Key":"<your inbound key>"}}'
```

The response contains the **signing secret once**. Afterwards it is masked.
Omit `secret` to have one generated; supply one (≥16 chars) to bring your own.

| Field | Meaning |
|---|---|
| `events` | any of `alert.raised` `alert.cleared` `alert.acknowledged` `alert.resolved` `tracker.arrived` `tracker.departed` (default: `alert.raised`) |
| `severities` | filter for `alert.raised` only (default `RED`,`CRITICAL`; `[]` = all). Acks/resolves/clears of an alert are never filtered by severity |
| `rule_ids` | e.g. `["R-06","R-10"]` (default all) |
| `region_id` / `city_id` / `branch_id` | scope; they intersect. A scoped subscription never receives a record from an unplaced camera |
| `headers` | extra request headers (your own auth); you cannot override the `X-FinBlade-*` ones |

## 2. What you receive

```http
POST /hooks/cctv HTTP/1.1
Content-Type: application/json
User-Agent: FinBlade-CCTV-Webhooks/1.0
X-FinBlade-Event: alert.raised
X-FinBlade-Delivery: dl-3f9c…
X-FinBlade-Timestamp: 1789540000
X-FinBlade-Signature: t=1789540000,v1=8a1f…
```

```json
{
  "schema_version": "1.0",
  "event": "alert.raised",
  "delivery_id": "dl-3f9c…",
  "sent_at": 1789540000.1,
  "tenant": {"name": "Wareed Medical Laboratories", "short": "Wareed", "country": "KSA"},
  "alert": {
    "alert_id": "4127", "rule_id": "R-06", "severity": "RED", "kind": "FIRE", "status": "OPEN",
    "message": "intrusion in Sample store", "zone_id": "STORE", "camera_id": "CAM-R1",
    "site_id": "RUH-01", "ts": 1789539998.4, "frame": "bookmarks/…jpg", "person_ref": "pr_…"
  },
  "context": {
    "branch": {"branch_id": "RUH-01", "name": "Riyadh Main Laboratory", "branch_type": "LAB",
               "city": "Riyadh", "region": "Central Region", "lat": 24.7256, "lon": 46.6893},
    "camera": {"camera_id": "CAM-R1", "name": "Lobby cam", "state": "ONLINE", "people_in_view": 3},
    "zone":   {"zone_id": "STORE", "zone_name": "Sample store", "restricted": true, "occupancy": 1, "status": "NORMAL"}
  },
  "links": {
    "alert": "https://cctv…/api/v1/alerts/4127",
    "frame": "https://cctv…/api/v1/incidents/4127/frame",
    "acknowledge": "https://cctv…/api/v1/alerts/4127/ack",
    "resolve": "https://cctv…/api/v1/alerts/4127/resolve",
    "console": "https://cctv…/web/ops.html?camera=CAM-R1"
  }
}
```

`links` are built from `FINBLADE_PUBLIC_URL` (fall back `FINBLADE_SELF_URL`);
set the former to the address your workflow can reach. `alert.resolved`
carries `status` RESOLVED|DISMISSED, `resolved_by`, `note`. Tracker events
carry `tracker_event` (`tracker_id`, `branch_id`, `distance_m`, `dwell_s` on
departure) instead of `alert`. `person_ref` is an opaque salted hash and
identifies nobody. A tracker is a vehicle; there is no driver.

**Severities:** `INFO` · `AMBER` (warning) · `RED` / `CRITICAL` · `COMPLIANCE`
(R-11, a person-policy breach). Rules R-01…R-12 are listed in `MCP.md` and
`CAPABILITIES.md`. R-10 (fire/smoke) and R-11 (PPE) come from evaluation
models — a workflow should treat them as "the detector reported", not as fact.

## 3. Verify the signature — always

```python
import hmac, hashlib, time

def verify(secret: str, body: bytes, header: str, window_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    ts, given = int(parts["t"]), parts["v1"]
    if abs(time.time() - ts) > window_s:
        return False                                   # replay
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)
```

Sign over the **raw request body bytes** — do not re-serialise the JSON.
(`finblade/webhooks.py` has `verify()` if the receiver is Python.)

## 4. Respond and retry semantics

- Answer **2xx** quickly (queue the work; don't run the workflow inline).
- **5xx / timeout / connection error** → retried: 5 s, 30 s, 2 m, 5 m, 15 m, 30 m, 1 h, 1 h (8 attempts, ~3 h), then `FAILED` and left for a human — the Webhooks page has **Retry**.
- **4xx** → final. The receiver rejected the payload; resending the same bytes cannot help.
- A retry sends the **identical body and delivery id**; the signature timestamp is fresh. **Deduplicate on `X-FinBlade-Delivery`.**
- Order is not guaranteed across deliveries; `alert.resolved` can arrive before a retried `alert.raised`. Key your state on `alert.alert_id`.

## 5. Operate

- **Send test** on the Webhooks page posts a synthetic `RED` `alert.raised` (`alert.test: true`) and shows the response.
- `GET /api/v1/webhooks/deliveries?webhook_id=…` — the log; `POST …/deliveries/{id}/retry`.
- The queue is the database: an outage on your side costs nothing, a restart loses nothing.
- Registering a webhook needs the **full** API key; it is a place this system sends data to.
