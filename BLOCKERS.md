# BLOCKERS — things I could not resolve unattended

Timestamped, most-blocking first. Each: what failed, what I tried, best hypothesis.

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
