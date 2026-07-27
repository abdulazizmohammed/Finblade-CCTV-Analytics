# Morning report — 2026-07-26 (cross-camera identity)

## TL;DR
You asked for cross-camera person tracking and said to forget the demo. It is
built, wired, running, and measured. **306 tests pass** (was 168). One person
walking between two cameras now gets one `global_ref`, and site occupancy stops
double-counting someone visible to two cameras at once.

**The number I cannot give you is the one that matters most:** how accurately it
links people across two *real* cameras. There is no such footage in `media/` —
every clip is a single scene. I built a ground-truth harness around a synthetic
second camera to validate the pipeline, but that is a proxy, not an answer. See
B-4; it needs ~20 minutes of your time to fix.

## Status
Cross-camera identity: **built and running.** Accuracy on real cameras:
**unvalidated.** Everything from previous sessions still green and untouched.

## What runs
- **Two independent camera processes → one shared identity registry** over HTTP.
  Proof: `bash scripts/verify_cross_camera.sh` — 4 identities, all 4 seen by both
  cameras, 18 matches, site occupancy correctly 2 people from 4 local bindings.
- **OSNet embeddings** (`models/osnet_x0_25_msmt17.pt`, 3 MB) on the GPU, behind a
  crop quality gate (size / confidence / aspect / frame-edge / occlusion) and a
  sampling budget. In the live run the gate rejected ~31% of candidate crops.
- **Three-gate matching:** sticky binding → transit feasibility → appearance with
  a runner-up margin. Ambiguity creates a new identity rather than guessing.
- **Topology config** (`config/topology.yaml`) distinguishing overlapping pairs
  (dt≈0 expected) from non-overlapping ones (a walk is required).
- **No FPS cost:** 24.7 FPS with ReID vs 22.0 without, uncapped, same clip.
- **Endpoints:** resolve / release / merge / stats / list / `{ref}` journey.
- **Privacy:** embeddings are RAM-only, cleared on track reap and TTL expiry,
  never persisted or logged. Tests assert no endpoint returns a vector.

## NEEDS YOUR EYES (do this first, ~20 min)
1. **Record real two-camera footage.** This is the blocker on everything else
   (B-4). Same people, two cameras, roughly synced clocks — one overlapping pair
   and one non-overlapping pair if you can. A rough note of who appears where and
   when is enough for me to score against.
2. **Pace the walks between cameras** and fill in `config/topology.yaml`. The
   transit times there are placeholders. This gate is what stops two similar
   strangers being merged; wrong numbers fail in both directions (B-5).
3. **Read `evidence/cross_camera_eval_dense.json`**, specifically `separability`.
   True-pair similarities min 0.80 / median 0.90; false pairs median 0.61 but
   **max 0.83**. They overlap. That is the honest risk picture.
4. **Decide the privacy posture with the client** (D-9). The system now holds
   biometric templates in memory, where before it held none. I kept them
   ephemeral and un-persisted, but "we hold no PII" is a weaker statement than it
   was, and that is a conversation, not a code change.

## Blockers (I could not resolve these)
- **B-4 — no genuine two-camera footage.** Cross-camera accuracy is unvalidated.
  Proxy result on a synthetic second camera: 26/27 pairs matched (96.3%), 1 false
  merge. Do not quote that to a client — camera B was a transformed copy of
  camera A, sharing clothing, pose and lighting.
- **B-5 — topology transit times are placeholders.** Site knowledge I cannot
  infer from video.
- **Threshold is provisional** (D-11). Raised 0.62 → 0.70 because 0.62 sat at the
  false-pair median. Cannot be finalised without real footage.

## Decisions I made without you (all reversible, detail in DECISIONS.md)
- **D-7** Built a feature CLAUDE.md explicitly cuts — you overrode it. Fully
  off-switchable (`reid.enabled: false`); nothing else in the pipeline calls it.
- **D-8** Used the network and added `boxmot`. **Caught a landmine:** the plain
  install wanted numpy 2.2.6 over your pinned 1.26.4 — an ABI break that could
  have taken torch/ultralytics/opencv down. Installed under a constraints file;
  every pin verified untouched. Pre-change venv snapshot at
  `/tmp/venv_before_reid.txt`.
- **D-9** Embeddings RAM-only, never persisted.
- **D-10** Bias toward splitting over merging.
- **D-11** Threshold 0.70, provisional.

## Tests
**306 passed / 0 failed** (~1.4s). Was 168 at the start of this session.
New: topology gating, crop quality + sampling, feature banks, the matcher
(including "physics beats appearance" and the ambiguity case), the identity
service, the ReID client, and **11 HTTP tests**.

Two real bugs the tests caught, both fixed:
- `IdentityService` did `registry or GlobalIdentityRegistry(...)`, but the
  registry defines `__len__` — so an empty one is falsy and the caller's registry
  and topology were silently discarded at startup, exactly when it is always
  empty.
- With the API unreachable, every track retried resolve on every frame — 1877
  failed POSTs in a 30s benchmark. Now backed off to one attempt per track per 2s.

**Also unblocked:** the FastAPI TestClient blocker from the last report. `httpx`
was the missing piece and it arrived as a boxmot dependency, so the HTTP layer is
now covered by unit tests rather than only the live demo script.

## Suggested next steps (ordered)
1. Record the two-camera footage (B-4) and fill in `config/topology.yaml` (B-5).
2. I retune the threshold on that footage and give you a real precision/recall
   figure, replacing the proxy.
3. Decide the client-facing privacy position (D-9).
4. Surface identity in the dashboard — a `global_ref` badge on tracked people and
   a journey view. Deliberately not built yet: showing identities whose accuracy
   is unvalidated invites more trust than the numbers currently support.
5. Persist completed journeys. `Track.summary()` and `GlobalIdentity.summary()`
   both produce persistable rows that nothing currently writes.

## Where things are
- Identity core: `finblade/{appearance,globalid,topology}.py`
- Service + routes: `services/api/identity.py`, routes in `app.py`
- Worker side: `services/inference/reid_client.py` (wired into `run_cpu.py`)
- Config: `config/topology.yaml`, test rig `config/{cameras,topology}.xcam.yaml`
- Proof: `scripts/verify_cross_camera.sh` (live), `scripts/eval_cross_camera.py`
  (measured), `evidence/cross_camera_eval_dense.json`
