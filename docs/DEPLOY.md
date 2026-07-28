# Deploying FinBlade CCTV to a test environment

Written from a working install, with the failures that actually happened during
bring-up called out. Roughly 30–45 minutes, most of it waiting on the torch
download.

---

## 1. What you are deploying

Two kinds of process, talking over HTTP on localhost:

```
                                 ┌──────────────────────────────┐
  camera 1 ──rtsp──► run_cpu.py ─┤                              │
                     :8090 MJPEG │   API  (uvicorn, :8000)      │
  camera 2 ──rtsp──► run_cpu.py ─┤   - SQLite  data/finblade.db │──► dashboard
                     :8091 MJPEG │   - identity registry (RAM)  │    /web/
  camera N ──rtsp──► run_cpu.py ─┤                              │
                                 └──────────────────────────────┘
```

* **One `run_cpu.py` per camera.** Detection, tracking, zones, metrics and rules
  run per camera; each serves its annotated MJPEG on its own port. The API
  launches these for you when a camera is added from the UI.
* **One API process.** Holds the database, the rules history, and the single
  cross-camera identity registry — identity has to be central because each
  camera worker only knows its own local track ids.
* **No Redis or Postgres needed.** SQLite and an in-process event bus are the
  defaults and are fine for a test environment.

---

## 2. Prerequisites

| | Requirement | Notes |
|---|---|---|
| OS | Linux (Ubuntu 22.04 verified) | WSL2 works |
| Python | **3.10** | 3.10.12 verified |
| GPU | NVIDIA, driver ≥ 550 | verified on RTX PRO 2000 Blackwell, driver 595.79, 8 GB |
| Disk | **~10 GB** | the venv alone is 7.5 GB (torch + CUDA) |
| RAM | 8 GB+ | |

CPU-only works — drop the `+cu128` suffixes in `requirements.txt`, omit the
extra index URL, and set `device: cpu` in each camera config. Expect roughly
3–5 FPS per camera instead of ~24.

### System packages on a fresh Ubuntu server

```bash
sudo apt update
sudo apt install -y \
    python3.10-venv python3-pip \
    libgl1 libglib2.0-0 \
    git curl
```

Why each, verified against the installed binaries rather than copied from a
generic list:

* **`python3.10-venv`** — Ubuntu 22.04 ships Python 3.10 but *not* the `venv`
  module. Without it `python3 -m venv .venv` fails with a message suggesting
  `apt install python3.10-venv`, which is easy to miss in a script.
* **`libgl1` and `libglib2.0-0`** — the single most likely bring-up failure.
  `ultralytics` depends on **`opencv-python`** (the full build), and that wins
  the import over `opencv-python-headless`. The full build links against
  `libGL.so.1`, `libX11`, and `libglib-2.0`, none of which a headless server
  has. Symptom: `ImportError: libGL.so.1: cannot open shared object file`, on a
  machine with no display, from a package with "headless" in the requirements.
* **`git`, `curl`** — cloning and fetching weights.

### What you do NOT need

* **CUDA Toolkit.** The torch wheels bundle their own CUDA runtime as pip
  packages (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, `nvidia-cuda-runtime-cu12`
  and friends). Verified: `nvcc` is not installed on the working machine and
  torch still reports `cuda True`. Installing the toolkit wastes several GB and
  risks a version conflict.
* **cuDNN as a system package** — also bundled by pip.
* **ffmpeg.** The OpenCV wheel bundles its own; RTSP decode works without a
  system ffmpeg. (`ffprobe` is absent on the working machine.)

### NVIDIA driver

The **driver** is the one GPU thing that must come from the OS:

```bash
ubuntu-drivers devices          # see what is recommended
sudo ubuntu-drivers autoinstall # or: sudo apt install nvidia-driver-570
sudo reboot
```

CUDA 12.8 needs driver **≥ 525** via minor-version compatibility; **≥ 570** is
recommended. Verified working on 595.79. Confirm before installing anything else:

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
```

If that command fails, fix the driver first — everything downstream depends on
it, and torch will silently fall back to CPU rather than erroring.

---

## 2b. Getting the code onto the test machine

**There is no git remote configured.** The repository is local-only: 67 commits
on `master`, tag `v1.0.0-test`. So "clone the repo" needs one of the following
first.

### What must travel, and what must not

| | Size | |
|---|---|---|
| `.git` (source + history) | 117 MB | **copy** |
| `models/*.pt` | 9.2 MB | **copy** — gitignored, or refetch with `scripts/get_weights.py` |
| `scripts/bin/mediamtx` | 52 MB | copy only if republishing clips as RTSP; or `bash scripts/rtsp_setup.sh` |
| `media/*.mp4` | 123 MB | only if testing without real cameras |
| `.venv` | **7.5 GB** | **NEVER copy** — rebuild on the target |
| `data/`, `evidence/` | — | don't copy; runtime state, regenerated |

Do not copy `.venv`. It contains absolute paths baked into the venv config and
compiled CUDA extensions built for this exact machine; copying it produces
failures that look like corrupted installs. Rebuilding takes ~10 minutes.

### Option A — a git host (best if you have one)

```bash
# on dev
git remote add origin git@your-host:team/finblade-cctv.git
git push -u origin master --tags

# on test
git clone git@your-host:team/finblade-cctv.git finblade-cctv
```

Weights still transfer separately (gitignored) — either run
`scripts/get_weights.py` on the target, or `scp models/*.pt` across.

### Option B — a git bundle (no server needed, keeps full history)

One file, carries every commit and tag, moves over scp or a USB stick.

```bash
# on dev
git bundle create finblade.bundle --all          # ~92 MB
scp finblade.bundle models/*.pt user@test-host:~/

# on test
git clone finblade.bundle finblade-cctv
cd finblade-cctv && git remote remove origin     # the bundle file is not a live remote
mkdir -p models && mv ~/*.pt models/
```

Verify before trusting it: `git bundle verify finblade.bundle` should say
"The bundle records a complete history."

To ship an update later, a much smaller incremental bundle:
`git bundle create update.bundle <last-tag>..master`

### Option C — direct copy (simplest, no git needed on the target)

```bash
rsync -av --progress \
  --exclude '.venv' --exclude 'data' --exclude 'evidence' \
  --exclude '__pycache__' --exclude '*.pyc' \
  /home/usv/finblade-cctv/ user@test-host:/opt/finblade-cctv/
```

Keeps `.git`, so history and `git pull` from dev still work later. Add
`--exclude 'media'` to skip the 123 MB of test clips, and `--exclude '.git'` if
you want source only.

No network path between the machines? Tar it:

```bash
tar --exclude='.venv' --exclude='data' --exclude='evidence' \
    --exclude='__pycache__' -czf finblade.tar.gz -C /home/usv finblade-cctv
```

---

## 3. Install

```bash
git clone <your-repo> finblade-cctv && cd finblade-cctv

python3.10 -m venv .venv
.venv/bin/python -m pip install --upgrade pip

# The constraints file is NOT optional — see the note in it. Without it pip
# pulls numpy 2.x and the vision stack breaks at runtime, not install time.
.venv/bin/pip install -r requirements.txt -c constraints.txt \
    --extra-index-url https://download.pytorch.org/whl/cu128
```

Verify the stack before going further. If this prints anything other than
`numpy 1.26.4` and `cuda True`, stop and fix it — everything downstream assumes
these:

```bash
.venv/bin/python -c "
import numpy, torch, ultralytics, cv2, boxmot
print('numpy', numpy.__version__)
print('torch', torch.__version__, '| cuda', torch.cuda.is_available())
print('ultralytics', ultralytics.__version__)
"
```

### Model weights

Not in git (`models/*.pt` is gitignored). Fetch both — detection **and** ReID:

```bash
.venv/bin/python scripts/get_weights.py
```

This pulls `yolov8n.pt` (~6 MB) and `osnet_x0_25_msmt17.pt` (~3 MB). It needs
outbound internet once. On an air-gapped box, copy both files into `models/`
from a machine that has them.

Without the OSNet weights the system still runs — detection, tracking, zones,
metrics and rules are unaffected — but cross-camera identity disables itself and
says so loudly in the camera log. It does **not** fall back to a fake embedder.

### Run the tests

```bash
.venv/bin/python -m unittest discover -s tests
```

331 tests, ~2 seconds. If httpx is missing, 11 HTTP tests skip silently rather
than fail — check the count, not just the OK.

---

## 4. Configure

### Zones — draw them, don't guess

Start the API (section 5), add a camera, then open
`http://<host>:8000/tools/zone-editor.html`.

**Drag the zone's bottom edge to the very bottom of the frame.** Occupancy
counts *foot points* — the bottom-centre of each person's box — and anyone whose
box is clipped by the frame edge has their foot point ON that edge. A zone inset
even a few pixels from the bottom excludes them and reports 0 occupancy while
people are plainly being tracked. This cost an hour during bring-up.

The runner warns when it happens:

```
camera CAM-06: 72% of foot points are in NO zone (701/978); 17% of those sit
near the bottom of the frame — the zone polygon probably needs to extend to
the bottom edge
```

Watch for that in the camera log after drawing zones.

### Camera topology — required for cross-camera identity

Edit `config/topology.yaml` and use the **exact `camera_id` values you typed
into the UI**. A pair that appears nowhere in this file falls back to the
permissive default.

```yaml
overlapping_pairs:          # views that share floor — same person on both AT ONCE
  - a: CAM-ENTRANCE
    b: CAM-LOBBY

transits:                   # separate areas with a walk between them
  - a: CAM-LOBBY
    b: CAM-GATE
    min_seconds: 12         # MEASURE this: pace the walk
    max_seconds: 90
```

The two kinds behave oppositely: an overlapping pair *expects* dt≈0, a
non-overlapping pair *rejects* dt≈0 as physically impossible. Getting this
wrong is the single most common reason cross-camera matching appears broken —
a mis-set minimum silently rejects every candidate before appearance is scored.

Check `unknown_pair` in `/api/v1/identity/stats` after a few minutes. Anything
above 0 means this file does not cover the cameras actually running.

### Environment variables

| Variable | Default | Use |
|---|---|---|
| `FINBLADE_DB` | `data/finblade.db` | SQLite path |
| `FINBLADE_TOPOLOGY` | `config/topology.yaml` | topology file |
| `FINBLADE_INMEMORY` | unset | `1` = don't persist (tests) |
| `FINBLADE_SELF_URL` | `http://127.0.0.1:8000` | what the API tells workers to call back on |
| `FINBLADE_REPORT_INTERVAL` | `3600` | R-08 scheduled report cadence, seconds |
| `DATABASE_URL` | unset | switches to Postgres |
| `REDIS_URL` | unset | switches to the Redis Streams bus |
| `FB_LOG_LEVEL` | `INFO` | |

---

## 5. Run

```bash
bash scripts/start_stack.sh     # API + cameras, detached, stays up
bash scripts/stop_all.sh        # everything down
```

Or the API alone, and add cameras from the UI:

```bash
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000
```

Then open `http://<host>:8000/web/cameras.html` → **Add camera**, give it an id
and an RTSP URL. The API starts a detection pipeline for it and assigns an MJPEG
port from 8090 upwards.

| Page | URL |
|---|---|
| Dashboard | `/web/dashboard.html` |
| Cameras | `/web/cameras.html` |
| History | `/web/history.html` |
| Reports | `/web/report.html` |
| Zone editor | `/tools/zone-editor.html` |

### Testing without real cameras

`scripts/rtsp_dual.sh` republishes local clips as RTSP so you can exercise the
real network path. Needs MediaMTX (`bash scripts/rtsp_setup.sh` fetches it).

---

## 6. Verify

```bash
curl -s localhost:8000/api/v1/cameras          | python3 -m json.tool
curl -s localhost:8000/api/v1/identity/stats   | python3 -m json.tool
curl -s localhost:8000/api/v1/identity/counts  | python3 -m json.tool
```

Healthy after a few minutes with two cameras running:

* every camera `effective_state: ONLINE` with a non-zero `input_fps`
* `reid.status: ready` in the camera log (`grep ReID scripts/cam_*.log`)
* `rejected_topology` **not** growing steadily — if it is, the topology file is
  wrong for these cameras
* `unknown_pair` at 0
* `unique_total` climbing then **plateauing**. If it climbs forever the matcher
  is splitting one person into many; check crop quality and the topology first.

---

## 7. Things that will bite you

Every one of these actually happened during bring-up.

**RTSP defaults to UDP and stalls.** Already fixed in `camera_worker.py` (forces
`rtsp_transport=tcp`), but if you add another RTSP consumer, force TCP there
too. The symptom is a camera stuck `RECONNECTING` writing megabytes of
`can't be used to capture by name` into its log.

**MJPEG ports move.** The manager assigns the next free port from 8090, so a
camera's port changes across restarts. Read `stream_url` from
`/api/v1/cameras`; don't hard-code.

**Deleting alerts leaves the JPEGs behind** unless you go through the API.
`DELETE /api/v1/alerts?scope=closed` removes the snapshot files too;
`DELETE /api/v1/frames/orphaned` cleans up ones already stranded. Snapshots are
now written only for critical density (R-02) and restricted intrusion (R-06) —
loitering used to write them continuously and produced 944 MB in a day.

**The database grows fast.** Zone state is written every 5 seconds per zone and
events per transition: a day of two cameras reached 380 MB / 569k events. Plan a
retention job before any long soak test.

**Don't `pip install` anything without `-c constraints.txt`.** A stray install
that upgrades numpy will break the vision stack at runtime rather than at
install time, which is a miserable way to lose an afternoon.

---

## 8. What this is not ready for

Be clear-eyed before pointing anything real at it:

* **No authentication or TLS.** Explicitly out of scope. Anyone who can reach
  port 8000 can view every feed, acknowledge alerts and delete data. Put it on
  an isolated VLAN, or behind a reverse proxy that terminates TLS and
  authenticates.
* **No process supervision.** `start_stack.sh` detaches processes; it does not
  restart them if they die. Use systemd for anything long-running.
* **SQLite, single file, no backups.** Fine for a test box. Set `DATABASE_URL`
  for Postgres if this becomes shared.
* **Cross-camera accuracy is unvalidated on real cameras** — see BLOCKERS.md
  B-4. The figures measured so far come from a synthetic second camera and do
  not predict real-world accuracy.
* **Biometric templates exist in memory.** ReID embeddings are held in RAM,
  never persisted or logged, and dropped on TTL expiry — but they now exist,
  where before they did not. See DECISIONS.md D-9; this is worth raising with
  whoever owns data protection before a real deployment.
