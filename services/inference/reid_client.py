"""Worker-side cross-camera identity: sample crops, embed, resolve to a ref.

Sits between the inference loop and the API's identity service. The loop hands
it the frame and this frame's tracks; it decides which crops are worth
embedding, batches them through OSNet, accumulates a small feature bank per
track, and — once a track has enough views to be worth matching — asks the API
who this person is.

DEGRADES, NEVER FAKES. If the weights are missing, torch is unavailable or the
API is unreachable, ReID switches itself off and says so. Detection, tracking,
zones, metrics and rules are untouched and keep running; cross-camera identity
simply reports as unavailable. A stub embedder would emit confident wrong
identities, which is worse than no feature at all (CLAUDE.md rule 1).

Embeddings are held only in this object's banks and are dropped when the track
is reaped. Nothing writes them to evidence/, logs or disk.
"""

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from finblade.appearance import (CropQualityGate, EmbeddingSampler,
                                 OSNetEmbedder, TrackFeatureBank)

log = logging.getLogger("finblade.reid")

BBox = Tuple[float, float, float, float]


class ReIDResolver:
    """Per-camera ReID front end. One instance per inference process."""

    def __init__(
        self,
        camera_id: str,
        api_post_json,                      # callable(path, payload) -> dict|None
        weights: Optional[str] = None,
        device: str = "0",
        interval_s: float = 1.0,
        max_samples: int = 5,
        budget_per_frame: int = 8,
        min_samples_to_resolve: int = 2,
        bank_capacity: int = 5,
        enabled: bool = True,
        retry_interval_s: float = 2.0,
        min_crop_height: float = 96.0,
        min_crop_confidence: float = 0.5,
    ):
        self.camera_id = camera_id
        self._post_json = api_post_json
        self.min_samples_to_resolve = max(1, int(min_samples_to_resolve))
        self.bank_capacity = bank_capacity
        # Without this, an unreachable API is retried for every track on every
        # frame — a 30s run with the API down produced 1877 failed POSTs.
        self.retry_interval_s = float(retry_interval_s)
        self._last_attempt: Dict[int, float] = {}

        # Tunable because the right floor depends on the source resolution.
        # 96px suits a 1080p+ main stream. On a 640x360 substream — often the
        # only option over a WAN link — 96px is 27% of frame height, so only
        # people very close to the camera are ever embedded and cross-camera
        # identity quietly stops working for everyone else. Lowering it trades
        # embedding quality for coverage; see docs/DEPLOY.md.
        self.gate = CropQualityGate(min_height_px=float(min_crop_height),
                                    min_confidence=float(min_crop_confidence))
        self.sampler = EmbeddingSampler(interval_s=interval_s,
                                        max_samples=max_samples,
                                        budget_per_frame=budget_per_frame)
        self.embedder = OSNetEmbedder(weights=weights, device=device)

        self._banks: Dict[int, TrackFeatureBank] = {}
        self._refs: Dict[int, str] = {}
        self.enabled = bool(enabled)
        self.status = "disabled" if not enabled else "not_loaded"
        self.stats = {"embedded": 0, "gated_out": 0, "resolved": 0,
                      "matched": 0, "resolve_failed": 0}

    # ---- lifecycle --------------------------------------------------------
    def load(self) -> bool:
        """Load the embedder. Returns False (and disables ReID) on failure."""
        if not self.enabled:
            return False
        try:
            self.embedder.load()
            self.status = "ready"
            log.info("camera %s: ReID ready (%s)", self.camera_id,
                     self.embedder.weights)
            return True
        except Exception as exc:                       # noqa: BLE001
            self.enabled = False
            self.status = f"unavailable: {exc.__class__.__name__}: {exc}"
            # Loud: the human must know cross-camera identity is off, and why.
            log.error("camera %s: ReID DISABLED — %s", self.camera_id, exc)
            log.error("camera %s: detection/tracking/zones/rules continue normally; "
                      "cross-camera identity is unavailable this run", self.camera_id)
            return False

    @property
    def ready(self) -> bool:
        return self.enabled and self.embedder.loaded

    # ---- per-frame work ---------------------------------------------------
    def observe(self, frame, tracks: Sequence[Tuple[int, float, float, float, float]],
                confidences: Dict[int, float], now: float,
                frame_w: int, frame_h: int) -> None:
        """Embed a budgeted subset of this frame's tracks.

        ``tracks`` is the loop's (tid, x1, y1, x2, y2) list; ``confidences`` maps
        tid -> detection confidence.
        """
        if not self.ready or not tracks:
            return

        boxes_by_tid = {int(t[0]): (float(t[1]), float(t[2]), float(t[3]), float(t[4]))
                        for t in tracks}
        all_boxes = list(boxes_by_tid.values())

        # Ask the sampler first — cheapest filter, and it enforces the frame budget.
        candidates = self.sampler.select(list(boxes_by_tid), now)
        if not candidates:
            return

        chosen: List[int] = []
        chosen_boxes: List[BBox] = []
        for tid in candidates:
            box = boxes_by_tid[tid]
            ok, reason = self.gate.check(box, confidences.get(tid, 1.0),
                                         frame_w, frame_h)
            if not ok or self.gate.occluded_by(box, all_boxes):
                self.stats["gated_out"] += 1
                continue
            chosen.append(tid)
            chosen_boxes.append(box)

        if not chosen:
            return

        try:
            vectors = self.embedder.embed(frame, chosen_boxes)
        except Exception:                              # noqa: BLE001
            # A failure here must not kill the frame loop; ReID stays on and
            # retries next frame, but the failure is visible in the log.
            log.exception("camera %s: embedding failed", self.camera_id)
            return

        for tid, vec in zip(chosen, vectors):
            bank = self._banks.setdefault(
                tid, TrackFeatureBank(capacity=self.bank_capacity))
            bank.add(vec)
            self.sampler.record(tid, now)
            self.stats["embedded"] += 1

    def resolve_pending(self, now: float, zone_of_track: Dict[int, Optional[str]]
                        ) -> Dict[int, str]:
        """Resolve tracks that have enough views and no ref yet.

        Returns the newly assigned {track_id: global_ref}. Resolution is
        deliberately deferred until the bank has ``min_samples_to_resolve``
        views: binding is sticky on the server, so a hasty match on one weak
        crop cannot be undone.
        """
        assigned: Dict[int, str] = {}
        if not self.ready:
            return assigned

        for tid, bank in self._banks.items():
            if tid in self._refs or bank.n < self.min_samples_to_resolve:
                continue
            last = self._last_attempt.get(tid)
            if last is not None and (now - last) < self.retry_interval_s:
                continue                    # back off; the API was just down
            self._last_attempt[tid] = now
            resp = self._post_json("/api/v1/identity/resolve", {
                "camera_id": self.camera_id,
                "local_track_id": int(tid),
                "ts": now,
                "zone_id": zone_of_track.get(tid),
                "embeddings": bank.vectors,
            })
            if not resp or not resp.get("global_ref"):
                self.stats["resolve_failed"] += 1
                continue
            ref = resp["global_ref"]
            self._refs[tid] = ref
            self.stats["resolved"] += 1
            if resp.get("matched"):
                self.stats["matched"] += 1
                log.info("camera %s: track %s matched %s (score %.3f, %s)",
                         self.camera_id, tid, ref, resp.get("score", 0.0),
                         resp.get("reason"))
            assigned[tid] = ref
        return assigned

    # ---- track lifecycle --------------------------------------------------
    def global_ref(self, track_id: int) -> Optional[str]:
        return self._refs.get(int(track_id))

    def drop(self, track_id: int) -> None:
        """Track left the scene: free its vectors and release the binding."""
        tid = int(track_id)
        bank = self._banks.pop(tid, None)
        if bank is not None:
            bank.vectors.clear()               # explicit: templates do not linger
        self.sampler.drop(tid)
        self._last_attempt.pop(tid, None)
        if self._refs.pop(tid, None) is not None and self.ready:
            self._post_json("/api/v1/identity/release", {
                "camera_id": self.camera_id, "local_track_id": tid})

    def snapshot(self) -> dict:
        """Run summary for evidence/metrics.json — counts only, never vectors."""
        return {
            "status": self.status,
            "enabled": self.enabled,
            "tracks_with_bank": len(self._banks),
            "tracks_with_ref": len(self._refs),
            **self.stats,
        }
