# FinBlade API — integration requirements

**For:** the FinBlade platform developer building the receiving API
**From:** the CCTV crowd-analytics service
**Status:** specification of what the CCTV side will send. Field names, types and
cadences below are taken from the running system, not proposed.

---

## 1. What this integration is

A CCTV crowd-analytics service currently renders everything below on its own
dashboard. This integration pushes the same data to FinBlade so it can be stored,
correlated and displayed there instead.

**Direction is one-way: CCTV → FinBlade.** The CCTV side is the producer. It
already has its own database; FinBlade is an additional sink, not a replacement.

**You are building six receiving endpoints.** Suggested paths, all `POST`:

| Stream | Endpoint | Cadence | Volume per camera |
|---|---|---|---|
| Zone state | `/api/v1/cctv/zone-state` | every 5 s per zone | 720/hour/zone |
| Events | `/api/v1/cctv/events` | on occurrence | traffic-dependent, see §7 |
| Alerts | `/api/v1/cctv/alerts` | on rule fire/clear | low; bursty |
| Camera health | `/api/v1/cctv/camera-health` | every 5 s per camera | 720/hour/camera |
| People counts | `/api/v1/cctv/people-counts` | every 5 s (site-wide) | 720/hour total |
| Snapshots | `/api/v1/cctv/snapshots` | every 30 s per camera | 120/hour/camera, ~2 KB/s |

All payloads are JSON, `Content-Type: application/json`, UTF-8.

---

## 2. Conventions that apply to every payload

**Timestamps** are Unix epoch **seconds as a float** (e.g. `1785136653.207702`),
UTC, never a formatted string. Millisecond precision is meaningful; sub-second
ordering within a camera is reliable, across cameras it is not (see §8).

**Identifiers**

| Field | Format | Meaning |
|---|---|---|
| `site_id` | string | e.g. `"SITE-DXB-01"` — configured per deployment |
| `camera_id` | string | operator-chosen, e.g. `"CAM-06"`. Not guaranteed to be a slug — treat as opaque, may contain mixed case and hyphens |
| `zone_id` | string | operator-chosen, e.g. `"ZONE-01"`. Unique per camera, **not** globally unique — key on `(camera_id, zone_id)` |
| `event_id` | UUIDv4 string | unique per event; use for idempotency |
| `person_ref` | `pr_` + 16 lowercase hex | anonymous, per-camera |
| `global_ref` | `gp_` + 16 lowercase hex | anonymous, cross-camera identity |

**Critical: `person_ref` and `global_ref` are NOT stable identifiers.** They are
salted hashes regenerated at every service restart. The same physical person gets
a different ref after a restart, by design — this is what keeps the data
non-identifying. Do **not** use them as long-term primary keys, build person
profiles on them, or attempt to correlate them across days. They are valid only
for correlating records within one continuous session.

---

## 3. Zone state — `POST /api/v1/cctv/zone-state`

The core occupancy stream. One message per zone every 5 seconds.

```json
{
  "zone_id": "ZONE-01",
  "camera_id": "CAM-06",
  "zone_name": "Lobby",
  "zone_type": "MONITORED",
  "restricted": false,
  "ts": 1785136653.207702,
  "occupancy": 4,
  "density": 0.067,
  "capacity_pct": 10.0,
  "capacity_max": 40,
  "area_sqm": 60.0,
  "peak_occupancy": 12,
  "avg_occupancy": 4.4,
  "trend": "rising",
  "status": "NORMAL",
  "inflow_per_min": 31.0,
  "outflow_per_min": 28.0,
  "net_flow": 3.0,
  "inflow_5m": 31.4,
  "outflow_5m": 31.2,
  "inflow_15m": 32.2,
  "outflow_15m": 31.67
}
```

| Field | Type | Notes |
|---|---|---|
| `occupancy` | int ≥ 0 | people currently inside the polygon |
| `density` | float ≥ 0 | people per m² = `occupancy / area_sqm` |
| `capacity_pct` | float ≥ 0 | may exceed 100 |
| `peak_occupancy` / `avg_occupancy` | int / float | since service start, not rolling |
| — | — | *see the note below on `avg_occupancy` under sparse storage* |
| `trend` | enum | `rising` \| `falling` \| `flat` |
| `status` | enum | `NORMAL` \| `WARNING` \| `CRITICAL` |
| `zone_type` | enum | `MONITORED` \| `RESTRICTED` \| `ENTRANCE` \| `EXIT` \| `TRANSITION` \| `UNMONITORED` |
| `restricted` | bool | permanently off-limits area |
| flow fields | float | people per minute; `net_flow` = in − out, may be negative |

**Required for validation:** `zone_id`, `camera_id`, `ts`, `occupancy`,
`density`, `capacity_pct`, `inflow_per_min`, `outflow_per_min`, `status`.
The rest are supplementary — accept the message if they are absent.

### This stream is unchanged by our write-on-change storage

We now store a zone-state row only when occupancy or status changes, plus a
keepalive every 5 minutes — 1,674,955 rows of our own nine-day history become
7,243. **This push is not affected.** It sends a snapshot of every zone's
current reading on every tick, taken from the live table rather than the
history, so you keep receiving one message per zone every 5 seconds exactly as
specified above.

We are flagging it only because it changes one thing on your side if you store
what we send: **do not compute long-window averages by averaging our messages
equally.** Our own reports moved to weighting each reading by how long it held,
and on live data the two answers differ by up to 8×. If you would rather have
the aggregate than compute it, ask — we already produce it.

---

## 4. Events — `POST /api/v1/cctv/events`

Discrete occurrences. Common envelope plus type-specific fields.

**Envelope, present on every event:**

```json
{
  "event_id": "3f2a...uuid4",
  "event_type": "ZONE_ENTRY",
  "camera_id": "CAM-06",
  "site_id": "SITE-DXB-01",
  "timestamp": 1785136653.207702
}
```

**The 13 event types and their additional fields:**

| `event_type` | Extra fields | Meaning |
|---|---|---|
| `ZONE_ENTRY` | `zone_to` str, `person_ref` str, `confidence` float 0–1 | person entered a zone |
| `ZONE_EXIT` | `zone_from` str, `person_ref` str | person left a zone |
| `ZONE_TRANSITION` | `zone_from` str, `zone_to` str, `person_ref` str | moved zone→zone |
| `DENSITY_UPDATE` | `zone_id` str, `occupancy` int, `density` float | **status crossing** — see the note below |
| `CAPACITY_WARNING` | `zone_id` str, `occupancy` int, `capacity_pct` float | ≥90% of capacity |
| `RESTRICTED_ZONE_ENTRY` | `zone_id` str, `person_ref` str | entered a no-go area |
| `RESTRICTED_ZONE_EXIT` | `zone_id` str, `person_ref` str, `duration` float | left it; `duration` seconds inside |
| `LOITERING_START` | `zone_id` str, `person_ref` str, `dwell_time` float | dwell exceeded threshold |
| `LOITERING_END` | `zone_id` str, `person_ref` str, `dwell_time` float | loitering ended |
| `CAMERA_HEARTBEAT` | — | camera alive |
| `CAMERA_ONLINE` | — | camera came up |
| `CAMERA_OFFLINE` | `last_seen` float | no frames past threshold |
| `CAMERA_RECOVERED` | — | came back after an outage |

Example:

```json
{
  "event_id": "8c1d4e2a-...",
  "event_type": "RESTRICTED_ZONE_EXIT",
  "camera_id": "CAM-06",
  "site_id": "SITE-DXB-01",
  "timestamp": 1785136700.5,
  "zone_id": "ZONE-02",
  "person_ref": "pr_4dbe3172855a94b4",
  "duration": 12.5
}
```

### `DENSITY_UPDATE` is no longer a 5-second sample

It used to fire once per zone every five seconds. It now fires **only when a
zone's density status crosses** — `NORMAL` ↔ `WARNING` ↔ `CRITICAL` — plus once
on first sighting of each zone so a consumer joining the stream has a starting
value.

Why: it was 1,674,979 rows against 2,553 for every other event type combined —
99.85% of the events table — and each one duplicated the `zone-state` message
sent at the same microsecond, with the same `occupancy` and `density`. You were
receiving the same numbers twice over the wire. On a thirty-zone site that is
21,600 messages an hour reduced to a handful a day.

**Nothing is lost.** Continuous density still arrives on the **zone-state**
stream every 5 seconds (§3), which is where it always was. And a person
entering and leaving inside one 5-second window was never visible in
`DENSITY_UPDATE` anyway — a sampler cannot see anything shorter than its sample
interval. That visit is in `ZONE_ENTRY` / `ZONE_EXIT`, which fire per person,
immediately, and now carry the resulting `occupancy` and `density` themselves.

If you need every tick, say so and we set `FINBLADE_DENSITY_UPDATE_MODE=always`
— it is one environment variable and a camera restart.

**Reject** with `422` if: `event_type` is unknown, any envelope field is missing
or the wrong type, `timestamp < 0`, `confidence` outside `[0,1]`, `occupancy` or
`density` negative, or `person_ref` does not match `^pr_[0-9a-f]{16}$`.

That last check is a deliberate PII guard — a `person_ref` that is not an
anonymous hash means something has gone wrong upstream and the message must not
be stored. Please reproduce it on your side.

---

## 5. Alerts — `POST /api/v1/cctv/alerts`

```json
{
  "alert_id": "1042",
  "rule_id": "R-06",
  "severity": "RED",
  "message": "restricted zone ZONE-02 entered",
  "ts": 1785136653.2,
  "zone_id": "ZONE-02",
  "camera_id": "CAM-06",
  "person_ref": "pr_4dbe3172855a94b4",
  "kind": "FIRE",
  "status": "OPEN",
  "frame": "/bookmarks/bm_CAM-06_00012.jpg"
}
```

**Rules that fire:**

| `rule_id` | Trigger | Typical severity |
|---|---|---|
| `R-01` | density above the warning threshold (default 2.0/m²) | `AMBER` |
| `R-02` | density above the critical threshold (default 4.0/m²) | `RED` |
| `R-03` | occupancy ≥ 90% of capacity | `AMBER` |
| `R-05` | loitering beyond the per-zone threshold | `AMBER` |
| `R-06` | entry into a restricted zone (immediate) | `RED` |
| `R-07` | camera silent > 30 s | `RED` |
| `R-08` | scheduled occupancy report generated | `INFO` |

`severity` ∈ `INFO` \| `AMBER` \| `RED` \| `CRITICAL`.
`kind` ∈ `FIRE` (condition began) \| `CLEAR` (condition ended). A `CLEAR` is
informational — pair it with the preceding `FIRE` for the same
`(rule_id, zone_id, camera_id)`.

**Lifecycle.** `status` ∈ `OPEN` → `ACK` → `RESOLVED` \| `DISMISSED`. If an
operator acknowledges or closes an alert in FinBlade, we need that reflected back
— see the open question in §10.

`frame` is a relative URL to a JPEG snapshot, present only for `R-02` and `R-06`.
It is served by the CCTV host, not embedded. See §9 for what that means for you.

---

## 6. Camera health — `POST /api/v1/cctv/camera-health`

```json
{
  "camera_id": "CAM-06",
  "site_id": "SITE-DXB-01",
  "ts": 1785136653.2,
  "health": {
    "state": "ONLINE",
    "enabled": true,
    "input_fps": 24.0,
    "resolution": [752, 416],
    "last_valid_ts": 1785136653.1,
    "seconds_since_valid": 0.1,
    "frozen": false,
    "reconnecting": false,
    "dropped_frames": 11,
    "reconnects": 11,
    "loops": 0,
    "frame_seq": 8421
  }
}
```

`state` ∈ `ONLINE` \| `DEGRADED` \| `RECONNECTING` \| `OFFLINE` \| `DISABLED`.

`DEGRADED` means frames are arriving but are frozen or below the expected rate —
it is not the same as offline, and operators treat it differently. `resolution`
is `[width, height]` or `null` before the first frame.

---

## 7. People counts — `POST /api/v1/cctv/people-counts`

Cross-camera de-duplicated counts. **This stream does not require zones** — it
works on cameras with no polygons drawn.

```json
{
  "site_id": "SITE-DXB-01",
  "ts": 1785136653.2,
  "live": 4,
  "unique_total": 8,
  "cross_camera": 7,
  "per_camera": [
    { "camera_id": "CAM-06", "live": 2, "unique": 7 },
    { "camera_id": "Cam-07", "live": 3, "unique": 8 }
  ]
}
```

| Field | Meaning |
|---|---|
| `live` | distinct people visible right now, site-wide |
| `unique_total` | distinct people since service start (footfall) |
| `cross_camera` | of those, how many were seen by more than one camera |
| `per_camera[].live` / `.unique` | the same two numbers per camera |

**Do not sum `per_camera` to get a site total.** It will exceed `unique_total`
whenever someone was seen by more than one camera — the difference is exactly
the double-counting that de-duplication removed.

The relationship is an inequality, NOT an equality:

```
sum(per_camera[].unique) - unique_total >= cross_camera
```

It is only equal when every cross-camera person was seen by exactly two
cameras. Someone seen by three contributes 3 to the sum but 1 to
`unique_total` — a difference of 2 — while counting only once in
`cross_camera`. Observed live: sum 118, unique_total 93 (difference 25) against
cross_camera 16, because several people were picked up by three cameras.

Use the inequality as a validation assertion. An equality check will fail on
any real multi-camera site.

**`unique_total` resets to 0 when the CCTV service restarts.** It is cumulative
within a session only. Treat a decrease as a restart boundary, not as bad data,
and do not compute deltas across it.

---

## 7b. Camera snapshots — `POST /api/v1/cctv/snapshots`

Periodic JPEG thumbnails, one message per online camera. **This is not video.**

```json
{
  "camera_id": "CAM-01",
  "site_id": "RUH-01",
  "ts": 1785311800.4,
  "format": "jpeg",
  "resolution": "640x360",
  "people_in_view": 2,
  "bytes": 62495,
  "image_base64": "/9j/4AAQSkZJRgABAQ..."
}
```

The image is the **annotated** frame — the same one the operator sees, with
boxes, track ids and zone polygons drawn on it.

### Why thumbnails and not a video stream

Continuous video is not pushed, deliberately. Frames are re-encoded as JPEG at
roughly 10x the size of the equivalent H.265, which is **20-40 Mbit/s per
viewer** at 1080p with no fan-out — ten dashboards mean ten encodes. That is
unusable across a WAN and disproportionate for a dashboard tile.

One frame per camera every 30 seconds is about **2 KB/s per camera** — four
orders of magnitude less — and is enough for a dashboard tile, incident context
or a "what did it look like" view. Measured: 62 KB per frame at 640x360.

Cadence is configurable per deployment (`FINBLADE_SNAPSHOT_INTERVAL`, seconds;
0 disables). At the 30s default with 10 cameras that is ~20 KB/s.

**Only ONLINE cameras send.** A snapshot from an offline camera would be a stale
frame from before it dropped — worse than none, because it looks live.

### If you need genuine live video

PULL it instead — see `FINBLADE_CLIENT_GUIDE.md` §6:

```
GET /api/v1/cameras/{camera_id}/stream        MJPEG, embeddable in <img>
GET /api/v1/cameras/{camera_id}/snapshot      one JPEG, on demand
```

Only do that over a link that can carry it. On a WAN, prefer the pushed
thumbnails and open the live stream on demand for a single camera at a time.

---

## 8. Volume and delivery

**Steady-state rates**, derived from the 5-second cadences:

| Stream | Per hour | Per day |
|---|---|---|
| Zone state | 720 × zones | 17,280 × zones |
| Camera health | 720 × cameras | 17,280 × cameras |
| People counts | 720 | 17,280 |

For a 10-camera site with 3 zones each: ~44,000 messages/hour ≈ **1.05 M/day**
before any events or alerts.

**Events are traffic-dependent and can dominate.** A test deployment of a
handful of cameras accumulated **569,000 events** over a few days. Budget for
events to exceed all other streams combined at a busy site.

**Please support batching.** Accepting an array of objects at the same endpoint
would cut request volume by roughly 10× on the zone-state and event streams:

```json
{ "batch": [ {...}, {...} ] }
```

Confirm your preferred batch shape and maximum size and we will conform.

**Ordering and clock skew.** Each camera runs as an independent process with its
own wall clock. Messages from one camera arrive in order; **across cameras they
may be a few seconds out of order**. Do not assume a global ordering. If you need
one, order by `ts` on receipt rather than arrival.

**Retries.** The CCTV side will retry failed deliveries, so **you must be
idempotent**. Deduplicate on `event_id` for events and on
`(camera_id, zone_id, ts)` for zone state. Return `2xx` for a duplicate rather
than an error.

**Timeouts.** Please respond within 2 seconds. The producer's HTTP calls are
best-effort with short timeouts so that a slow sink never stalls video
processing — if you are slow, we drop the message rather than queue it.

---

## 9. Snapshot images

Alerts for `R-02` and `R-06` carry a `frame` field — a relative URL such as
`/bookmarks/bm_CAM-06_00012.jpg`. The image is **not** in the payload.

Two options, please choose:

1. **You fetch it** from the CCTV host over HTTP. Simplest, but requires network
   reachability from FinBlade to the CCTV box, and the file is deleted when the
   alert is cleared.
2. **We push it** as base64 in the alert payload, or as `multipart/form-data` to
   a separate endpoint. Larger requests (~100–200 KB per image) but no inbound
   reachability needed.

Option 2 is more robust for anything crossing a network boundary. Note these are
images of people in a monitored space — whichever option you choose, they need
the same handling and retention rules as any CCTV still.

---

## 10. Non-functional requirements

**Authentication.** The CCTV service currently has none of its own. For this
integration please specify one of: a static API key header (simplest), OAuth2
client-credentials, or mTLS. We will implement whatever you specify. State the
header name and rotation expectations.

**Transport.** HTTPS required if this leaves the local network. The CCTV host may
be on an isolated VLAN; confirm egress is permitted.

**Error semantics.** Please use: `2xx` accepted, `4xx` permanently bad (we will
log and drop — do not expect a retry to fix it), `5xx` or timeout transient (we
will retry). Return a JSON body with a machine-readable reason on `4xx` so
failures are diagnosable.

**Rate limiting.** If you rate-limit, return `429` with `Retry-After`. Do not
silently drop; we cannot detect that.

**Retention.** Zone state at 5-second granularity is large — nine days of eight
zones was 1.15 GB on our side before we stopped storing unchanged repeats.
Tell us your retention, and whether you would rather we sent you the same thing
we now store: a message only when a zone's occupancy or status changes, plus a
keepalive every 5 minutes. That is a 99.6% reduction on real traffic and loses
no transition. We have not changed this push unilaterally because §3 specifies
5 seconds and you may be relying on the fixed cadence as a liveness signal —
though `CAMERA_HEARTBEAT` and the camera-health post both already provide it.

---

## 11. Privacy and compliance — read before designing storage

This system is deliberately built to hold **no personally identifying
information**, and the integration must not weaken that.

* **No names, faces, or biometric templates are sent.** Ever.
* `person_ref` and `global_ref` are salted hashes with a per-session salt.
  They cannot be correlated across service restarts, by design.
* The CCTV service does compute appearance embeddings for cross-camera matching.
  **These never leave the CCTV process** — they are held in RAM, never written to
  disk, never logged, and are not part of any payload in this document. Please do
  not request them; supplying them would turn this into a biometric data flow
  with materially different legal obligations.
* Snapshot images (§9) **are** identifying. They are the only identifying
  artifact in this integration and should drive your retention and access policy.

If FinBlade intends to build persistent person profiles, that is a different
system with different consent and legal requirements, and it cannot be done with
the identifiers in this specification. Raise it before designing for it.

---

## 12. Transport, and the one bidirectional flow

### Outbound (CCTV → FinBlade): REST, not WebSocket

Everything in sections 3–7 is server-to-server bulk telemetry. REST is the right
fit and the spec above assumes it:

* **Retries need semantics.** At ~1M messages/day some deliveries will fail.
  `2xx` accepted / `4xx` drop / `5xx` retry is unambiguous. A WebSocket that is
  "open" tells the sender nothing about whether a message was processed, so you
  would end up building an ack-and-replay protocol on top of it.
* **Batching** is trivial over REST and cuts round trips ~10x.
* **Backpressure** works: return `429`/`503` and the producer backs off. A socket
  just queues in memory until something falls over.
* Load balancers, gateways, auth headers, request logs and `curl` debugging all
  work out of the box.

**Transport will not make the data fresher.** Zone state is COMPUTED on a
5-second cadence — pushing it over a socket delivers the same 5-second-old
number sooner, not a newer one. The only genuinely latency-sensitive stream is
alerts, and those are POSTed the moment a rule fires, so REST is already
sub-second there.

Use WebSockets between the FinBlade backend and FinBlade's own browsers. That is
internal to FinBlade and needs nothing from this side.

### Return path (FinBlade → CCTV): operator acknowledgements

Confirmed in scope. When an operator acknowledges, resolves or dismisses an
alert in FinBlade, this system must reflect it — otherwise the two alert feeds
diverge and neither is authoritative.

**The constraint: the CCTV host is usually not reachable inbound.** It sits on a
site LAN, often behind NAT or a VPN, dialling out. Designing the return path as
"FinBlade calls the CCTV host" fails on most real deployments.

**Recommended: return acknowledgements in the RESPONSE BODY of the posts this
system is already making.** No inbound connection, no firewall rules, no extra
polling traffic — it rides on requests that exist anyway. This system already
uses exactly this pattern internally: camera workers receive control commands in
the response to their 5-second health post.

So the alert POST response becomes:

```json
{
  "accepted": true,
  "pending_acks": [
    {
      "alert_id": "1042",
      "action": "ACK",
      "by": "operator@finblade",
      "ts": 1785136999.1,
      "note": "security dispatched"
    }
  ]
}
```

`action` is one of `ACK`, `RESOLVED`, `DISMISSED`. `alert_id` is the id THIS
system sent you in section 5 — echo it back unchanged; do not substitute a
FinBlade-internal id, or we cannot match it.

**Delivery rules:**

* Keep returning an acknowledgement until this system confirms it. We will
  re-apply the same one harmlessly — applying an ack twice is a no-op here.
* Confirmation comes on the next post as `"acked_confirmed": ["1042"]` in the
  request body. Drop those from your pending list.
* Latency is not critical. An operator acknowledgement arriving within one
  post cycle (5 seconds) is fine.

**CONFIRMED: the CCTV host IS reachable from FinBlade**, so the response-body
mechanism above is not needed. Call these directly instead. Contract below is
verified against a running instance, not proposed.

**Acknowledge**

```
POST /api/v1/alerts/{alert_id}/ack
{"acknowledged_by": "operator@finblade"}

200 {"acknowledged": true, "alert_id": "al-1",
     "acknowledged_by": "operator@finblade", "acknowledged_at": 1785310240.25}
```

**Resolve or dismiss**

```
POST /api/v1/alerts/{alert_id}/resolve
{"action": "RESOLVED"|"DISMISSED", "resolved_by": "operator@finblade",
 "note": "security dispatched"}

200 {"ok": true, "alert_id": "al-1", "status": "RESOLVED",
     "resolved_by": "operator@finblade", "resolved_at": 1785310240.26,
     "note": "security dispatched"}
```

`alert_id` is the value sent in section 5. Pass it back unchanged.

### 409 does not mean failure — do not retry it

Repeating an action returns **409**, not 200:

```
re-ack       -> 409 {"acknowledged": false, "error": "unknown or already-acknowledged alert"}
re-resolve   -> 409 {"ok": false, "error": "unknown or already-closed alert"}
unknown id   -> 409 {"acknowledged": false, "error": "unknown or already-acknowledged alert"}
```

The effect is idempotent — state does not change — but the status code is 409 in
all three cases, including an alert id that does not exist. So 409 means "this
alert is already in that state, OR it is unknown"; the two are not
distinguishable from the response. Treat 409 as terminal success-or-ignore and
stop retrying. Retrying will never turn into a 200.

### AUTHENTICATION IS REQUIRED BEFORE THIS GOES ANYWHERE REAL

Verified on a running instance: **every call above succeeded with no
credentials of any kind.** There is currently no authentication on this API.

Reachable from FinBlade means reachable from anything else with a route to that
host. These endpoints mutate state, and the same unauthenticated API also
exposes `DELETE /api/v1/alerts` and `DELETE /api/v1/cameras/{id}`, plus every
live video feed.

This is tracked on the CCTV side and is not FinBlade's to fix, but it does gate
the integration. One of the following must be in place first:

* a shared API key or bearer token enforced on this API (smallest change), or
* mutual TLS between the two services, or
* network isolation that genuinely restricts the CCTV host to FinBlade traffic
  only — not merely "it is on an internal VLAN".

Tell us which scheme you want and it will be implemented on this side to match.

---

## 13. Open questions for FinBlade

1. Endpoint paths and base URL — accept the suggestions in §1, or specify yours?
2. Authentication scheme and header name (§10)?
3. Batch support: preferred envelope and max batch size (§8)?
4. Snapshot images: you fetch, or we push (§9)?
5. ~~Should operator actions propagate back? Is the host reachable?~~
   **BOTH CONFIRMED.** Acknowledgements flow back via direct calls to
   `/api/v1/alerts/{id}/ack` and `/resolve` — contract verified in §12.
   **This now blocks on authentication**: those endpoints currently accept
   unauthenticated calls. Choose a scheme (§12) and it will be implemented.
6. Retention and pre-aggregation preference for zone state (§10)?
7. ~~Do you want raw `DENSITY_UPDATE` events, given zone state already carries
   density every 5 seconds?~~ **Asked twice, unanswered, so we picked the
   reversible option.** They now fire only on a `NORMAL`/`WARNING`/`CRITICAL`
   crossing rather than every 5 seconds — see §4. Continuous density is
   unchanged on the zone-state stream. If you were consuming every tick, tell
   us and we restore it with one environment variable.
8. ~~Can the chatbot have a database view to query directly?~~
   **We are proposing tools instead — please review.** Six read tools with
   schemas and descriptions are in `integrations/finblade_ai/tools.py`, with a
   worked agent loop in `chat.py`. Three reasons a view does not work, in
   `integrations/finblade_ai/README.md` §8: our store is a SQLite file with no
   listener; its schema changed twice this month and a view would have broken
   silently both times; and the rules that make the numbers correct are not in
   the schema — a row means "and it stayed that way", an absent row is
   ambiguous, and `zone_id` is not unique. Reimplementing those in your SQL
   means rediscovering three bugs we have already fixed. **If the tools do not
   fit your bot, send us the queries you need and we will add endpoints.**
9. Do the three new history endpoints (`/zones/{id}/series`, `/at`,
   `/duration` — client guide §4b) cover what the bot needs to ask? They are
   the ones we would have to add for a view to be replaced entirely.
