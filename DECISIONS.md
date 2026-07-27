# DECISIONS made without you (all reversible)

## D-1 — Build the deterministic core stdlib-only, test with `unittest`
**Choice:** The whole post-detection core (geometry, zones, debounce, metrics,
events, schema validation, rule engine) is written in pure Python + stdlib
(`hashlib`, `dataclasses`), tested with `python3 -m unittest`. No pytest, no
pydantic, no third-party test deps.
**Why:** Only `numpy` + `pyyaml` are installed and you said do not download.
CLAUDE.md calls the unit tests "your real deliverable overnight" — writing them
against libraries that aren't installed would leave them unrun. Stdlib means
they run and pass **tonight**, giving you a real green/red count in the morning.
**Reverse:** Tests are plain `unittest.TestCase`; `pip install pytest` and they
run under pytest unchanged (naming is pytest-compatible).

## D-2 — CPU everywhere; did NOT touch the GPU/OpenVINO config
**Choice:** Left `config/cameras.yaml` (device: GPU, OpenVINO IR path) and
`services/inference/main.py` **untouched**. Added `config/cameras.dev.yaml`
(device: CPU, `models/yolov8n.pt`, local file source) as a separate variant.
**Why:** CLAUDE.md rule 4 (CPU only, no Arc passthrough in WSL) + rule 3 (never
rewrite the human's config, zone polygons especially) + rule 7 (don't refactor
working code). A new file satisfies all three.
**Reverse:** Delete `config/cameras.dev.yaml`; nothing else changed.

## D-3 — Added a CPU inference runner instead of editing main.py
**Choice:** New `services/inference/run_cpu.py` (CPU + `.pt` + file/RTSP source +
wired to the tested core) rather than editing `main.py`.
**Why:** Same rule-3/7 reasoning. `main.py` stays as the human's Arc/OpenVINO
scaffold; the CPU path is additive.
**Reverse:** Delete the file.

## D-4 — Network was UP; I deliberately did not use it
**Choice:** A one-shot probe showed PyPI reachable. I still installed/downloaded
**nothing** (no weights, no clips, no packages).
**Why:** Your explicit instruction: "Assume no network. Do not download
anything." The rule is honored by choice, not by circumstance. BLOCKERS.md lists
the exact `pip install` + file placements to unblock in the morning.
**Reverse:** N/A — nothing was fetched to undo.

## D-5 — Did not rename the video clip
**Choice:** Left `media/1903279-uhd_1920_1440_30fps.mp4` as-is; pointed the dev
config at that exact name.
**Why:** Renaming assumes it IS the intended `clip.mp4`; that's a judgement I
can't verify (I can't see it). Config indirection avoids guessing.
**Reverse:** `mv media/1903279-uhd_1920_1440_30fps.mp4 media/clip.mp4` if you want
the canonical name; then either config works.

## D-7 — Built cross-camera ReID, which CLAUDE.md explicitly cuts
**Choice:** Implemented cross-camera person identity (appearance embeddings +
topology gating + a global identity registry), listed under "DO NOT BUILD".
**Why:** You asked for it directly and said "forget about the demo". I flagged
the conflict and the cost first; you confirmed. A standing rule written before
the request loses to an explicit instruction after it.
**Reverse:** The feature is additive and off-switchable. Set `reid.enabled:
false` in a camera config, or delete `finblade/{appearance,globalid,topology}.py`
+ `services/{api/identity.py,inference/reid_client.py}` and the identity routes
in `app.py`. Detection, tracking, zones, metrics and rules never call into it.

## D-8 — Used the network and added a dependency, under a constraints file
**Choice:** Installed `boxmot` and downloaded `models/osnet_x0_25_msmt17.pt`
(3 MB), overriding CLAUDE.md rules 2 and 5 — with your go-ahead.
**Why:** There is no way to do appearance ReID without an appearance model, and
ultralytics 8.3.40's BoT-SORT ReID is a stub (`self.encoder = None`, "Haven't
supported BoT-SORT(reid) yet"), so the flag cannot supply one.
**The trap I avoided:** `pip install boxmot` wanted to pull **numpy 2.2.6**,
upgrading the pinned numpy 1.26.4 — an ABI break for torch/torchvision/
ultralytics/scipy/opencv, which could have taken the working pipeline down. I
installed under a constraints file pinning numpy/torch/torchvision/ultralytics/
scipy, which resolved to boxmot 19.0.0 and left every pin untouched (verified).
**Chose msmt17 over market1501:** MSMT17 is larger and shot across more cameras
and lighting conditions, so it generalises better to real CCTV.
**Reverse:** `pip uninstall boxmot`; a snapshot of the 81-package venv from
before the change is at `/tmp/venv_before_reid.txt`.

## D-9 — Embeddings are RAM-only and never persisted
**Choice:** Feature vectors live only in the worker's per-track banks and the
API's in-memory gallery. They are cleared on track reap and on TTL expiry, and
are never written to the database, evidence/, or any log — every API response
returns only an opaque `gp_` ref, salted per session like `person_ref`.
**Why:** A ReID embedding is a biometric template — it is exactly the thing that
makes a person re-identifiable later. The product's whole privacy claim is "we
hold no PII". Keeping vectors ephemeral and un-persisted is what lets that claim
survive adding this feature. There are tests asserting no endpoint leaks one.
**This is still a posture change and needs your sign-off**: the data now exists
in memory at all, and crosses a loopback HTTP boundary, which was not true
before. Under GDPR/UAE DP law that is worth a conversation with the client.
**Reverse:** Nothing to delete — no vector is stored anywhere.

## D-10 — Ambiguity creates a new identity rather than guessing
**Choice:** A match must clear the threshold AND beat the runner-up by a margin.
When several candidates are close, the matcher creates a NEW identity instead of
picking the top one.
**Why:** The two errors are not symmetric. An unnecessary split counts one
person as two — a quiet metrics error. A wrong merge puts a stranger's movements
under someone else's ref, and if it drives a restricted-zone alert it accuses the
wrong person. In a uniformed environment (staff, hi-vis) near-ties are the norm,
not the edge case.
**Reverse:** `margin=0.0` in `GlobalIdentityRegistry` restores pick-the-best.

## D-11 — Match threshold 0.70, explicitly provisional
**Choice:** Raised the default from 0.62 to 0.70.
**Why:** Measured on the dense clip (`evidence/cross_camera_eval_dense.json`):
true-pair similarities ran min 0.80 / median 0.90, false pairs median 0.61 /
max 0.83. 0.62 sat *at the false-pair median*, which is poor hygiene. The
distributions **overlap** (gap −0.034), so no threshold separates them cleanly —
the margin rule and topology gate do the real work, and this value is only a
floor. That is also why sweeping 0.62/0.70/0.78 changed nothing.
**Why it is not final:** that evaluation's second camera is a transformed copy of
the first, so the two views share clothing, pose and lighting. Real cameras will
push true-pair scores DOWN, and 0.70 may then be too strict. See B-4.
**Reverse:** One constructor arg in `finblade/globalid.py`.

## D-6 — Hysteresis clear thresholds
**Choice:** amber on=2.0/off=1.8, red on=4.0/off=3.6, capacity on=90%/off=85%.
**Why:** CLAUDE.md requires "falling to 1.9 does NOT clear amber" → off-threshold
must be < 1.9; 1.8 (10% band) is a conventional choice. Symmetric ~10% band for
the others.
**Reverse:** Thresholds are constructor args in `finblade/rules.py`
(`RuleThresholds`); change in one place.
