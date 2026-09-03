"""R-11 PPE compliance: the temporal state machine (cases G-T).

Driven entirely from synthetic evidence — no model, no GPU, no weights. That is
the whole reason the state machine lives in finblade/ and speaks in
positive/negative/absent rather than in YOLO results.

Every threshold used here is a GUESS (see PPEThresholds). These tests prove the
BEHAVIOUR is right — one miss does not convict, sustained evidence does,
recovery clears — not that the numbers are calibrated. They are not.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.ppe import (COMPLIANT, COMPLIANT_CANDIDATE, EV_ABSENT,
                          EV_NEGATIVE, EV_POSITIVE, HARDHAT, MASK,
                          NONCOMPLIANT, NONCOMPLIANT_CANDIDATE, PPEThresholds,
                          PPETracker, UNKNOWN, VEST, evidence_for)
from finblade.rules import RuleEngine


def tracker(**kw):
    return PPETracker("CAM-01", PPEThresholds(**kw))


def feed(tr, track, ppe, evidence, seconds, t0, conf=0.9, dt=0.5):
    """Hold one kind of evidence for a stretch. Returns transitions seen."""
    out, t = [], t0
    while t < t0 + seconds:
        s = tr.observe(track, ppe, evidence, conf, t, dt)
        if s:
            out.append((round(t, 2), s))
        t += dt
    return out


class TestZoneScoping(unittest.TestCase):
    def test_G_worker_outside_a_compliance_zone_is_never_judged(self):
        """No zone entry recorded -> permanently in grace -> nothing evaluated.
        A corridor must not accuse anyone of not wearing a hardhat."""
        tr = tracker()
        self.assertTrue(tr.in_grace(1, "ZONE-A", 0.0))
        self.assertTrue(tr.in_grace(1, "ZONE-A", 10_000.0))

    def test_H_entering_a_zone_starts_a_grace_period(self):
        tr = tracker(entry_grace_s=5.0)
        tr.note_in_zone(1, "ZONE-A", 100.0)
        self.assertTrue(tr.in_grace(1, "ZONE-A", 102.0))
        self.assertFalse(tr.in_grace(1, "ZONE-A", 106.0))

    def test_Q_the_same_track_gets_fresh_grace_in_a_different_zone(self):
        """Different zone, different requirements, so the worker gets the same
        chance to be seen properly on arriving."""
        tr = tracker(entry_grace_s=5.0)
        tr.note_in_zone(1, "ZONE-A", 100.0)
        tr.note_in_zone(1, "ZONE-B", 130.0)
        self.assertFalse(tr.in_grace(1, "ZONE-A", 132.0))
        self.assertTrue(tr.in_grace(1, "ZONE-B", 132.0))

    def test_leaving_a_zone_clears_its_entry(self):
        tr = tracker(entry_grace_s=5.0)
        tr.note_in_zone(1, "ZONE-A", 100.0)
        tr.left_zone(1, "ZONE-A")
        self.assertTrue(tr.in_grace(1, "ZONE-A", 200.0))


class TestVerdicts(unittest.TestCase):
    def test_I_sustained_positive_evidence_reaches_COMPLIANT(self):
        tr = tracker(recovery_confirm_s=5.0)
        trans = feed(tr, 1, HARDHAT, EV_POSITIVE, 8.0, 100.0)
        self.assertIn(COMPLIANT, [s for _, s in trans])
        self.assertEqual(tr.verdict(1, HARDHAT), COMPLIANT)

    def test_J_sustained_explicit_NO_hardhat_reaches_NONCOMPLIANT(self):
        tr = tracker(violation_confirm_s=8.0)
        trans = feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        self.assertIn(NONCOMPLIANT, [s for _, s in trans])

    def test_K_a_single_frame_miss_does_not_convict(self):
        """The failure this whole module exists to prevent."""
        tr = tracker(violation_confirm_s=8.0)
        feed(tr, 1, HARDHAT, EV_POSITIVE, 10.0, 100.0)
        self.assertEqual(tr.verdict(1, HARDHAT), COMPLIANT)
        tr.observe(1, HARDHAT, EV_ABSENT, 0.0, 111.0, 0.5)   # one bad frame
        self.assertEqual(tr.verdict(1, HARDHAT), COMPLIANT,
                         "one missed detection must not change the verdict")

    def test_K2_a_short_burst_of_misses_still_does_not_convict(self):
        tr = tracker(violation_confirm_s=8.0)
        feed(tr, 1, HARDHAT, EV_POSITIVE, 10.0, 100.0)
        feed(tr, 1, HARDHAT, EV_ABSENT, 2.0, 111.0, conf=0.0)
        self.assertNotEqual(tr.verdict(1, HARDHAT), NONCOMPLIANT)

    def test_L_sustained_ABSENCE_eventually_convicts_but_slowly(self):
        """Absence is real evidence - a model that has stopped emitting
        NO-Hardhat for a bare head is a genuine failure - but it is weaker than
        an explicit NO-, so it must take materially longer."""
        weak = tracker(violation_confirm_s=8.0, absence_weight=0.25)
        strong = tracker(violation_confirm_s=8.0, absence_weight=0.25)
        t_abs = next((t for t, s in feed(weak, 1, HARDHAT, EV_ABSENT, 120.0,
                                         100.0, conf=0.0) if s == NONCOMPLIANT),
                     None)
        t_neg = next((t for t, s in feed(strong, 2, HARDHAT, EV_NEGATIVE, 120.0,
                                         100.0) if s == NONCOMPLIANT), None)
        self.assertIsNotNone(t_abs)
        self.assertIsNotNone(t_neg)
        self.assertGreater(t_abs - 100.0, (t_neg - 100.0) * 2,
                           "absence must convict far more slowly than NO-")

    def test_M_putting_the_hardhat_on_clears_after_the_recovery_period(self):
        tr = tracker(violation_confirm_s=8.0, recovery_confirm_s=5.0)
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        self.assertEqual(tr.verdict(1, HARDHAT), NONCOMPLIANT)
        trans = feed(tr, 1, HARDHAT, EV_POSITIVE, 30.0, 120.0)
        self.assertIn(COMPLIANT, [s for _, s in trans])
        self.assertEqual(tr.verdict(1, HARDHAT), COMPLIANT)

    def test_recovery_is_not_instant(self):
        tr = tracker(violation_confirm_s=8.0, recovery_confirm_s=5.0)
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        tr.observe(1, HARDHAT, EV_POSITIVE, 0.9, 120.0, 0.5)
        self.assertNotEqual(tr.verdict(1, HARDHAT), COMPLIANT)

    def test_low_confidence_detections_are_ignored(self):
        tr = tracker(min_confidence=0.4, violation_confirm_s=8.0)
        # A 0.1-confidence NO-Hardhat is noise, and must be treated as silence
        # rather than as an accusation.
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 4.0, 100.0, conf=0.1)
        st = tr.state_of(1, HARDHAT)
        self.assertEqual(st.neg_ticks, 0)
        self.assertGreater(st.abs_ticks, 0)


class TestIndependence(unittest.TestCase):
    def test_O_one_worker_can_violate_several_items_at_once(self):
        tr = tracker(violation_confirm_s=8.0)
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        feed(tr, 1, VEST, EV_NEGATIVE, 12.0, 100.0)
        feed(tr, 1, MASK, EV_POSITIVE, 12.0, 100.0)
        self.assertEqual(tr.verdict(1, HARDHAT), NONCOMPLIANT)
        self.assertEqual(tr.verdict(1, VEST), NONCOMPLIANT)
        self.assertEqual(tr.verdict(1, MASK), COMPLIANT)

    def test_P_workers_hold_independent_state(self):
        tr = tracker(violation_confirm_s=8.0)
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        feed(tr, 2, HARDHAT, EV_POSITIVE, 12.0, 100.0)
        self.assertEqual(tr.verdict(1, HARDHAT), NONCOMPLIANT)
        self.assertEqual(tr.verdict(2, HARDHAT), COMPLIANT)

    def test_N_an_item_the_zone_does_not_require_is_never_asked_about(self):
        """Zone scoping is the CALLER's job - the tracker only holds state for
        items it is asked about. Nothing asks about mask, so mask stays UNKNOWN
        and no mask alert can exist."""
        tr = tracker(violation_confirm_s=8.0)
        for ppe in (HARDHAT, VEST):          # zone requires these two only
            feed(tr, 1, ppe, EV_NEGATIVE, 12.0, 100.0)
        self.assertEqual(tr.verdict(1, MASK), UNKNOWN)
        self.assertIsNone(tr.state_of(1, MASK))

    def test_R_dropping_a_track_cleans_up_all_of_its_state(self):
        """State must not outlive the track it describes: the dicts would grow
        without bound on a busy site, and a recycled ByteTrack id would inherit
        a stranger's compliance verdict."""
        tr = tracker(recovery_confirm_s=5.0)
        # 8s, comfortably past the 5s recovery bar, so the surviving track is
        # in a settled state and the assertion below means something.
        for ppe in (HARDHAT, VEST, MASK):
            feed(tr, 1, ppe, EV_POSITIVE, 8.0, 100.0)
        feed(tr, 2, HARDHAT, EV_POSITIVE, 8.0, 100.0)
        tr.note_in_zone(1, "ZONE-A", 100.0)
        self.assertEqual(tr.tracked(), 4)

        removed = tr.drop_track(1)
        self.assertEqual(removed, 3)
        self.assertEqual(tr.verdict(1, HARDHAT), UNKNOWN)
        self.assertIsNone(tr.state_of(1, VEST))
        # The other worker is untouched.
        self.assertEqual(tr.verdict(2, HARDHAT), COMPLIANT)
        self.assertEqual(tr.tracked(), 1)
        # ...and the zone-entry bookkeeping went with it, so a recycled id
        # starts with fresh grace rather than inheriting an expired one.
        self.assertTrue(tr.in_grace(1, "ZONE-A", 200.0))


class TestEvidenceReduction(unittest.TestCase):
    def test_explicit_NO_beats_a_positive_when_both_appear(self):
        """Two detections disagreeing about one head is not evidence that the
        worker is fine. For safety the ambiguous case must not resolve to OK."""
        ev, conf = evidence_for(HARDHAT, [("hardhat", 0.9), ("no_hardhat", 0.5)])
        self.assertEqual(ev, EV_NEGATIVE)
        self.assertEqual(conf, 0.5)

    def test_nothing_seen_is_absent_not_negative(self):
        self.assertEqual(evidence_for(HARDHAT, []), (EV_ABSENT, 0.0))

    def test_other_items_do_not_leak_between_types(self):
        ev, _ = evidence_for(HARDHAT, [("safety_vest", 0.9), ("no_mask", 0.9)])
        self.assertEqual(ev, EV_ABSENT)

    def test_strongest_of_several_same_class_detections_wins(self):
        ev, conf = evidence_for(VEST, [("safety_vest", 0.4), ("safety_vest", 0.8)])
        self.assertEqual((ev, conf), (EV_POSITIVE, 0.8))


class TestRuleEmission(unittest.TestCase):
    def test_S_T_one_OPEN_alert_per_violation_not_one_per_frame(self):
        """S and T together: the rule only speaks on a TRANSITION, so repeated
        violation frames cannot produce duplicate OPEN alerts - and therefore
        cannot produce duplicate snapshots either."""
        eng = RuleEngine()
        tr = tracker(violation_confirm_s=8.0)
        alerts = []
        t = 100.0
        while t < 160.0:
            new = tr.observe(1, HARDHAT, EV_NEGATIVE, 0.9, t, 0.5)
            if new:
                a = eng.evaluate_ppe("CAM-01", "ZONE-A", 1, HARDHAT, new,
                                     tr.state_of(1, HARDHAT), t)
                if a:
                    alerts.append(a)
            t += 0.5
        opens = [a for a in alerts if a.kind == "FIRE"]
        self.assertEqual(len(opens), 1, "exactly one OPEN for one violation")
        self.assertEqual(opens[0].rule_id, "R-11")
        self.assertEqual(opens[0].zone_id, "ZONE-A")

    def test_the_alert_says_who_where_what_and_when(self):
        eng = RuleEngine()
        tr = tracker(violation_confirm_s=8.0)
        last = None
        t = 100.0
        while t < 140.0:
            new = tr.observe(7, HARDHAT, EV_NEGATIVE, 0.9, t, 0.5)
            if new == NONCOMPLIANT:
                last = eng.evaluate_ppe("CAM-03", "ZONE-WELDING", 7, HARDHAT,
                                        new, tr.state_of(7, HARDHAT), t,
                                        person_ref="pr_abc")
                break
            t += 0.5
        self.assertIsNotNone(last)
        self.assertEqual(last.camera_id, "CAM-03")     # where (camera)
        self.assertEqual(last.zone_id, "ZONE-WELDING")  # where (zone)
        self.assertEqual(last.person_ref, "pr_abc")     # who
        self.assertIn("hardhat", last.message)          # what
        self.assertIn("7", last.message)                # which track

    def test_recovery_emits_a_CLEAR(self):
        eng = RuleEngine()
        tr = tracker(violation_confirm_s=8.0, recovery_confirm_s=5.0)
        feed(tr, 1, HARDHAT, EV_NEGATIVE, 12.0, 100.0)
        clears = []
        t = 120.0
        while t < 160.0:
            new = tr.observe(1, HARDHAT, EV_POSITIVE, 0.9, t, 0.5)
            if new:
                a = eng.evaluate_ppe("CAM-01", "ZONE-A", 1, HARDHAT, new,
                                     tr.state_of(1, HARDHAT), t)
                if a and a.kind == "CLEAR":
                    clears.append(a)
            t += 0.5
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0].severity, "INFO")

    def test_candidate_states_do_not_alert(self):
        eng = RuleEngine()
        tr = tracker(violation_confirm_s=8.0)
        tr.observe(1, HARDHAT, EV_NEGATIVE, 0.9, 100.0, 0.5)
        self.assertEqual(tr.verdict(1, HARDHAT), NONCOMPLIANT_CANDIDATE)
        self.assertIsNone(eng.evaluate_ppe("CAM-01", "Z", 1, HARDHAT,
                                           NONCOMPLIANT_CANDIDATE,
                                           tr.state_of(1, HARDHAT), 100.0))


class TestStdlibOnly(unittest.TestCase):
    def test_ppe_core_imports_nothing_heavy(self):
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys;sys.path.insert(0, %r);"
                "import finblade.ppe, finblade.geometry, finblade.rules;"
                "print(','.join(m for m in ('torch','cv2','numpy','ultralytics')"
                " if m in sys.modules))" % repo)
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
