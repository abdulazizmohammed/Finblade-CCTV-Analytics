# FinBlade CCTV — capabilities

What this system can actually do, as of the last update below. One entry per
capability, with the file that implements it so a claim here can be checked
against code rather than taken on trust.

**Last updated:** 2026-09-01

---

## How to keep this current

**Update this file in the same commit that adds or removes a capability.** Not
afterwards — a capabilities list that lags the code is worse than none, because
it is trusted and wrong. If you add an endpoint, an event type, a rule, a
source type or a stream, it belongs here before the commit lands.

Three rules for what goes in:

1. **Say what it does, not what it is for.** "Publishes merged occupancy counts
   to a Redis stream" — not "provides observability".
2. **Cite the file.** Every row names the module that implements it.
3. **Status is honest, and "runs" is not "correct".** See the markers below.

| Marker | Meaning |
|---|---|
| **Built** | Implemented, tested, and exercised against a live dependency where one exists |
| **Runs** | Executes end to end, but correctness depends on human visual judgement (see below) |
| **Partial** | Implemented for some cases; the gaps are named |
| **Planned** | Designed and agreed, not implemented |
| **Cut** | Deliberately not built — see [CLAUDE.md](../CLAUDE.md) |

### The "Runs" marker exists for a reason

Nothing automated in this repo can tell whether a bounding box is on a person or
whether a zone polygon sits on the floor. Anything downstream of the detector
inherits that. Those capabilities are marked **Runs** and are qualified with what
was measured (frames processed, detections per frame) rather than asserted as
correct. Evidence for human review is written to `evidence/`. See the PRIME
DIRECTIVE in [CLAUDE.md](../CLAUDE.md). The manual checks that cover those
capabilities are `TC-M-01`..`TC-M-10` in [TEST_CASES.md](TEST_CASES.md).

---

## 1. Video ingest and detection

| Capability | Status | Implementation |
|---|---|---|
| RTSP and file video decode, loop playback, reconnect | Runs | `services/inference/run_cpu.py` |
| YOLO person detection (COCO class 0) | Runs | `run_cpu.py`, `models/yolo11s.pt`, CUDA with CPU fallback |
| ByteTrack persistent per-camera track IDs | Runs | Ultralytics built-in tracker |
| Annotated MJPEG preview per camera | Runs | one HTTP port per worker, proxied by the API |
| Detection masking via `UNMONITORED` zones | Built | `finblade/zones.py` `in_ignored_region` |
| Frame-skip to a target process FPS | Built | `process_fps` in the camera config |
| Tracking-quality regime reporting (RELIABLE / STRAINED / SATURATED) | Built | `finblade/crowding.py` |

One OS process per camera, launched and supervised by the API
(`services/api/camera_manager.py`). Workers post to the API over HTTP; they do
not write to the database or the bus directly.

## 2. Identity

| Capability | Status | Implementation |
|---|---|---|
| Anonymous `pr_` person_ref, salted per session | Built | `finblade/identity.py` |
| Anonymous `gp_` global_ref, one per person across cameras | Built | `finblade/globalid.py` |
| OSNet appearance embeddings with crop-quality gating | Built | `finblade/appearance.py` |
| Transit-time feasibility gate before appearance scoring | Built | `finblade/topology.py` + `config/topology.yaml` |
| Co-presence exclusion (two tracks at once = two people) | Built | `globalid.py` `_exclusive` |
| Deferred re-match of provisional single-camera identities | Built | `globalid.py` `retry_min_bank` |
| Topology-derived template retention, with a hard ceiling | Built | `globalid.py` `retention_for` |
| Match-decision journal for evaluation runs | Built | `finblade/reid_journal.py`, off unless `FINBLADE_REID_JOURNAL` is set |
| Topology proposal from observed sightings | Built | `finblade/topology_survey.py`, `scripts/propose_topology.py` |
| Manual identity merge | Built | `POST /api/v1/identity/merge` |

**Privacy boundary.** Embeddings cross a process boundary exactly once, to
`POST /api/v1/identity/resolve`, are held in RAM by `services/api/identity.py`,
and are dropped on TTL. They are never persisted, logged, or returned. Every
identifier leaving the system is an opaque salted hash.

## 3. Spatial model

| Capability | Status | Implementation |
|---|---|---|
| Zone polygons in pixel or normalized coordinates | Built | `finblade/zones.py` |
| 8 zone types: `MONITORED` `RESTRICTED` `ENTRANCE` `EXIT` `DOOR` `OUTSIDE` `TRANSITION` `UNMONITORED` | Built | `zones.py` `ZONE_TYPES` |
| Foot-point zone assignment, restricted wins overlaps | Built | `finblade/geometry.py`, `zones.py` |
| N-frame boundary debounce | Built | `finblade/debounce.py` |
| Physical areas — one room, several cameras, counted once | Built | `finblade/areas.py`, `physical_area_id` on a zone |
| Browser zone editor | Built | `tools/zone-editor.html` |
| Camera topology: overlapping pairs and transit windows | Built | `config/topology.yaml` |
| Ground-plane homography / metric site map | **Planned** | Part B — needs real point correspondences |

The spatial model is **symbolic, not metric**. It records that two polygons are
the same room; it does not know where either sits in space. Nothing projects a
detection into shared world coordinates yet.

## 4. Metrics

| Capability | Status | Implementation |
|---|---|---|
| Occupancy per zone | Runs | `finblade/metrics.py` |
| Density (occupancy / `area_sqm`) | Runs | `metrics.py` |
| Capacity percentage | Built | `metrics.py` |
| Dwell time accumulation | Built | `metrics.py`, `finblade/tracks.py` |
| Inflow / outflow rates | Built | `metrics.py` |
| 5-second aggregate zone state | Built | `metrics.py` |
| Distinct-people occupancy across cameras | Built | `areas.py` `distinct_occupancy` |
| Time-weighted aggregation over a sampled series | Built | `finblade/timeweight.py` |
| Sparse-history reads: buckets, instants, durations | Built | `finblade/series.py` |
| Business-day windowing (06:00–18:00, site-local) | Built | `finblade/window.py` |

## 5. Events

17 types, one envelope, one validator shared by the pipeline and the API
(`finblade/events.py`, reused via `services/api/schema.py`).

`ZONE_ENTRY` `ZONE_EXIT` `ZONE_TRANSITION` `DENSITY_UPDATE` `CAPACITY_WARNING`
`RESTRICTED_ZONE_ENTRY` `RESTRICTED_ZONE_EXIT` `LOITERING_START` `LOITERING_END`
`CAMERA_HEARTBEAT` `CAMERA_ONLINE` `CAMERA_OFFLINE` `CAMERA_RECOVERED`
`WRONG_DIRECTION` `GROUP_CROSSING` `FACILITY_ENTRY` `FACILITY_EXIT`

| Capability | Status | Implementation |
|---|---|---|
| Hand-written schema validation, no pydantic | Built | `events.py` `validate_event` |
| PII guard — a non-anonymous `person_ref` is rejected | Built | `events.py` |
| Optional occupancy/density stamped on movement events | Built | `_OPTIONAL_SCHEMA` |
| Write-on-change gating for `DENSITY_UPDATE` and zone state | Built | `finblade/emission.py` |

## 6. Multi-source observations

The seam that lets a non-camera sensor publish. Added in Part A.

| Capability | Status | Implementation |
|---|---|---|
| Source-agnostic detection schema | Built | `finblade/observation.py` |
| Source types `CAMERA` `RADAR` `LIDAR` | Built | `observation.py` `SOURCE_TYPES` |
| Object classes `PERSON` `VEHICLE` `BICYCLE` `UNKNOWN` | Built | only `PERSON` is consumed downstream |
| `IMAGE` vs `SITE` coordinate frames | Built | uncalibrated sources can still publish |
| Cross-frame refusals (no metres on a pixel position) | Built | `_validate_position`, `_validate_velocity` |
| Optional velocity block | Built | nothing consumes it yet |
| Appearance-capability enforcement | Built | `APPEARANCE_CAPABLE = {CAMERA}` |
| Per-source accounting: accepted, rejected, silent, conflicts | Built | `services/api/fusion.py` |
| Geometric fusion of observations | **Planned** | Part B |

Radar can publish and be counted today. It **cannot** be identity-fused with
camera tracks until ground-plane calibration exists, because it carries no
appearance channel — a property of the sensor, not a gap in the code.

## 7. Rules and alerts

| Rule | What fires it | Status |
|---|---|---|
| R-01 | density above the warning threshold (amber) | Built |
| R-02 | density above the critical threshold (red) | Built |
| R-03 | occupancy at or above capacity percentage | Built |
| R-04 | bottleneck detection | **Cut** |
| R-05 | loitering beyond a per-zone dwell threshold | Built |
| R-06 | restricted-zone intrusion — immediate, one per visit | Built |
| R-07 | camera silent longer than 30s; clears on recovery | Built |
| R-08 | occupancy report, scheduled and on demand | Built |
| R-09 | head count above a per-zone threshold, area-independent | Built |

Implemented in `finblade/rules.py`. Hysteresis (separate on/off thresholds) and
a 10-second debounce apply to all density and capacity rules; R-06 is immediate
by design but still one alert per visit.

| Capability | Status | Implementation |
|---|---|---|
| Wrong-way movement against a declared route | Built | `finblade/flowrules.py` `WrongWayDetector` |
| Group crossing — N distinct people through one boundary | Built | `flowrules.py` `GroupCrossingDetector` |
| Alert acknowledge / resolve, with incident frames | Built | `services/api/service.py` |
| Orphaned incident-frame cleanup | Built | `/api/v1/frames/orphaned` |

## 8. Facility presence

Occupancy that survives someone walking out of every camera's view. Counted on
**door crossings by global identity**, never on per-camera detection counts.

| Capability | Status | Implementation |
|---|---|---|
| Roster of who is inside, persisted across restarts | Built | `finblade/presence.py` `FacilityRoster` |
| Direction resolved from the zones either side of a door | Built | `presence.py` `DoorPolicy` |
| One-way `ENTRANCE`/`EXIT` zones resolve on arrival | Built | `presence.py` |
| Two-way `DOOR` zones resolve on departure | Built | ambiguous crossings counted, never guessed |
| Declared opening baseline that drains as people leave | Built | `POST /api/v1/facility/baseline` |
| Per-door entry/exit totals and rates | Built | `presence.py` `DoorCounters` |
| Drift report — roster entries nobody has seen | Built | `GET /api/v1/facility/stale` |
| Identity rekey when two refs merge | Built | `presence.py` `rekey` |

## 9. Event bus

| Stream | Carries | Status |
|---|---|---|
| `fb:events` | every ingested event, as posted | Built |
| `fb:facility` | merged facility counts from the roster | Built |

`services/api/bus.py`. One Redis connection, two streams; `InMemoryBus` is the
test double. Publishing is active only when `REDIS_URL` is set — otherwise the
API runs on the in-process bus and nothing outside it sees a count. Which is
live is visible at `/api/v1/health` → `checks.facility_counts.bus`.

Counts publish **on change plus a keepalive** (`finblade/emission.py`
`StateWriteGate`, tuned by `FINBLADE_COUNT_WRITES` / `FINBLADE_COUNT_KEEPALIVE`),
so that a quiet building is distinguishable from a dead publisher. A door
crossing publishes immediately.

## 10. Storage and analytics

Postgres is the only durable backend. `FINBLADE_INMEMORY=1` selects an in-memory
store for tests; it is explicitly opt-in and never a fallback.

**14 tables** — `alerts` `area_state_ts` `camera_transits` `cameras` `events`
`facility_doors` `facility_meta` `facility_presence` `forwarder_cursors`
`physical_areas` `reports` `zone_live` `zone_state_ts` `zones`
(`services/api/ddl_pg.sql`, idempotent `CREATE`/`ALTER ... IF NOT EXISTS`).

**17 SQL views** for direct chatbot querying (`services/api/analytics_views.py`):
`v_zone_intervals` `v_zone_current` `v_zone_events` `v_zone_entries` `v_alerts`
`v_facility_current` `v_facility_roster` `v_facility_crossings` `v_facility_doors`
`v_area_current` `v_area_intervals` `v_camera_status` `v_zone_config`
`v_timeline` `v_journey_fragments` `v_journey_links` `v_journey_traces`

| Capability | Status | Implementation |
|---|---|---|
| Column-level grants; `person_ref` unreachable through any view | Built | `scripts/pg_grants.py` |
| Write-on-change zone history | Built | `emission.py` `StateWriteGate` |
| Opt-in retention pruning | Built | `FINBLADE_RETENTION_DAYS`, off by default |
| Schema + view apply and verification | Built | `scripts/pg_apply.py` |

## 11. HTTP API

**66 routes** — 63 under `/api/v1`, plus `/healthz`, `/readyz` and the `/ws`
WebSocket. `services/api/app.py` is a thin adapter; logic lives in
`service.py`, `identity.py` and `fusion.py`, all testable without FastAPI.

| Group | Routes |
|---|---|
| Ingest | `events/ingest`, `zones/state`, `observations/ingest`, `cameras/health`, `alerts` |
| Live state | `zones/state`, `summary`, `areas/state`, `facility/occupancy`, `/ws` |
| History | `history/events`, `history/alerts`, `movement`, `zones/{id}/series`, `/at`, `/duration` |
| Identity | `resolve`, `release`, `merge`, `stats`, `counts`, `list`, `tuning`, `{global_ref}` |
| Observations | `ingest`, `stats`, `sources`, `recent` |
| Facility | `occupancy`, `members`, `baseline`, `stale`, `roster` |
| Cameras | CRUD, `start`, `stop`, `stream`, `snapshot`, `simulate-failure`, `restore` |
| Alerts | list, get, `ack`, `resolve`, bulk delete |
| Reports | `generate`, `{id}`, `occupancy` as HTML / JSON / CSV |
| Zones & areas | zone CRUD, area CRUD |
| Ops | `health`, `healthz`, `readyz`, `finblade/status`, `finblade/flush`, `frames/orphaned` |

| Capability | Status | Implementation |
|---|---|---|
| API-key auth, two roles, off unless a key is set | Built | `services/api/auth.py` |
| Full key: everything. Integration key: every GET, `/ws`, and only alert ack/resolve | Built | role-based, not a route list |
| `?key=` accepted only on browser primitives that cannot send headers | Built | MJPEG, snapshot, `/ws`, saved frames |
| Credential redaction on anything leaving the process | Built | `services/api/redact.py` |
| WebSocket push at 2 Hz with 5s REST polling fallback | Built | `/ws` |
| OpenAPI spec that declares the auth scheme | Built | `app.py` `_openapi_with_auth` |

## 12. Dashboard

Static HTML served by the API. No framework, no chart library, no CDN — FinBlade
deploys on-prem and air-gapped. Styled entirely through `web/finblade-theme.css`;
see the UI theme rules in [CLAUDE.md](../CLAUDE.md), which are semantic rather
than cosmetic.

| Page | Contents | Status |
|---|---|---|
| `web/dashboard.html` | live feeds, zone cards, alert feed with acknowledge, unique-people counts, facility occupancy | Built |
| `web/cameras.html` | camera provisioning and pipeline control | Built |
| `web/history.html` | event and alert history, movement | Built |
| `web/report.html` | occupancy report generation | Built |
| `tools/zone-editor.html` | draw and save zone polygons | Built |

The dashboard's "In facility" KPI tile shows door-counted occupancy, aggregate
door flow, the observed/declared split when a baseline is set, and the drift
count when roster entries have gone unseen. It polls
`GET /api/v1/facility/occupancy` every 3s on its own timer rather than riding
`/ws` — the socket pushes at 2 Hz for a figure that moves on door crossings, and
its 5s REST fallback carries zones and alerts only, so a socket-fed tile would go
stale exactly when the socket dropped. The `/ws` payload is unchanged and still
carries cameras, zones and alerts only.

On a site with **no door zones configured** the roster can never move, so the
tile falls back to distinct people *in view* (`GET /api/v1/identity/counts`,
`live`) and labels itself `in view · no doors, not door-counted`. The label is
load-bearing: door-counted occupancy keeps counting someone who walks into a
corridor no camera watches, and the in-view figure does not — it drops to zero
when the last person leaves frame. The two are never shown as the same number
without saying which is on screen. If the counts endpoint is also unavailable
the tile says the count cannot be produced rather than rendering a bare `0`.

## 13. Integrations

| Capability | Status | Implementation |
|---|---|---|
| Forward zone state, events, alerts, camera health and people counts to the FinBlade platform | Built | `services/api/forwarder.py` |
| Store-tailing with a cursor — the database is the queue, so an outage costs nothing | Built | `forwarder.py` |
| Operator actions taken in FinBlade applied to local alerts | Built | `_apply_finblade_ack` |
| Chart tags on live-feed responses | Built | `services/api/charts.py` |
| 8 chatbot tools: `cctv_live_state` `cctv_zone_history` `cctv_zone_at_time` `cctv_zone_duration` `cctv_alerts` `cctv_occupancy_report` `cctv_camera_snapshot` `cctv_incident_frame` | Built | `integrations/finblade_ai/tools.py` |

## 14. Operations

| Capability | Status | Implementation |
|---|---|---|
| API as a systemd service, restart on crash, start on boot | Built | `deploy/finblade-api.service`, `scripts/install_service.sh` |
| Dev Postgres as a systemd service, ordered before the API | Built | `deploy/finblade-postgres.service`, `scripts/install_pg_service.sh` |
| Camera pipeline autostart after an API restart | Built | `FINBLADE_AUTOSTART_CAMERAS` |
| Server-side camera-offline monitor | Built | `app.py` `_offline_monitor` |
| Background loop failure counters surfaced in `/api/v1/health` | Built | `_loop_errors` |
| Evidence artifacts: annotated frames, contact sheet, metrics, events, alerts | Built | written to `evidence/` |
| Cross-camera identity evaluation harness | Built | `scripts/eval_cross_camera.py` |
| Secret scanning, credential-leak checks | Built | `scripts/secret_scan.sh` |

**Test suite: 1520 passing, 15 skipped.** Runs headless against the in-memory
store, and against a real Postgres where a cluster is reachable. 10 of the skips
are UI-behaviour tests that lift JavaScript out of `web/dashboard.html` and run
it under node (`test_dashboard_sort.py`, `test_facility_tile.py`); they skip
where no JS runtime is installed, which includes the current dev box — so that
JavaScript is **not** covered by a green run here. Every API route
is referenced by at least one test. What each capability is verified by — and
what still needs a human — is in [TEST_CASES.md](TEST_CASES.md).

## 15. Not built

**Planned** — agreed, designed, not implemented:

- Ground-plane homography and geometric fusion (Part B). Blocked on real point
  correspondences from an overlapping camera pair; must not be built against
  guessed merge radii.
- Radar identity fusion — depends on Part B.

**Cut** — deliberately out of scope, see [CLAUDE.md](../CLAUDE.md):

- Sankey flow diagram
- Site heatmap
- R-04 bottleneck detection
- Video clip bookmarking
- User management, multi-tenancy (API-key auth itself is built)
- Any model training or fine-tuning
