# Morning report — 2026-07-24 (architect phases 1a–10)

## TL;DR
The full stack is built and green. On top of the earlier overnight core, I worked
through a 10-phase architect plan — one phase per commit, tests + a live run after
each. **164 unit/integration tests pass** (`python3 -m unittest discover -s tests`,
~1.4s). The vision path runs on the **NVIDIA RTX PRO 2000 (Blackwell)** GPU at
~23 FPS on the synthetic clip. **I still cannot see** — bounding-box / zone-polygon
placement is unverified by me and needs your eyes (see NEEDS YOUR EYES).

Skipped **Phase 11 (ReID/VLM)** on purpose — you told me to, and it collides with
CLAUDE.md's "no cross-camera ReID" cut and the no-network rule anyway.

## Status
Phases complete: **1a, 1b, 2, 3, 4, 5, 6, 7, 8, 9, 10** — all committed
(latest `f73799b`). Phase 11 intentionally skipped. Vision correctness:
**unverified by design.**

## What runs (verified live this session)
One command brings it all up: `bash scripts/demo.sh` (API + two camera feeds), then
open http://localhost:8000/web/dashboard.html.
- **GPU detection + ByteTrack** on the synthetic clip, ~23 FPS, non-blocking capture
  thread (a slow consumer never stalls the camera).
- **Zones**: editor at `/tools/zone-editor.html` → save to server → runner loads them
  (normalized polygons, per-zone thresholds/loiter/type/colour).
- **Metrics/events**: occupancy, density, capacity %, dwell, inflow/outflow +
  net/5m/15m windows, the full event set incl. restricted entry/exit (with dwell
  duration), loitering start/end, camera online/offline/recovered.
- **Rule engine** R-01/02/03/05/06/07/08 with **sustained-duration** density
  hysteresis (must hold >threshold for 10s to fire — brief spikes no longer alert).
- **Alert lifecycle**: OPEN → ACK → RESOLVED/DISMISSED + operator note; feed +
  history reflect it.
- **Camera health screen** (`/web/cameras.html`): live state/fps/resolution, central
  simulate-failure / restore, register/delete.
- **Dashboard**: live feeds with **overlay toggles** (zones/boxes/ids/feet/dwell),
  zone cards with occupancy **sparklines**, alert feed, KPIs.
- **Reports**: enriched occupancy (per-zone peak/avg + alert counts), **CSV export**,
  and an in-process **R-08 scheduler** (hourly; on-demand + saved-report list).
- Automated end-to-end proof: `bash scripts/demo_pass.sh` walks events → alert
  resolve → camera sim/restore → on-demand report → CSV → scheduled report → health.

## NEEDS YOUR EYES (do this first, ~10 min)
1. Open `evidence/contact_CAM-SYN-01.jpg` — are boxes on people? Do the zone
   polygons sit on the floor where you expect? (The synthetic clip's polygons are
   demo placeholders, not surveyed floor coordinates.)
2. Open `evidence/metrics_CAM-SYN-01.json` — is avg detections/frame plausible for
   the scene? Any wild track-ID count?
3. For your REAL terminal clip (`config/cameras.dev.yaml` /
   `media/1903279-uhd_1920_1440_30fps.mp4`): draw real zone polygons in the editor
   and save them; the placeholders won't match your floor.

## Blockers (could not resolve)
- **App-level HTTP endpoints aren't in the unittest suite.** FastAPI's TestClient
  needs `httpx`, which isn't installed and I can't fetch (no-network rule). I cover
  the endpoints with the **live** `scripts/demo_pass.sh` instead — that's how I
  caught a real 500 (a missing `Response` import) in Phase 10. If you `pip install
  httpx`, I can add a proper TestClient smoke suite.
- **Zone polygons are placeholders** for both the synthetic and the real clip —
  a human-judgement task I won't guess at (CLAUDE.md rule 3).

## Decisions I made without you (reversible)
- **GPU**: used the NVIDIA RTX PRO 2000 (CUDA, torch 2.11+cu128), NOT the Intel Arc
  iGPU (WSL passthrough is the known time-sink CLAUDE.md warns off). Configs default
  to `device: cuda` with a clean CPU fallback (`_resolve_device`).
- **R-08 scheduler** runs in-process (asyncio), interval via
  `FINBLADE_REPORT_INTERVAL` (default 3600s) — no external scheduler/cron, per the
  no-external-infra rule.
- **Sustained-duration hysteresis**: changed the density latch from fire-on-crossing
  to hold-for-10s (Req 9). This makes density alerts appear more slowly by design;
  the sustain window is configurable if you want the demo snappier.
- Did **not** touch `config/cameras.yaml` (your GPU/OpenVINO zones) or `main.py`.
  Added variant configs (`cameras.synthetic.yaml`, `cameras.cam2.yaml`, etc.).
- Skipped Phase 11 (ReID/VLM) per your instruction + the cut-scope/no-network rules.

## Tests
**164 passed / 0 failed.** `python3 -m unittest discover -s tests` (~1.4s, stdlib
only). New since the overnight 84: TrackReaper/registry, CameraWorker, per-zone
rule thresholds, sustained hysteresis, ZoneStats/flow windows, camera-health +
SQLite store (incl. old-DB migrations), alert lifecycle, occupancy-report
enrichment + CSV + reports persistence, and all-pages theme compliance.

## Suggested next steps (ordered)
1. Eyeball the contact sheet + metrics (above); draw real zone polygons for your clip.
2. `pip install httpx` → I add a FastAPI TestClient smoke suite so the HTTP layer
   is covered by unit tests, not just the live demo pass.
3. If you want cross-camera ReID or a VLM scene-description feature (the cut items),
   place the model weights in `models/` and say so — I'll wire it then, not before.
4. Optionally lower the density-hysteresis sustain window for a snappier live demo.

## Where things are
- Core: `finblade/` · Tests: `tests/` · Runner: `services/inference/run_cpu.py`
- API: `services/api/` · UIs: `web/` (dashboard, cameras, history, report) +
  `tools/zone-editor.html` · Evidence: `evidence/`
- Demos: `scripts/demo.sh` (interactive), `scripts/demo_pass.sh` (automated)
- Logs: `BLOCKERS.md`, `DECISIONS.md`
