# Morning report — 2026-07-24 (overnight, unattended)

## TL;DR
The deterministic brain of the system is **built and verified**: metrics, events,
schema, rule engine, API logic, dashboard — **84 unit/integration tests, all
green**, runnable in 0.005s with `python3 -m unittest discover -s tests`.
The **vision front-end could not run** (no model weights, and cv2/ultralytics/
torch aren't installed; you said no downloads). So I did NOT fake it — I built
the CPU runner ready to go and left a blocker. **~15 min of your time unblocks it.**

## Status
Slices complete: **2, 3, 5 fully; 4 (logic) fully; 6 (build+theme) fully.**
Slice **1 (spine) code-complete but UNRUN** — blocked on weights + detection deps.
Vision correctness: **unverified by design** (I have no eyes; see NEEDS YOUR EYES).

## What runs (verified tonight)
- `finblade/` core, pure stdlib, **84 tests green**:
  - point-in-polygon (inside/outside/on-edge/on-vertex/concave), foot point
  - restricted-zone-wins assignment
  - N-frame boundary debounce (N-1 no-fire, N fires; flicker doesn't flip)
  - density, capacity %, dwell (accumulate + reset), inflow/outflow windowed
  - anonymous person_ref (salted hash, PII-guarded, verified opaque)
  - 6 event types + schema validation (valid accepted, malformed rejected)
  - **rule engine R-01/02/03/05/06/07/08** with hysteresis (1.9 does NOT clear)
    + 10s debounce (oscillation → exactly one alert) + camera offline 29s/31s/recover
  - API ingest/ack/zone-state logic via in-memory store+bus (no DB/HTTP needed)
  - themed occupancy report renders with zero hard-coded colours
- Synthetic scenario replay (`scripts/gen_synthetic_evidence.py`) drove the REAL
  rule engine to fire R-03→R-01→R-02(+clear)→R-06 intrusion→R-07 offline. Output
  in `evidence/`. This is UC-57 (density-critical on demand, no crowd).

## What does NOT run (and why)
- **Detection / tracking / annotated feed** — `models/yolov8n.pt` absent and
  `opencv`/`ultralytics`/`torch` not installed. `run_cpu.py` exits with a BLOCKER
  rather than fake a detector. → BLOCKERS.md B-1.
- **API over HTTP + Postgres + Redis** — fastapi/uvicorn/redis/psycopg2 not
  installed; no redis-server/postgres/docker present. Logic is tested via
  in-memory backends; only the network/DB plumbing is unexercised. → BLOCKERS.md B-3.

## NEEDS YOUR EYES / HANDS (do this first, ~15 min)
1. **There is NO contact_sheet.jpg yet** — it needs real detection. To get it:
   - Place weights at `models/yolov8n.pt` (COCO, person=0).
   - `python3 -m venv .venv && source .venv/bin/activate`
   - `pip install ultralytics==8.3.40 opencv-python-headless==4.10.0.84 numpy==1.26.4 pyyaml==6.0.2 torch`
     (requirements.txt pins untouched; NO OpenVINO/GPU — CPU per rule 4)
   - `python services/inference/run_cpu.py --config config/cameras.dev.yaml --seconds 60 --no-serve`
   - **Then** open `evidence/contact_sheet.jpg`: are boxes on people? do the zone
     polygons sit on the floor? (polygons are still the placeholder rectangles.)
2. Your clip is present but named `media/1903279-uhd_1920_1440_30fps.mp4`, not
   `clip.mp4`. The dev config already points at the real name, so no rename needed
   — but confirm that IS the intended clip. → BLOCKERS.md B-2.
3. Open `evidence/metrics.json` — note `detection_ran: false`. After step 1 it will
   carry real avg-detections/frame; sanity-check that number is plausible.

## Blockers (could not resolve unattended)
- **B-1** detection deps + weights absent → whole vision path unrun.
- **B-2** clip misnamed vs config/README (`clip.mp4`).
- **B-3** Redis/Postgres/Docker absent → API/bus/persistence unrun end-to-end.
Full detail, what I tried, and fixes in **BLOCKERS.md**.

## Decisions I made without you (all reversible — see DECISIONS.md)
- Built the core stdlib-only + `unittest` so tests actually RUN tonight (no pytest
  installed, no downloads). Pytest-compatible naming if you add it later.
- CPU-only: **did NOT touch** `config/cameras.yaml` (GPU/OpenVINO) or `main.py`.
  Added `config/cameras.dev.yaml` + `services/inference/run_cpu.py` instead.
- Network was UP; I still downloaded nothing, per your instruction.
- Did NOT rename your clip; pointed config at its real filename.

## Tests
**84 passed / 0 failed.** `python3 -m unittest discover -s tests` (0.005s).
Coverage maps to CLAUDE.md's required list: PIP incl. on-edge & restricted-overlap,
foot point, N-frame debounce, hysteresis (1.9 no-clear), 10s debounce
one-alert, camera 29/31/recover, density/capacity/dwell, event schema valid+
malformed, person_ref no-PII, plus a headless post-detection integration test.

## Suggested next steps (ordered)
1. Do the 15-min unblock above; eyeball the contact sheet + metrics.
2. Draw real zone polygons into `config/cameras.yaml` from `media/cam1_frame.jpg`,
   copy them into `cameras.dev.yaml`; re-run; confirm occupancy vs head-count (UC-13).
3. Stand up Redis + Postgres (add to docker-compose), `pip install fastapi uvicorn
   redis psycopg2-binary`, apply `services/api/ddl.sql`, run
   `uvicorn services.api.app:app --port 8000`, open `web/dashboard.html`.
4. Wire `run_cpu.py` to POST events to the API (currently writes evidence jsonl;
   the HTTP client call is a ~10-line addition once the API is up).
5. Write the demo runsheet (UC-60, your task) once the feed is visually confirmed.

## Where things are
- Core: `finblade/`  · Tests: `tests/`  · CPU runner: `services/inference/run_cpu.py`
- API: `services/api/`  · Dashboard: `web/dashboard.html`  · Evidence: `evidence/`
- Logs: `BLOCKERS.md`, `DECISIONS.md`
