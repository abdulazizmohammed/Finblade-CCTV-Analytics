# USE_CASES.md — FinBlade CCTV crowd analytics

Ordered simple → complex. Each level depends only on levels below it, so this is
also the implementation order.

**Status key:** `BUILD` = in scope for Sunday · `CUT` = explicitly out of scope,
do not build · `STRETCH` = only if everything else is green.

---

## Level 1 — Foundation

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-01 | BUILD | Decode video source | Clip or RTSP opens; frames read at 1080p 15–25 FPS |
| UC-02 | BUILD | Deterministic loop playback | Clip repeats identically every run |
| UC-03 | BUILD | Save reference still frame | Frame written to disk for zone drawing |
| UC-04 | BUILD | Annotated feed to browser | Browser shows annotated view, not raw RTSP |
| UC-05 | BUILD | Stream reconnect | Source drops → auto-reopen, no crash |

## Level 2 — Detection & tracking

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-06 | BUILD | Person detection | Boxes + confidence; class filtered to person (COCO id 0) |
| UC-07 | BUILD | Detection tuning | Expose `imgsz` and model size in config; no code change to adjust |
| UC-08 | BUILD | Persistent track IDs | ID survives brief occlusion; no swap when two people cross |
| UC-09 | BUILD | Ground-contact point | Foot point (bottom-centre of bbox) computed and drawn |

## Level 3 — Zones & occupancy

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-10 | BUILD | Zone definition | Polygon + `capacity_max`, `area_sqm`, `restricted` loaded from config |
| UC-11 | BUILD | Point-in-polygon assignment | Person → zone by foot point; restricted zones win ties |
| UC-12 | BUILD | Boundary debounce | N consecutive frames required; no per-frame flicker |
| UC-13 | BUILD | Live occupancy per zone | Count matches manual head-count ±1 (**human-verified**) |
| UC-14 | BUILD | Restricted-zone occupancy | Person inside no-go polygon detected and flagged |

## Level 4 — Derived metrics

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-15 | BUILD | Density per m² | occupancy ÷ `area_sqm`. Approximate until UC-53; that is expected |
| UC-16 | BUILD | Capacity utilisation | occupancy ÷ `capacity_max` as percentage |
| UC-17 | BUILD | Dwell time | Seconds accumulated per track; resets on exit |
| UC-18 | BUILD | Inflow / outflow rate | Persons/min crossing boundary; debounced against hovering |
| UC-19 | BUILD | Anonymous person_ref | Hash only. No PII in any output, log, or table |
| UC-20 | BUILD | Zone state aggregate | occupancy/density/inflow/outflow/status computed every 5s |

## Level 5 — Event generation

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-21 | BUILD | ZONE_ENTRY | Fires once on entry; correct `zone_to`, timestamp, confidence |
| UC-22 | BUILD | ZONE_EXIT | Fires once on exit; no duplicates from jitter |
| UC-23 | BUILD | DENSITY_UPDATE | Emitted with current occupancy + density |
| UC-24 | BUILD | CAMERA_HEARTBEAT | Periodic while stream healthy |
| UC-25 | BUILD | CAMERA_OFFLINE | Emitted after 30s silence |
| UC-26 | BUILD | ZONE_TRANSITION | Correct `zone_from`/`zone_to` on movement between zones |

## Level 6 — API & persistence

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-27 | BUILD | Event bus | Redis Streams publish/consume reliably |
| UC-28 | BUILD | `POST /api/v1/events/ingest` | Validates and accepts exact payload schema; rejects malformed |
| UC-29 | BUILD | `POST /api/v1/zones/state` | Accepts aggregate state at 5s cadence |
| UC-30 | BUILD | Core tables | `zones`, `cameras`, `zone_events`, `alerts` persisted |
| UC-31 | BUILD | Time series | `zone_state_ts` written continuously, queryable by range |

## Level 7 — Rule engine

Ordered easiest → hardest within the level.

| ID | Rule | Status | Use case | Acceptance |
|---|---|---|---|---|
| UC-32 | R-07 | BUILD | Camera offline | NOC alert after >30s no heartbeat; clears on recovery |
| UC-33 | R-06 | BUILD | Restricted-zone intrusion | Immediate security alert the moment feet cross in |
| UC-34 | R-01 | BUILD | Density warning | Zone card → amber above 2.0/m² |
| UC-35 | R-02 | BUILD | Density critical | Red + notification above 4.0/m² |
| UC-36 | R-03 | BUILD | Capacity threshold | Entry-restriction recommendation at ≥90% capacity |
| UC-37 | — | BUILD | Hysteresis + debounce | Separate on/off thresholds + 10s debounce; no flapping |
| UC-38 | R-05 | BUILD | Loitering | Alert when dwell exceeds threshold, tied to person + zone |
| UC-39 | R-08 | BUILD | Scheduled report | Hourly generation + distribution, and on-demand |
| UC-40 | R-04 | CUT | Bottleneck detection | Inflow > outflow ×2 for 60s. Out of scope |

## Level 8 — Dashboard

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-41 | BUILD | Live annotated feed panel | Feed with boxes + zone overlays embedded in dashboard |
| UC-42 | BUILD | Zone cards | Occupancy, capacity, trend, status colour, live |
| UC-43 | BUILD | Camera health grid | Online/offline per camera; flips on disconnect |
| UC-44 | BUILD | Alert feed + acknowledge | Operator acknowledges; `acknowledged_by` + time recorded |
| UC-45 | BUILD | WebSocket transport | Sub-second updates |
| UC-46 | BUILD | REST fallback | 5s polling takes over when WebSocket drops |
| UC-47 | STRETCH | Occupancy timeline | Per-zone trend chart over time |
| UC-48 | CUT | Site heatmap | Floor-plan density overlay. Out of scope |
| UC-49 | CUT | Zone-to-zone Sankey | Out of scope |
| UC-50 | BUILD | Occupancy report | HTML/PDF generated on demand and on schedule |

## Level 9 — Multi-camera & advanced

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-51 | CUT | Second camera concurrent | Out of scope |
| UC-52 | CUT | Video clip bookmark | Out of scope |
| UC-53 | CUT | Homography calibration | Site-visit task, not a dev task |
| UC-54 | CUT | Geometric cross-camera handoff | Out of scope |
| UC-55 | CUT | Appearance re-ID pilot | Out of scope |
| UC-56 | BUILD | Embedding TTL | Redis key-expiry configured and verified. Privacy story — keep it |

## Level 10 — Demo integration

| ID | Status | Use case | Acceptance |
|---|---|---|---|
| UC-57 | BUILD | Event injection tooling | Density-critical reachable on demand without a real crowd |
| UC-58 | BUILD | End-to-end latency | Detection → dashboard lag measured and reported |
| UC-59 | BUILD | Full 10-step run | Demo script runs in order, no manual DB fiddling |
| UC-60 | — | Rehearsal + runsheet | **Human task.** Agent writes the runsheet draft only |

---

## Human-verified only

The agent cannot confirm these. Produce evidence, flag in MORNING.md, move on:

- UC-13 — occupancy vs manual head-count
- UC-06/07 — whether boxes are actually on people
- UC-10/11 — whether zone polygons sit on the floor where intended
- UC-59/60 — whether the demo looks right
