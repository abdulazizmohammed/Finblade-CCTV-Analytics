"""Global identity registry — one anonymous ref per person across cameras.

Each camera worker produces its own ByteTrack ids, which are local integers that
mean nothing outside that process (CAM-A track 5 and CAM-B track 5 are unrelated
people). This module is the single place where those local tracks are resolved
to a shared ``global_ref``, so a person walking from one camera's view into
another's keeps one identity.

MATCHING POLICY — three gates, in this order, because each is cheaper and more
decisive than the next:

  1. Sticky binding. A local track that already has a global ref keeps it. Track
     identity within a camera is ByteTrack's job; re-deciding it every frame
     would make refs flicker.
  2. Physics. topology.feasible() drops candidates the person could not have
     reached in the elapsed time. This runs BEFORE scoring and removes most of
     what appearance alone would wrongly accept.
  3. Appearance, with a margin. The best candidate must clear ``threshold`` AND
     beat the runner-up by ``margin``. The margin is what makes crowds safe: in
     a uniformed group several candidates score highly, and the honest answer
     there is "I don't know", not "the top one by 0.01".

Failing to match creates a NEW identity. That is the deliberate bias: an
unnecessary split (one person counted as two) is a quiet metrics error, while a
wrong merge (two people counted as one) puts a stranger's movements under
someone else's ref and is visible, misleading, and — where it drives a
restricted-zone alert — actively harmful.

PRIVACY: feature banks live here in RAM only and are dropped on TTL expiry.
The ``global_ref`` handed back is opaque and salted per session, exactly like
person_ref in identity.py. No vector is ever persisted or returned to a client.

Pure stdlib — unit-testable without torch, cv2 or a camera.
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from finblade.appearance import TrackFeatureBank
from finblade.topology import CameraTopology

Binding = Tuple[str, int]  # (camera_id, local_track_id)


@dataclass
class MatchResult:
    global_ref: str
    matched: bool                 # True = joined an existing identity
    score: float = 0.0            # similarity of the accepted candidate
    runner_up: float = 0.0        # best rejected candidate, for margin auditing
    reason: str = ""
    candidates: int = 0

    def as_dict(self) -> dict:
        return {
            "global_ref": self.global_ref,
            "matched": self.matched,
            "score": round(self.score, 4),
            "runner_up": round(self.runner_up, 4),
            "reason": self.reason,
            "candidates": self.candidates,
        }


@dataclass
class GlobalIdentity:
    global_ref: str
    bank: TrackFeatureBank
    first_seen: float
    last_seen: float
    last_camera: str
    cameras_seen: Dict[str, float] = field(default_factory=dict)
    zones_visited: List[Tuple[str, str, float]] = field(default_factory=list)
    active: Set[Binding] = field(default_factory=set)

    def note_seen(self, camera_id: str, now: float,
                  zone_id: Optional[str] = None) -> None:
        self.last_seen = now
        self.last_camera = camera_id
        self.cameras_seen[camera_id] = now
        if zone_id:
            if not self.zones_visited or self.zones_visited[-1][:2] != (camera_id, zone_id):
                self.zones_visited.append((camera_id, zone_id, now))

    def journey(self) -> List[dict]:
        return [{"camera_id": c, "zone_id": z, "ts": t}
                for c, z, t in self.zones_visited]

    def summary(self) -> dict:
        """Persistable view — deliberately contains no embedding."""
        return {
            "global_ref": self.global_ref,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "cameras_seen": sorted(self.cameras_seen),
            "camera_count": len(self.cameras_seen),
            "journey": self.journey(),
            "samples": self.bank.n,
        }


class GlobalIdentityRegistry:
    """Resolves (camera, local track) -> global_ref, and holds the live gallery."""

    def __init__(
        self,
        topology: Optional[CameraTopology] = None,
        threshold: float = 0.62,
        margin: float = 0.06,
        ttl_seconds: float = 300.0,
        max_identities: int = 2000,
        bank_capacity: int = 5,
        session_salt: Optional[str] = None,
    ):
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if margin < 0:
            raise ValueError("margin must be >= 0")
        self.topology = topology if topology is not None else CameraTopology.empty()
        self.threshold = threshold
        self.margin = margin
        self.ttl_seconds = ttl_seconds
        self.max_identities = max_identities
        self.bank_capacity = bank_capacity
        self.session_salt = session_salt or secrets.token_hex(16)

        self._identities: Dict[str, GlobalIdentity] = {}
        self._bindings: Dict[Binding, str] = {}
        self._seq = 0
        self.stats = {"created": 0, "matched": 0, "rejected_margin": 0,
                      "rejected_topology": 0, "expired": 0}

    # ---- ref minting ------------------------------------------------------
    def _mint_ref(self) -> str:
        """Opaque, unguessable, session-scoped — mirrors identity.PersonRefHasher.

        Salted so refs cannot be correlated across runs, which keeps the
        anonymity property even though identities now span cameras.
        """
        self._seq += 1
        digest = hashlib.sha256(f"{self.session_salt}:g:{self._seq}".encode("utf-8"))
        return "gp_" + digest.hexdigest()[:16]

    # ---- lifecycle --------------------------------------------------------
    def expire(self, now: float) -> List[str]:
        """Drop identities unseen within the TTL. Returns the refs removed.

        This is also the privacy control: it is what bounds how long a
        biometric template exists in memory.
        """
        stale = [ref for ref, ident in self._identities.items()
                 if (now - ident.last_seen) > self.ttl_seconds and not ident.active]
        for ref in stale:
            self._forget(ref)
        self.stats["expired"] += len(stale)
        return stale

    def _forget(self, ref: str) -> None:
        ident = self._identities.pop(ref, None)
        if ident is None:
            return
        ident.bank.vectors.clear()      # explicit: do not leave templates around
        for binding in list(self._bindings):
            if self._bindings[binding] == ref:
                del self._bindings[binding]

    def _evict_if_full(self) -> None:
        if len(self._identities) <= self.max_identities:
            return
        # Oldest-inactive-first; never evict something a camera is still using.
        evictable = [(i.last_seen, r) for r, i in self._identities.items() if not i.active]
        evictable.sort()
        for _, ref in evictable[: len(self._identities) - self.max_identities]:
            self._forget(ref)

    # ---- the main entry point --------------------------------------------
    def resolve(
        self,
        camera_id: str,
        local_track_id: int,
        bank: TrackFeatureBank,
        now: float,
        zone_id: Optional[str] = None,
    ) -> MatchResult:
        """Resolve a local track to a global ref, matching or creating as needed.

        ``bank`` should hold at least two views before calling — a single crop
        is a weak signature and binding is sticky, so an early bad decision
        cannot be undone without release().
        """
        binding = (camera_id, int(local_track_id))

        existing = self._bindings.get(binding)
        if existing is not None and existing in self._identities:
            ident = self._identities[existing]
            ident.note_seen(camera_id, now, zone_id)
            for v in bank.vectors[-1:]:          # keep the signature fresh
                ident.bank.add(v)
            return MatchResult(global_ref=existing, matched=True,
                               reason="existing_binding")

        self.expire(now)

        best_ref, best_score = None, -1.0
        runner_up = -1.0
        considered = 0
        topo_rejected = 0

        for ref, ident in self._identities.items():
            # Gate 1: one person cannot be two live tracks on the same camera.
            if any(c == camera_id and t != binding[1] for c, t in ident.active):
                continue
            # Gate 2: physics.
            ok, _reason = self.topology.feasible(
                ident.last_camera, camera_id, now - ident.last_seen)
            if not ok:
                topo_rejected += 1
                continue
            # Gate 3: appearance.
            considered += 1
            score = bank.similarity(ident.bank)
            if score > best_score:
                runner_up = best_score
                best_ref, best_score = ref, score
            elif score > runner_up:
                runner_up = score

        self.stats["rejected_topology"] += topo_rejected
        runner_up = max(runner_up, 0.0)
        best_score = max(best_score, 0.0)

        if best_ref is not None and best_score >= self.threshold:
            if (best_score - runner_up) >= self.margin or considered == 1:
                ident = self._identities[best_ref]
                for v in bank.vectors:
                    ident.bank.add(v)
                ident.note_seen(camera_id, now, zone_id)
                ident.active.add(binding)
                self._bindings[binding] = best_ref
                self.stats["matched"] += 1
                return MatchResult(global_ref=best_ref, matched=True,
                                   score=best_score, runner_up=runner_up,
                                   reason="appearance_match", candidates=considered)
            # Ambiguous: several plausible people. Splitting is the safe error.
            self.stats["rejected_margin"] += 1
            reason = "ambiguous_margin"
        else:
            reason = "below_threshold" if considered else "no_candidates"

        ref = self._mint_ref()
        new_bank = TrackFeatureBank(capacity=self.bank_capacity)
        for v in bank.vectors:
            new_bank.add(v)
        ident = GlobalIdentity(global_ref=ref, bank=new_bank, first_seen=now,
                               last_seen=now, last_camera=camera_id)
        ident.note_seen(camera_id, now, zone_id)
        ident.active.add(binding)
        self._identities[ref] = ident
        self._bindings[binding] = ref
        self.stats["created"] += 1
        self._evict_if_full()
        return MatchResult(global_ref=ref, matched=False, score=best_score,
                           runner_up=runner_up, reason=reason, candidates=considered)

    def release(self, camera_id: str, local_track_id: int) -> Optional[str]:
        """Local track ended. Keep the identity warm so another camera can match it."""
        binding = (camera_id, int(local_track_id))
        ref = self._bindings.pop(binding, None)
        if ref and ref in self._identities:
            self._identities[ref].active.discard(binding)
        return ref

    # ---- corrections ------------------------------------------------------
    def merge(self, keep_ref: str, drop_ref: str) -> bool:
        """Fold ``drop_ref`` into ``keep_ref`` (an operator or offline correction)."""
        if keep_ref == drop_ref:
            return False
        keep = self._identities.get(keep_ref)
        drop = self._identities.get(drop_ref)
        if keep is None or drop is None:
            return False
        for v in drop.bank.vectors:
            keep.bank.add(v)
        keep.first_seen = min(keep.first_seen, drop.first_seen)
        if drop.last_seen > keep.last_seen:
            keep.last_seen, keep.last_camera = drop.last_seen, drop.last_camera
        for cam, ts in drop.cameras_seen.items():
            keep.cameras_seen[cam] = max(keep.cameras_seen.get(cam, 0.0), ts)
        keep.zones_visited = sorted(keep.zones_visited + drop.zones_visited,
                                    key=lambda z: z[2])
        for binding in list(drop.active):
            keep.active.add(binding)
            self._bindings[binding] = keep_ref
        self._identities.pop(drop_ref, None)
        return True

    # ---- queries ----------------------------------------------------------
    def get(self, ref: str) -> Optional[GlobalIdentity]:
        return self._identities.get(ref)

    def ref_for(self, camera_id: str, local_track_id: int) -> Optional[str]:
        return self._bindings.get((camera_id, int(local_track_id)))

    def all_refs(self) -> List[str]:
        return list(self._identities)

    def active_refs(self) -> Set[str]:
        return {ref for ref in self._bindings.values()}

    def site_occupancy(self) -> int:
        """Distinct people on site right now.

        The point of cross-camera identity for counting: someone standing in the
        overlap of two cameras is two local tracks but one person, and summing
        per-camera occupancy would count them twice.
        """
        return len(self.active_refs())

    def cross_camera_refs(self) -> List[str]:
        """Identities that have genuinely been seen by more than one camera."""
        return [r for r, i in self._identities.items() if len(i.cameras_seen) > 1]

    def snapshot(self) -> dict:
        return {
            "identities": len(self._identities),
            "active_bindings": len(self._bindings),
            "site_occupancy": self.site_occupancy(),
            "cross_camera": len(self.cross_camera_refs()),
            "stats": dict(self.stats),
        }

    def __len__(self) -> int:
        return len(self._identities)
