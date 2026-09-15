# BLOCKERS — things I could not resolve unattended

Timestamped, most-blocking first. Each: what failed, what I tried, best hypothesis.

---

## B-9 — The chart changes are UNVERIFIED against live pipeline data  [HIGH]

**What:** the new business-day counts and the `people_on_site` fallback are
tested and were driven end to end over real HTTP, but only against events
injected by hand. No camera has run against them.

**Why not:** the camera service is scheduled on the TEST SERVER, not this box.
On this box `media/` is empty, `scripts/bin/mediamtx` is absent, and the local
Postgres had no finblade schema at all until this session. Nothing here can
produce a detection.

**What it needs (~5 min once the stream is up at 06:00 KSA):**
1. `GET /api/v1/identity/counts` — check `window.unique_total` against a rough
   door count for the day. It should be plausible; the session `unique_total`
   beside it will still drift upward, and that is expected.
2. `GET /api/v1/identity/stats` — compare `stats.created` against
   `stats.matched`. If `created` dwarfs `matched`, ReID is re-minting people
   rather than recognising them, and the windowed figure will inflate too — just
   more slowly. That is the next thing to tune (see D-11, B-4).
3. `GET /api/v1/movement` — the `zone_transitions` labels should now read
   `LOBBY -> ATRIUM`, not `? -> ?`.

## B-10 — 6 test modules cannot run: pytest is not installed  [MEDIUM]

**What:** `test_areas`, `test_areas_api`, `test_cross_camera_dedup`,
`test_dashboard_sort`, `test_topology_survey`, `test_wisenet_import` all
`import pytest`, which is not in the venv and not in `requirements.txt` or
`constraints.txt`. They error at import; the other 1,269 tests run and pass.

**What I tried:** nothing — CLAUDE.md forbids changing pinned dependencies, and
installing a test dependency is the same class of change.

**Hypothesis / what it needs:** `pip install pytest` and add it to
`requirements.txt`, OR rewrite those six against `unittest` like the rest of the
suite (D-1 says the suite is deliberately stdlib-only, so the second is more
consistent with how this repo was built). Until then those areas have no
coverage running in CI.

**Also:** `tests/test_pg_grants.py` needs a superuser Postgres role to
`DROP ROLE`; it errors under the unprivileged `finblade` role this session
created locally.

---

## B-4 — No genuine two-camera footage: cross-camera accuracy is UNVALIDATED  [HIGH]

**What:** Cross-camera identity is built, wired and running, but nothing in
`media/` shows one person leaving one camera's view and entering another's.
Every clip is a single scene. So the one number that matters — how often it
correctly links the same person across two *real* cameras — cannot be measured.

**What I did instead:** `scripts/eval_cross_camera.py` derives a synthetic second
camera from a clip by a known transform (flip + brightness + scale). Because the
transform is invertible, boxes that overlap after mapping back are the same
person by construction — exact ground truth on real people. Results on
`media/1903279…` (400 frames): 27 ground-truth pairs, 26 matched (96.3%), 1 false
merge, 3 ambiguous candidates rejected by the margin rule.

**Why that is NOT the answer:** camera B is a transformed *copy*, so both views
share clothing, pose, lighting and viewpoint. Real cameras differ far more. Those
figures validate the plumbing and threshold behaviour end to end; they do not
predict real cross-camera accuracy, and must not be quoted to a client as if they
do. The measured separability makes the risk concrete: even on this easy case
true-pair and false-pair similarity distributions **overlap** (worst true 0.80 vs
best false 0.83).

**What it needs (~20 min of your time, then I can finish the job):**
1. Record two clips of the same people from two cameras, ideally one overlapping
   pair and one non-overlapping pair, with roughly synchronised clocks.
2. Note who appears where and when — even a rough list ("blue jacket: cam1
   0:05-0:20, cam2 0:31-0:50") is enough to score against.
3. Pace the walk between the cameras and put the real seconds into
   `config/topology.yaml` — the transit times there are placeholders, and they
   are what stops similar-looking strangers being linked.

With those, I can retune the threshold on real data (D-11) and give you a true
precision/recall figure instead of a proxy.

---

## B-5 — Topology transit times are placeholders  [MEDIUM]

**What:** `config/topology.yaml` ships with empty pair lists and a permissive
default window (2–120s). Unknown pairs are allowed through and flagged as
`unknown_pair`.

**Why it matters:** the transit gate is what stops appearance matching linking
two strangers in similar clothing at opposite ends of a site. Set the minimum too
low and strangers get merged; too high and real handovers are missed. This is
site knowledge — I cannot infer it from video, and guessing it would silently
degrade accuracy in whichever direction I guessed wrong.

**What it needs:** walk each camera-to-camera route, time it, fill in the file.
Mark any pairs whose views share floor area as `overlapping_pairs` — those behave
oppositely (dt≈0 is expected, not suspicious).

---

## B-6 — Identity bindings outlive the people they count  [MEDIUM]

**What:** `registry.site_occupancy()` — served as `live` by
`GET /api/v1/identity/counts` and as `site_occupancy` by `/identity/stats` — is
`len(active_refs())`, i.e. a count of live **bindings**, not of visible people.
It drifts upward and does not come back down.

**Evidence, measured on CAM-F-01:** the In-facility tile read 13 while
`people_in_view` was 8. Stopping the camera returned
`{"identity_bindings_released": 13}` in one go. Restarted clean, bindings then
tracked `people_in_view` within ±1 across a 150s watch (6/5, 8/8, 9/8, 7/8, 7/8,
9/8) — so this is not a fast leak, it is stale bindings accumulating across
worker restarts.

**Why it happens:** a binding is released when its track dies
(`reid.drop` → `POST /identity/release`), or wholesale by `release_camera` when
the API *notices* a camera stop or go offline. A worker killed abruptly releases
nothing, and `expire()` deliberately skips any identity that is still bound — so
orphaned bindings pin their identities permanently. `release_camera`'s own
docstring already records an earlier instance of exactly this ("a site total of
6 people while the only two running cameras reported 1 each").

**Worked around, NOT fixed.** The dashboard no longer renders this figure: the
In-facility tile and the per-camera live badge both use `people_in_view` from
the camera heartbeat, which is rebuilt from each frame's tracks and cannot
outlive anyone. The underlying count is still wrong for any other consumer.

**What it needs:** a liveness signal on the binding itself, so `expire()` can
reclaim one whose worker is gone. Identity `last_seen` is NOT that signal —
ReID resolve is budgeted to a subset of crops per frame, so a person standing in
plain view can have a `last_seen` of 50s+ (observed: six identities at exactly
51.0s while all six were on screen). Filtering on it would under-count. The
honest fix is probably for the camera heartbeat to carry its live track ids, and
for the registry to drop bindings no heartbeat has claimed for one offline
window.

---

## B-7 — No JavaScript runtime: dashboard behaviour tests cannot execute  [LOW]

**What:** `node` is absent from WSL and from the Windows host, and nothing else
(`qjs`, `d8`, `deno`, `bun`) is present either.

**Why it matters:** `tests/test_facility_tile.py` lifts `facilityWindow`,
`facilitySub` and `inViewTotal` verbatim out of `web/dashboard.html` and runs
them under node, so they exercise shipped code rather than a copy. Without a
runtime those 12 tests skip and only the static source guards run. The
In-facility arithmetic committed today is therefore **guarded but unexecuted**.

**What I did instead:** re-implemented `inViewTotal`'s exact semantics
(`+v||0` coercion, ONLINE filter) in Python and ran the same three cases plus
the live `/api/v1/cameras` payload — offline/disabled excluded, missing and null
fields coerce to 0 rather than NaN, empty list gives 0. That validates the
logic, not the JavaScript.

**What it needs:** `apt install nodejs` (no-download rule, so not attempted).
One command, and 12 real tests start running.

---

## B-9 — The in-house lab PPE checkpoint is silent on the site camera  [HIGH]

**What (2026-09-15):** `models/ppe_yolo11s_best.pt` is integrated (D-35), loads
on the pinned ultralytics, passes its tests and runs at 0.18 s/frame on CPU —
and on `media/LAB-PPE.mp4` it emits **no box above confidence 0.25**, on full
frames or on 84 padded person crops. At the visible threshold 0.5 it produces
nothing at all. The rule engine therefore judges nobody: every track stays
UNKNOWN (which the zone card counts as compliant — see D-35).

**Evidence:** `evidence/lab_ppe/` — `contact_sheet.jpg` (21 sampled frames,
person boxes teal, any PPE box ≥ 0.05 drawn), `clip_probe.json`,
`crop_probe.json`, `crops/` (the only crops that produced anything, all below
0.25), `ppe_check.log` (the real pipeline path: 20 PPE runs, 0 detections,
0 journalled), `CAM-LAB.jsonl` (empty journal — that emptiness is the finding).

**Domain gap, not threshold or scale — CONFIRMED by the human on
2026-09-15 after viewing `evidence/lab_ppe/contact_sheet.jpg`.** The 47 training-domain
test images are eye-level close-ups — two people fill a 640 px frame, white
coats, a production-line setting. The site clip is an overhead fisheye at
1284×716 with five people at ~145 px median height in blue gowns and hair
caps. Cropping people to 640 did not help, so it is not only resolution.
The model has not seen this viewpoint, garment colour, or lighting.

**What I tried:** thresholds down to 0.05 (13 boxes over 21 frames, no class
above 0.10 more than 4 times); person crops padded 25 %; imgsz 640 (its
training size). Not tried: larger imgsz (would not fix viewpoint), test-time
augmentation, lowering the pin (forbidden, and irrelevant — it loads).

**What it needs:** frames from the actual cameras in the training set. The
raw journal (`evidence/ppe_raw/<camera>.jsonl`) will show precision on
whatever the model does emit, but it cannot manufacture recall. Concretely:
sample frames from LAB-PPE.mp4 and the live lab cameras, label the five items,
add to `~/ppe/data/train` + `valid`, retrain (`~/ppe/train.py`), keep the
class order, overwrite `models/ppe_yolo11s_best.pt`. The class-order
assertion and `tests/test_lab_ppe_model.py` will tell you if the retrain
broke the contract.

**Not a code blocker.** Everything downstream of the detector is exercised
(184 PPE tests, 1781 total) and the integration is complete; what is missing
is a model that responds to this footage.

---

## B-8 — Medical PPE cannot run: no weights, and YOLO26 needs a pin change  [SUPERSEDED by D-35 / B-9]

> **2026-09-15:** the medical slot now runs FinBlade's own YOLO11s lab
> checkpoint (`medical_ppe.checkpoint: finblade_lab_yolo11s`), which loads on
> the pinned 8.3.40. The YOLO26 candidate below is still selectable and still
> cannot load. The remaining problem is B-9. Detail kept for the record.

**What:** the medical/laboratory PPE profile is implemented, tested and
configurable, but no medical detector can be loaded.

**Two independent causes:**

1. **Weights absent.** `models/` has no medical checkpoint. The candidate,
   `stormbreaker20/yolo26s-mppe-detector-v2`, was never downloaded.
2. **The pinned framework cannot load it.** `ultralytics==8.3.40` has **no
   YOLO26 support at all** — its model families are 3/5/6/8/9/10/11/rt-detr and
   zero files in the package reference yolo26. YOLO26 requires **8.4.x**.

**Why I did not just lift the pin.** CLAUDE.md forbids changing pinned versions,
and the risk is real rather than procedural: **ByteTrack ships inside
ultralytics** and we call `model.track(tracker="bytetrack.yaml")`. A version
bump can change track-ID assignment, and track IDs key the ReID bindings, dwell,
R-05 loitering, the PPE state machine and the evidence crops. It would also
reload yolo11s and both YOLOv8 checkpoints under new code, and torch
2.11.0+cu128 compatibility with 8.4.x is unverified.

**Licence conflict, unresolved.** The repository states **MIT**, but its stated
base is `yolo26s.pt`, which is Ultralytics **AGPL-3.0**. An AGPL derivative
relabelled MIT is not something to rely on. Verify repository, model and dataset
terms before any commercial claim. Recorded in `models/MANIFEST.yaml` with
`license_verified: false`.

**Also worth knowing about the candidate:** 2,931 images, 60 epochs, trained at
**512px** against our 1920×1080 cameras, and the model card publishes **no
mAP/precision/recall at all**. Only 4 of its 14 classes are negatives, so gown,
scrubs, face shield, goggles, coverall and shoe covers could be judged only on
absence.

**What it needs:** a decision on the pin, or a medical checkpoint built on
YOLOv8/YOLO11 — which would run on 8.3.40 today. The vocabulary, anatomy, rules,
UI and tests are all model-agnostic, so swapping in a different checkpoint is a
change to `MEDICAL_CLASS_MAP` alone.

**CANDIDATES EVALUATED SO FAR, and why the shortlist is empty:**

| Candidate | Outcome |
|---|---|
| `stormbreaker20/yolo26s-mppe-detector-v2` | Cannot load — YOLO26 needs ultralytics 8.4.x. No published metrics. Licence conflict (MIT claimed over an AGPL base). |
| `keremberke/yolov8s-protective-equipment-detection` | **Measured and rejected** — see `models/MANIFEST.yaml`. Runs, but never emits `mask` or `no_mask` on either clip at any threshold down to 0.05, and detects zero helmets on construction footage. Published mAP50 0.278 was honest. |
| CPPE-5 | Right domain (coverall, face shield, gloves, mask, goggles) but POSITIVE CLASSES ONLY, and no released YOLOv8/YOLO11 checkpoint found — it is a dataset. |
| Hygiene compliance, Sensors 2025, doi 10.3390/s25196140 | **Best remaining candidate.** 31k images, hospital domain among its sources, paired classes plus `incorrect_mask` and `hairnet`/`no_hairnet`, mAP50 0.857, and a YOLOv8n variant that would run on the current pin. **Release URL and licence UNVERIFIED** — MDPI, PMC and ResearchGate all block automated access. Needs someone with journal access to open the Data Availability Statement. |

**The uncomfortable conclusion.** No public checkpoint is trained on medical
labs at ceiling-CCTV distance. The industrial model asserts `NO-Mask` on a
visibly masked technician at 1.00/frame; the one alternative with the right
class structure is silent. Both failure modes end in a false accusation under
R-11, one loudly and one through the absence path. The real answer is footage
from the actual site, and probably a model fitted to it — which reopens the
day-one decision to cut all training and fine-tuning. That cut was right for a
Sunday deadline and may not fit a medical-lab product.

---

## B-1 — Vision pipeline cannot execute (detection deps + weights absent)  [RESOLVED]

> RESOLVED in a later session once the human authorised installs: bootstrapped
> pip into ~/.local (no sudo), created `.venv`, installed CPU torch + the pinned
> deps + lapx, downloaded `models/yolov8n.pt`. Detection now runs on the clip;
> real evidence in `evidence/`. Detail below kept for the record.


**What:** The detect → track slice (Slice 1 / UC-06..09) cannot run in this
environment.

**Evidence (probed `/home/usv/finblade-cctv`, WSL Ubuntu-22.04, Python 3.10.12):**
- `ultralytics` — NOT installed
- `opencv` (`cv2`) — NOT installed
- `torch` — NOT installed
- `models/` — empty. `models/yolov8n.pt` is **absent**; no OpenVINO IR either.

**What I tried:** Probed the interpreter for every dep; inspected `models/`.
I did **not** install anything or download weights — per your standing rule
("assume no network; do not download anything"; weights are human-placed).

**Hypothesis / what it needs (morning, ~10 min):**
1. Place real weights at `models/yolov8n.pt` (COCO, person=class 0).
2. Create a venv and install the CPU stack (NO GPU/OpenVINO — see DECISIONS D-2):
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install "ultralytics==8.3.40" "opencv-python-headless==4.10.0.84" \
               "numpy==1.26.4" "pyyaml==6.0.2" torch --index-url <your mirror>
   ```
   (`requirements.txt` pins kept intact — I did not change them.)
3. Then run `python services/inference/run_cpu.py` (CPU runner I added — see D-3).

The **entire deterministic core** (metrics, events, rule engine, schema,
geometry) is built and unit-tested WITHOUT any of this — see MORNING.md.

---

## B-2 — Input files misnamed / missing  [MEDIUM]

**What:** Config + README expect `media/clip.mp4`; it does not exist.

**Present instead:** `media/1903279-uhd_1920_1440_30fps.mp4` (91 MB, 1920×1440,
30 fps). This is almost certainly the clip you recorded, under its original
download name.

**Missing entirely:** `media/clip_dense.mp4` (optional dense-crowd clip).

**What I tried:** Listed `media/`. I did **not** rename the file — renaming or
assuming identity is a judgement call I left to you (one command:
`mv media/1903279-uhd_1920_1440_30fps.mp4 media/clip.mp4`). The CPU dev config
I added (`config/cameras.dev.yaml`) already points at the real filename so you
can run without renaming.

---

## B-3 — Backing services absent (Redis / Postgres / Docker)  [MEDIUM]

**What:** Slice 4 (API + persistence) and the event bus cannot run end-to-end.

**Evidence:** `docker`, `redis-server`, `psql` all "command not found";
`redis`, `psycopg2`, `fastapi`, `uvicorn` Python packages NOT installed.

**What I tried:** Probed binaries + packages. Did not install (no-download rule).

**Hypothesis / what it needs:** `docker compose up` once Redis/Postgres services
are added to compose (I left the human's compose intact and drafted the api/db
config separately). The API request/response **logic and schema validation are
implemented and unit-tested pure-Python**, so the untested part is only the
network/DB plumbing.
