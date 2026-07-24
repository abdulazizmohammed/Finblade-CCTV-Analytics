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

## D-6 — Hysteresis clear thresholds
**Choice:** amber on=2.0/off=1.8, red on=4.0/off=3.6, capacity on=90%/off=85%.
**Why:** CLAUDE.md requires "falling to 1.9 does NOT clear amber" → off-threshold
must be < 1.9; 1.8 (10% band) is a conventional choice. Symmetric ~10% band for
the others.
**Reverse:** Thresholds are constructor args in `finblade/rules.py`
(`RuleThresholds`); change in one place.
