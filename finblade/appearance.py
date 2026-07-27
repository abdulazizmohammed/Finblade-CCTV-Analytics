"""Appearance embeddings for cross-camera re-identification.

An embedding is what makes cross-camera matching possible: ByteTrack's motion
model cannot survive a person leaving one camera and appearing in another, so
identity has to be carried by how the person looks.

PRIVACY — read before extending this module. A person-ReID embedding is a
biometric template: it is precisely the artifact that makes an individual
re-identifiable later. The rest of this system deliberately holds no such thing
(see identity.py). The rules that keep that promise defensible:

  * embeddings live in RAM only — never written to disk, evidence/, logs or the
    database;
  * they are dropped when the track is reaped and when the gallery TTL expires;
  * nothing outside the process ever sees a vector — the identity service
    returns an opaque global ref, and that ref is what gets persisted.

Breaking any of those turns a "we store no biometrics" product into one that
does. See DECISIONS.md D-11.

PERFORMANCE — measured, not assumed. boxmot's crop preprocessing is CPU-bound
and scales linearly with crop count (~6ms/crop on this box; 1 crop 19.6ms,
32 crops 184ms). Embedding every person every frame would cost ~120ms/frame at
20 people and drop the pipeline from ~23 FPS to ~8. So we do NOT embed densely.
CropQualityGate discards crops that would yield a poor vector anyway, and
EmbeddingSampler spreads sampling thinly over each track's lifetime under a
hard per-frame budget. A track needs a handful of good views, not hundreds.

The gate and the sampler are pure stdlib so they unit-test without torch.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

BBox = Tuple[float, float, float, float]  # x1, y1, x2, y2


# --------------------------------------------------------------------------
# Crop quality gating (pure stdlib)
# --------------------------------------------------------------------------

@dataclass
class CropQualityGate:
    """Rejects crops that would produce a misleading embedding.

    A bad crop is worse than no crop: a half-occluded or truncated person still
    yields a confident-looking 512-d vector, and that vector poisons the track's
    feature bank and causes false cross-camera matches. Rejecting is cheap;
    a wrong identity link is not.
    """

    min_height_px: float = 96.0     # below this, detail is gone at 256x128 input
    min_width_px: float = 32.0
    min_confidence: float = 0.5     # stricter than the detector's 0.35
    max_aspect: float = 1.1         # w/h; a person taller than wide. Wider =>
                                    # merged detections or a person lying down
    min_aspect: float = 0.15        # implausibly thin => sliver of a box
    edge_margin_px: float = 8.0     # touching frame border => likely truncated

    def check(self, bbox: BBox, confidence: float, frame_w: int,
              frame_h: int) -> Tuple[bool, str]:
        """Return (accepted, reason). ``reason`` is "ok" when accepted."""
        x1, y1, x2, y2 = bbox
        w = x2 - x1
        h = y2 - y1

        if w <= 0 or h <= 0:
            return False, "degenerate"
        if confidence < self.min_confidence:
            return False, "low_confidence"
        if h < self.min_height_px:
            return False, "too_small"
        if w < self.min_width_px:
            return False, "too_narrow"

        aspect = w / h
        if aspect > self.max_aspect:
            return False, "aspect_wide"
        if aspect < self.min_aspect:
            return False, "aspect_thin"

        m = self.edge_margin_px
        if x1 <= m or y1 <= m or x2 >= (frame_w - m) or y2 >= (frame_h - m):
            return False, "truncated_at_edge"

        return True, "ok"

    def occluded_by(self, bbox: BBox, others: Sequence[BBox],
                    max_overlap: float = 0.25) -> bool:
        """True if another box covers more than ``max_overlap`` of this one.

        Crowd scenes are the case cross-camera ReID most needs to survive and
        the case where crops are most often two people in one box. Note this is
        intersection over THIS box's area, not IoU — a large occluder that
        swallows a small box has low IoU but ruins the crop completely.
        """
        x1, y1, x2, y2 = bbox
        area = (x2 - x1) * (y2 - y1)
        if area <= 0:
            return True
        for o in others:
            if o is bbox or tuple(o) == tuple(bbox):
                continue
            ox1, oy1, ox2, oy2 = o
            iw = min(x2, ox2) - max(x1, ox1)
            ih = min(y2, oy2) - max(y1, oy1)
            if iw <= 0 or ih <= 0:
                continue
            if (iw * ih) / area > max_overlap:
                return True
        return False


# --------------------------------------------------------------------------
# Sampling cadence + per-frame budget (pure stdlib)
# --------------------------------------------------------------------------

class EmbeddingSampler:
    """Decides which tracks get embedded on this frame.

    Two independent limits, both needed:
      * per-track cadence — a track is sampled at most every ``interval_s`` and
        at most ``max_samples`` times in its life. Consecutive frames of the
        same person are near-duplicates; they add cost and no information.
      * per-frame budget — never embed more than ``budget`` crops in one frame,
        so a sudden crowd degrades the sample rate rather than the frame rate.

    When more tracks want sampling than the budget allows, tracks with the
    fewest samples win. A brand-new track (0 samples, therefore unmatchable
    across cameras) must not be starved by established ones.
    """

    def __init__(self, interval_s: float = 1.0, max_samples: int = 5,
                 budget_per_frame: int = 8):
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        if max_samples <= 0:
            raise ValueError("max_samples must be > 0")
        if budget_per_frame <= 0:
            raise ValueError("budget_per_frame must be > 0")
        self.interval_s = interval_s
        self.max_samples = max_samples
        self.budget_per_frame = budget_per_frame
        self._count: Dict[int, int] = {}
        self._last_ts: Dict[int, float] = {}

    def wants_sample(self, track_id: int, now: float) -> bool:
        tid = int(track_id)
        if self._count.get(tid, 0) >= self.max_samples:
            return False
        last = self._last_ts.get(tid)
        return last is None or (now - last) >= self.interval_s

    def select(self, track_ids: Sequence[int], now: float) -> List[int]:
        """Pick the tracks to embed this frame, fewest-samples-first."""
        wanting = [int(t) for t in track_ids if self.wants_sample(t, now)]
        # Sort by (samples so far, track id) — the id keeps it deterministic,
        # which matters for reproducible tests and evidence runs.
        wanting.sort(key=lambda t: (self._count.get(t, 0), t))
        return wanting[: self.budget_per_frame]

    def record(self, track_id: int, now: float) -> None:
        tid = int(track_id)
        self._count[tid] = self._count.get(tid, 0) + 1
        self._last_ts[tid] = now

    def sample_count(self, track_id: int) -> int:
        return self._count.get(int(track_id), 0)

    def drop(self, track_id: int) -> None:
        tid = int(track_id)
        self._count.pop(tid, None)
        self._last_ts.pop(tid, None)

    def tracked(self) -> int:
        return len(self._count)


# --------------------------------------------------------------------------
# Per-track feature bank
# --------------------------------------------------------------------------

def _l2_normalize(vec: Sequence[float]) -> List[float]:
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return list(vec)
    return [v / norm for v in vec]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors. Inputs need not be normalized.

    Kept in pure Python so the matching logic is testable without numpy; the
    vectors are 512-d and compared a few times per track, not per pixel.
    """
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    na = sum(v * v for v in a) ** 0.5
    nb = sum(v * v for v in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


@dataclass
class TrackFeatureBank:
    """The K most recent embeddings for one track, plus their mean.

    A single view of a person is fragile — they turn, get partly occluded, walk
    under a different light. Averaging several L2-normalized views gives a much
    more stable signature, which is what we actually match on. We keep the
    individual vectors too, because max-similarity-over-views beats
    mean-similarity when the two cameras see opposite sides of a person.
    """

    capacity: int = 5
    vectors: List[List[float]] = field(default_factory=list)

    def add(self, embedding: Sequence[float]) -> None:
        self.vectors.append(_l2_normalize(embedding))
        if len(self.vectors) > self.capacity:
            self.vectors.pop(0)      # oldest out; recent views describe them best

    @property
    def n(self) -> int:
        return len(self.vectors)

    def mean_vector(self) -> Optional[List[float]]:
        if not self.vectors:
            return None
        dim = len(self.vectors[0])
        acc = [0.0] * dim
        for v in self.vectors:
            for i, x in enumerate(v):
                acc[i] += x
        return _l2_normalize([x / len(self.vectors) for x in acc])

    def similarity(self, other: "TrackFeatureBank") -> float:
        """Best similarity between any pair of views, blended with the means.

        Pure max-over-pairs is too eager (one lucky angle links two strangers);
        pure mean-to-mean is too timid (it washes out a genuine match seen from
        two different sides). Taking the larger of [mean-to-mean] and
        [0.9 x best-pair] keeps a strong single-view match while still requiring
        it to be clearly strong.
        """
        if not self.vectors or not other.vectors:
            return 0.0
        best_pair = max(cosine_similarity(a, b)
                        for a in self.vectors for b in other.vectors)
        m1, m2 = self.mean_vector(), other.mean_vector()
        mean_sim = cosine_similarity(m1, m2) if m1 and m2 else 0.0
        return max(mean_sim, 0.9 * best_pair)


# --------------------------------------------------------------------------
# The embedder itself (the only part that needs torch)
# --------------------------------------------------------------------------

class OSNetEmbedder:
    """Thin wrapper over boxmot's OSNet ReID backend.

    Imports are lazy so that every module above stays importable — and testable
    — on a machine with no torch and no weights. Callers that cannot load the
    model should degrade to spatio-temporal matching, NOT to a fake embedder:
    a stub vector would silently produce confident wrong identities, which is
    the failure mode this system can least afford (CLAUDE.md rule 1).
    """

    DEFAULT_WEIGHTS = "models/osnet_x0_25_msmt17.pt"

    def __init__(self, weights: str = None, device: str = "0"):
        self.weights = weights or self.DEFAULT_WEIGHTS
        self.device = device
        self._reid = None

    def load(self) -> None:
        from pathlib import Path            # lazy: keep import cost off the
        from boxmot.reid import ReID        # test path entirely
        path = Path(self.weights)
        if not path.exists():
            raise FileNotFoundError(
                f"ReID weights not found at {path}. Run scripts/get_weights.py. "
                "Do not substitute a stub embedder."
            )
        self._reid = ReID(weights=path, device=self.device)

    @property
    def loaded(self) -> bool:
        return self._reid is not None

    def embed(self, frame, bboxes: Sequence[BBox]) -> List[List[float]]:
        """Return one L2-normalized embedding per bbox (empty list if none)."""
        if not self.loaded:
            raise RuntimeError("OSNetEmbedder.load() has not been called")
        if not bboxes:
            return []
        import numpy as np
        boxes = np.asarray(bboxes, dtype=np.float32).reshape(-1, 4)
        feats = self._reid(frame, boxes=boxes)
        if feats is None or getattr(feats, "size", 0) == 0:
            return []
        return [list(map(float, row)) for row in feats]
