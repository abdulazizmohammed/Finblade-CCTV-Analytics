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

**`?key=` in the query string works on exactly two routes** — the video stream
(§6) and the WebSocket (§3b). It is rejected everywhere else. A key in a URL
leaks into access logs, browser history and `Referer` headers, so it exists only
where a browser primitive physically cannot send a header: an `<img>` element,
and the `WebSocket` constructor.

From server-side code, always use the header form. `?key=` is there for browsers,
not for convenience.

The dashboard pages themselves (`/web`, `/tools`) load without a key; they then
prompt for it. Do not treat that as a way in — every data route is gated.

---

## 2b. Calling this API from another app or host

**CORS is already open.** `allow_origins=["*"]`, all methods, all headers. A
browser app on any origin can call every route without a server-side change, and
the `Authorization` header passes through. Nothing to request from us here.

**Network.** The API listens on **TCP 8000**. Give us the egress IP or CIDR your
app calls from and we will allow it; the port is deliberately not open to
`0.0.0.0/0`, because these responses include live video of people.

**If your app is served over HTTPS, browser calls to this API will be blocked**
before they leave the page — not by CORS and not by our auth, but by mixed-content
policy: an `https://` page cannot fetch `http://`. The console says the request
was blocked, which reads like a network fault and is not one.

Three ways out, in order of how little work they are:

* **Call us from your backend instead of the browser.** Server-to-server has no
  mixed-content rule, and it keeps the API key off the client — where it would
  otherwise be readable by anyone who opens devtools. Recommended regardless.
* **We put TLS in front of the API** (ALB + ACM certificate, or Caddy/nginx on
  the host). Note this needs a domain we control: a public CA will not issue for
  an `ec2-*.compute-1.amazonaws.com` hostname, so this is not a same-afternoon
  change.
* **Serve your app over HTTP too.** Works, but the wrong direction for anything
  going to production.

**WebSocket scheme must follow the page.** An `https://` page cannot open a
`ws://` socket; it needs `wss://`, which again depends on TLS being terminated in
front of us. The dashboard already switches automatically based on
`location.protocol`.

**Quick check from your own machine**, before wiring anything up:

```bash
KEY=your-api-key
CCTV=http://<cctv-host>:8000

curl -s -o /dev/null -w 'with key:    %{http_code}\n' -H "Authorization: Bearer $KEY" $CCTV/api/v1/cameras
curl -s -o /dev/null -w 'without key: %{http_code}\n'                                 $CCTV/api/v1/cameras
```

**200 then 401** means auth and reachability are both correct. A hang or timeout
on the first is a firewall or security-group matter, not a key problem — the two
fail differently and it is worth telling them apart before asking us.

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
is tracking. `people_in_zones` counts only those inside a drawn polygon.

**Do not sum `people_in_view` across cameras and call it site occupancy.** It
double-counts anyone visible to two cameras at once, and it includes detections
anywhere in the frame, including off the monitored floor. Our own dashboard was
doing this and reported 5–7 people while three were in the building.

For a site total, pick one deliberately:

| you want | use | caveat |
|---|---|---|
| people on monitored floor | sum of `occupancy` from `/zones/state` | reads 0 where no polygon is drawn |
| distinct humans on site | `live` from `/identity/counts` | depends on ReID accuracy — see §7 |
| raw per-camera activity | `people_in_view`, **per camera** | never summed across cameras |

We headline zone occupancy. A person is counted where they are standing, not
where a camera happens to see them.

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
It means occupancy **cannot be computed**, which is not the same as zero people;
do not render it as an empty site.

`status` is `NORMAL` | `WARNING` | `CRITICAL`. It is semantic, not a colour:
NORMAL is deliberately un-coloured in our UI so amber and red carry the urgency.

Full field list, captured live: `zone_id`, `camera_id`, `zone_name`, `zone_type`,
`restricted`, `ts`, `occupancy`, `density`, `capacity_pct`, `peak_occupancy`,
`avg_occupancy`, `trend`, `inflow_per_min`, `outflow_per_min`, `net_flow`,
`inflow_5m`, `outflow_5m`, `inflow_15m`, `outflow_15m`, `status`, `capacity_max`,
`area_sqm`.

### Zone definitions — the polygons themselves

`/zones/state` is live measurement. For the static configuration — shape,
capacity, thresholds — read:

```
GET /api/v1/zones            # optional ?camera_id=CAM-04
```
```json
{"zones": [
  {"camera_id": "CAM-03", "zone_id": "ZONE-01", "zone_name": "Lobby",
   "zone_type": "ENTRANCE", "restricted": false,
   "capacity_max": 40, "area_sqm": 60.0,
   "warning_density": 2.0, "critical_density": 4.0,
   "loitering_threshold_sec": 30.0, "colour": "#4fdce0", "enabled": true,
   "normalized_polygon": [[0.38281, 0.42222], [0.19531, 0.56111],
                          [0.31406, 0.98056], [0.88906, 0.91389]],
   "polygon": [], "adjacency_list": [], "updated_at": 1785389847.2}
]}
```

Two things about this payload:

* **`normalized_polygon` is what you want.** Coordinates are fractions of frame
  width/height (0.0–1.0), so they survive a resolution change. `polygon` holds
  absolute pixels and is often empty — do not read it as "no zone".
* **`zone_id` is only unique per camera.** `ZONE-01` exists on CAM-03 *and*
  CAM-04 as different areas. Key on `(camera_id, zone_id)`, never `zone_id`
  alone.

`zone_type` is `MONITORED` | `ENTRANCE` | `RESTRICTED` | `UNMONITORED`.
`UNMONITORED` is a detection mask: everything inside it is discarded before
tracking, so it will never appear in occupancy.

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
  {"alert_id": "6960", "rule_id": "R-05", "severity": "AMBER",
   "message": "loitering 30s in ZONE-01", "zone_id": "ZONE-01",
   "camera_id": "CAM-04", "person_ref": "pr_b5e7403742d720aa",
   "ts": 1785394128.9, "frame": null, "kind": "FIRE",
   "status": "OPEN", "acknowledged_by": null, "acknowledged_at": null,
   "note": null, "resolved_by": null, "resolved_at": null}
]}
```

Active feed = `OPEN` + `ACK`. Resolved and dismissed drop out. `frame` is present
only for critical density (R-02) and restricted intrusion (R-06) — it is `null`
on everything else, so guard before building an `<img>`.

`severity` is `AMBER` | `RED` | `INFO`; `status` is `OPEN` | `ACK` | `RESOLVED` |
`DISMISSED`. Note these are **different vocabularies** from zone `status`
(`NORMAL`/`WARNING`/`CRITICAL`) — a zone describes a condition, an alert
describes an incident, and mapping one onto the other loses meaning.

`person_ref` is an anonymous per-session hash, present on person-scoped rules
like R-05 loitering. It carries no PII and, like `global_ref`, does not survive a
service restart.

Rules you will see: **R-01** amber density, **R-02** red density, **R-03**
capacity ≥90%, **R-05** loitering, **R-06** restricted-zone entry (immediate),
**R-07** camera offline >30s, **R-08** occupancy report.

---

## 3b. Real-time push — WebSocket

For a live dashboard, do not poll. Connect once:

```
ws://<cctv-host>:8000/ws            # wss:// if TLS is terminated in front of us
```

A browser cannot set headers on a WebSocket, so the key goes in the query string
— this is the second and last route where that is accepted:

```javascript
const ws = new WebSocket(`ws://cctv-host:8000/ws?key=${YOUR_KEY}`);
ws.onmessage = e => render(JSON.parse(e.data));
```

From server-side code, send `Authorization: Bearer` on the handshake instead.

**Without a valid key the socket is closed with code 1008** (policy violation) —
not left hanging. Treat 1008 as "fix your credentials", not as a transient drop
to retry.

Each message is a **complete snapshot**, roughly twice a second — not a delta, so
there is no state to reconstruct and a missed message costs nothing:

```json
{
  "cameras": [ ... same shape as GET /api/v1/cameras ... ],
  "zones":   [ ... same shape as GET /api/v1/zones/state ... ],
  "alerts":  [ ... same shape as GET /api/v1/alerts ... ],
  "ts": 1785394227.13
}
```

`cameras`, `zones` and `alerts` are built by the same code paths as the REST
routes, so the push and a poll cannot disagree.

**Always keep the REST fallback.** Our own dashboard degrades to 5s polling of
the three GET endpoints when the socket drops, and reconnects in the background.
A WebSocket through a corporate proxy is not a given.

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

### Single frame instead — use this over a WAN

```
GET /api/v1/cameras/{camera_id}/snapshot
```

Returns one annotated JPEG and closes. This is the right choice for anything
remote, for a thumbnail grid, or for attaching an image to an alert record. Poll
it at whatever rate you need; a 1–5s refresh costs a tiny fraction of the MJPEG
stream and degrades gracefully on a bad link.

**Prefer this to `/stream` unless you are on the same LAN.** A wall of MJPEG
tiles over a VPN is the single easiest way to saturate the link.

Per-camera MJPEG servers also exist on ports 8090+, one per camera. **Do not use
them.** They are internal, the port assignment changes when cameras restart, and
they are not exposed on any deployment where only 8000 is open. Always go through
`/api/v1/cameras/{id}/stream`.

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

**Match your poll rate to what actually changes.** The two live measures update
on different cadences:

| data | refreshed | poll no faster than |
|---|---|---|
| camera health, `people_in_view` | ~1s | 1s |
| zone occupancy, density, flow | **5s aggregate** | 5s |
| alerts | on rule fire | 5s |

Polling zone state at 1s returns the same numbers five times — that window is a
specified aggregation period, not a refresh rate, and there is nothing fresher to
get. **Or use the WebSocket (§3b) and stop polling**, which is what we do.

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

Full machine-readable schema: `GET /openapi.json` (no key required). Generate a
client from it rather than hand-writing one.

---

## 10. Complete route index

Every route the service exposes, so nothing here is a surprise. **35 routes**;
you need about ten of them.

### Read these

| route | §
|---|---|
| `GET /api/v1/cameras` | 3 |
| `GET /api/v1/zones` · `GET /api/v1/zones/state` | 3 |
| `GET /api/v1/alerts` | 3 |
| `GET /api/v1/identity/counts` · `list` · `stats` · `/{global_ref}` | 3, 7 |
| `GET /api/v1/history/events` · `history/alerts` | 4 |
| `GET /api/v1/reports/occupancy.json` · `.csv` · `/reports` · `/reports/{id}` | 4 |
| `GET /api/v1/movement` | 4 |
| `GET /api/v1/cameras/{id}/stream` · `/snapshot` | 6 |
| `WS  /ws` | 3b |
| `GET /openapi.json` | 9 |

`GET /api/v1/finblade/status` is also readable — it reports whether we are
successfully pushing to you, with per-stream counters and `last_error`. Useful
when reconciling: if your ingest looks quiet, check here before assuming the
cameras are down.

### Act on these

| route | notes |
|---|---|
| `POST /api/v1/alerts/{id}/ack` | §5 |
| `POST /api/v1/alerts/{id}/resolve` | §5 — **409 is terminal, do not retry** |

### Do NOT call these

They exist for the camera workers, the operator UI and demo tooling. Several are
destructive and none are rate-limited; the API key that grants you read access
grants these too, so this list is the only thing standing between an integration
bug and a wiped deployment.

| route | why not |
|---|---|
| `POST /api/v1/events/ingest`, `POST /api/v1/zones/state`, `POST /api/v1/cameras/health`, `POST /api/v1/alerts` | worker→API ingest. Writing here fabricates measurements. |
| `POST /api/v1/cameras`, `DELETE /api/v1/cameras/{id}` | provisioning. Delete stops the pipeline and drops the row. |
| `POST /api/v1/cameras/{id}/start` · `/stop` | starts and stops live analytics. |
| `POST /api/v1/cameras/{id}/simulate-failure` · `/restore` | forces a camera OFFLINE to demo R-07. Will fire real alerts. |
| `DELETE /api/v1/alerts`, `DELETE /api/v1/frames/orphaned` | bulk deletion of alert history and snapshots. |
| `POST /api/v1/zones` | overwrites zone polygons. |
| `POST /api/v1/identity/resolve` · `release` · `merge`, `POST /api/v1/identity/tuning` | identity internals; tuning changes matching behaviour site-wide at runtime. |
| `POST /api/v1/finblade/flush`, `POST /api/v1/reports/generate` | forces a push / builds a report. Harmless but ours to trigger. |
| `GET /api/v1/reports/occupancy` | returns HTML for the operator UI. Use `.json`. |

If you need any of these exposed deliberately, ask — we would rather add a
scoped endpoint than have you drive the internal ones.

---

## 11. Known limits — read before you design around this

**Cross-camera identity is not validated on real two-camera footage.** The
matcher runs and is unit-tested, but its accuracy figure comes from a synthetic
second camera. Do not build anything that depends on `live` being exact across
cameras until we have measured it on your site. Watch `unknown_pair` in
`/identity/stats`: above 0 means the camera topology file does not cover the
cameras actually running.

**Seated people may not appear in zone occupancy.** Zone membership uses the
foot point — the bottom-centre of the bounding box. For someone sitting, that
lands on the seat, which may fall outside the polygon. A person can therefore be
counted in `people_in_view` and in no zone at all. If a seating area reads 0 with
people in it, this is why.

**Detection quality depends on the camera's stream.** On a 640×360 substream a
stationary person is detected in roughly half of frames; on the main stream the
same person scores far higher. Counts are smoothed over a short window to absorb
that, but a low-resolution feed produces a noisier number and no API change fixes
it.

**Counts reset when the service restarts.** `unique_total`, `global_ref` and
`person_ref` are all per-session. A decrease is a restart, not bad data.

**Timestamps come from the CCTV host clock** and each camera is an independent
process, so ordering holds within a camera but not across them.
