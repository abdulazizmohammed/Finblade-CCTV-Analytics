"""PPE compliance state, per (camera, track, PPE type).

WHAT THIS EXISTS TO PREVENT. A detector that misses a hardhat for one frame is
not evidence that somebody took their hat off. Occlusion, motion blur, a turned
head and a bad crop all produce the same missing detection as genuine
non-compliance, and treating them alike would produce an alert stream nobody
believes — which is worse than no alert at all, because it also trains people to
ignore the real ones.

So evidence accumulates over time and a verdict needs sustaining, in the same
shape as every other rule in this system.

POSITIVE AND NEGATIVE EVIDENCE ARE NOT EQUALLY STRONG, and the spec is right to
insist on it. The checkpoint emits both `Hardhat` and `NO-Hardhat`:

    NO-Hardhat present   the model looked at that head and asserted no hat.
                         Direct evidence, and it is what a violation should
                         normally rest on.
    Hardhat absent       could mean no hat, or could mean the model did not
                         see one this frame. Weak, and it is the reading a
                         missed detection produces.

Absence alone can still convict, because a model that has stopped emitting
NO-Hardhat for an unhatted worker is a real failure mode — but it takes longer,
which is what ABSENCE_WEIGHT expresses. An explicit NO- reaches the verdict at
full speed; silence takes several times as long.

Pure stdlib. No numpy, no torch, no cv2 — the whole point is that this is
provable without a model.
"""

from typing import Dict, Iterable, List, Optional, Tuple

# --- PPE types -------------------------------------------------------------
HARDHAT = "hardhat"
VEST = "safety_vest"
MASK = "mask"
PPE_TYPES = (HARDHAT, VEST, MASK)

# --- states ----------------------------------------------------------------
UNKNOWN = "UNKNOWN"
COMPLIANT_CANDIDATE = "COMPLIANT_CANDIDATE"
COMPLIANT = "COMPLIANT"
NONCOMPLIANT_CANDIDATE = "NONCOMPLIANT_CANDIDATE"
NONCOMPLIANT = "NONCOMPLIANT"
# Distinct from UNKNOWN, and the distinction is the point. UNKNOWN means "not
# decided yet"; this means "cannot be decided, and pretending otherwise would
# be a claim we cannot support". A worker 40px tall produces no PPE detections
# whatever they are wearing, so feeding that silence to the state machine would
# convict them on the detector's blindness rather than on their behaviour.
NOT_ASSESSABLE = "NOT_ASSESSABLE"

# --- evidence --------------------------------------------------------------
EV_POSITIVE = "positive"     # e.g. Hardhat detected on this person
EV_NEGATIVE = "negative"     # e.g. NO-Hardhat detected on this person
EV_ABSENT = "absent"         # neither seen this tick


class PPEThresholds:
    """Everything tunable, in one place and out of the rule body.

    ALL OF THESE ARE GUESSES. No PPE footage has been measured on this system.
    They are starting values chosen to be conservative — slow to accuse, quick
    to forgive — and must be retuned against real CCTV before an R-11 alert is
    treated as calibrated. See DECISIONS.md D-32.
    """

    def __init__(self,
                 entry_grace_s: float = 5.0,
                 violation_confirm_s: float = 8.0,
                 recovery_confirm_s: float = 5.0,
                 min_confidence: float = 0.40,
                 absence_weight: float = 0.25,
                 min_person_height_px: float = 120.0):
        # Time after entering a compliance zone before anything is judged. A
        # worker walking in while still pulling their hat on is not a violation,
        # and the first seconds inside a zone are also where the detector has
        # the worst view of them.
        self.entry_grace_s = float(entry_grace_s)
        # Sustained evidence needed to move CANDIDATE -> NONCOMPLIANT.
        self.violation_confirm_s = float(violation_confirm_s)
        # Sustained good evidence needed to clear. Shorter than the violation
        # timer on purpose: being slow to accuse is caution, being slow to
        # forgive is just an alert that outlives its cause.
        self.recovery_confirm_s = float(recovery_confirm_s)
        # Detections below this are ignored entirely.
        self.min_confidence = float(min_confidence)
        # How much a silent tick counts toward a violation, relative to an
        # explicit NO- detection. 0.25 means absence takes 4x as long to
        # convict. 0.0 would mean only explicit NO- can ever convict.
        self.absence_weight = float(absence_weight)
        # Below this person-box height in pixels, PPE is NOT JUDGED AT ALL.
        #
        # The most important guard in the module. A distant worker yields no
        # hardhat detection whether or not they are wearing one, and absence is
        # evidence in this rule — so without a floor the system convicts people
        # for standing far from the camera. Observed directly on
        # media/PPEVideo.mp4 before this existed: tracks with no PPE detections
        # of any kind drifting to NONCOMPLIANT on silence alone.
        #
        # 120px is a GUESS, of the same order as ReID's min_crop_height (96px)
        # and for the same reason: below some size there is not enough detail
        # for the model to hold an opinion worth recording. Measure it on real
        # CCTV before trusting it.
        self.min_person_height_px = float(min_person_height_px)


class PPEState:
    """One (track, ppe_type) verdict and the evidence behind it."""

    __slots__ = ("state", "since", "credit", "last_evidence", "last_conf",
                 "first_seen", "confirmed_at", "pos_ticks", "neg_ticks",
                 "abs_ticks")

    def __init__(self, now: float):
        self.state = UNKNOWN
        self.since = now
        # Seconds-equivalent of accumulated evidence toward the pending verdict.
        # Signed: positive counts toward compliant, negative toward violation.
        self.credit = 0.0
        self.last_evidence = EV_ABSENT
        self.last_conf = 0.0
        self.first_seen: Optional[float] = None      # when the violation began
        self.confirmed_at: Optional[float] = None    # when it was confirmed
        self.pos_ticks = 0
        self.neg_ticks = 0
        self.abs_ticks = 0

    def summary(self) -> dict:
        """Evidence summary for the alert. Counts, not vectors or images."""
        return {"state": self.state, "positive": self.pos_ticks,
                "negative": self.neg_ticks, "absent": self.abs_ticks,
                "last_evidence": self.last_evidence,
                "last_confidence": round(self.last_conf, 4)}


class PPETracker:
    """Per-camera PPE state for every track, keyed (track_id, ppe_type).

    Deliberately keyed on the LOCAL track id. PPE state follows the existing
    tracker lifecycle; it does not build a second identity system, and it does
    not attempt cross-track ReID. When ByteTrack loses and re-acquires someone
    they are a new track with fresh state, which is the same bargain every
    other per-track feature in this system makes.
    """

    def __init__(self, camera_id: str, thresholds: Optional[PPEThresholds] = None):
        self.camera_id = camera_id
        self.t = thresholds or PPEThresholds()
        self._states: Dict[Tuple[object, str], PPEState] = {}
        # When each track entered its current compliance zone, for the grace
        # period. Keyed (track, zone) so moving between zones restarts it —
        # a different zone is a different set of requirements and the worker
        # deserves the same grace on entering it.
        self._entered: Dict[Tuple[object, str], float] = {}

    # ---- lifecycle --------------------------------------------------------
    def note_in_zone(self, track_id: object, zone_id: str, now: float) -> None:
        key = (track_id, zone_id)
        if key not in self._entered:
            self._entered[key] = now

    def left_zone(self, track_id: object, zone_id: str) -> None:
        self._entered.pop((track_id, zone_id), None)

    def in_grace(self, track_id: object, zone_id: str, now: float) -> bool:
        entered = self._entered.get((track_id, zone_id))
        if entered is None:
            return True                       # not known to be in the zone yet
        return (now - entered) < self.t.entry_grace_s

    def drop_track(self, track_id: object) -> int:
        """Forget a departed track. Returns how many states were removed.

        Called from the tracker's own reap path so PPE state cannot outlive the
        track it describes — otherwise the dicts grow without bound on a busy
        site and a recycled track id would inherit a stranger's verdict.
        """
        doomed = [k for k in self._states if k[0] == track_id]
        for k in doomed:
            del self._states[k]
        for k in [k for k in self._entered if k[0] == track_id]:
            del self._entered[k]
        return len(doomed)

    def assessable(self, person_bbox) -> bool:
        """Is this person big enough in frame for a PPE verdict to mean anything?

        Called BEFORE any evidence is folded in. A track that fails this is not
        fed at all — not fed "absent", which would drift it toward a violation.
        The distinction between "no hat" and "too far away to tell" has to be
        made here, because once silence reaches the state machine it is
        indistinguishable from a bare head.
        """
        if not person_bbox or len(person_bbox) < 4:
            return False
        return (float(person_bbox[3]) - float(person_bbox[1])) >= \
            self.t.min_person_height_px

    def state_of(self, track_id: object, ppe_type: str) -> Optional[PPEState]:
        return self._states.get((track_id, ppe_type))

    def zone_summary(self, track_zone_ppe) -> dict:
        """Per-zone people counts for the UI.

        ``track_zone_ppe`` is [(track_id, zone_id, assessable_bool), ...] for
        everyone currently in a PPE zone.

        COUNTS ARE PEOPLE, NOT VIOLATIONS, and the two differ. One worker
        missing both hardhat and vest is ONE non-compliant person and TWO
        entries in `violations`. A UI that sums `violations` to get a headcount
        will over-report, which is precisely the mistake the alert feed already
        invites — hence the invariant asserted in the tests:

            compliant + non_compliant + not_assessable == occupancy

        `not_assessable` is reported rather than folded into either bucket. A
        count that quietly buries "we could not tell" inside "compliant" or
        "non-compliant" is a worse lie than admitting the gap.
        """
        out = {}
        for track_id, zone_id, ok in track_zone_ppe or ():
            z = out.setdefault(zone_id, {"compliant": 0, "non_compliant": 0,
                                         "not_assessable": 0, "violations": {}})
            if not ok:
                z["not_assessable"] += 1
                continue
            bad = [ppe for (tid, ppe), st in self._states.items()
                   if tid == track_id and st.state == NONCOMPLIANT]
            if bad:
                z["non_compliant"] += 1
                for ppe in bad:
                    z["violations"][ppe] = z["violations"].get(ppe, 0) + 1
            else:
                z["compliant"] += 1
        return out

    def retain_types(self, keep) -> int:
        """Forget every verdict for a PPE type nobody requires any more.

        Returns how many were dropped.

        WHY THIS IS NEEDED AT ALL. A verdict is held per (track, ppe_type) and
        outlives any single frame — that persistence is the point, it is what
        stops a momentary occlusion clearing a violation. But it means a zone's
        requirement changing mid-shift leaves verdicts behind for items that are
        no longer asked for, and zone_summary above scans EVERY state held for a
        track rather than only the ones still required. Without this, unticking
        "safety vest" stops the alerts but leaves the zone card reporting that
        person non-compliant with a vest violation until they leave frame — the
        card and the alert feed disagreeing about the same instant.

        Deliberately keyed on the TYPE, not the zone: a track moves between
        zones, so "still required somewhere on this camera" is the only question
        that can be answered correctly from here.
        """
        keep = set(keep or ())
        gone = [k for k in self._states if k[1] not in keep]
        for k in gone:
            del self._states[k]
        return len(gone)

    def verdict(self, track_id: object, ppe_type: str) -> str:
        st = self._states.get((track_id, ppe_type))
        return st.state if st else UNKNOWN

    def tracked(self) -> int:
        return len(self._states)

    # ---- the state machine ------------------------------------------------
    def observe(self, track_id: object, ppe_type: str, evidence: str,
                confidence: float, now: float, dt: float) -> Optional[str]:
        """Fold one tick of evidence in. Returns the NEW state on a transition.

        ``dt`` is the seconds since this track was last observed, so the
        machine measures real elapsed time rather than counting frames — a
        detector running at 2 Hz and one at 15 Hz must reach the same verdict
        at the same wall-clock moment.
        """
        key = (track_id, ppe_type)
        st = self._states.get(key)
        if st is None:
            st = self._states[key] = PPEState(now)

        if evidence == EV_POSITIVE and confidence < self.t.min_confidence:
            evidence = EV_ABSENT          # too weak to count as seeing it
        if evidence == EV_NEGATIVE and confidence < self.t.min_confidence:
            evidence = EV_ABSENT

        st.last_evidence = evidence
        st.last_conf = float(confidence or 0.0)
        if evidence == EV_POSITIVE:
            st.pos_ticks += 1
            st.credit += dt
        elif evidence == EV_NEGATIVE:
            st.neg_ticks += 1
            st.credit -= dt
        else:
            st.abs_ticks += 1
            # Absence is weaker evidence of non-compliance than an explicit
            # NO- detection, and this is where that asymmetry lives.
            st.credit -= dt * self.t.absence_weight

        # Clamp so a long stretch of one verdict cannot bank credit that makes
        # the opposite verdict take minutes to reach afterwards.
        lo = -(self.t.violation_confirm_s * 1.5)
        hi = self.t.recovery_confirm_s * 1.5
        st.credit = max(lo, min(hi, st.credit))

        return self._transition(st, now)

    def _transition(self, st: PPEState, now: float) -> Optional[str]:
        prev = st.state
        if st.credit <= -self.t.violation_confirm_s:
            new = NONCOMPLIANT
        elif st.credit >= self.t.recovery_confirm_s:
            new = COMPLIANT
        elif st.credit < 0:
            new = NONCOMPLIANT_CANDIDATE
        elif st.credit > 0:
            new = COMPLIANT_CANDIDATE
        else:
            new = st.state if st.state != UNKNOWN else UNKNOWN

        if new == prev:
            return None
        st.state = new
        st.since = now
        if new == NONCOMPLIANT_CANDIDATE and st.first_seen is None:
            st.first_seen = now
        if new == NONCOMPLIANT:
            st.confirmed_at = now
        if new in (COMPLIANT, COMPLIANT_CANDIDATE):
            # A recovered worker starts clean: keeping first_seen would make a
            # later, unrelated violation claim to have begun hours ago.
            st.first_seen = None
            st.confirmed_at = None
        return new


def evidence_for(ppe_type: str, positives: Iterable[Tuple[str, float]]) -> Tuple[str, float]:
    """Reduce this tick's associated detections for one PPE type to a verdict.

    ``positives`` is (class_name, confidence) for detections already associated
    with this person. An explicit NO- wins over a positive when both appear:
    two detections disagreeing about one head is not evidence of compliance,
    and for safety the ambiguous case should not be resolved in favour of "they
    are fine".
    """
    best_pos = best_neg = 0.0
    for name, conf in positives or ():
        if name == ppe_type:
            best_pos = max(best_pos, float(conf))
        elif name == "no_" + ppe_type:
            best_neg = max(best_neg, float(conf))
    if best_neg > 0:
        return EV_NEGATIVE, best_neg
    if best_pos > 0:
        return EV_POSITIVE, best_pos
    return EV_ABSENT, 0.0
