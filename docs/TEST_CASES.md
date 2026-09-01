# FinBlade CCTV — test cases

How each capability in [CAPABILITIES.md](CAPABILITIES.md) is verified, what is
covered automatically, and what still needs a human. Use case IDs (`UC-nn`)
refer to [USE_CASES.md](../USE_CASES.md); rule IDs to `finblade/rules.py`.

**Last updated:** 2026-09-01

---

## Running the suite

```bash
.venv/bin/python -m pytest tests/ -q
```

Postgres-backed tests connect to `127.0.0.1:5432` by default and skip themselves
if nothing is listening. `FINBLADE_TEST_DSN` overrides the DSN;
`FINBLADE_INMEMORY=1` selects the in-memory store for the rest.

The suite is headless. It needs no video, no GPU, no camera and no network.

---

## Part A — automated

Every route is now referenced by at least one test (57 route paths, 0
uncovered), and every module under `finblade/` and `services/api/` has at least
one test file naming it.

| Area | Verified by |
|---|---|
| Geometry, foot point, point-in-polygon incl. on-edge | `test_geometry.py`, `test_zones.py` |
| Zone assignment, restricted precedence, detection masks | `test_zones.py` |
| Boundary debounce, N-1 vs N frames | `test_debounce.py` |
| Density, capacity %, dwell, inflow/outflow, 5s aggregate | `test_metrics.py` |
| Anonymous refs, PII guard | `test_identity.py`, `test_events.py` |
| Event schema — valid accepted, malformed rejected | `test_events.py` |
| Observation schema, frame discipline, privacy guards | `test_observation.py` |
| Observation HTTP routes | `test_observation_api.py` |
| Fusion accounting per source | `test_fusion_service.py` |
| Rule engine, hysteresis, 10s debounce | `test_rules.py` |
| Flow rules — wrong way, group crossing | `test_flowrules.py` |
| Camera offline 29s/31s, recovery | `test_rules.py`, `test_integration_ops.py` |
| Cross-camera identity, topology gating, transit windows | `test_globalid.py`, `test_topology.py`, `test_identity_transit.py` |
| Appearance banks, crop gating, sampling | `test_appearance.py` |
| Cross-camera dedup, distinct occupancy | `test_cross_camera_dedup.py`, `test_areas.py` |
| Facility roster, doors, baseline, drift | `test_presence.py`, `test_facility_baseline.py`, `test_facility_ingest.py` |
| Facility counts on `fb:facility`, gate, keepalive | `test_facility_counts_stream.py` |
| Store parity — in-memory vs Postgres | `test_store_conformance.py` |
| Analytics views, journey views | `test_analytics_views.py`, `test_journey_views.py` |
| Column grants, `person_ref` unreachable | `test_pg_grants.py` |
| Auth roles, query-key restriction | `test_auth.py` |
| Credential redaction | `test_credential_redaction.py` |
| Forwarder cursor, retry, outage replay | `test_forwarder.py` |
| Camera pipeline supervision, argv injection | `test_camera_manager.py` |
| Report generation, CSV/JSON parity | `test_uncovered_routes.py` |
| Pipeline smoke — N frames, >0 events | `test_pipeline_integration.py` |
| Theme compliance | `test_theme_compliance.py` |

### What automated tests cannot establish

Everything above tests logic given inputs. **None of it establishes that the
inputs are right.** A perfect pass is consistent with every bounding box being
on a chair, and with every zone polygon drawn on a wall. That is Part B's job.

---

## Part B — manual, requires human eyes

These exist because no automated check in this repo can perform them. Run them
before a demo or a release. Each names what to look at and what "pass" means.

### TC-M-01 — Detections are on people (UC-06)
1. Run a pipeline against `media/clip.mp4`.
2. Open `evidence/contact_sheet.jpg`.

**Pass:** boxes sit on people. **Fail:** boxes on chairs, reflections, posters,
or a TV showing people — mask those regions with an `UNMONITORED` zone rather
than raising `conf_threshold` (see `config/cameras.template.yaml`).
**Also check** `evidence/metrics.json`: avg detections/frame of 0 is a blocker.

### TC-M-02 — Zone polygons are on the floor (UC-10)
1. Open the contact sheet, or `tools/zone-editor.html` over a still.

**Pass:** each polygon covers the floor area it names. **Fail:** occupancy
permanently 0 in every zone usually means polygons are wrong, not that the code
is — do not "fix" it by adjusting thresholds.

### TC-M-03 — Occupancy matches a head count ±1 (UC-13)
1. Pick a frame with several people in one zone.
2. Compare the zone card against a manual count.

**Pass:** within ±1. This is the headline acceptance test.

### TC-M-04 — Track IDs survive brief occlusion (UC-08)
1. Watch someone pass behind an obstacle or another person.

**Pass:** the ID is the same after. **Fail:** unique track IDs far exceeding the
plausible number of people in `evidence/metrics.json`.

### TC-M-05 — No zone flapping at a boundary (UC-12)
1. Watch someone stand astride a zone edge.

**Pass:** the zone assignment holds; no per-frame flicker, no event storm.

### TC-M-06 — Cross-camera identity is the same person (UC-08 / identity)
1. With two cameras running, walk one person between them.
2. `GET /api/v1/identity/list?cross_camera_only=1`.

**Pass:** one `gp_` ref spans both cameras. **Fail either way matters:** two refs
for one person means a missed handover; one ref for two people is worse. Check
`GET /api/v1/identity/stats` — a non-zero `unknown_pair` means
`config/topology.yaml` does not cover the cameras actually running.

### TC-M-07 — Facility count survives leaving camera view
1. Walk in through a door zone, then out of all camera coverage.
2. `GET /api/v1/facility/occupancy`.

**Pass:** still counted. **Then** walk out through the door: count decrements.
**Check** `GET /api/v1/facility/stale` for entries nobody has seen — that is the
drift report, and it is read-only by design.

### TC-M-08 — Alert severity reads correctly on a wall display (UI theme)
1. Trigger an amber density alert, then a red one, then a restricted intrusion.

**Pass:** NORMAL is colourless grey; teal appears only on chrome, never as
status; a restricted zone is magenta and dashed, flashing red on intrusion —
never drawn solid red. These are semantic rules, not cosmetic; see CLAUDE.md.

### TC-M-09 — Dashboard numerals do not twitch
1. Watch occupancy and density update for 30 seconds.

**Pass:** columns stay still (tabular numerals). **Fail:** layout shifts as
digits change.

### TC-M-10 — Air-gap check
1. Load the dashboard with the network blocked.

**Pass:** renders identically. **Fail:** any request to a CDN or font host.

---

## Part C — live-dependency checks

Automated tests fake these. Run once per environment.

### TC-L-01 — Redis streams
```bash
redis-cli -n 0 XLEN fb:events
redis-cli -n 0 XLEN fb:facility
redis-cli -n 0 XRANGE fb:facility - + COUNT 5
```
**Pass:** `fb:facility` carries `FACILITY_COUNTS` records and `fb:events` does
not. Confirm the live backend at `/api/v1/health` →
`checks.facility_counts.bus` reads `RedisStreamBus`, not `InMemoryBus`.

### TC-L-02 — Postgres schema and views
```bash
.venv/bin/python scripts/pg_apply.py "$DATABASE_URL"
```
**Pass:** `ALL CHECKS PASSED` — 14 tables, 17 views, chatbot query shape runs.

### TC-L-03 — Services survive a restart
```bash
sudo systemctl restart finblade-postgres
systemctl is-active finblade-postgres redis-server
```
**Pass:** both active, database intact.
**NOT YET RUN:** a full `wsl --shutdown` cold boot.

### TC-L-04 — RTSP ingest
```bash
bash scripts/verify_rtsp_cross_camera.sh
```
**Pass:** streams open, workers post events, no reconnect loop.

---

## Part D — known gaps

Stated rather than hidden. None is a regression; all are untested areas.

| Gap | Why it matters | Suggested cover |
|---|---|---|
| `services/inference/run_cpu.py` (72 KB) has no direct unit test | The largest single file; the whole vision loop | Extract pure helpers and test those; the cv2/YOLO path stays manual |
| No end-to-end test from video frame to alert | Each stage is tested; the seams between them are not | A recorded-clip integration test asserting an expected alert fires |
| `wsl --shutdown` cold boot unverified | The systemd units are new | Run it once and record the result |
| Observation → event conversion | Does not exist yet; nothing consumes observations downstream | Part B |
| Load and soak behaviour | Unknown at many cameras or over days | Not attempted |
| `web/*.html` has no browser-level test | Only theme compliance is checked statically | Manual, TC-M-08/09/10 |

---

## Before adding a new capability

1. `pytest tests/ -q` — green.
2. New capability has automated tests for everything deterministic.
3. Anything needing eyes has a `TC-M-nn` entry added here.
4. [CAPABILITIES.md](CAPABILITIES.md) updated **in the same commit**
   (CLAUDE.md standing rule 9).
5. Route coverage still complete — every `@app` route referenced by some test.
