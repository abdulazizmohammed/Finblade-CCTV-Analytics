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

**`?key=` in the query string works on five routes only**, all read-only and all
loaded by a browser primitive that physically cannot send a header:

| route | why |
|---|---|
| `/api/v1/cameras/{id}/stream` (§6) | `<img src>` — MJPEG |
| `/api/v1/cameras/{id}/snapshot` (§6) | `<img src>` — single frame |
| `/ws` (§3b) | the `WebSocket` constructor takes a URL and nothing else |
| `/bookmarks/*` | saved incident frames, shown as `<img>` |
| `/media/*` | reference stills for the zone editor, shown as `<img>` |

It is rejected everywhere else, including on every JSON route. A key in a URL
leaks into access logs, browser history and `Referer` headers, so the exception
is kept to cases where there is no alternative.

From server-side code, always use the header form. `?key=` is there for browsers,
not for convenience.

The dashboard pages themselves (`/web`, `/tools`) load without a key; they then
prompt for it. Do not treat that as a way in — every data route is gated.

### Ask for the integration key, not the operator key

There are two keys. Yours should be the scoped one:

| | operator key | integration key |
|---|---|---|
| every `GET`, and `/ws` | ✅ | ✅ |
| `POST /alerts/{id}/ack` · `/resolve` | ✅ | ✅ |
| `DELETE /alerts`, `DELETE /cameras/{id}`, `DELETE /frames/orphaned` | ✅ | ❌ 403 |
| `POST /zones` (overwrites polygons) | ✅ | ❌ 403 |
| camera provisioning, start/stop, identity tuning | ✅ | ❌ 403 |

It covers everything in §10's "read these" and "act on these" lists, and nothing
in "do NOT call these" — so that list stops depending on your restraint.

**`401` and `403` mean different things.** `401` is a missing or wrong key.
`403` is a valid key on a route outside its scope. If you see `403`, the
credential is fine and the call is not — do not go hunting for a key bug.

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

### One call for a whole dashboard — start here

```
GET /api/v1/summary
```
```json
{
  "site_id": "SITE-01",
  "cameras": [ ... same shape as GET /api/v1/cameras ... ],
  "zones":   [ ... same shape as GET /api/v1/zones/state ... ],
  "alerts":  [ ... same shape as GET /api/v1/alerts ... ],
  "counts":  { ... same shape as GET /api/v1/identity/counts ... },
  "summary": {
    "people_in_zones": 12,
    "people_live": 14,
    "cameras": {"online": 11, "degraded": 1, "reconnecting": 0,
                "offline": 0, "disabled": 0},
    "zones":   {"normal": 8, "warning": 2, "critical": 1},
    "alerts":  {"open_total": 5, "amber": 3, "red": 1, "critical": 1, "info": 0}
  },
  "ts": 1785394227.13
}
```

The `summary` block is the same data pre-tallied, so a tile does not reduce
three arrays to render one number and every consumer counts the same way.

**`people_in_zones` is `null`, never `0`, when no polygons are drawn.**
Occupancy that cannot be computed is not an empty site. Fall back to
`counts.live`, or render "zones not configured".

`site_id` is `FINBLADE_SITE_ID` if configured, otherwise derived from the
cameras — and `null` if they disagree, because a wrong site label is worse than
an absent one for a platform routing records by it.

The `/ws` frame in REST form. Every section is built by the same code as the
individual route it mirrors, so a poller and the socket cannot disagree.

**Prefer it to calling the four separately.** Four calls per refresh is four
round trips and four different instants stitched into one render — the camera
count and the zone occupancy on screen come from moments up to a second apart.

Two differences from `GET /api/v1/cameras`, both deliberate:

* **`stream_url` is absent.** It points at an internal per-camera MJPEG server
  on 8090+, whose port is reassigned when a camera restarts and which is not
  exposed wherever only 8000 is open. A remote consumer that reads it gets a URL
  that works in testing and fails intermittently in production.
* **`snapshot_path` and `stream_path` are present** — relative and stable, and
  the correct things to proxy. Relative because your proxy, not this host,
  decides the public origin.

Poll it at 1s if you want camera counts fresh; `zones` inside it is still a
5-second aggregate and cannot be fresher (§8). Or use the WebSocket (§3b).

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

**This endpoint is unaffected by write-on-change** (below). The camera still
posts every 5 seconds and this reading still advances every 5 seconds, whether
or not anything changed. Only the stored history is thinned.

### Write-on-change: what the history contains

The zone-state history (`zone_state_ts`, behind the reports and any time-range
query) now records a row when a zone's **occupancy or status changes**, plus a
keepalive row every 5 minutes per zone. It used to record every 5-second post.

Measured on nine days of our own data: 1,674,955 rows become 7,243 — 1.15 GB
becomes about 5 MB. 99.7% of those rows were byte-identical to the row before
them, and 97.7% recorded an empty zone.

What this does and does not change for you:

| | Before | Now |
|---|---|---|
| `/zones/state`, `/summary`, `/ws` | every 5s | **unchanged, every 5s** |
| `ZONE_ENTRY` / `ZONE_EXIT` / alerts | per person, immediate | **unchanged** |
| Rows in the stored history | 720/hour/zone | ~12/hour/zone when quiet |
| Report averages | mean of rows | **time-weighted by duration** |

Two consequences worth reading before you build against it:

**A gap in the history means "nothing changed", not "no data".** To reconstruct
occupancy at an arbitrary past instant, take the most recent row **at or before**
that instant and hold it forward. Do not interpolate, and do not treat a missing
row as zero. If you need this, ask us for `/api/v1/zones/{zone_id}/series` —
it is on the roadmap specifically so you do not have to implement the hold
yourself.

**Do not average the rows.** They no longer cover equal time, so a mean over
rows over-weights busy periods — a quiet hour writes 12 rows and a busy minute
writes one. The reports already do this correctly; see `coverage` below.

Both of these are why `/zones/{zone_id}/series` exists (§4b) — it does the hold
and the weighting for you, and reports where the holes are.

A sub-5-second visit is carried by `ZONE_ENTRY`/`ZONE_EXIT`, not by this table,
and always was — a 5-second sampler cannot see anything shorter than 5 seconds.
Those events are per person, fire on the frame they happen, and are untouched.

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

### Filtering and one alert by id

`/alerts` and `/zones/state` take optional filters; omitting them returns
everything, exactly as before.

```
GET /api/v1/alerts?severity=RED&status=OPEN&zone_id=&camera_id=&rule_id=&site_id=
GET /api/v1/zones/state?camera_id=&zone_id=&site_id=
GET /api/v1/alerts/{alert_id}          # open OR closed
```

Filters are case-insensitive. `/alerts/{id}` is how you follow up on an alert
you were pushed — scanning the active feed misses it the moment an operator
resolves it out of the feed. It adds `frame_url` when an incident image exists.

### Health — which part is broken

```
GET /healthz          open, liveness only
GET /readyz           open, terse readiness
GET /api/v1/health    keyed, full breakdown
```
```json
{"healthy": true, "site_id": "SITE-01", "checks": {
  "store": {"ok": true},
  "cameras": {"ok": true, "total": 12, "online": 11, "offline": 0},
  "forwarder": {"ok": true, "enabled": true, "last_error": null,
                "seconds_since_success": 3.2},
  "report_scheduler": {"ok": true, "errors": 0},
  "offline_monitor": {"ok": true, "errors": 0},
  "state_writes": {"ok": true, "mode": "change", "keepalive_s": 300.0,
                   "zones_tracked": 8, "written": 7243,
                   "suppressed": 1667712, "suppressed_pct": 99.57}}}
```

Use this to tell **CCTV analytics down** from **one camera down** from **the
push to FinBlade failing** — three problems with three different owners. A
disabled forwarder reports `ok: true`; off is not broken.

`state_writes` reports the write-on-change gate. It is never `ok: false` —
suppression is the intended behaviour — but a `suppressed_pct` of 100 over a
long run means no history is being recorded at all, and that is worth an alert
on your side.

---

### Chart tags — these endpoints describe how to draw themselves

Implemented to `live-feed-chart-tags.md` schema 1. Six endpoints add a
top-level `finblade` key so a tile renders without anyone mapping fields by
hand:

| endpoint | charts offered |
|---|---|
| `/api/v1/zones/state` | `zone_occupancy`, `zone_density`, `zone_flow`, `occupancy_share` |
| `/api/v1/summary` | `people_on_site`, `people_live`, `open_alerts`, `alerts_by_severity`, `camera_health`, `camera_people` |
| `/api/v1/identity/counts` | `live_now`, `footfall_total`, `cross_camera`, `live_per_camera` |
| `/api/v1/movement` | `zone_transitions` |
| `/api/v1/reports/occupancy.json` | `occupancy_peak_avg` |

```json
{
  "zones": [ ... unchanged ... ],
  "finblade": {
    "schema": 1,
    "charts": [
      {"id": "zone_occupancy", "type": "bar", "title": "Occupancy by zone",
       "unit": "people", "labels": ["Lobby", "Reception", "1F-Passage"],
       "datasets": [{"label": "Occupancy", "data": [4, 2, 0]}]}
    ]
  }
}
```

**Purely additive** — every existing key keeps its meaning, and `?charts=0`
omits the block entirely. It costs about 750 bytes per response.

Three behaviours worth knowing, because they are deliberate:

* **A number that cannot be computed is omitted, not sent as 0.** With no zone
  polygons drawn there is no `people_on_site` metric at all, rather than a
  metric reading zero. A measured zero *is* sent.
* **No chart is ever a sum of `people_in_view`.** `camera_people` and
  `live_per_camera` are one bar per camera and say so in their titles; adding
  them up double-counts anyone two cameras can see.
* **Zone labels gain a camera prefix as soon as two cameras are present**
  (`CAM-01 / Lobby`), because `zone_id` is unique only within a camera and two
  bars would otherwise carry the same name.

An all-zero pie is not sent either — three slices of nothing reads as a fault.

---

## 4. History and reports

```
GET /api/v1/history/events?from=<epoch>&to=<epoch>&limit=500&offset=0
        &camera_id=&zone_id=&event_type=&person_ref=&site_id=
GET /api/v1/history/alerts?from=<epoch>&to=<epoch>&limit=500&offset=0
        &camera_id=&rule_id=&severity=&status=&zone_id=&site_id=
GET /api/v1/reports/occupancy.json?from=<epoch>&to=<epoch>&camera_id=&zone_id=
GET /api/v1/reports/occupancy.csv?from=<epoch>&to=<epoch>
GET /api/v1/movement?minutes=15&camera_id=      # zone-to-zone transition counts
```

All timestamps are **Unix epoch seconds as floats**, UTC. `from`/`to` are query
parameters spelled exactly that way.

**Pagination.** Both history routes return a `page` block alongside the array:

```json
{"alerts": [ ... ], "page": {"limit": 500, "offset": 0, "returned": 500,
                             "has_more": true}}
```

`has_more` is derived by reading one row past the window, so it is a fact, not
an inference from `returned == limit`. Page with `offset`. Without this a full
page and the end of the data look identical, which is how a report quietly
loses a day.

`/movement` accepts `from`/`to` as well as `minutes` — `minutes` counts back
from now and cannot express "last Tuesday".

## 4b. Zone history — three questions, three endpoints

These read the sparse history correctly so you do not have to. Each one holds
each stored reading forward to the next, excludes time the camera could not
observe, and tells you where the holes are.

```
GET /api/v1/zones/{zone_id}/series?from=&to=&hours=&bucket=300&camera_id=
GET /api/v1/zones/{zone_id}/at?ts=&camera_id=
GET /api/v1/zones/{zone_id}/duration?from=&to=&hours=&camera_id=
        &field=occupancy&op=gt&value=0        # or &status=WARNING
```

**`zone_id` alone may be ambiguous.** Every camera numbers its zones from
ZONE-01, so a bare id can name several unrelated areas. These routes return
**409** listing the candidates rather than picking one — pass `camera_id`.
A zone that has never existed is 404.

### `/series` — how it changed over time

```json
{"zone_id": "ZONE-01", "camera_id": "CAM-01",
 "from": 1786069197.2, "to": 1786083597.2,
 "bucket_seconds": 600, "requested_bucket_seconds": 600,
 "bucket_adjusted": false, "rows_in_window": 44, "coverage": 0.9167,
 "gaps": [{"from": 1786079997.2, "to": 1786081197.2, "seconds": 1200.0,
           "reason": "camera_offline"}],
 "points": [
   {"from": 1786069200.0, "to": 1786069800.0, "occupancy": 0.0,
    "density": 0.0, "capacity_pct": 0.0, "peak_occupancy": 0,
    "coverage": 1.0, "samples": 2},
   {"from": 1786080000.0, "to": 1786080600.0, "occupancy": null,
    "density": null, "capacity_pct": null, "peak_occupancy": null,
    "coverage": 0.0, "samples": 0}
 ]}
```

* **`null` means the camera was not observing. It is not zero.** Break the
  line; do not draw an empty room. A bucket's own `coverage` says how much of
  it was seen — a partially observed bucket carries a real value for the part
  the camera did see.
* `gaps[].reason` is `camera_offline` when the outage was logged, or `no_data`
  when a camera stopped reporting without saying so (a killed worker announces
  nothing). They need different responses from whoever is on call.
* `peak_occupancy` per bucket, because a ten-minute mean of 0.1 still means
  someone walked through — use it when the question is "was anyone there".
* **Buckets align to the epoch**, not to your `from`. Two requests a minute
  apart return the same bucket boundaries, so a refresh cannot contradict what
  the user just saw. The first bucket therefore usually starts slightly before
  `from`.
* **Read `bucket_seconds` rather than assuming yours was used.** A window that
  would exceed 1000 buckets is coarsened, and `bucket_adjusted` says so.
  1000 is also the chart-tag point cap, so the JSON and the chart never
  disagree.
* Carries a chart tag (`zone_occupancy_series`) whose data preserves the nulls.

### `/at` — what it read at one moment

```json
{"zone_id": "ZONE-01", "camera_id": "CAM-01", "at": 1786078197.2,
 "state": {"occupancy": 6, "density": 0.5, "status": "NORMAL",
           "ts": 1786077897.2, "age_seconds": 300.0, "stale": false},
 "camera_offline": false, "trustworthy": true}
```

Returns the last reading **at or before** the instant, never the next one —
under write-on-change the next row can be hours later, and using it would
report a state from a time that had not happened yet.

`age_seconds` is how old that reading was at the instant you asked about.
`trustworthy: false` means it had outlived what one reading may stand for, or
the camera was logged offline: phrase it as "the last reading, from N minutes
earlier", not as fact. `"state": null` means nothing was recorded at or before
that time — say that, do not render zero.

### `/duration` — how long a condition held

```json
{"zone_id": "ZONE-01", "camera_id": "CAM-01",
 "condition": {"field": "occupancy", "op": "gt", "value": 0},
 "total_seconds": 3600.0, "episode_count": 1, "longest_seconds": 3600.0,
 "episodes": [{"from": 1786076397.2, "to": 1786079997.2, "seconds": 3600.0}],
 "unobserved_seconds": 1200.0, "coverage": 0.9167}
```

Answers in **seconds of real time**, not row counts. Time the camera could not
see is excluded from the total and splits an episode in two rather than
bridging it — a camera down for four hours must never be reported as a
four-hour breach. When `unobserved_seconds` is non-zero the honest phrasing is
"at least N minutes, and M minutes were not observed".

`field` is `occupancy` | `density` | `capacity_pct`; `op` is `gt` | `gte` |
`lt` | `lte` | `eq`. Or give `status=NORMAL|WARNING|CRITICAL` instead. Both
sets are closed and a name outside them is a **422** — zero seconds from a
typo is indistinguishable from a real answer of "never".

---

### Report averages are time-weighted, and carry `coverage`

Each zone in `occupancy.json` now looks like this:

```json
{"zone_id": "ZONE-01", "zone_name": "Lobby", "samples": 13,
 "avg_occupancy": 0.082, "peak_occupancy": 5,
 "avg_density": 0.0068, "avg_capacity_pct": 0.33,
 "coverage": 1.0,
 "sampled": {"avg_occupancy": 0.385, "avg_density": 0.032,
             "avg_capacity_pct": 1.54},
 "time_weighted": {"avg_occupancy": 0.082, "avg_density": 0.0068,
                   "avg_capacity_pct": 0.33, "peak_occupancy": 5,
                   "coverage": 1.0, "observed_seconds": 3660.0},
 "alert_count": 0}
```

`avg_occupancy` and the other averages are now **weighted by how long each
reading held**, not by how many rows carry it. In the example above the zone
held five people for one minute of an hour: 0.082 is right, and 0.385 — the
mean of the surviving rows — is what the old arithmetic gave. That gap is the
whole reason for the change; it widens as the history gets sparser.

* `sampled` is the old row-mean, kept so you can compare across the change.
  Reports generated before this release contain only that number.
* `coverage` is the fraction of the window the camera was actually observing,
  0.0–1.0. **Render it whenever it is below about 0.95.** An average over four
  hours of a twenty-four-hour window is a real number about a small slice of the
  day, and presenting it unqualified is the misreading this field exists to
  prevent. `samples` no longer tells you this — under write-on-change, a low row
  count means a quiet zone, not a missing one.
* `observed_seconds` is the same figure in absolute terms.
* Downtime is excluded from the average rather than counted as empty, so a
  camera that was down for half a window describes the half it saw.

`peak_occupancy` is a maximum, not an average, and is unchanged.

Each zone also carries `gaps` in the same shape as `/series` — `coverage: 0.4`
does not say whether a camera was down once overnight or flapping all day, and
those support different conclusions from the same average.

`totals.min_coverage` is the **worst** zone's coverage, not the mean. A report
whose zones range from 1.00 to 0.05 is not "52% observed" — one camera was
down, and anything drawn from it is unsafe in a way an average would hide.

`occupancy.csv` gains a `Coverage` column, positioned next to the averages for
the same reason.

**One zone per camera per row.** Previously the report grouped on `zone_id`
alone, which merged two cameras' unrelated ZONE-01s into a single row whose
averages spanned both physical areas. Rows are now keyed on
`(camera_id, zone_id)`, so a site with the same zone ids on several cameras
will see more rows than before — that is the fix, not a regression.

### Incident images

```
GET /api/v1/incidents/{alert_id}/frame
```

The frame captured **when the incident happened**, by alert id. Prefer it to
both `/bookmarks/<file>` (a filesystem-shaped path, not a contract) and the
live camera snapshot — for an alert raised twenty minutes ago, a live snapshot
is a different scene wearing the incident's label. Present for R-02 and R-06
only; `404` with a reason otherwise.

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

Every route the service exposes, so nothing here is a surprise. **49 routes
under `/api/v1`**, plus `/ws`, `/healthz`, `/readyz`, `/openapi.json` and the
static UI. You need about a dozen of them.

(Counted with `scripts/count_routes.py` rather than by hand — the figure here
had drifted to 41 while the service grew past it.)

### Read these

| route | what it is for | § |
|---|---|---|
| `GET /api/v1/summary` | everything a dashboard needs, one call, one instant | 3 |
| `GET /api/v1/cameras` | per-camera health, fps, resolution, people in view | 3 |
| `GET /api/v1/zones` | zone definitions: polygon, capacity, thresholds | 3 |
| `GET /api/v1/zones/state` | live occupancy, density, flow, status | 3 |
| `GET /api/v1/alerts` | the active feed (OPEN + ACK), filterable | 3 |
| `GET /api/v1/alerts/{id}` | one alert, open or closed | 3 |
| `GET /api/v1/identity/counts` · `list` · `stats` · `/{global_ref}` | distinct people, de-duplicated across cameras | 3, 7 |
| `GET /api/v1/history/events` · `history/alerts` | ranged history, paginated | 4 |
| `GET /api/v1/zones/{id}/series` | bucketed history, gap-filled, with `coverage` and `gaps` | 4b |
| `GET /api/v1/zones/{id}/at` | what a zone read at one instant | 4b |
| `GET /api/v1/zones/{id}/duration` | how long a condition held, in seconds | 4b |
| `GET /api/v1/reports/occupancy.json` · `.csv` · `/reports` · `/reports/{id}` | occupancy reporting | 4 |
| `GET /api/v1/movement` | zone-to-zone transition counts | 4 |
| `GET /api/v1/cameras/{id}/snapshot` | one annotated JPEG — **prefer this over a WAN** | 6 |
| `GET /api/v1/cameras/{id}/stream` | annotated MJPEG — LAN only, 20–40 Mbit/s per viewer | 6 |
| `GET /api/v1/incidents/{alert_id}/frame` | the frame from **when** an incident happened | 4 |
| `WS  /ws` | complete snapshot ~2×/second | 3b |
| `GET /healthz` · `/readyz` | liveness / readiness, no key needed | 3 |
| `GET /api/v1/health` | which dependency is broken | 3 |
| `GET /openapi.json` | machine-readable schema, no key needed | 9 |

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
destructive and none are rate-limited.

**On the integration key (§2) these return `403` and this list is enforced, not
advisory.** On the operator key it is advisory only — that key grants all of
them, and the list is then the only thing between an integration bug and a wiped
deployment. Use the integration key.

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
