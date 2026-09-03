"""Worker-side fire/smoke detection: a second model on the same decoded frame.

Sits between the inference loop and finblade.rules R-10. The loop hands it the
frame it already decoded; this decides whether it is due to run, batches one
predict, and returns the highest confidence per hazard class. The RULE decides
whether that constitutes an alert — this module never does.

DEGRADES, NEVER FAKES. Missing weights, no torch, a corrupt checkpoint: it
switches itself off and says so loudly. Detection, tracking, zones, metrics and
every other rule are untouched. A stub that returned zeros would be worse than
off, because "no fire" and "not looking" would become indistinguishable, and the
one is a statement while the other is silence. Mirrors ReIDResolver.

WHY 2 Hz AND NOT PER FRAME. Phase 1 measured a second always-on model at
1080p/imgsz1280 costing ~21ms per frame — roughly doubling GPU inference and
taking one camera from ~24fps to ~10fps. A fire that matters persists for
seconds; sub-second flicker is the false-positive generator, not the signal. At
2 Hz the second model costs ~13% of its per-frame price. Same reasoning
EmbeddingSampler applies to OSNet: a handful of good looks, not hundreds.

CLASS ORDER IS READ FROM THE CHECKPOINT, NOT ASSUMED. This model is
{0: 'smoke', 1: 'fire'} — smoke first, which is the opposite of the obvious
guess. Hard-coding indices would have swapped every fire alert for a smoke one
and produced a system that looked like it worked.

THE MODEL IS AN EVALUATION CHECKPOINT, NOT A VALIDATED FIRE ALARM. See
DECISIONS.md D-31 and docs/CAPABILITIES.md before anyone relies on it.
"""

import logging
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger("finblade.hazard")


class HazardDetector:
    """One fire/smoke model per camera worker, sampled on a cadence."""

    def __init__(self, camera_id: str, weights: Optional[str] = None,
                 device: str = "0", interval_s: float = 0.5,
                 conf_threshold: float = 0.30, imgsz: int = 640,
                 enabled: bool = True):
        self.camera_id = camera_id
        self.weights = weights or "models/fire_smoke_yolov8n.pt"
        self.device = str(device)
        self.interval_s = float(interval_s)
        # DELIBERATELY BELOW the rule's arming thresholds (fire_on 0.60,
        # smoke_on 0.65). The detector's floor decides what is reported; the
        # rule's thresholds decide what alerts. Setting this at or above the
        # rule's bar would hide every sub-threshold reading, and the rule's
        # hysteresis would then have nothing to clear against.
        self.conf_threshold = float(conf_threshold)
        self.imgsz = int(imgsz)
        self.enabled = bool(enabled)
        self.status = "disabled" if not enabled else "not_loaded"
        self._model = None
        self._names: Dict[int, str] = {}
        self._last_run = 0.0
        self.stats = {"runs": 0, "detections": 0, "errors": 0,
                      "fire_frames": 0, "smoke_frames": 0}

    # ---- lifecycle --------------------------------------------------------
    def load(self) -> bool:
        if not self.enabled:
            return False
        try:
            import os
            if not os.path.exists(self.weights):
                raise FileNotFoundError(
                    "hazard weights not found at %s. Fetch the checkpoint or "
                    "set hazard.enabled: false. Do NOT substitute a stub."
                    % self.weights)
            from ultralytics import YOLO
            self._model = YOLO(self.weights)
            self._model.to("cuda:" + self.device if self.device.isdigit()
                           else self.device)
            self._names = dict(self._model.names)
            lowered = {i: str(n).lower() for i, n in self._names.items()}
            if not any(n in ("fire", "smoke") for n in lowered.values()):
                raise ValueError(
                    "checkpoint classes %r contain neither 'fire' nor 'smoke'; "
                    "this is not a fire/smoke detector" % (self._names,))
            self._names = lowered
            self.status = "ready"
            log.info("camera %s: hazard detection ready (%s, classes=%s, %.1f Hz)",
                     self.camera_id, self.weights, self._names,
                     1.0 / self.interval_s if self.interval_s else 0.0)
            return True
        except Exception as exc:                            # noqa: BLE001
            self.enabled = False
            self.status = "unavailable: %s: %s" % (exc.__class__.__name__, exc)
            log.error("camera %s: HAZARD DETECTION DISABLED — %s",
                      self.camera_id, exc)
            log.error("camera %s: everything else continues; fire/smoke is "
                      "simply not being watched this run", self.camera_id)
            return False

    @property
    def ready(self) -> bool:
        return self.enabled and self._model is not None

    # ---- per-frame --------------------------------------------------------
    def due(self, now: float) -> bool:
        """Is it time to look again?"""
        return self.ready and (now - self._last_run) >= self.interval_s

    def observe(self, frame, now: float) -> Dict[str, Tuple[float, int]]:
        """Run the model. Returns {class_name: (max_confidence, count)}.

        Returns an entry for every class the checkpoint knows, with 0.0 when
        nothing was seen. That is not padding: R-10's latch clears on a
        sustained LOW reading, so a caller handed only positives would have
        nothing to clear on and the alert would latch on for ever.

        A DEAD DETECTOR RETURNS {} INSTEAD, and that asymmetry is deliberate.
        "I looked and saw no fire" is a reading and must be fed to the latch;
        "I am not looking" is not, and feeding it as 0.0 would let a failed
        model silently CLEAR a live fire alert. An empty dict means the rule is
        not called at all and the latch holds whatever state it had. In
        practice the loop never gets here — due() is False when not ready — but
        the semantics have to be right for the case where it does.
        """
        out = {n: (0.0, 0) for n in set(self._names.values())}
        if not self.ready:
            return out
        self._last_run = now
        try:
            res = self._model.predict(frame, conf=self.conf_threshold,
                                      imgsz=self.imgsz, verbose=False,
                                      device=self.device)[0]
        except Exception:                                   # noqa: BLE001
            # Must not kill the frame loop. Counted so a detector that has been
            # failing all run is visible rather than inferred from silence.
            self.stats["errors"] += 1
            log.exception("camera %s: hazard inference failed", self.camera_id)
            return out
        self.stats["runs"] += 1
        if res.boxes is None or len(res.boxes) == 0:
            return out
        for cls_i, conf in zip(res.boxes.cls.tolist(), res.boxes.conf.tolist()):
            name = self._names.get(int(cls_i))
            if name is None:
                continue
            best, count = out.get(name, (0.0, 0))
            out[name] = (max(best, float(conf)), count + 1)
            self.stats["detections"] += 1
        if out.get("fire", (0.0, 0))[1]:
            self.stats["fire_frames"] += 1
        if out.get("smoke", (0.0, 0))[1]:
            self.stats["smoke_frames"] += 1
        return out

    def snapshot(self) -> dict:
        return {"status": self.status, "enabled": self.enabled,
                "weights": self.weights, "interval_s": self.interval_s,
                "classes": sorted(set(self._names.values())) or None,
                **self.stats}
