# Reading live CCTV data — guide for the FinBlade developer

**Companion to `FINBLADE_API_REQUIREMENTS.md`.** That document specifies what
FinBlade must BUILD to receive pushed data. This one is the opposite direction:
how to READ live data straight out of the CCTV service.

Every request and response below was captured from a running instance.

---

## 1. Two ways to get the data — you probably want both

| | Push (CCTV → FinBlade) | Pull (FinBlade → CCTV) |
|---|---|---|
| Who initiates | CCTV, every 5s | FinBlade, whenever it likes |
| Good for | durable history, alerting, working through outages | dashboards, ad-hoc queries, backfilling a page on load |
| Needs | FinBlade to expose endpoints | network route to the CCTV host |
| Survives an outage | yes — the store is the queue, it replays | no — a missed poll is simply missed |

**Use push for anything you must not lose** (events, alerts). **Use pull for
rendering a screen** — a dashboard opening should fetch current state rather
than wait for the next push.

---

## 2. Authentication

Every `/api/v1` route requires the key. Two equivalent forms:

```bash
curl -H "Authorization: Bearer $KEY"  http://cctv-host:8000/api/v1/cameras
curl -H "X-API-Key: $KEY"             http://cctv-host:8000/api/v1/cameras
```

Missing or wrong key:

```
401 {"error":"unauthorized",
     "detail":"supply the API key as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'"}
```

**`?key=` in the query string works on the video stream route ONLY** (§6). It is
rejected everywhere else — a key in a URL leaks into access logs, browser
history and `Referer` headers, so it exists solely because an `<img>` element
cannot send headers.

The dashboard pages themselves (`/web`, `/tools`) load without a key; they then
prompt for it. Do not treat that as a way in — every data route is gated.

---

## 3. Live state — the endpoints a dashboard needs

### Cameras and people counts

```
GET /api/v1/cameras
```
```json
{"cameras": [
  {"camera_id": "CAM-01", "site_id": "RUH-01", "state": "ONLINE",
   "effective_state": "ONLINE", "input_fps": 30.0, "resolution": "640x360",
   "last_seen": 1785309812.5, "seconds_since_seen": 1.2,
   "people_in_view": 2, "people_in_zones": 0,
   "stream_url": "http://127.0.0.1:8090/stream",
   "dropped_frames": 0, "reconnects": 0, "online": true}
]}
```

`people_in_view` is the count that **needs no zones** — every person the camera
is tracking. `people_in_zones` counts only those inside a drawn polygon. On a
site where some cameras have zones and some do not, summing `people_in_zones`
under-reports; sum `people_in_view` for a true headcount.

Use `effective_state` (not `state`) — it accounts for how long since the camera
last reported. Values: `ONLINE`, `DEGRADED`, `RECONNECTING`, `OFFLINE`,
`DISABLED`. `DEGRADED` means frames are arriving but frozen or slow; it is not
the same as offline and operators treat it differently.

### Zone occupancy

```
GET /api/v1/zones/state
```
```json
{"zones": [
  {"zone_id": "ZONE-01", "camera_id": "CAM-01", "zone_name": "Lobby",
   "occupancy": 4, "density": 0.067, "capacity_pct": 10.0,
   "peak_occupancy": 12, "avg_occupancy": 4.4, "trend": "rising",
   "status": "NORMAL", "restricted": false,
   "inflow_per_min": 31.0, "outflow_per_min": 28.0, "net_flow": 3.0,
   "ts": 1785136653.2}
]}
```

Returns `{"zones": []}` when no zones are drawn — that is normal, not an error.
Fall back to `people_in_view` from `/cameras` for a headcount.

`status` is `NORMAL` | `WARNING` | `CRITICAL`. It is semantic, not a colour:
NORMAL is deliberately un-coloured in our UI so amber and red carry the urgency.

### Cross-camera people counts

```
GET /api/v1/identity/counts
```
```json
{"live": 4, "unique_total": 93, "cross_camera": 16,
 "per_camera": [{"camera_id": "CAM-01", "live": 2, "unique": 23}],
 "ts": 1785306062.3}
```

* `live` — distinct people on site right now, de-duplicated across cameras
* `unique_total` — distinct people since the service started (footfall)
* `cross_camera` — how many of those were seen by more than one camera

**`sum(per_camera.unique)` deliberately exceeds `unique_total`.** Someone seen by
two cameras counts once site-wide but once per camera. The difference is the
double-counting removed. It is an inequality, not an equality:
`sum − unique_total >= cross_camera` (someone on three cameras adds 2 to the
difference but 1 to `cross_camera`).

**`unique_total` resets to 0 when the CCTV service restarts.** Treat a decrease
as a session boundary, not bad data, and do not compute deltas across it.

### Active alerts

```
GET /api/v1/alerts
```
```json
{"alerts": [
  {"alert_id": "1042", "rule_id": "R-06", "severity": "RED",
   "message": "restricted zone ZONE-02 entered", "zone_id": "ZONE-02",
   "camera_id": "CAM-01", "status": "OPEN", "ts": 1785136653.2,
   "frame": "/bookmarks/bm_CAM-01_00012.jpg"}
]}
```

Active feed = `OPEN` + `ACK`. Resolved and dismissed drop out. `frame` is present
only for critical density (R-02) and restricted intrusion (R-06).

---

## 4. History and reports

```
GET /api/v1/history/events?from=<epoch>&to=<epoch>&limit=500
        &camera_id=&zone_id=&event_type=&person_ref=
GET /api/v1/history/alerts?from=<epoch>&to=<epoch>&limit=500
        &camera_id=&rule_id=
GET /api/v1/reports/occupancy.json?from=<epoch>&to=<epoch>&camera_id=&zone_id=
GET /api/v1/reports/occupancy.csv?from=<epoch>&to=<epoch>
GET /api/v1/movement?minutes=15&camera_id=      # zone-to-zone transition counts
```

All timestamps are **Unix epoch seconds as floats**, UTC. `from`/`to` are query
parameters spelled exactly that way.

---

## 5. Acting on alerts

```
POST /api/v1/alerts/{alert_id}/ack
{"acknowledged_by": "operator@finblade"}
-> 200 {"acknowledged": true, "alert_id": "1042", ...}

POST /api/v1/alerts/{alert_id}/resolve
{"action": "RESOLVED"|"DISMISSED", "resolved_by": "operator@finblade",
 "note": "security dispatched"}
-> 200 {"ok": true, "status": "RESOLVED", ...}
```

**409 is not a failure — do not retry it.** Repeating an action returns 409, and
so does an unknown `alert_id`; the two are indistinguishable from the response.
The effect is idempotent, but a client that retries on any non-2xx will retry
forever. Treat 409 as terminal.

---

## 6. Live video

```
GET /api/v1/cameras/{camera_id}/stream
```

`multipart/x-mixed-replace; boundary=frame` — MJPEG, renderable directly in an
`<img>`:

```html
<img src="http://cctv-host:8000/api/v1/cameras/CAM-01/stream?key=YOUR_KEY">
```

This is the **only** route accepting `?key=`, because a browser cannot attach
headers to an image request. From server-side code, use the header instead.

Overlay toggles pass through as query params — `?zones=0&ids=0&boxes=0&feet=0&dwell=0&gid=0`.

Two things to know before embedding it:

* **Bandwidth.** Frames are re-encoded as JPEG, roughly 10x the size of the
  equivalent H.265. At 1080p/20fps that is 20-40 Mbit/s **per viewer**. Fine on a
  LAN, unusable over a WAN. For remote viewing, ask us to reduce the frame rate
  or downscale before encoding.
* **One request per viewer.** There is no fan-out; ten dashboards mean ten
  encodes.

---

## 7. Identity (cross-camera)

```
GET /api/v1/identity/list?cross_camera_only=true&limit=200
GET /api/v1/identity/{global_ref}          # journey: cameras + zones in order
GET /api/v1/identity/stats                 # matcher counters and config
```

`global_ref` looks like `gp_4dbe3172855a94b4`.

**It is not a stable identifier.** It is a salted hash regenerated at every
service restart, deliberately, so the data stays non-identifying. Do not use it
as a database key, build person profiles on it, or correlate across days. It is
valid only within one continuous session.

Watch two counters in `/identity/stats`:

* `unknown_pair` above 0 — the camera topology file does not cover the cameras
  actually running, so cross-camera matching is degraded
* `rejected_margin` climbing fast — embeddings are too weak, usually low
  source resolution

---

## 8. Operational notes

**Poll no faster than every 5 seconds.** Zone state and people counts are
*computed* on a 5s cadence. Polling at 1s returns the same numbers four times
and wastes both ends. There is nothing fresher to get.

**Expect and handle these:**

* `{"zones": []}` — no zones drawn. Normal. Fall back to `people_in_view`.
* `unique_total` going down — the CCTV service restarted.
* Camera `stream_url` changing between calls — ports are assigned dynamically
  from 8090. Never cache or hard-code it; read it from `/cameras`, or just use
  `/api/v1/cameras/{id}/stream`, which is stable.
* `409` on ack/resolve — already in that state, or unknown. Terminal.
* `401` — key missing, wrong, or you used `?key=` on a non-stream route.

**Clock.** All timestamps come from the CCTV host's clock. Within one camera
they are ordered; across cameras they can be a few seconds out, because each
camera runs as an independent process. Do not assume a global ordering.

---

## 9. Quick start

```bash
export KEY=your-api-key
export CCTV=http://cctv-host:8000

curl -s -H "Authorization: Bearer $KEY" $CCTV/api/v1/cameras          | jq
curl -s -H "Authorization: Bearer $KEY" $CCTV/api/v1/identity/counts  | jq
curl -s -H "Authorization: Bearer $KEY" $CCTV/api/v1/zones/state      | jq
curl -s -H "Authorization: Bearer $KEY" $CCTV/api/v1/alerts           | jq
```

If the first returns `401`, the key is wrong. If it returns an empty camera
list, none are registered yet — that is a CCTV-side configuration matter, not an
API problem.

Full machine-readable schema: `GET /openapi.json` (no key required).
