"""Worker-side PPE detection: a third model on the same decoded frame.

Same dependency-isolation contract as HazardDetector, and the same refusal to
fake: missing weights, no torch or a corrupt checkpoint switch it off loudly and
everything else keeps running. A stub returning "no PPE detected" would be far
worse here than for fire, because absence of a positive detection is itself
evidence in R-11 — a broken detector would quietly accuse every worker on site.

NOTHING ULTRALYTICS ESCAPES THIS FILE. `detect()` returns a list of plain
dicts — class_name, confidence, bbox, ts — and the rule layer never sees a
Results object, a tensor or a device. That is what lets finblade/ppe.py and
finblade/geometry.py be exercised exhaustively with no model at all.

CLASS NAMES ARE READ FROM THE CHECKPOINT AND MAPPED EXPLICITLY. This model has
13 classes; Phase 3 uses 7 of them. The rest — Gloves, Goggles, No_Harness and
notably Fall-Detected — are DROPPED rather than passed through, so a later phase
that wants fall detection has to enable it deliberately instead of finding it
already half-wired.

MODEL LIMITATION, recorded here because it belongs next to the code that uses
it: the published performance of this checkpoint is markedly weaker for
NO-Safety Vest and for Mask / NO-Mask than for Hardhat. Do NOT compensate by
lowering min_confidence — that converts a recall problem into a false-accusation
problem. Threshold tuning belongs to a validation phase on real CCTV footage.
See DECISIONS.md D-32.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional, Set

log = logging.getLogger("finblade.ppe")

# Checkpoint class name -> the vocabulary finblade/ppe.py speaks. Anything not
# in here is ignored: an unknown class must never become evidence.
CLASS_MAP = {
    "Hardhat": "hardhat",
    "NO-Hardhat": "no_hardhat",
    "Safety Vest": "safety_vest",
    "NO-Safety Vest": "no_safety_vest",
    "Mask": "mask",
    "NO-Mask": "no_mask",
    "Person": "person",
}
# Present in the checkpoint, deliberately unused in Phase 3. Named rather than
# silently skipped so the omission is legible.
IGNORED_CLASSES = {"Fall-Detected", "Gloves", "Goggles", "NO-Gloves",
                   "NO-Goggles", "No_Harness"}

# --- medical / laboratory checkpoint ---------------------------------------
# Candidate: stormbreaker20/yolo26s-mppe-detector-v2.
#
# NOT WIRED TO A FILE YET. The weights are absent from models/ and the pinned
# ultralytics 8.3.40 has no YOLO26 support at all (its model families are
# 3/5/6/8/9/10/11/rt-detr), so nothing here has been exercised against real
# weights. The map exists so the vocabulary, the bands and the rules can be
# built and tested now, and so swapping in ANY medical checkpoint later is a
# mapping change rather than a pipeline change.
#
# SPELLING IS NOT ASSUMED. The model card lists these names with UNDERSCORES
# ("Surgical_Gloves") while the integration brief wrote them with spaces
# ("Surgical Gloves"). Rather than bet on either, lookup is done through
# _canon() below, which folds case, spaces and hyphens — so all of
# "Surgical Gloves", "Surgical_Gloves" and "surgical-gloves" resolve alike.
# The literal keys here are documentation of what we expect to see; the
# matching is what actually decides.
MEDICAL_CLASS_MAP = {
    "Surgical_Gloves": "surgical_gloves",
    "Surgical_Mask": "surgical_mask",
    "Surgical_Gown": "surgical_gown",
    "Surgical_Cap": "surgical_cap",
    "Surgical_Scrubs": "surgical_scrubs",
    "Face_Shield": "face_shield",
    "Goggles": "goggles",
    "Coverall": "coverall",
    "Shoe_Covers": "shoe_covers",
    "Person": "person",
    # The four negative classes this checkpoint publishes. Note what is NOT
    # here: there is no No_Surgical_Gown, No_Goggles, No_Face_Shield,
    # No_Coverall, No_Shoe_Covers or No_Surgical_Scrubs. Those six items can
    # therefore only ever be judged on ABSENCE of a positive detection, which
    # the state machine weights at absence_weight (0.25) precisely because
    # absence is ambiguous. That is a property of the checkpoint, not a bug
    # here, and it is why none of them can be better than "evaluation".
    "No_Surgical_Gloves": "no_surgical_gloves",
    "No_Surgical_Cap": "no_surgical_cap",
    # Two BROAD negatives that do not map onto one item each. "No_Facial_Gear"
    # means no mask AND no shield AND no goggles; "No_Medical_Attire" means no
    # gown/scrubs/coverall. Mapping either onto a single item would invent
    # evidence the model did not give — a person with goggles but no mask is
    # "No_Facial_Gear" to this model, and calling that "no_surgical_mask" would
    # be right by luck. They are carried through under their own names and are
    # NOT consumed as per-item evidence until someone decides what they mean.
    "No_Facial_Gear": "no_facial_gear",
    "No_Medical_Attire": "no_medical_attire",
}
MEDICAL_IGNORED_CLASSES = set()

# --- FinBlade laboratory checkpoint (models/ppe_yolo11s_best.pt) ------------
#
# THE FIRST MEDICAL-PROFILE MODEL THAT ACTUALLY LOADS. YOLO11s, trained
# in-house on 226 images (267 boxes, 16-36 per class); loads on the pinned
# ultralytics 8.3.40 because YOLO11 is a supported family there, even though
# the checkpoint was written by 8.4.152. Its ten classes come in five pairs:
# index i is the item WORN, index i + 5 is the same item MISSING — which is
# exactly the positive/negative structure the evidence model needs, and what
# the two earlier medical candidates lacked.
#
# THIS IS THE CANONICAL CLASS ORDER. It is asserted against `model.names` at
# load time (expected_names below): a retrained checkpoint with a shuffled
# class list would otherwise turn "Mask" into "No Mask" silently, and the
# rule engine would accuse every masked person in the room. A retrain MUST
# keep this order; a checkpoint that does not is refused, not adapted.
PPE_CLASSES: Dict[int, str] = {
    0: "Gloves",
    1: "Goggles",
    2: "Haircap",
    3: "Labcoat",
    4: "Mask",
    5: "No Gloves",
    6: "No Goggles",
    7: "No Haircap",
    8: "No Labcoat",
    9: "No Mask",
}
# The index at which the "missing" half begins: class i >= this is a violation
# of item i - PPE_VIOLATION_OFFSET.
PPE_VIOLATION_OFFSET = 5

# Checkpoint class -> finblade medical vocabulary. Every name maps; nothing is
# ignored. "Haircap" is the checkpoint's word for a bouffant / surgical cap
# and lands on surgical_cap; "Labcoat" is its own item (finblade.ppe.LAB_COAT)
# rather than a stand-in for surgical_gown, because they are different
# garments and the UI must not claim a gown is being judged. Gloves/Mask/
# Goggles land on the medical items of the same meaning.
LAB_CLASS_MAP = {
    "Gloves": "surgical_gloves",
    "Goggles": "goggles",
    "Haircap": "surgical_cap",
    "Labcoat": "lab_coat",
    "Mask": "surgical_mask",
    "No Gloves": "no_surgical_gloves",
    "No Goggles": "no_goggles",
    "No Haircap": "no_surgical_cap",
    "No Labcoat": "no_lab_coat",
    "No Mask": "no_surgical_mask",
}
LAB_IGNORED_CLASSES = set()

# WHICH CHECKPOINT FILLS THE MEDICAL SLOT. Selected by `medical_ppe.checkpoint`
# in the camera config; the class map, the default weights path and the
# name-order assertion travel together so swapping models is one config word
# rather than four edits that can drift. `expected_names: None` means "read
# the names from the weights and warn about gaps" — the behaviour every
# earlier checkpoint had; a dict means "refuse to load unless they match".
MEDICAL_CHECKPOINTS = {
    "finblade_lab_yolo11s": {
        "weights": "models/ppe_yolo11s_best.pt",
        "class_map": LAB_CLASS_MAP,
        "ignored_classes": LAB_IGNORED_CLASSES,
        "expected_names": PPE_CLASSES,
    },
    # The YOLO26 candidate. Kept so its mapping stays selectable once a pin
    # change is decided; cannot load today (BLOCKERS.md B-8).
    "mppe_yolo26s_v2": {
        "weights": "models/medical_ppe_yolo26s_v2.pt",
        "class_map": MEDICAL_CLASS_MAP,
        "ignored_classes": MEDICAL_IGNORED_CLASSES,
        "expected_names": None,
    },
}
DEFAULT_MEDICAL_CHECKPOINT = "finblade_lab_yolo11s"


def _canon(name: str) -> str:
    """Fold a checkpoint's class name to a comparable key.

    Checkpoints are trained by other people and their label files are
    inconsistent about case, spaces, hyphens and underscores. Matching on the
    raw string means a checkpoint that spells it "Safety-Vest" silently
    contributes nothing, and a zone requiring a vest is then judged on silence.
    """
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


class PPEDetector:
    """One PPE model per camera worker, sampled on a cadence."""

    def __init__(self, camera_id: str, weights: Optional[str] = None,
                 device: str = "0", interval_s: float = 0.5,
                 conf_threshold: float = 0.35, imgsz: int = 640,
                 enabled: bool = False,
                 # Which vocabulary this instance speaks. Defaulting to the
                 # industrial map keeps every existing call site working
                 # unchanged; the medical detector is the same class with a
                 # different map, NOT a second implementation.
                 class_map: Optional[Dict[str, str]] = None,
                 ignored_classes=None,
                 profile: str = "industrial",
                 # NMS IoU. None leaves ultralytics' default (0.7) in place,
                 # which is what the industrial detector has always run at;
                 # the lab checkpoint's config sets 0.5, the value it was
                 # evaluated with.
                 iou_threshold: Optional[float] = None,
                 # Exact index -> name map the weights MUST carry, or load()
                 # refuses. None means read-and-warn, the older behaviour.
                 expected_names: Optional[Dict[int, str]] = None,
                 # Raw-detection journal: directory for <camera_id>.jsonl, one
                 # line per box the model emitted at or above raw_log_conf —
                 # BEFORE mapping and BEFORE the user-visible threshold, so
                 # real-world precision can be measured and hard examples
                 # collected for retraining. None (the default) logs nothing.
                 raw_log_dir: Optional[str] = None,
                 raw_log_conf: Optional[float] = None):
        self.camera_id = camera_id
        self.profile = str(profile)
        self.class_map = dict(class_map if class_map is not None else CLASS_MAP)
        self.ignored_classes = set(
            ignored_classes if ignored_classes is not None else IGNORED_CLASSES)
        # Canonical lookup built once — see _canon. The literal map stays as
        # written so it still reads as documentation.
        self._canon_map = {_canon(k): v for k, v in self.class_map.items()}
        self.weights = weights or "models/ppe_safetyvision_v2.pt"
        self.device = str(device)
        self.interval_s = float(interval_s)
        self.conf_threshold = float(conf_threshold)
        self.iou_threshold = None if iou_threshold is None else float(iou_threshold)
        self.imgsz = int(imgsz)
        self.expected_names = (None if expected_names is None
                               else {int(k): str(v) for k, v in expected_names.items()})
        self.raw_log_dir = raw_log_dir
        # The journal floor never sits ABOVE the visible threshold: a journal
        # that omits what the rule engine saw cannot measure it.
        self.raw_log_conf = (min(float(raw_log_conf), self.conf_threshold)
                             if raw_log_conf is not None else self.conf_threshold)
        self.raw_log_path: Optional[str] = (
            os.path.join(raw_log_dir, "%s.jsonl" % camera_id) if raw_log_dir else None)
        self._raw_fh = None
        self.enabled = bool(enabled)
        self.status = "disabled" if not enabled else "not_loaded"
        self._model = None
        self._names: Dict[int, str] = {}
        # Vocabulary items (positives only, never "no_" or "person") that the
        # LOADED weights have a class for. Empty until load(). ppe_served reads
        # this so a zone cannot require an item this checkpoint cannot see —
        # the lab model covers five of the medical profile's items, and the
        # other five would otherwise be judged on silence.
        self.served_types: Set[str] = set()
        self._last_run = 0.0
        self.last_detections: List[dict] = []
        self.last_ts = 0.0
        self.stats = {"runs": 0, "detections": 0, "ignored": 0, "errors": 0,
                      "loads": 0, "below_threshold": 0, "logged": 0,
                      "log_errors": 0}

    # ---- lifecycle --------------------------------------------------------
    def load(self) -> bool:
        """Load once. Idempotent: a second call is a no-op, so a caller that
        retries cannot quietly put two copies of the model on the GPU."""
        if not self.enabled:
            return False
        if self._model is not None:
            return True
        try:
            import os
            if not os.path.exists(self.weights):
                raise FileNotFoundError(
                    "PPE weights not found at %s. Fetch the checkpoint or set "
                    "ppe.enabled: false. Do NOT substitute a stub - absence of "
                    "a detection is evidence in R-11, so a fake detector would "
                    "accuse everyone." % self.weights)
            from ultralytics import YOLO
            self._model = YOLO(self.weights)
            self._model.to("cuda:" + self.device if self.device.isdigit()
                           else self.device)
            # READ THE CLASSES FROM THE WEIGHTS, never from documentation. The
            # medical model card and the integration brief disagreed about
            # whether the names use spaces or underscores; only the file knows.
            self._names = {int(k): str(v) for k, v in dict(self._model.names).items()}
            if self.expected_names is not None and self._names != self.expected_names:
                # REFUSED, not adapted. Index order is what separates "Mask"
                # from "No Mask" in this checkpoint family; a retrain that
                # shuffled it would invert every verdict. The mismatch is
                # spelled out so the fix is obvious.
                raise ValueError(
                    "checkpoint class map does not match the expected %s "
                    "order. got %r, expected %r. A retrained checkpoint must "
                    "keep the class order of the original; if the order "
                    "changed deliberately, update PPE_CLASSES and the class "
                    "map together." % (self.profile, self._names,
                                       self.expected_names))
            present = {_canon(n) for n in self._names.values()}
            known = present & set(self._canon_map)
            if not known:
                raise ValueError(
                    "checkpoint classes %r contain none of the %s PPE classes "
                    "expected (%s)" % (sorted(self._names.values()),
                                       self.profile,
                                       ", ".join(sorted(self.class_map))))
            missing = {k for k in self.class_map if _canon(k) not in present}
            if missing:
                # Not fatal - a checkpoint with hardhat but no mask class is
                # still useful for hardhat zones - but it must be visible, or a
                # mask requirement would silently never be judged.
                log.warning("camera %s: %s checkpoint lacks PPE classes %s; "
                            "zones requiring those items cannot be judged",
                            self.camera_id, self.profile, sorted(missing))
            # The converse, and just as important: a class the checkpoint has
            # that we do not consume. Named so the omission is a decision on
            # the record rather than a silent drop.
            unmapped = sorted(n for n in self._names.values()
                              if _canon(n) not in self._canon_map)
            if unmapped:
                log.info("camera %s: %s checkpoint emits unmapped classes %s "
                         "(ignored by design)", self.camera_id, self.profile,
                         unmapped)
            # What this checkpoint can actually vouch for: an item is served
            # only if the weights carry its POSITIVE class. A negative alone
            # ("No X" with no "X") could never observe anyone compliant.
            self.served_types = {
                self._canon_map[c] for c in known
                if not self._canon_map[c].startswith("no_")
                and self._canon_map[c] != "person"}
            if self.raw_log_path:
                os.makedirs(self.raw_log_dir, exist_ok=True)
                self._raw_fh = open(self.raw_log_path, "a", encoding="utf-8")
                log.info("camera %s: raw %s PPE detections >= %.2f journalled "
                         "to %s", self.camera_id, self.profile,
                         self.raw_log_conf, self.raw_log_path)
            self.status = "ready"
            self.stats["loads"] += 1
            log.info("camera %s: PPE detection ready (%s, %d classes mapped, "
                     "%.1f Hz)", self.camera_id, self.weights, len(known),
                     1.0 / self.interval_s if self.interval_s else 0.0)
            return True
        except Exception as exc:                            # noqa: BLE001
            self.enabled = False
            self.status = "unavailable: %s: %s" % (exc.__class__.__name__, exc)
            log.error("camera %s: PPE DETECTION DISABLED — %s",
                      self.camera_id, exc)
            log.error("camera %s: detection, tracking, zones and every other "
                      "rule continue; PPE compliance is simply not judged this "
                      "run", self.camera_id)
            return False

    @property
    def ready(self) -> bool:
        return self.enabled and self._model is not None

    def due(self, now: float) -> bool:
        return self.ready and (now - self._last_run) >= self.interval_s

    # ---- inference --------------------------------------------------------
    def detect(self, frame, now: float, frame_id: Optional[int] = None) -> List[dict]:
        """Normalised detections. Never raises, never leaks a YOLO object.

        Returns [{class_name, confidence, bbox, ts, class_id, raw_class,
        is_violation}, ...] with class_name in the finblade vocabulary and
        is_violation True for any "no_" class. An inference failure returns []
        and is counted - it must not take the frame loop down with it.

        The model runs at the JOURNAL floor (raw_log_conf), never above the
        visible threshold; every box at that floor is journalled, and only
        boxes at or above conf_threshold are returned. NMS keeps a box or drops
        it on the strength of higher-scoring neighbours, so lowering the floor
        cannot change which boxes clear the higher bar.
        """
        if not self.ready:
            return []
        self._last_run = now
        out: List[dict] = []
        try:
            kw = {"conf": self.raw_log_conf, "imgsz": self.imgsz,
                  "verbose": False, "device": self.device}
            if self.iou_threshold is not None:
                kw["iou"] = self.iou_threshold
            res = self._model.predict(frame, **kw)[0]
        except Exception:                                   # noqa: BLE001
            self.stats["errors"] += 1
            log.exception("camera %s: PPE inference failed", self.camera_id)
            return []
        self.stats["runs"] += 1
        boxes = getattr(res, "boxes", None)
        if boxes is None or len(boxes) == 0:
            self.last_detections, self.last_ts = [], now
            return []
        for box, cls_i, conf in zip(boxes.xyxy.tolist(), boxes.cls.tolist(),
                                    boxes.conf.tolist()):
            raw = self._names.get(int(cls_i))
            mapped = self._canon_map.get(_canon(raw))
            bbox = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            # Journalled BEFORE any filtering, unmapped classes included: the
            # point is to see what the model does, not what we kept.
            self._journal(now, frame_id, int(cls_i), raw, mapped, float(conf), bbox)
            if mapped is None:
                self.stats["ignored"] += 1
                continue
            if float(conf) < self.conf_threshold:
                self.stats["below_threshold"] += 1
                continue
            out.append({
                "class_name": mapped,
                "confidence": float(conf),
                "bbox": bbox,
                "ts": float(now),
                "class_id": int(cls_i),
                "raw_class": str(raw),
                "is_violation": mapped.startswith("no_"),
            })
            self.stats["detections"] += 1
        self.last_detections, self.last_ts = out, now
        return out

    def _journal(self, now, frame_id, class_id, raw, mapped, conf, bbox) -> None:
        """One JSON line per raw box. Class, confidence, box, frame, time —
        and nothing that identifies a person: no crop, no track, no ref."""
        if self._raw_fh is None:
            return
        rec = {"ts": round(float(now), 3), "frame": frame_id,
               "camera": self.camera_id, "profile": self.profile,
               "class_id": class_id, "class": raw, "mapped": mapped,
               "conf": round(conf, 4),
               "bbox": [round(v, 1) for v in bbox]}
        try:
            self._raw_fh.write(json.dumps(rec) + "\n")
            self._raw_fh.flush()
            self.stats["logged"] += 1
        except OSError as exc:
            # A full disk must cost the journal, not the camera. Counted,
            # logged once, and the journal is closed so it cannot spam.
            self.stats["log_errors"] += 1
            log.error("camera %s: raw PPE journal %s failed (%s); journalling "
                      "stopped for this run", self.camera_id,
                      self.raw_log_path, exc)
            try:
                self._raw_fh.close()
            finally:
                self._raw_fh = None

    def detections_for(self, now: float, max_age_s: float = 1.0) -> List[dict]:
        """Last detections, if still current — for the annotator. Same staleness
        guard as the hazard detector: a PPE box left on screen after the model
        stopped seeing it is a lie about what the frame contained."""
        if not self.last_detections or (now - self.last_ts) > max_age_s:
            return []
        return list(self.last_detections)

    def snapshot(self) -> dict:
        return {"status": self.status, "enabled": self.enabled,
                "weights": self.weights, "interval_s": self.interval_s,
                "conf_threshold": self.conf_threshold,
                "iou_threshold": self.iou_threshold, "imgsz": self.imgsz,
                "served_types": sorted(self.served_types),
                "raw_log": self.raw_log_path,
                "raw_log_conf": self.raw_log_conf if self.raw_log_path else None,
                **self.stats}
