# FinBlade CCTV — capabilities

What this system can actually do, as of the last update below. One entry per
capability, with the file that implements it so a claim here can be checked
against code rather than taken on trust.

For *what each capability is built out of* — library, version, and the
deliberate non-choices (no pydantic, no ORM, no JS framework, no chart library)
— see [TECH_STACK.md](TECH_STACK.md).

**Last updated:** 2026-09-15

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
and are dropped on TTL. They are **never written to disk, a log, the database,
`evidence/`, or any response body — in any mode.** Every identifier leaving the
system is an opaque salted hash.

**How long they are held has two modes. The default is unchanged.**

| | Default | Extended |
|---|---|---|
| Enabled by | nothing — this is the default | `FINBLADE_REID_EXTENDED_RETENTION=ram` |
| Held for | 300s, ceiling 1800s | up to **24h** (`FINBLADE_REID_EXTENDED_TTL`, hard-clamped) |
| Stored as | the raw template | projected under a rotating epoch key |
| Where | RAM | RAM — no datastore, no Redis, nothing on disk |

Extended retention stores each template as `Qv` for a per-window random
orthogonal `Q` (`finblade/cancelable.py`). Because `Q` is orthogonal, cosine
similarity is preserved **exactly**, so matching accuracy is mathematically
unchanged.

Validated three ways: the property itself and threshold safety in
`tests/test_cancelable.py` (cosine drift < 1e-9, zero pairs crossing 0.70 over
400 trials), registry-level decision equivalence in
`tests/test_extended_retention.py`, and end to end through
`scripts/eval_cross_camera.py --extended-retention` — 400 frames producing an
identical report with the transform on and off, down to a byte-identical
`registry_stats` (`evidence/tier3_baseline.json`, `evidence/tier3_extended.json`).

**What that does and does not give you.** It gives *key-dependent
confidentiality with per-window unlinkability*: a memory dump without the key
yields vectors in an unknown basis, and once a window's key is destroyed its
templates cannot be re-projected, so dumps more than two windows apart cannot
be correlated. It is **not non-invertible** — `Q⁻¹ = Qᵀ`, so anyone holding the
epoch key recovers the template exactly. The one-way property in BioHashing
comes from a quantisation step that costs matching accuracy; this deliberately
does not do that, and so deliberately does not claim it.

Two keys are live at once (12h epochs by default), so someone present at a
window boundary is carried forward rather than dropped, and real retention is
bounded at `2 × epoch`. Immediate erasure is `registry.erase_templates()`,
which destroys the keys as well as the gallery.

**Enabling it is an operational and legal decision, not a tuning knob.** It is
off unless explicitly named, logs a warning at startup when on, and is reported
in `/api/v1/identity/stats` and `/api/v1/health` — including a warning when the
topology's transit windows are narrower than the retention window, which makes
the mode inert. It requires the same documented sign-off as
[DECISIONS.md](../DECISIONS.md) D-9 required for the current posture; see D-30.

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

### Organisation hierarchy: Region → City → Branch

The customer's network is a strict tree and every camera hangs off a branch.
**A camera's `site_id` is its branch id** — the key every camera, zone
reading, event and alert already carried, so the hierarchy attaches to the
data that exists rather than adding a second key that could disagree with it.

| Capability | Status | Implementation |
|---|---|---|
| Regions, cities, branches as tables with `ON DELETE RESTRICT` foreign keys | Built | `ddl_pg.sql` `regions` `cities` `branches` `org_meta` |
| Validation and referential checks (422 bad parent, 409 has children) | Built | `finblade/org.py`, `service.py` `save_region/city/branch`, `delete_*` |
| Tree with camera / zone / alert counts rolled up per level | Built | `GET /api/v1/org`, `org.build_tree` |
| Cameras whose `site_id` matches no branch listed as **unassigned**, counted in the network total only | Built | `org.build_tree`; `POST /cameras` returns a `warning` |
| A branch that still owns cameras refuses deletion | Built | `service.delete_branch` |
| `region_id` / `city_id` / `branch_id` narrowing on `cameras`, `zones/state`, `alerts`, `summary`, `history/events`, `history/alerts` | Built | `app.py` `_scope`; filters intersect, an unknown id returns nothing |
| Whole-tree import, idempotent | Built | `POST /api/v1/org/import`, `scripts/seed_org.py`, `config/org.wareed.yaml` |
| Tenant name / country on the tree | Built | `org_meta`, `POST /api/v1/org/meta` |
| `v_org_hierarchy` view for chatbot roll-ups by region | Built | `analytics_views.py`; join `site_id = branch_id` |

Not a foreign key from `cameras.site_id`, deliberately: workers post `site_id`
before anyone has drawn the org chart, and a camera that names an unknown
branch must be visible as unassigned, not rejected. Tests:
`tests/test_org.py` (logic, both store backends, service, HTTP routes),
`tests/test_analytics_views.py::TestOrgHierarchy`.

### Map and GPS trackers

A branch carries `lat`/`lon` and a geofence radius (default 150 m). A
tracker is a phone running Traccar Client, or a 4G tracker unit, on a lab
vehicle or a device in transit. **A tracker is a vehicle or an asset, never a
person**: there is no driver field anywhere, and the OsmAnd `driverUniqueId`
is dropped on parse.

| Capability | Status | Implementation |
|---|---|---|
| KSA map with every placed branch pinned, coloured by its roll-up; city clusters when zoomed out | Built | `web/map.html` — inline SVG, no tiles, no CDN |
| Tap a branch → counts, cameras, vehicles present, and doors to its dashboard / cameras / history / reports / settings | Built | `web/map.html` panel; `?branch=` deep link |
| Place or move a branch pin by tapping the map; edit name, type, city, geofence | Built | `web/map.html` Settings → `POST /api/v1/org/branches` |
| Position ingest, three dialects: OsmAnd/Traccar Client (`?id=&lat=&lon=…`), OpenGTS `gprmc`, JSON | Built | `GET|POST /api/v1/trackers/ingest`, `finblade/gps.py` |
| `?key=` accepted on the ingest route (a tracker cannot set a header) | Built | `services/api/auth.py` |
| No-fix rejection (`0,0`, `$GPRMC` status `V`, out-of-range) | Built | `gps.py` `_check_point`, `parse_gprmc` |
| Branch geofence arrival / departure with hysteresis (exit at 1.5× radius) and a 2-report confirm | Built | `gps.GeofenceEngine`; events `TRACKER_ARRIVED` / `TRACKER_DEPARTED` with `dwell_s` |
| Geofence state survives an API restart | Built | restored from `tracker_live` |
| R-12: tracker silent > 5 min (`FINBLADE_TRACKER_SILENT_S`) raises AMBER, auto-resolves on the next report | Built | `service.check_silent_trackers`, `_tracker_monitor` in `app.py` |
| Live vehicle markers over `/ws`, with heading arrow, last-hour trail, state MOVING / STOPPED / OFFLINE | Built | `/ws` frame carries `trackers`; `GET /api/v1/trackers/{id}/track` |
| Register / list / delete trackers; unregistered reporters kept and labelled | Built | `web/trackers.html`, `POST/GET/DELETE /api/v1/trackers` |
| Phone pairing instructions (Traccar Client) and a browser-based reporter | Built | `web/trackers.html`, `web/tracker.html` (needs HTTPS for geolocation) |
| Region / City / Branch scope on trackers, by home branch | Built | `GET /api/v1/trackers?region_id=…`, `/summary` |
| Position history under retention | Built | `tracker_positions` pruned by `FINBLADE_RETENTION_DAYS` |
| Route replay for a demo without a vehicle | Built | `scripts/replay_route.py --from RUH-01 --to KHJ-01` |
| Street-level tiles | **Planned** | self-hosted PMTiles + vendored MapLibre; needs a one-time offline data download |
| BLE tags (the Xiaomi Tag) as "arrived at branch" / "on board" beacons | **Planned** | `kind: BLE_TAG` is accepted; no gateway yet — needs a hardware test of address rotation |

The country outline in `map.html` is a **hand-drawn ~40-vertex schematic**,
not a survey boundary. Tests: `tests/test_gps.py` (dialects, geometry,
geofence, both store backends, service, HTTP).

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

23 types, one envelope, one validator shared by the pipeline and the API
(`finblade/events.py`, reused via `services/api/schema.py`).

`ZONE_ENTRY` `ZONE_EXIT` `ZONE_TRANSITION` `DENSITY_UPDATE` `CAPACITY_WARNING`
`RESTRICTED_ZONE_ENTRY` `RESTRICTED_ZONE_EXIT` `LOITERING_START` `LOITERING_END`
`CAMERA_HEARTBEAT` `CAMERA_ONLINE` `CAMERA_OFFLINE` `CAMERA_RECOVERED`
`WRONG_DIRECTION` `GROUP_CROSSING` `FACILITY_ENTRY` `FACILITY_EXIT`
`HAZARD_FIRE` `HAZARD_SMOKE` `PPE_VIOLATION` `PPE_COMPLIANT`
`TRACKER_ARRIVED` `TRACKER_DEPARTED`

The two `TRACKER_*` events come from a GPS tracker crossing a branch
geofence, not from a camera: `camera_id` carries the tracker id and `site_id`
the branch. They carry no `person_ref` and do not count as a camera
heartbeat.

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
| R-10 | sustained fire or smoke in view | **Runs** — see below |
| R-11 | required PPE missing on a tracked person in a compliance zone | **Runs** — see below |
| R-12 | GPS tracker silent longer than 5 min (amber); clears on the next report | Built — `service.py` `check_silent_trackers` |

Implemented in `finblade/rules.py`. Hysteresis (separate on/off thresholds) and
a 10-second debounce apply to all density and capacity rules; R-06 is immediate
by design but still one alert per visit. R-10 uses the same `HysteresisLatch`
with a **3-second** sustain — a fire alert that waits ten seconds to arm is ten
seconds of fire.

**R-10 fire/smoke is an EVALUATION capability, not a fire alarm.** It is marked
**Runs**, and the distinction matters more here than anywhere else in this file:

- **Checkpoint:** `rabahdev/fire-smoke-yolov8n`, trained on D-Fire (**CC0-1.0**,
  documented, with published held-out metrics). Classes `{0: smoke, 1: fire}` —
  read from the checkpoint at load, never assumed.
- **LICENSING:** the checkpoint is built on Ultralytics YOLOv8 and inherits
  **AGPL-3.0**. Commercial or production deployment must be covered by the
  approved Ultralytics commercial licensing arrangement, or otherwise satisfy
  AGPL. The same dependency already applies to `ultralytics` and
  `models/yolo11s.pt`; this states it rather than adding it.
- **No fire footage has been run through this system.** Every threshold
  (`fire_on 0.60`, `smoke_on 0.65`, 3s sustain) is a guess, labelled as one in
  the source, and must be retuned against real detections before an R-10 alert
  is treated as calibrated.
- **Off by default** — `hazard.enabled: false`. A second always-on model is a
  per-camera GPU decision. Measured cost: 16.6 ms warm, 32 MiB VRAM, ~33 ms of
  GPU per second per camera at 2 Hz.
- Fire is RED, smoke AMBER with a higher arming bar, because steam, dust and
  exhaust read as smoke and a red alert that turns out to be a kettle costs
  operator trust.

See [DECISIONS.md](../DECISIONS.md) D-31.

**R-11 PPE compliance is likewise an EVALUATION capability**, marked **Runs**.

- **Per tracked person, inside a compliance zone.** Never camera-wide: a
  violation belongs to somebody, and an alert that cannot say who is not
  actionable. Chain is camera → track → zone → associated detections →
  temporal state → alert.
- **Two vocabularies, one rule engine.** A zone's `ppe_profile` picks which:
  `industrial` (hardhat, safety_vest, mask) or `medical` (surgical gloves /
  mask / gown / cap / scrubs, face shield, goggles, coverall, shoe covers,
  lab coat). Defaults to `industrial`, so every zone predating profiles keeps
  working with no migration. The state machine and `evaluate_ppe` are
  item-agnostic — a profile is data, not a second pipeline. Items whose profile
  does not own them are dropped with a warning rather than judged, because a
  medical detector asked for a hardhat would convict everyone on silence.
- **The medical detector is a separate model** (`medical_ppe:` config block),
  inert unless a zone asks for the medical profile. Both detectors run on the
  same tick when both are active, so neither one's items read as "absent" on a
  frame the other owned. **Runs** as of 2026-09-15 with FinBlade's in-house
  lab checkpoint `models/ppe_yolo11s_best.pt` (YOLO11s, ten classes in five
  worn/missing pairs: Gloves, Goggles, Haircap, Labcoat, Mask) — selected by
  `medical_ppe.checkpoint`, which binds class map, default weights and a
  **class-order assertion** together (`ppe_client.MEDICAL_CHECKPOINTS`). A
  retrained checkpoint whose `model.names` differ from `PPE_CLASSES` is refused
  at load, not adapted. It serves five of the profile's ten items; a zone
  requiring the other five (gown, scrubs, face shield, coverall, shoe covers)
  has those dropped with a warning — **item-level, not just profile-level**
  (`run_cpu.ppe_served` reads the detector's `served_types`).
- **The lab checkpoint is a first-pass placeholder** (226 training images;
  test mAP50 0.22, precision 0.20, recall 0.34) and the integration assumes
  so: user-visible threshold 0.5 (not 0.35), `absence_weight` 0.0 for the
  medical profile so a violation needs **sustained explicit `No X`
  detections** and silence never convicts, and a **raw-detection journal**
  (`evidence/ppe_raw/<camera>.jsonl`: class, confidence, box, frame,
  timestamp — no crops, no track ids) of every box ≥ 0.25, below the visible
  threshold, so real-world precision can be measured and hard examples pulled
  for the retrain. **Measured silent on `media/LAB-PPE.mp4`** — nothing above
  0.25 on full frames or on person crops — see BLOCKERS.md B-9 and
  `models/MANIFEST.yaml`.
- **Zone-scoped requirements.** `required_ppe` on a zone, e.g. `[hardhat,
  safety_vest]`. A worker without a mask in a zone that does not require masks
  is not in violation. Empty (the default) means no PPE rule applies — and the
  zone then reports no compliance figures at all, rather than reporting everyone
  as fine. Set per zone from the **zone editor**, as a dropdown of checkboxes
  offering exactly `finblade.ppe.PPE_TYPES`; changing it saves immediately and
  the running worker picks it up within ~4s, with no restart — verdicts already
  reached for an item that is no longer required are discarded at the same
  moment, so the zone card and the alert feed never disagree about it.
  **One exception:** if no zone on a camera declared any PPE when the worker
  started, the model was never loaded, and adding the *first* requirement needs
  that camera restarted. The worker logs a warning saying exactly that rather
  than leaving an operator waiting for alerts nothing is running to produce.
- **Association is anatomical, not IoU** (`finblade/geometry.py`): a hardhat
  must sit in the top band of the person box, a vest in the torso band. It
  refuses when containment is below 0.5 or when two people are too close to
  arbitrate between — an unattributable item is dropped, never guessed onto
  somebody.
- **One missed detection is not a violation.** Evidence accumulates per
  `(track, ppe_type)` through UNKNOWN → CANDIDATE → CONFIRMED, and an explicit
  `NO-Hardhat` counts **four times** as strongly as the model simply going
  quiet (`absence_weight`, overridable per profile via
  `PPEThresholds.absence_weight_by_profile`; the medical profile runs at 0).
- **Its own severity, `COMPLIANCE`, not amber.** A missing hardhat is a policy
  breach against a person, which is a different kind of thing from a density
  measurement (amber/red) or a place-based restriction (magenta). It gets
  `--fb-compliance` violet in the dashboard, the history page and the OpenCV
  annotator, so an operator can tell at a glance whether the room is filling up
  or somebody is under-equipped.
- **The alert carries a crop of the person it accuses.** `Alert.track_id` names
  the box, and the worker cuts that box out of the annotated frame (padded 35%
  horizontally, 12% vertically) to `evidence/bookmarks/bm_<cam>_<seq>_t<track>.jpg`.
  Viewable from the history page through the existing frame modal. `track_id` is
  a LOCAL tracker id — it identifies a box in one camera process, is reused
  after a restart, and is not a person identifier.
- **Checkpoint:** `ayushgupta7777/safetyvision-yolov8` v2. Its published
  performance is **markedly weaker for NO-Safety Vest and Mask/NO-Mask than for
  Hardhat** — do not compensate by lowering confidence thresholds, which turns
  a recall problem into a false-accusation problem against a named worker.
- Same **AGPL-3.0 via Ultralytics** dependency as R-10.
- **Distant or small people are not judged.** Below `min_person_height_px`
  (120 by default) a track is reported `not_assessable` rather than fed
  silence that would convict it (`PPETracker.assessable`).

See [DECISIONS.md](../DECISIONS.md) D-32, D-34, D-35. Manual validation:
`scripts/ppe_check.py --source <video> --required hardhat,safety_vest`, or for
the lab checkpoint `scripts/ppe_check.py --source <video> --profile medical
--required surgical_gloves,goggles,surgical_cap,lab_coat,surgical_mask
--raw-log evidence/ppe_raw`.

| Capability | Status | Implementation |
|---|---|---|
| Fire / smoke detection, 2 Hz second model | **Runs** | `services/inference/hazard_client.py` + `models/fire_smoke_yolov8n.pt` |
| Industrial PPE detection, 2 Hz third model | **Runs** | `services/inference/ppe_client.py` + `models/ppe_safetyvision_v2.pt` |
| Medical / lab PPE detection, same tick, swappable checkpoint with class-order assertion | **Runs** (silent on site footage — B-9) | `ppe_client.py` `MEDICAL_CHECKPOINTS` + `models/ppe_yolo11s_best.pt` |
| Raw PPE detection journal (class, conf, box, frame, ts) below the visible threshold | Built | `ppe_client.PPEDetector._journal` → `evidence/ppe_raw/<camera>.jsonl` |
| Item-level "not judged unless a loaded model has the class" guard | Built | `run_cpu.ppe_served` + `PPEDetector.served_types` |
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

**21 tables** — `alerts` `area_state_ts` `branches` `camera_transits` `cameras`
`cities` `events` `facility_doors` `facility_meta` `facility_presence`
`forwarder_cursors` `org_meta` `physical_areas` `regions` `reports`
`tracker_live` `tracker_positions` `trackers` `zone_live` `zone_state_ts` `zones`
(`services/api/ddl_pg.sql`, idempotent `CREATE`/`ALTER ... IF NOT EXISTS`).

**18 SQL views** for direct chatbot querying (`services/api/analytics_views.py`):
`v_zone_intervals` `v_zone_current` `v_zone_events` `v_zone_entries` `v_alerts`
`v_facility_current` `v_facility_roster` `v_facility_crossings` `v_facility_doors`
`v_area_current` `v_area_intervals` `v_camera_status` `v_zone_config`
`v_org_hierarchy` `v_timeline` `v_journey_fragments` `v_journey_links`
`v_journey_traces`

| Capability | Status | Implementation |
|---|---|---|
| Column-level grants; `person_ref` unreachable through any view | Built | `scripts/pg_grants.py` |
| Write-on-change zone history | Built | `emission.py` `StateWriteGate` |
| Opt-in retention pruning | Built | `FINBLADE_RETENTION_DAYS`, off by default |
| Schema + view apply and verification | Built | `scripts/pg_apply.py` |

## 11. HTTP API

**81 routes** — 78 under `/api/v1`, plus `/healthz`, `/readyz` and the `/ws`
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
| Organisation | `org` (tree + roll-ups), `org/index`, `org/import`, `org/meta`, `org/regions`, `org/cities`, `org/branches` (upsert + delete) |
| Trackers | `trackers/ingest` (GET+POST, three dialects, `?key=`), `trackers` list/register/delete, `trackers/{id}/track` |
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
| `web/dashboard.html` | live feeds, zone cards, alert feed with acknowledge, unique-people counts, facility occupancy; Region › City › Branch scope selector (client-side, `?region=` `?city=` `?branch=`) | Built |
| `web/network.html` | the Region → City → Branch tree with per-level roll-ups, camera pills, unassigned cameras, and add/rename/delete for every level; tenant name in the bar | Built |
| `web/map.html` | KSA map: branch pins by status, city clusters, live vehicles with trails, tap-to-panel with doors to dashboard / cameras / history / reports / settings, place-pin-by-tap | Built |
| `web/trackers.html` | register trackers, fleet status, Traccar Client pairing card with the exact URL | Built |
| `web/tracker.html` | the phone page: browser geolocation → ingest, for a desk demo | Built |
| `web/cameras.html` | camera provisioning and pipeline control; cameras grouped by branch under city and region, branch picked from a dropdown, `?branch=` narrowing | Built |
| `web/history.html` | event and alert history, movement; `?region_id=` `?city_id=` `?branch_id=` passed through to the API | Built |
| `web/report.html` | occupancy report generation | Built |
| `tools/zone-editor.html` | draw and save zone polygons, map zones to physical areas, set required PPE | Built |

The dashboard's "In facility" KPI tile shows door-counted occupancy, aggregate
door flow, the observed/declared split when a baseline is set, and the drift
count when roster entries have gone unseen. It polls
`GET /api/v1/facility/occupancy` every 3s on its own timer rather than riding
`/ws` — the socket pushes at 2 Hz for a figure that moves on door crossings, and
its 5s REST fallback carries zones and alerts only, so a socket-fed tile would go
stale exactly when the socket dropped. The `/ws` payload is unchanged and still
carries cameras, zones and alerts only.

On a site with **no door zones configured** the roster can never move, so the
tile falls back to people *in view* — `people_in_view` from the camera
heartbeat, summed over the cameras that are ONLINE — and labels itself
`in view · no doors, not door-counted`. The label is load-bearing: door-counted
occupancy keeps counting someone who walks into a corridor no camera watches,
and the in-view figure does not — it drops to zero when the last person leaves
frame. The two are never shown as the same number without saying which is on
screen. If the counts endpoint is also unavailable the tile says the count
cannot be produced rather than rendering a bare `0`.

**Not `GET /api/v1/identity/counts` `live`, which this used until 2026-09-04.**
That figure counts live identity *bindings*, and bindings survive a worker being
killed — measured at 13 while eight people were in frame (BLOCKERS.md B-6).
`people_in_view` is rebuilt from each frame's tracks, so it cannot outlive the
people it counts. The trade is that summing it across cameras double-counts
anyone visible to two at once, so above one online camera the subtitle reads
`in view · N cameras, not de-duplicated` rather than implying a site-wide
distinct-person total. The per-camera `live` badge under each feed uses the same
source for the same reason; the `unique` badge beside it stays on identity,
where cumulative footfall has no liveness problem.

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

**Test suite: 1557 passing, 15 skipped.** Runs headless against the in-memory
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
