"""Per-window key-dependent confidentiality for held appearance templates.

WHAT THIS IS, PRECISELY. Each retention window ("epoch") has its own random
orthogonal matrix Q. Templates are stored projected as Qv rather than as v.
Because Q is orthogonal (QᵀQ = I):

    (Qa)·(Qb) = aᵀQᵀQb = a·b        and        ‖Qa‖ = ‖a‖

so cosine similarity is preserved EXACTLY — not approximately — and matching
accuracy is mathematically unchanged. That is the whole reason this transform
was chosen over a lossy one.

WHAT THIS IS NOT. It is NOT non-invertible. Q⁻¹ = Qᵀ, so anyone holding the
epoch key recovers the raw template perfectly. Calling this a one-way or
"cancelable biometric" transform would be false: in BioHashing the one-way
property comes from a QUANTISATION step, and that step is exactly what costs
matching accuracy. We do not do it, so we do not get it.

The honest description, used everywhere in this codebase:

    key-dependent confidentiality with per-window unlinkability

  confidentiality  a heap dump WITHOUT the key yields vectors in an unknown
                   rotated basis. With the key it yields the templates.
  unlinkability    templates from an expired epoch cannot be re-projected into
                   the current one once that epoch's key is destroyed, so two
                   dumps more than two epochs apart cannot be correlated.

  NOT claimed      a dump taken NOW contains both live keys, so it can link
                   across the current and previous window. Two live keys is
                   what stops matching breaking at the boundary; the cost is
                   that the unlinkability boundary is 2 epochs, not 1.

This is defence in depth against memory inspection and crash dumps. It is not
a substitute for the templates being ephemeral, and it does not make holding
them for 24 hours a decision that can be taken lightly. See DECISIONS.md D-30.

numpy is imported lazily so this module — and everything importing it — stays
importable on a box without it. A 512x512 matmul in pure Python is ~262k
multiply-adds per vector, which is not a realistic option.
"""

import secrets
import threading
from typing import Dict, List, Optional, Sequence

# 12 hours. Two keys are live at once, so retention ranges [EPOCH, 2*EPOCH] and
# this is what makes 24h the ceiling rather than a coincidence.
DEFAULT_EPOCH_SECONDS = 43200.0

# Hard ceiling on the whole feature, independent of any configured value. A
# mistyped environment variable must not be able to turn a bounded window into
# an unbounded one — the same reasoning as globalid's max_retention_seconds.
MAX_EXTENDED_RETENTION_SECONDS = 86400.0


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:                              # pragma: no cover
        raise RuntimeError(
            "extended ReID retention needs numpy for the epoch transform. "
            "It is a pinned dependency; install per requirements.txt. "
            "Do NOT fall back to storing raw templates."
        ) from exc
    return np


def random_orthogonal(dim: int, seed: Optional[bytes] = None):
    """A Haar-random orthogonal matrix, via QR of a Gaussian matrix.

    The sign correction on R's diagonal is what makes the result uniform over
    the orthogonal group rather than biased by numpy's QR convention. It does
    not affect the similarity-preserving property — any orthogonal Q has that
    — but a biased key distribution would weaken the confidentiality claim.
    """
    np = _require_numpy()
    if dim <= 0:
        raise ValueError("dim must be positive")
    # A fresh OS-entropy seed unless one is supplied. Seeding is exposed only
    # so tests can be deterministic; production never passes one.
    rng = np.random.default_rng(
        list(seed) if seed is not None else list(secrets.token_bytes(32)))
    a = rng.standard_normal((dim, dim))
    q, r = np.linalg.qr(a)
    return q * np.sign(np.diag(r))


class EpochKey:
    """One window's key. The matrix never leaves this object."""

    __slots__ = ("epoch_id", "created_at", "dim", "_q")

    def __init__(self, epoch_id: int, created_at: float, dim: int,
                 seed: Optional[bytes] = None):
        self.epoch_id = int(epoch_id)
        self.created_at = float(created_at)
        self.dim = int(dim)
        self._q = random_orthogonal(dim, seed=seed)

    def project(self, vec: Sequence[float]) -> List[float]:
        np = _require_numpy()
        return list(map(float, self._q @ np.asarray(vec, dtype=np.float64)))

    def unproject(self, vec: Sequence[float]) -> List[float]:
        """Qᵀ. Present because re-projection across an epoch boundary needs it
        — and because pretending it does not exist would be dishonest about
        what this transform is."""
        np = _require_numpy()
        return list(map(float, self._q.T @ np.asarray(vec, dtype=np.float64)))

    def destroy(self) -> None:
        """Overwrite the matrix, then drop it.

        Zeroing first is deliberate: dropping the reference alone leaves the
        buffer in the allocator's free list until it happens to be reused, and
        a crash dump taken in between still contains the key.
        """
        if self._q is not None:
            try:
                self._q[:] = 0.0
            except Exception:                               # noqa: BLE001
                pass
        self._q = None


class EpochKeyring:
    """Two live keys: current and previous. Older keys are destroyed.

    WHY TWO. A single key rotated on a fixed boundary breaks matching for
    everyone present when it turns over — the naive design. Keeping the
    previous key means an identity last seen before the boundary can still be
    compared, and is re-projected forward the next time it is seen. Only
    someone unseen for an ENTIRE epoch is dropped, which is the intended
    expiry, not an artifact of the clock.

    The cost, stated plainly: retention is [epoch, 2*epoch] rather than
    exactly one epoch, and a memory dump can link across two windows.
    """

    def __init__(self, dim: int, epoch_seconds: float = DEFAULT_EPOCH_SECONDS,
                 now: float = 0.0, seed_factory=None):
        if epoch_seconds <= 0:
            raise ValueError("epoch_seconds must be > 0")
        self.dim = int(dim)
        self.epoch_seconds = float(epoch_seconds)
        self._lock = threading.Lock()
        self._seed_factory = seed_factory        # tests only
        self._next_id = 0
        self._keys: Dict[int, EpochKey] = {}
        self._current_id: Optional[int] = None
        self.stats = {"rolled": 0, "destroyed": 0, "orphaned": 0}
        self._roll(now)

    # ---- internals --------------------------------------------------------
    def _roll(self, now: float) -> None:
        key = EpochKey(self._next_id, now, self.dim,
                       seed=self._seed_factory(self._next_id) if self._seed_factory else None)
        self._keys[key.epoch_id] = key
        self._current_id = key.epoch_id
        self._next_id += 1
        self.stats["rolled"] += 1
        # Destroy everything older than the previous epoch.
        for eid in sorted(self._keys)[:-2]:
            self._keys.pop(eid).destroy()
            self.stats["destroyed"] += 1

    # ---- API --------------------------------------------------------------
    def maybe_roll(self, now: float) -> Optional[int]:
        """Advance the epoch if the window has elapsed. Returns the id retired,
        or None. Callers must drop any identity still on a retired epoch —
        its templates are no longer projectable and are dead weight."""
        with self._lock:
            cur = self._keys.get(self._current_id)
            if cur is None or (now - cur.created_at) < self.epoch_seconds:
                return None
            retiring = sorted(self._keys)[0] if len(self._keys) >= 2 else None
            self._roll(now)
            return retiring

    @property
    def current_epoch(self) -> int:
        return self._current_id

    def live_epochs(self) -> List[int]:
        return sorted(self._keys)

    def has(self, epoch_id: Optional[int]) -> bool:
        return epoch_id in self._keys

    def project(self, vec: Sequence[float], epoch_id: Optional[int] = None) -> List[float]:
        eid = self._current_id if epoch_id is None else epoch_id
        key = self._keys.get(eid)
        if key is None:
            raise KeyError("epoch %s is not live" % eid)
        return key.project(vec)

    def reproject(self, vec: Sequence[float], from_epoch: int,
                  to_epoch: Optional[int] = None) -> List[float]:
        """Move a stored template from one live epoch's basis into another.

        Composed as Q_to @ Q_fromᵀ. Exact, because both are orthogonal — the
        template is unchanged, only its basis moves.
        """
        to_epoch = self._current_id if to_epoch is None else to_epoch
        if from_epoch == to_epoch:
            return list(vec)
        src = self._keys.get(from_epoch)
        dst = self._keys.get(to_epoch)
        if src is None or dst is None:
            raise KeyError("epoch %s or %s is not live" % (from_epoch, to_epoch))
        return dst.project(src.unproject(vec))

    def destroy_all(self) -> int:
        """Erasure. Every key gone, so every stored template is unrecoverable.

        The immediate-deletion mechanism: destroying the keys is faster and
        more complete than walking the gallery, and it cannot miss one.
        """
        with self._lock:
            n = len(self._keys)
            for key in self._keys.values():
                key.destroy()
            self._keys.clear()
            self._current_id = None
            self.stats["destroyed"] += n
            return n

    def snapshot(self) -> dict:
        return {"dim": self.dim, "epoch_seconds": self.epoch_seconds,
                "current_epoch": self._current_epoch_safe(),
                "live_epochs": self.live_epochs(), **self.stats}

    def _current_epoch_safe(self):
        return self._current_id
