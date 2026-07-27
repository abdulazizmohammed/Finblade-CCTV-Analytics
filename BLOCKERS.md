# BLOCKERS — things I could not resolve unattended

Timestamped, most-blocking first. Each: what failed, what I tried, best hypothesis.

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
