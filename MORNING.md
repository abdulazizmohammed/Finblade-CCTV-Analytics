# Morning report — 2026-09-15 (lab PPE checkpoint integration)

## TL;DR
Your YOLO11s lab-PPE model is integrated, tested and running through the real
pipeline path. **1781 tests pass** (was 1557 in the last CAPABILITIES count;
184 of them are PPE, 25 new). It loads on the pinned ultralytics 8.3.40 —
no pin change, no new dependency.

**The finding you need before anything else:** on `media/LAB-PPE.mp4` the
model emits **no box above confidence 0.25** — not on full frames, not on
person crops. It is not a threshold or a scale problem; the training frames
are eye-level close-ups of white coats on a production line, and the site
camera is an overhead fisheye of blue gowns at ~145 px. B-9. The integration
is complete; the model cannot see this camera until it is retrained on frames
from it.

## Status
Lab checkpoint: **integrated, runs, journalled.** Accuracy on site footage:
**measured — silent.** Everything from previous sessions green and untouched.

## What runs
- **`models/ppe_yolo11s_best.pt`** (sha256 `09e28044…`, 19 MB, gitignored like
  every other `.pt`) is the medical profile's detector. `medical_ppe.checkpoint:
  finblade_lab_yolo11s` selects it from a registry that binds class map,
  default weights and a **class-order assertion** — a retrain whose
  `model.names` differ from `PPE_CLASSES` is refused at load, not adapted.
- **Class map:** Gloves/Goggles/Haircap/Mask → `surgical_gloves` / `goggles` /
  `surgical_cap` / `surgical_mask`; **Labcoat → new item `lab_coat`** (not
  `surgical_gown` — different garment). `No X` → `no_<item>`, `is_violation`
  on every detection dict.
- **Compliance over time:** the existing grace 5 s → confirm 8 s → recover 5 s
  state machine, **with absence weighted 0 for the medical profile** so a
  violation needs sustained explicit `No X` and silence never convicts (see
  "Decisions").
- **Raw journal:** every box ≥ 0.25 → `evidence/ppe_raw/<camera>.jsonl`
  (class, conf, box, frame, ts, camera; no crops, no track ids). The rule
  engine only sees boxes ≥ 0.5.
- **Item-level serving guard:** the checkpoint covers 5 of the medical
  profile's 10 items; a zone requiring gown/scrubs/face shield/coverall/shoe
  covers has those dropped with a warning instead of judged on silence.
- **Config:** `config/cameras.template.yaml` `medical_ppe:` block — enabled,
  conf 0.5, iou 0.5, imgsz 640, absence_weight 0.0, raw_log_dir, raw_log_conf.
- **Manual runner:** `scripts/ppe_check.py --profile medical --raw-log …`.
- CPU speed: 0.18 s/frame for the PPE model alone at 640.

## NEEDS YOUR EYES (do this first, ~5 min)
1. **Open `evidence/lab_ppe/contact_sheet.jpg`.** 21 frames of LAB-PPE.mp4;
   teal = person boxes (yolo11s), red/green = any lab-PPE box ≥ 0.05. There
   are almost none. Does that look like the same kind of footage the model
   was trained on? (One of your test images is 640 px of two people in white
   coats at eye level. This clip is not that.)
2. **Open `evidence/lab_ppe/crops/`** — the only person crops that produced a
   box at ≥ 0.10. All below 0.25. Are the labels even the right items?
3. **Confirm the vocabulary choices** in D-35: Haircap → `surgical_cap`,
   Labcoat → its own `lab_coat`. If you'd rather Labcoat be `surgical_gown`,
   that's one line in `LAB_CLASS_MAP` plus removing `lab_coat` from three
   lists.
4. **Confirm `medical_ppe.enabled: true` in the shared template** is what you
   want. It is inert on cameras with no medical zone, but it does load a
   third model on any camera that has one.

## Blockers (I could not resolve these)
- **B-9 — the checkpoint is silent on the site camera.** Full-frame boxes at
  0.25: zero. On 84 padded person crops at 0.25: zero. Evidence in
  `evidence/lab_ppe/`. Needs frames from the actual cameras in the training
  set; the raw journal measures precision on what the model emits but cannot
  manufacture recall.
- **The zone card counts UNKNOWN as compliant** (pre-existing, `zone_summary`).
  With absence weight 0 and a silent model, everyone in a lab zone reads
  "compliant". I did not change it — it's a UI-contract decision — but it is
  now the visible consequence of B-9 and worth deciding.
- **Sample images live outside the repo.** The real-weights test reads
  `/mnt/c/Users/ICSADMIN/ppe-dataset/test/images` (override with
  `FINBLADE_PPE_SAMPLES`) and skips if absent. I didn't copy frames with
  people in them into git.

## Decisions I made without you (all reversible, detail in DECISIONS.md D-35)
- **Medical profile, not a third profile.** The profile is already "Medical /
  Laboratory" everywhere, and the template said any YOLO8/11 medical
  checkpoint should drop in by mapping.
- **Absence weight 0.0 for medical.** On the model's own 47 test images at
  conf 0.5 it produced *no* Gloves, Haircap or Mask positive at all. Under the
  industrial weight (0.25) every person in a lab zone would be convicted in
  ~32 s on the detector's silence. `medical_ppe.absence_weight` overrides.
- **Visible 0.5 / journal 0.25.** As your brief asked; both in config.
- **`detect()` gained `class_id`, `raw_class`, `is_violation`.** Two adapter
  tests that pinned the exact key set were widened; the original four keys
  and every consumer are unchanged.
- **`ppe_served` is now item-granular.** Without it, wiring a 5-item model
  into a 10-item profile would have reintroduced the "judged on silence" bug
  the profile guard was written for.
- **Enabled in the shared template** (one line to flip back).

## Tests
**1781 passed / 0 failed / 17 skipped** (73 s, full suite). PPE files alone:
184 passed, none skipped — the two real-weights tests ran on CPU and asserted
`model.names == PPE_CLASSES` and that inference runs and journals on the first
10 sample images.

New file: `tests/test_lab_ppe_model.py` — class order vs the brief, worn/
missing pairing at offset 5, refusal of a shuffled retrain, threshold split
and journal contents (and that the journal names nobody), item-level serving,
absence weight per profile, sustained-negative-only conviction.

## Suggested next steps (ordered)
1. Look at the contact sheet (2 min). If you agree it's a domain gap, the fix
   is data, not code: extract ~200 frames from LAB-PPE.mp4 and the live lab
   cameras, label the five items, retrain with the same class order.
2. Drop the retrained `best.pt` over `models/ppe_yolo11s_best.pt` and run
   `pytest tests/test_lab_ppe_model.py` — it will refuse a reordered class
   list and tell you if the model went fully silent on its own test split.
3. Then `scripts/ppe_check.py --source media/LAB-PPE.mp4 --profile medical
   --required surgical_gloves,goggles,surgical_cap,lab_coat,surgical_mask
   --raw-log evidence/ppe_raw --save-frames evidence/ppe_stills --device cpu`
   and read the journal for per-class confidence distributions.
4. Decide what UNKNOWN should mean on the zone card.
5. Journal rotation, if `raw_log_dir` stays on in production — it's unbounded
   append (~100 bytes/box).

## Where things are
- Vocabulary + state machine: `finblade/ppe.py` (`LAB_COAT`,
  `absence_weight_by_profile`), band in `finblade/geometry.py`
- Adapter + registry: `services/inference/ppe_client.py` (`PPE_CLASSES`,
  `LAB_CLASS_MAP`, `MEDICAL_CHECKPOINTS`, `served_types`, `_journal`)
- Worker wiring: `services/inference/run_cpu.py` (`ppe_served`, `ppe_med`)
- Config: `config/cameras.template.yaml` `medical_ppe:`; editor item list in
  `tools/zone-editor.html`
- Records: `models/MANIFEST.yaml` `medical_ppe`, DECISIONS D-35, BLOCKERS B-9,
  `docs/CAPABILITIES.md` R-11
- Evidence: `evidence/lab_ppe/` (contact sheet, probes, crops, journal, log)
