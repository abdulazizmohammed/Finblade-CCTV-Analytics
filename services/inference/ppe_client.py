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

import logging
import time
from typing import Dict, List, Optional

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


class PPEDetector:
    """One PPE model per camera worker, sampled on a cadence."""

    def __init__(self, camera_id: str, weights: Optional[str] = None,
                 device: str = "0", interval_s: float = 0.5,
                 conf_threshold: float = 0.35, imgsz: int = 640,
                 enabled: bool = False):
        self.camera_id = camera_id
        self.weights = weights or "models/ppe_safetyvision_v2.pt"
        self.device = str(device)
        self.interval_s = float(interval_s)
        self.conf_threshold = float(conf_threshold)
        self.imgsz = int(imgsz)
        self.enabled = bool(enabled)
        self.status = "disabled" if not enabled else "not_loaded"
        self._model = None
        self._names: Dict[int, str] = {}
        self._last_run = 0.0
        self.last_detections: List[dict] = []
        self.last_ts = 0.0
        self.stats = {"runs": 0, "detections": 0, "ignored": 0, "errors": 0,
                      "loads": 0}

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
            self._names = dict(self._model.names)
            known = set(self._names.values()) & set(CLASS_MAP)
            if not known:
                raise ValueError(
                    "checkpoint classes %r contain none of the PPE classes "
                    "Phase 3 needs (%s)" % (sorted(self._names.values()),
                                            ", ".join(sorted(CLASS_MAP))))
            missing = set(CLASS_MAP) - set(self._names.values())
            if missing:
                # Not fatal - a checkpoint with hardhat but no mask class is
                # still useful for hardhat zones - but it must be visible, or a
                # mask requirement would silently never be judged.
                log.warning("camera %s: checkpoint lacks PPE classes %s; zones "
                            "requiring those items cannot be judged",
                            self.camera_id, sorted(missing))
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
    def detect(self, frame, now: float) -> List[dict]:
        """Normalised detections. Never raises, never leaks a YOLO object.

        Returns [{class_name, confidence, bbox, ts}, ...] with class_name in the
        finblade vocabulary. An inference failure returns [] and is counted -
        it must not take the frame loop down with it.
        """
        if not self.ready:
            return []
        self._last_run = now
        out: List[dict] = []
        try:
            res = self._model.predict(frame, conf=self.conf_threshold,
                                      imgsz=self.imgsz, verbose=False,
                                      device=self.device)[0]
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
            mapped = CLASS_MAP.get(raw)
            if mapped is None:
                self.stats["ignored"] += 1
                continue
            out.append({
                "class_name": mapped,
                "confidence": float(conf),
                "bbox": (float(box[0]), float(box[1]),
                         float(box[2]), float(box[3])),
                "ts": float(now),
            })
            self.stats["detections"] += 1
        self.last_detections, self.last_ts = out, now
        return out

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
                "conf_threshold": self.conf_threshold, **self.stats}
