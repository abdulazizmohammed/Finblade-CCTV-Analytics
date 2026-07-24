# CLAUDE.md — FinBlade CCTV crowd-analytics demo

## Mission
Build a working CCTV crowd-analytics demo: RTSP/file video -> person detection ->
tracking -> zone occupancy/density -> events -> rule engine -> live dashboard.
Hard deadline: **Sunday**. The client demo is 10 scripted steps (see README).

You are working **unattended overnight**. The human reviews in the morning.

---

## PRIME DIRECTIVE: you cannot see

You have no eyes. You cannot tell whether bounding boxes are on people, whether a
zone polygon sits on the floor, or whether the demo "looks right".

Therefore:
- **Never claim the vision pipeline is correct.** You may only claim it *runs*.
- Anything requiring visual judgement -> produce evidence, flag for human, move on.
- The phrase "detection is working" is banned. Say "detection ran, N boxes/frame,
  evidence saved to ./evidence/" instead.

---

## Standing rules (violating these is worse than not finishing)

1. **Never mock, stub, fake, or bypass the detection path** to make something pass.
   If YOLO won't run, STOP that slice and log a blocker. A green test over a fake
   detector is actively harmful — it burns the human's morning.
2. **Never change pinned dependency versions** to resolve an error. Log it instead.
3. **Never delete or rewrite the human's config** (`config/cameras.yaml` zone
   polygons especially). Add new config files if you need variants.
4. **Use `device: CPU` for all development.** Do NOT attempt Intel Arc GPU
   passthrough in WSL — that is a known time sink and is a deployment-day concern,
   not a dev concern. CPU is fast enough for correctness work.
5. **Assume no network.** Model weights and video clips are pre-placed by the
   human (see below). If something needs downloading, log a blocker; do not spin.
6. **Commit to git after every green slice.** Small, frequent commits. If your
   session dies, progress must survive.
7. **Do not refactor working code.** No cleanup passes, no restructuring, no
   "improvements" to slices already verified. Forward progress only.
8. **Never wait for input.** There is no human awake. If you would ask a question,
   pick the most reversible option, log the decision in DECISIONS.md, continue.

---

## Inputs the human has placed for you

- `media/clip.mp4`        — moderate-traffic clip (primary test input)
- `media/clip_dense.mp4`  — dense crowd clip (optional; for density testing)
- `models/yolov8n.pt`     — pretrained weights (COCO; person = class 0)

If any are missing: log to BLOCKERS.md immediately and continue with whatever
slices don't depend on them (rule engine unit tests, API schema, DB layer all
work without video).

---

## Scope — BUILD these

Levels 1-7 complete, Level 8 partial:
- Video decode, loop playback, reconnect, annotated MJPEG view
- YOLO person detection (class 0 only) + ByteTrack persistent IDs
- Foot-point (bottom-centre of bbox) zone assignment, point-in-polygon
- N-frame boundary debounce
- Occupancy, density (occupancy / area_sqm), dwell time, inflow/outflow
- Anonymous person_ref hashing (no PII anywhere, ever)
- Events: ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, DENSITY_UPDATE,
  CAMERA_HEARTBEAT, CAMERA_OFFLINE
- Redis Streams event bus
- FastAPI: POST /api/v1/events/ingest, POST /api/v1/zones/state
- Postgres tables: zones, cameras, zone_events, zone_state_ts, alerts
- Rule engine: R-01 (amber >2.0/m2), R-02 (red >4.0/m2), R-03 (>=90% capacity),
  R-05 (loitering), R-06 (restricted zone, immediate), R-07 (camera offline >30s),
  R-08 (occupancy report, scheduled + on-demand)
- Hysteresis (separate on/off thresholds) + 10s debounce on ALL rules
- Dashboard: live annotated feed, zone cards, alert feed with acknowledge,
  occupancy report generation

## Scope — DO NOT BUILD (explicitly cut)

- Cross-camera re-identification (any form)
- Homography / camera calibration
- Sankey flow diagram
- Site heatmap
- R-04 bottleneck detection
- Second camera support
- Video clip bookmarking
- Authentication, user management, multi-tenancy
- Any model training or fine-tuning

Building cut items is a failure, not a bonus. Time is the binding constraint.

---

## UI theme — FinBlade brand (not a generic dark theme)

Import `web/finblade-theme.css` in every UI you build and use its CSS variables.
**Never hard-code a colour.** Brand accent is `#18bdc2`, taken from finblade.ai.
The dashboard is shown to a client under the FinBlade name — a default dark
template undermines that.

Three rules that are semantic, not cosmetic. Do not "improve" them:

1. **Teal is chrome, never status.** Brand teal marks interactive and structural
   elements only. It never means "this zone is fine" — teal and green are too
   close in hue to distinguish on a wall-mounted screen.

2. **NORMAL status has no colour.** Use `--fb-ok` (a muted grey). Calm must be
   visually silent so amber and red carry all the urgency. Do not add green.

3. **Restricted ≠ critical.**
   - `--fb-restricted` magenta, **dashed** stroke = permanently off-limits area
   - `--fb-critical` red, **solid** = something is wrong right now
   An intrusion is a magenta zone flashing red. Never draw a restricted boundary
   in red; it becomes unreadable next to a density alert.

Also required:
- **All numerals tabular.** Occupancy, density and timecodes update every second;
  use `--fb-font-data` with `font-variant-numeric: tabular-nums` or the layout
  twitches and the dashboard reads as unstable.
- **System fonts only.** No Google Fonts, no CDN font fetches. FinBlade deploys
  on-prem and air-gapped; a dashboard that phones out renders wrong.
- **Corner-bracket motif** (`.fb-bracket`) on zone cards and video frames only.
  It echoes framing marks on a monitored feed; used everywhere it means nothing.
- Keyboard focus visible, responsive to mobile, `prefers-reduced-motion` honoured.

The OpenCV annotator in the inference service must match the overlay colours at
the bottom of the theme file. **OpenCV takes BGR — reverse the hex channels.**

---

## Slice order (strict — do not skip ahead)

Each slice ends with: tests green -> evidence saved -> git commit -> next slice.

**Slice 1 — Spine.** Video -> detect -> track -> zone -> occupancy, printing to
console, annotated MJPEG on :8080. Runs on `media/clip.mp4`.

**Slice 2 — Metrics.** Density, dwell, inflow/outflow, anonymous person_ref,
5-second aggregate zone state.

**Slice 3 — Events + bus.** Emit the 6 event types into Redis Streams. Boundary
debounce enforced here.

**Slice 4 — API + persistence.** FastAPI endpoints with Pydantic schema
validation; Postgres tables; zone_state_ts written continuously.

**Slice 5 — Rule engine.** R-01/02/03/05/06/07/08 with hysteresis + debounce.
This slice must have thorough unit tests (see below).

**Slice 6 — Dashboard.** Live feed panel, zone cards, alert feed + acknowledge,
report generation. WebSocket with 5s REST polling fallback.

If you cannot complete a slice, do NOT proceed past it into dependent slices.
Log the blocker and instead deepen tests/robustness on completed slices.

---

## Testing — what you CAN verify yourself

Write real pytest unit tests for everything deterministic and headless:

- Point-in-polygon: point inside / outside / exactly on edge / in overlapping
  zones (restricted must win)
- Foot-point calculation from bbox coordinates
- Boundary debounce: N-1 frames does NOT trigger, N frames DOES
- Hysteresis: crossing 2.0 triggers amber; falling to 1.9 does NOT clear;
  falling below the clear threshold DOES clear
- 10s debounce: rapid oscillation produces exactly one alert, not many
- Camera offline: 29s silence no alert, 31s silence alert, recovery clears it
- Density arithmetic, capacity percentage, dwell accumulation
- Event payload schema validation: valid payload accepted, malformed rejected
- person_ref: same person -> same hash within session; output contains no PII

Also write an integration smoke test: feed the clip, assert the pipeline runs
N frames without exception and emits >0 events.

**These tests are your real deliverable overnight.** They are what makes the
morning verification fast.

---

## Evidence protocol (how you compensate for having no eyes)

Create `./evidence/` and during every pipeline run save:

1. **Annotated frames** — one every ~5 seconds of video, as
   `evidence/frames/frame_0001.jpg`, with boxes, track IDs, foot points, zone
   polygons, and occupancy labels drawn.
2. **Contact sheet** — tile those frames into `evidence/contact_sheet.jpg`
   (grid, ~4 across). This is the single most useful artifact for the human;
   they can spot a misplaced zone or missed detections in seconds.
3. **`evidence/metrics.json`** — per-run summary: frames processed, avg FPS,
   avg/min/max detections per frame, unique track IDs seen, track ID switch
   count estimate, occupancy per zone over time, events emitted by type.
4. **`evidence/events.jsonl`** — every event emitted, one JSON per line.
5. **`evidence/alerts.jsonl`** — every alert fired, with rule ID and timestamp.

Sanity flags to raise in MORNING.md if you see them in your own metrics:
- avg detections per frame is 0 -> detection is not working, blocker
- occupancy in every zone is always 0 -> zone polygons likely wrong (expected;
  the human hasn't drawn real ones yet — flag it, don't "fix" it by guessing)
- unique track IDs >> plausible number of people -> ID instability
- FPS below 5 on CPU -> note it; do not optimise, just report

---

## Deliverable: MORNING.md

Write `MORNING.md` before you finish. The human reads this first, half-awake.
Keep it blunt and short. Format:

```
# Morning report — <timestamp>

## Status
Slices complete: 1,2,3 / Slice 4 partial / Slices 5,6 not started

## What runs
- <one line each, factual, no claims about visual correctness>

## NEEDS YOUR EYES (do this first, ~5 min)
1. Open evidence/contact_sheet.jpg — are boxes on people? are zone polygons
   on the floor where you expect?
2. Open evidence/metrics.json — does avg detections/frame look plausible?
3. <anything else requiring judgement>

## Blockers (I could not resolve these)
- <what, what I tried, what I think it needs>

## Decisions I made without you
- <choice, why, how to reverse it>

## Tests
X passed / Y failed. Failing: <list>

## Suggested next steps
- <ordered>
```

---

## When you get stuck

Do NOT loop on the same error. Rule: **three genuine attempts, then stop.**
Log to BLOCKERS.md (what failed, what you tried, your best hypothesis), then
move to the next slice that doesn't depend on it. An hour lost to one error is
an hour not spent on five other slices.

Never disable a test to make the suite green. Never `try/except: pass` to make
an error disappear. Both destroy the human's ability to trust the morning report.
