# FinBlade CCTV — capability → technology map

What each capability is actually built out of. The companion to
[CAPABILITIES.md](CAPABILITIES.md), which says *what* the system does; this says
*with what*.

**Last updated:** 2026-09-03

Versions below are the ones **actually installed and running**, taken from
`requirements.txt` / `constraints.txt` and verified against the live venv — not
aspirational pins. `constraints.txt` is not optional: `numpy==1.26.4` is an ABI
floor for torch, torchvision, ultralytics, scipy and opencv, and bumping it
takes the vision pipeline down.

Status markers follow CAPABILITIES.md: **Built** = implemented and tested;
**Runs** = executes end to end but correctness depends on human visual
judgement (see the PRIME DIRECTIVE in [CLAUDE.md](../CLAUDE.md)).

**On "stdlib only" below.** The `finblade/` core is stdlib with exactly two
exceptions, both verified: `finblade/config.py` and `finblade/topology.py`
import `PyYAML` because they read config files, and `finblade/cancelable.py`
imports `numpy` lazily (only when extended retention is enabled). Everything
else in that package — geometry, zones, debounce, rules, flowrules, metrics,
presence, areas, events, appearance, identity, globalid, series, timeweight,
window, emission — imports nothing outside the standard library. That is what
lets most of the 1557 tests run with no torch, no cv2 and no GPU.

---

## 1. Video ingest and detection

| Capability | Status | Technology |
|---|---|---|
| RTSP / file decode, loop, reconnect | Built | `opencv-python-headless` 4.10.0.84 (FFmpeg backend) |
| Person detection, COCO class 0 only | Runs | `ultralytics` 8.3.40 + `models/yolo11s.pt`, `torch` 2.11.0+cu128 |
| Multi-object tracking, persistent IDs | Runs | ByteTrack, bundled in `ultralytics` (`tracker="bytetrack.yaml"`); `lapx` 0.9.4 for linear assignment |
| Annotated MJPEG per camera | Built | `flask` 3.0.3 + `cv2.imencode`, one lightweight server per worker process |
| Frame decimation / cost control | Built | `process_fps` in camera config; own loop, no library |
| One process per camera | Built | `subprocess` from `services/api/camera_manager.py` |

Detection currently runs on **CUDA** (`device: cuda` in
`config/cameras.template.yaml`), against CLAUDE.md's own `device: CPU` rule.
Hardware: NVIDIA RTX PRO 2000 Blackwell Laptop, 8 GB.

## 2. Identity

| Capability | Status | Technology |
|---|---|---|
| Anonymous `pr_` person_ref | Built | `hashlib` SHA-256 + per-session salt, `finblade/identity.py` — stdlib only |
| Anonymous `gp_` global_ref | Built | same construction, `finblade/globalid.py` |
| Appearance embeddings (512-d) | Runs | OSNet via `boxmot` 19.0.0 + `models/osnet_x0_25_msmt17.pt` |
| Cosine similarity matching | Built | pure Python in `finblade/appearance.py` — deliberately no numpy, so the matcher unit-tests without torch |
| Crop-quality gating | Built | `CropQualityGate`, pure stdlib |
| Sampling cadence + per-frame budget | Built | `EmbeddingSampler`, pure stdlib |
| Transit-time physics gate | Built | `finblade/topology.py` + `config/topology*.yaml` — one of the two `PyYAML` 6.0.2 users in the core |
| Optional 24h retention, epoch-projected | Built | `finblade/cancelable.py` — random orthogonal matrices via `numpy.linalg.qr`. Off by default |

**No face recognition, no hand geometry, anywhere.** That is what keeps this
outside BIPA's scope and it is a deliberate boundary, not an omission.

## 3. Spatial model

| Capability | Status | Technology |
|---|---|---|
| Zone polygons, point-in-polygon | Built | own ray-casting in `finblade/geometry.py` — stdlib, no shapely |
| Foot-point assignment, restricted wins | Built | `finblade/zones.py`, stdlib |
| N-frame boundary debounce | Built | `finblade/debounce.py`, stdlib |
| Physical areas (one room, N cameras) | Built | `finblade/areas.py`, stdlib |
| Browser zone editor | Built | `tools/zone-editor.html` — vanilla JS + `<canvas>`, no framework |
| Ground-plane homography | **Not built** | Part B; blocked on real point correspondences |

The spatial model is **symbolic, not metric**: it records that two polygons are
the same room, not where either sits in space.

## 4. Metrics

| Capability | Status | Technology |
|---|---|---|
| Occupancy, density, capacity % | Runs | `finblade/metrics.py`, stdlib arithmetic |
| Dwell, inflow/outflow | Built | `finblade/metrics.py` + `finblade/tracks.py`, stdlib |
| Distinct-people de-duplication | Built | `finblade/areas.py` set union over global refs |
| Time-weighted aggregation | Built | `finblade/timeweight.py`, stdlib |
| Sparse-history reads | Built | `finblade/series.py`, stdlib |
| Business-day windowing | Built | `finblade/window.py` + `zoneinfo` (stdlib) |

## 5. Events and bus

| Capability | Status | Technology |
|---|---|---|
| 17 event types, one envelope | Built | `finblade/events.py` — **hand-written validation, no pydantic** |
| PII guard on `person_ref` | Built | `events.py`, asserts the ref is an anonymous hash |
| `fb:events` / `fb:facility` streams | Built | Redis Streams (`XADD`/`XREAD`) via `redis` 5.2.1 |
| In-memory bus for tests | Built | `InMemoryBus`, stdlib |
| Write-on-change emission gating | Built | `finblade/emission.py`, stdlib |

Schema validation is deliberately hand-rolled rather than pydantic so
`finblade/` stays importable and testable with no third-party dependency.

## 6. Rules and alerts

| Capability | Status | Technology |
|---|---|---|
| R-01/02/03/05/06/07/08/09 | Built | `finblade/rules.py` — `HysteresisLatch`, stdlib only |
| R-10 fire/smoke rule | Built | `finblade/rules.py` `evaluate_hazard` — stdlib; the rule is provable without a model |
| Fire/smoke detection | **Runs** | `models/fire_smoke_yolov8n.pt` (D-Fire, CC0-1.0) via `ultralytics`, in `services/inference/hazard_client.py` at 2 Hz. **AGPL-3.0 via YOLOv8 — see D-31 before commercial deployment.** Evaluation checkpoint, not a validated fire alarm |
| Hysteresis + sustained-duration gate | Built | same class; separate on/off thresholds |
| Wrong-way, group crossing | Built | `finblade/flowrules.py`, stdlib |
| Alert ack / resolve / dismiss | Built | `services/api/service.py` + Postgres |
| Incident frame capture | Built | `cv2.imwrite` in the worker, served by FastAPI |

Every rule is pure stdlib and takes time as a float parameter, which is what
makes the suite deterministic and headless.

## 7. Facility presence

| Capability | Status | Technology |
|---|---|---|
| Roster surviving loss of camera view | Built | `finblade/presence.py` `FacilityRoster`, stdlib |
| Door direction from adjacent zones | Built | `presence.py` `DoorPolicy` |
| Per-door tallies and rates | Built | `presence.py` `DoorCounters` |
| Declared opening baseline | Built | `POST /api/v1/facility/baseline` |
| Merged counts published on change | Built | `emission.py` gate + Redis Streams |

## 8. Storage and analytics

| Capability | Status | Technology |
|---|---|---|
| Durable store, 14 tables | Built | **PostgreSQL 16.2** via `psycopg[binary,pool]` 3.3.4 |
| 17 analytics SQL views | Built | plain SQL in `services/api/analytics_views.py` |
| Column-level grants | Built | `scripts/pg_grants.py` — `person_ref` unreachable through any view |
| In-memory store for tests | Built | `services/api/store.py`, opt-in via `FINBLADE_INMEMORY=1` |
| Retention pruning | Built | SQL `DELETE` on a background task, off by default |

No ORM. Hand-written SQL throughout, so the schema and the queries are
inspectable in one place.

## 9. HTTP API

| Capability | Status | Technology |
|---|---|---|
| 63 `/api/v1` routes (74 incl. framework) | Built | **FastAPI** 0.139.2 on **uvicorn** 0.51.0 |
| WebSocket push at 2 Hz | Built | `websockets` 15.0.1 — *not* pulled in by bare uvicorn |
| API-key auth, two roles | Built | own middleware, `services/api/auth.py` |
| Credential redaction | Built | `services/api/redact.py`, `re` |
| Worker → API posts | Built | `requests` 2.34.2 |

Business logic lives in `service.py` / `identity.py` / `fusion.py`, all
framework-agnostic and unit-testable without FastAPI. `app.py` is a thin
adapter.

## 10. Dashboard and web UI

| Capability | Status | Technology |
|---|---|---|
| Ops dashboard, zone cards, alert feed | Built | **vanilla HTML/CSS/JS. No framework, no build step, no CDN** |
| Live feeds | Built | MJPEG `<img>` + snapshot polling with an in-flight cap |
| Charts / sparklines | Built | hand-written inline **SVG** — no chart library |
| Theming | Built | CSS custom properties in `web/finblade-theme.css` |
| Fonts | Built | system font stacks only — air-gapped, nothing fetched |

Zero JS dependencies is a deployment requirement, not a preference: this ships
on-prem and air-gapped.

## 11. Integrations

| Capability | Status | Technology |
|---|---|---|
| Forwarder to the FinBlade platform | Built | `requests` + store-tailing with a DB cursor |
| 8 chatbot tools | Built | JSON tool schemas, `integrations/finblade_ai/tools.py` |

## 12. Operations

| Capability | Status | Technology |
|---|---|---|
| API + Postgres as services | Built | **systemd** units in `deploy/` |
| Camera autostart after restart | Built | `FINBLADE_AUTOSTART_CAMERAS` + `camera_manager.py` |
| Health / readiness | Built | `/api/v1/health`, `/healthz`, `/readyz` |
| Evidence artifacts | Built | `cv2` frame writes + contact sheet, `json` metrics |
| RTSP test rig | Built | **MediaMTX** (external binary) + `ffmpeg` republishing clips |
| Host platform | — | WSL2, Ubuntu 22.04, Python 3.10.12 |

## 13. Testing

| Capability | Status | Technology |
|---|---|---|
| 1557 tests | Built | `unittest` (stdlib) run under `pytest`; `httpx` 0.28.1 for the API tests |
| Headless by default | Built | `finblade/` is stdlib-only, so most tests need no torch/cv2/GPU |
| UI behaviour tests | Partial | JS lifted from the shipped HTML and run under **node** — skipped where no JS runtime exists, which includes this dev box |
| Cross-camera evaluation | Built | `scripts/eval_cross_camera.py` — synthetic second camera by known transform |

## 14. Explored, NOT part of the product

| Thing | Status | Technology |
|---|---|---|
| VLM/CLIP person-attribute tagging | **Exploration only** | `open_clip_torch` 3.3.0, `timm` 1.0.29, `safetensors` 0.8.0 — installed in the venv; scripts live outside the repo in `/home/usv/clip_eval/`. Not wired into the pipeline |

These three packages are installed but **nothing in the product imports them**.
Verified not to disturb any pinned version.

---

## Deliberate non-choices

Worth recording, because each was a decision and each keeps recurring:

- **No pydantic** — hand-written validation keeps `finblade/` dependency-free.
- **No ORM** — hand-written SQL keeps the schema inspectable.
- **No JS framework, no CDN, no web fonts** — air-gapped deployment.
- **No shapely/numpy in the geometry and rules core** — headless testability.
- **No chart library** — inline SVG.
- **No model training or fine-tuning** — pretrained weights only (CLAUDE.md).
- **No face or hand-geometry extraction** — keeps the system outside BIPA scope.
- **No VLM in the vision path** — a per-frame API call cannot happen on an
  air-gapped deployment, and the cost was prohibitive besides.
