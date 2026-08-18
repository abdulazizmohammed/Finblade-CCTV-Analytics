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
    # Diagnosis. Present on the object, absent from as_dict() — the HTTP reply
    # goes back to a camera worker on every resolve and does not need the
    # candidate list. The journal does.
    scored: List[dict] = field(default_factory=list)
    rejected_topology: int = 0
    rejected_simultaneous: int = 0
    unknown_pairs: int = 0
    gallery: int = 0

    def as_dict(self) -> dict:
        """The wire format. Deliberately unchanged — workers parse this."""
        return {
            "global_ref": self.global_ref,
            "matched": self.matched,
            "score": round(self.score, 4),
            "runner_up": round(self.runner_up, 4),
            "reason": self.reason,
            "candidates": self.candidates,
        }

    def journal_entry(self, camera_id: str, local_track_id: int, ts: float,
                      zone_id=None, bank_size: int = 0) -> dict:
        """One replayable record of this decision.

        Everything a later question needs — "would 0.65 have matched?", "how
        often did physics leave exactly one candidate?" — and nothing that
        could reconstruct a person: no vectors, no crops, no boxes, and no
        person_ref. global_ref is the same opaque salted hash already stored.
        """
        return {
            "ts": round(ts, 3),
            "camera": camera_id,
            "track": int(local_track_id),
            "zone": zone_id,
            "bank": bank_size,
            "gallery": self.gallery,
            "candidates": self.candidates,
            "rejected_topology": self.rejected_topology,
            "rejected_simultaneous": self.rejected_simultaneous,
            "unknown_pairs": self.unknown_pairs,
            "best": round(self.score, 4),
            "runner_up": round(self.runner_up, 4),
            "decision": self.reason,
            "matched": self.matched,
            "global_ref": self.global_ref,
            "scored": self.scored,
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
        # Measured on media/1903279 via scripts/eval_cross_camera.py (see
        # evidence/cross_camera_eval_dense.json): true-pair similarities ran
        # min 0.80 / median 0.90, false pairs median 0.61 / max 0.83. The two
        # distributions OVERLAP, so no threshold separates them cleanly — the
        # runner-up margin and the topology gate do the real work, and this
        # value is only a floor. 0.62 sat at the false-pair median, which is
        # poor hygiene; 0.70 clears most of them without cutting into observed
        # true pairs. NOT final: that eval's second camera is a transformed
        # copy, so real cameras will push true-pair scores DOWN. Retune from
        # genuine two-camera footage before deployment.
        threshold: float = 0.70,
        margin: float = 0.06,
        ttl_seconds: float = 300.0,
        # Multiplier on the slowest journey that could start where somebody was
        # last seen. A surveyed maximum is the longest walk anyone PACED, not a
        # ceiling on reality: a lift can be slower on the day. 1.5 keeps the
        # record alive for a journey half again as slow as the survey.
        transit_grace: float = 1.5,
        # HARD CEILING on how long any template may be held, whatever the
        # topology says. Retention is derived from transit windows now, and a
        # mistyped max_seconds in a YAML file must not be able to quietly turn
        # a five-minute privacy bound into an all-day one. ttl_seconds is the
        # floor, this is the ceiling, and the topology moves within them.
        max_retention_seconds: float = 1800.0,
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
        self.transit_grace = max(1.0, float(transit_grace))
        self.max_retention_seconds = max(float(ttl_seconds),
                                         float(max_retention_seconds))
        self.max_identities = max_identities
        self.bank_capacity = bank_capacity
        self.session_salt = session_salt or secrets.token_hex(16)

        self._identities: Dict[str, GlobalIdentity] = {}
        self._bindings: Dict[Binding, str] = {}
        # (camera_id, global_ref) pairs EVER seen. Deliberately survives TTL
        # expiry and gallery eviction: it is the cumulative footfall tally, and
        # a person who left an hour ago still counts as one unique person seen.
        # Zone-independent by construction — counting people needs identity, not
        # geometry, so this works with no zone polygons defined at all.
        # Memory is bounded by (unique people x cameras) short strings, and the
        # refs are the same anonymous salted hashes stored nowhere else.
        self._seen_pairs: Set[Tuple[str, str]] = set()
        # Refs PROVEN to be different people, because they were tracked at the
        # same moment on the same camera. One person cannot be in two places at
        # once, so this is hard physical evidence — and unlike appearance it
        # still holds when two people are dressed identically. Consolidation
        # consults it before folding two records together.
        self._exclusive: Dict[str, Set[str]] = {}
        self._seq = 0
        self.stats = {"created": 0, "matched": 0, "rejected_margin": 0,
                      "rejected_topology": 0, "expired": 0,
                      # Candidates that were live on another, non-overlapping
                      # camera at that moment. One body, two places: refused
                      # regardless of how well the appearance scored.
                      "rejected_simultaneous": 0,
                      # Candidates that passed the physics gate but scored
                      # under `threshold`, plus the highest such score seen.
                      # Together they say whether the threshold is set right
                      # for THIS site's cameras and angles.
                      "below_threshold": 0, "best_rejected_score": 0.0,
                      # Ambiguities resolved by folding two gallery records of
                      # the same person together instead of splitting again.
                      "consolidated": 0,
                      # Candidates evaluated for a camera pair that appears in
                      # no topology entry. Non-zero means the topology file does
                      # not cover the cameras actually running, so those pairs
                      # are falling back to the permissive default. Surfaced in
                      # /api/v1/identity/stats so incomplete config is visible
                      # rather than silently degrading match quality.
                      "unknown_pair": 0}

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
    def retention_for(self, camera_id: str) -> float:
        """How long to keep someone last seen on ``camera_id``.

        THE TTL ALONE WAS TOO SHORT, and in the one case that matters most.
        The longest surveyed transit on this site is 270s (ground floor to the
        second by lift); the TTL was a flat 300s. Thirty seconds of headroom for
        a journey whose duration is dominated by how long a lift takes to
        arrive. Beyond that the record was deleted, so the person could not be
        matched on arrival — not rejected, not scored, simply absent — and they
        surfaced on the far camera as a brand new visitor.

        Retention is therefore the TTL or the slowest journey that could start
        where they were last seen, whichever is longer, plus a grace margin.
        Someone standing at a lift door is kept longer than someone last seen
        mid-corridor, because for them a long silence is expected rather than
        evidence they have gone.

        Bounded on BOTH sides. ttl_seconds is the floor and
        max_retention_seconds the ceiling, so a mistyped transit window cannot
        silently stretch how long a biometric template survives in memory.
        """
        return min(self.max_retention_seconds,
                   max(self.ttl_seconds,
                       self.topology.longest_transit_from(camera_id)
                       * self.transit_grace))

    def in_transit(self, now: float) -> List[str]:
        """Refs currently UNOBSERVED: held, but on no camera right now.

        Someone in a lift, on a stairwell, or in a corridor no camera watches.
        They are neither active nor forgotten, and without a name for that
        state it is indistinguishable from an idle record about to be dropped.
        """
        return sorted(ref for ref, i in self._identities.items()
                      if not i.active and (now - i.last_seen) <= self.retention_for(i.last_camera))

    def state_of(self, ref: str, now: float) -> str:
        """TRACKED (on a camera now) / UNOBSERVED (held, between cameras) /
        UNKNOWN (never seen, or already released)."""
        ident = self._identities.get(ref)
        if ident is None:
            return "UNKNOWN"
        if ident.active:
            return "TRACKED"
        if (now - ident.last_seen) <= self.retention_for(ident.last_camera):
            return "UNOBSERVED"
        return "UNKNOWN"

    def expire(self, now: float) -> List[str]:
        """Drop identities held longer than their retention. Returns refs removed.

        This is also the privacy control: it is what bounds how long a
        biometric template exists in memory. Retention is now per-identity
        rather than one constant — see retention_for() — so this stays a bound,
        just a differently-shaped one.
        """
        stale = [ref for ref, ident in self._identities.items()
                 if (now - ident.last_seen) > self.retention_for(ident.last_camera)
                 and not ident.active]
        for ref in stale:
            self._forget(ref)
        self.stats["expired"] += len(stale)
        return stale

    def _forget(self, ref: str) -> None:
        ident = self._identities.pop(ref, None)
        if ident is None:
            return
        ident.bank.clear()              # explicit: do not leave templates around
        for binding in list(self._bindings):
            if self._bindings[binding] == ref:
                del self._bindings[binding]

    def _note_co_present(self, ref: str, camera_id: str) -> None:
        """Record that ``ref`` is distinct from everyone else live on this camera."""
        for (cam, _tid), other in self._bindings.items():
            if cam != camera_id or other == ref:
                continue
            self._exclusive.setdefault(ref, set()).add(other)
            self._exclusive.setdefault(other, set()).add(ref)

    def _are_exclusive(self, a: str, b: str) -> bool:
        return b in self._exclusive.get(a, ())

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
            self._seen_pairs.add((camera_id, existing))
            ident.note_seen(camera_id, now, zone_id)
            for v in bank.vectors[-1:]:          # keep the signature fresh
                # Stamped with the camera, so this steady drip from whichever
                # camera holds the binding cannot crowd every other viewpoint
                # out of the bank. See TrackFeatureBank._evict.
                ident.bank.add(v, source=camera_id)
            return MatchResult(global_ref=existing, matched=True,
                               reason="existing_binding")

        self.expire(now)

        gallery_size = len(self._identities)
        best_ref, best_score = None, -1.0
        runner_up_ref, runner_up = None, -1.0
        considered = 0
        topo_rejected = 0
        unknown_pairs = 0
        simultaneous = 0
        # Every candidate that reached scoring, with what physics said about
        # it. This is what makes the decision replayable offline: raising a
        # threshold changes WHICH candidates cross it, so knowing only the
        # winner's score cannot tell you what else would have crossed too.
        # Scores and elapsed times only - no vectors leave this loop.
        scored: List[dict] = []

        for ref, ident in self._identities.items():
            # Gate 1: one person cannot be two live tracks on the same camera.
            if any(c == camera_id and t != binding[1] for c, t in ident.active):
                continue
            # Gate 1b: nor two live tracks on cameras that do not overlap.
            #
            # This was previously only implicit, and the implication does not
            # always hold. A candidate visible RIGHT NOW on another camera has
            # dt near zero, which the physics gate rejects as "too_fast" —
            # but only while that pair has a non-zero minimum. With
            # allow_unknown_pairs the fallback minimum is ZERO, so any camera
            # missing from the topology (one added from the UI, say) silently
            # loses the exclusion, and a person plainly standing in front of
            # CAM-01 could be handed to a lookalike on the new camera.
            #
            # Being ACTIVELY BOUND elsewhere is stronger evidence than a stale
            # last_seen, and it does not depend on the survey being complete:
            # if the two views share no floor area, the same body cannot be in
            # both, whatever the crops score.
            if any(c != camera_id and not self.topology.is_overlapping(c, camera_id)
                   for c, _t in ident.active):
                self.stats["rejected_simultaneous"] += 1
                simultaneous += 1
                continue
            # Gate 2: physics.
            ok, reason = self.topology.feasible(
                ident.last_camera, camera_id, now - ident.last_seen)
            if reason == "unknown_pair":
                unknown_pairs += 1
            if not ok:
                topo_rejected += 1
                continue
            # Gate 3: appearance.
            considered += 1
            score = bank.similarity(ident.bank)
            scored.append({"ref": ref, "score": round(score, 4),
                           "dt": round(now - ident.last_seen, 2),
                           "from": ident.last_camera, "gate": reason})
            if score > best_score:
                runner_up_ref, runner_up = best_ref, best_score
                best_ref, best_score = ref, score
            elif score > runner_up:
                runner_up_ref, runner_up = ref, score

        self.stats["rejected_topology"] += topo_rejected
        scored.sort(key=lambda c: -c["score"])
        self.stats["unknown_pair"] += unknown_pairs
        runner_up = max(runner_up, 0.0)
        best_score = max(best_score, 0.0)

        def _bind(ref: str, reason: str) -> MatchResult:
            ident = self._identities[ref]
            for v in bank.vectors:
                ident.bank.add(v, source=camera_id)
            ident.note_seen(camera_id, now, zone_id)
            ident.active.add(binding)
            self._note_co_present(ref, camera_id)
            self._bindings[binding] = ref
            self._seen_pairs.add((camera_id, ref))
            self.stats["matched"] += 1
            return MatchResult(global_ref=ref, matched=True, score=best_score,
                               runner_up=runner_up, reason=reason,
                               candidates=considered, scored=scored,
                               rejected_topology=topo_rejected,
                               rejected_simultaneous=simultaneous,
                               unknown_pairs=unknown_pairs,
                               gallery=gallery_size)

        if best_ref is not None and best_score >= self.threshold:
            if (best_score - runner_up) >= self.margin or considered == 1:
                return _bind(best_ref, "appearance_match")

            # Ambiguous — but "ambiguous" has two opposite meanings, and
            # treating them alike is what made unique counts run away:
            #
            #   two DIFFERENT people scoring alike  -> splitting is correct
            #   two records of the SAME person      -> merging is correct
            #
            # The second case is self-inflicted: every split leaves another
            # near-identical copy of one person in the gallery, which makes the
            # next match more ambiguous, which causes another split. Observed
            # live as rejected_margin=95 against created=100 for a clip
            # containing about five people.
            #
            # Distinguishing them is cheap: ask whether the top two candidates
            # look like each other. If they do, they are one person already
            # split, so consolidate them and accept rather than splitting again.
            # ...unless they were tracked simultaneously on one camera, which
            # proves they are two people however alike they look. Appearance
            # alone cannot make that call: two staff in the same uniform score
            # just as high as two records of one person, and merging them would
            # put a stranger's movements under someone else's ref.
            if (runner_up_ref is not None and runner_up_ref in self._identities
                    and not self._are_exclusive(best_ref, runner_up_ref)):
                twin = self._identities[best_ref].bank.similarity(
                    self._identities[runner_up_ref].bank)
                if twin >= self.threshold:
                    self.merge(best_ref, runner_up_ref)
                    self.stats["consolidated"] += 1
                    return _bind(best_ref, "consolidated_duplicate")

            # Genuinely different people. Splitting stays the safe error.
            self.stats["rejected_margin"] += 1
            reason = "ambiguous_margin"
        else:
            reason = "below_threshold" if considered else "no_candidates"
            if considered:
                # Counted, and the best score kept, because this is the number
                # that tells you whether the threshold is wrong for this site.
                # A high near-miss rate with best scores clustered just under
                # the threshold means genuine matches are being refused — the
                # cross-angle case, where two cameras see opposite sides of one
                # person — and the fix is to lower it, not to distrust ReID.
                self.stats["below_threshold"] += 1
                prev = self.stats.get("best_rejected_score", 0.0)
                self.stats["best_rejected_score"] = round(
                    max(prev, best_score), 4)

        ref = self._mint_ref()
        new_bank = TrackFeatureBank(capacity=self.bank_capacity)
        for v in bank.vectors:
            new_bank.add(v, source=camera_id)
        ident = GlobalIdentity(global_ref=ref, bank=new_bank, first_seen=now,
                               last_seen=now, last_camera=camera_id)
        ident.note_seen(camera_id, now, zone_id)
        ident.active.add(binding)
        self._identities[ref] = ident
        # Anyone else live on this camera right now is definitively a different
        # person from this one.
        self._note_co_present(ref, camera_id)
        self._bindings[binding] = ref
        self._seen_pairs.add((camera_id, ref))
        self.stats["created"] += 1
        self._evict_if_full()
        return MatchResult(global_ref=ref, matched=False, score=best_score,
                           runner_up=runner_up, reason=reason,
                           candidates=considered, scored=scored,
                           rejected_topology=topo_rejected,
                           rejected_simultaneous=simultaneous,
                           unknown_pairs=unknown_pairs,
                           gallery=gallery_size)

    def release(self, camera_id: str, local_track_id: int) -> Optional[str]:
        """Local track ended. Keep the identity warm so another camera can match it."""
        binding = (camera_id, int(local_track_id))
        ref = self._bindings.pop(binding, None)
        if ref and ref in self._identities:
            self._identities[ref].active.discard(binding)
        return ref

    def release_camera(self, camera_id: str) -> int:
        """Release EVERY binding held by one camera. Returns how many.

        Call this when a camera goes offline. A worker releases its tracks one
        by one as they leave the scene, but a worker that dies — crash, kill,
        network loss — releases nothing. Those bindings then pin their
        identities permanently: expire() skips anything still bound, so the
        identity never times out and keeps counting toward site occupancy
        forever. Observed live as a site total of 6 people while the only two
        running cameras reported 1 each, the other 4 coming from three dead
        camera processes.

        The identities themselves are kept — someone who walked out of a camera
        that then died may still reappear elsewhere, and the normal TTL can now
        reclaim them.
        """
        doomed = [b for b in self._bindings if b[0] == camera_id]
        for binding in doomed:
            ref = self._bindings.pop(binding, None)
            if ref and ref in self._identities:
                self._identities[ref].active.discard(binding)
        return len(doomed)

    # ---- corrections ------------------------------------------------------
    def merge(self, keep_ref: str, drop_ref: str) -> bool:
        """Fold ``drop_ref`` into ``keep_ref`` (an operator or offline correction)."""
        if keep_ref == drop_ref:
            return False
        keep = self._identities.get(keep_ref)
        drop = self._identities.get(drop_ref)
        if keep is None or drop is None:
            return False
        # Carry the source labels across, or a merge would erase the viewpoint
        # diversity the two records had between them — which is usually the
        # most useful thing about a merged pair.
        drop_sources = drop.bank.sources or [None] * len(drop.bank.vectors)
        for v, src in zip(drop.bank.vectors, drop_sources):
            keep.bank.add(v, source=src)
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
        # Two refs turned out to be one person, so the cumulative tally must
        # drop by one too — otherwise a corrected merge leaves footfall inflated.
        self._seen_pairs = {(cam, keep_ref if ref == drop_ref else ref)
                            for cam, ref in self._seen_pairs}
        # Exclusions transfer: anyone proven distinct from the dropped ref is
        # proven distinct from the surviving one.
        for other in self._exclusive.pop(drop_ref, set()):
            peers = self._exclusive.get(other)
            if peers is not None:
                peers.discard(drop_ref)
                if other != keep_ref:
                    peers.add(keep_ref)
            if other != keep_ref:
                self._exclusive.setdefault(keep_ref, set()).add(other)
        self._exclusive.get(keep_ref, set()).discard(keep_ref)
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
        """Distinct people on site RIGHT NOW.

        The point of cross-camera identity for counting: someone standing in the
        overlap of two cameras is two local tracks but one person, and summing
        per-camera occupancy would count them twice.
        """
        return len(self.active_refs())

    # ---- cumulative unique counts (no zones required) ---------------------
    def unique_total(self) -> int:
        """Distinct people seen since startup, across all cameras.

        This is the footfall number, and it needs no zone polygons: it counts
        identities, not positions. Someone seen on three cameras counts once.
        Survives TTL expiry — a person who has left still happened.
        """
        return len({ref for _, ref in self._seen_pairs})

    def unique_by_camera(self) -> Dict[str, int]:
        """Distinct people seen by each camera since startup.

        Note these will not sum to unique_total whenever a person was seen by
        more than one camera — that is the whole point, and the difference is
        exactly the double-counting a per-camera tally would have produced.
        """
        counts: Dict[str, int] = {}
        for cam, _ref in self._seen_pairs:
            counts[cam] = counts.get(cam, 0) + 1
        return counts

    def live_by_camera(self) -> Dict[str, int]:
        """Distinct people currently visible to each camera.

        Counts distinct refs, not bindings: two local tracks on one camera that
        resolved to the same person count once.
        """
        pairs = {(cam, ref) for (cam, _tid), ref in self._bindings.items()}
        counts: Dict[str, int] = {}
        for cam, _ref in pairs:
            counts[cam] = counts.get(cam, 0) + 1
        return counts

    def cross_camera_refs(self) -> List[str]:
        """LIVE identities seen by more than one camera (gallery-scoped).

        Only counts identities still in the gallery, so it drops on TTL expiry.
        Use cross_camera_total() for a figure comparable with unique_total().
        """
        return [r for r, i in self._identities.items() if len(i.cameras_seen) > 1]

    def cross_camera_total(self) -> int:
        """Cumulative count of people ever seen by more than one camera.

        Must be derived from _seen_pairs, not the gallery: mixing a live count
        with cumulative ones makes the numbers fail to reconcile
        (unique_by_camera summed, minus unique_total, should equal this).
        """
        cams: Dict[str, Set[str]] = {}
        for cam, ref in self._seen_pairs:
            cams.setdefault(ref, set()).add(cam)
        return sum(1 for c in cams.values() if len(c) > 1)

    def snapshot(self) -> dict:
        return {
            "identities": len(self._identities),
            "active_bindings": len(self._bindings),
            "site_occupancy": self.site_occupancy(),
            "cross_camera": len(self.cross_camera_refs()),
            # Zone-independent people counting. These are the numbers to use
            # when no zone polygons are defined.
            "unique_total": self.unique_total(),
            "unique_by_camera": self.unique_by_camera(),
            "live_by_camera": self.live_by_camera(),
            "stats": dict(self.stats),
        }

    def __len__(self) -> int:
        return len(self._identities)
