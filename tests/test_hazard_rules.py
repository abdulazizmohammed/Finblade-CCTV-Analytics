"""R-10 fire/smoke, and its event types.

The detector does not exist yet — no fire/smoke checkpoint has been sourced,
because the community ones carry no licence, no documented training set and no
published evaluation. That does not stop the RULE being provable: it consumes a
confidence float, so it can be driven exhaustively from synthetic readings with
no GPU, no torch and no model. This is what the stdlib-only rule for finblade/
buys.

What these DO establish: that the latch arms only on sustained evidence, clears
only on sustained absence, keeps fire and smoke independent, survives a scene
reset, and that a malformed hazard event is rejected.

What they CANNOT establish: whether a real detector's confidence means anything.
Every threshold here is a guess (see RuleThresholds) and must be retuned against
real fire footage before anyone treats an R-10 alert as calibrated.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.events import (HAZARD_FIRE, HAZARD_SMOKE, new_event,
                             validate_event)
from finblade.rules import RuleEngine, RuleThresholds, SEV_AMBER, SEV_INFO, SEV_RED

FIRE = RuleEngine.HAZARD_FIRE
SMOKE = RuleEngine.HAZARD_SMOKE


def engine(**kw):
    t = RuleThresholds(**kw) if kw else RuleThresholds()
    return RuleEngine(t)


def feed(eng, zone, cls, conf, t0, until, step=1.0):
    """Hold a confidence for a stretch; return every alert produced."""
    out, t = [], t0
    while t <= until:
        a = eng.evaluate_hazard(zone, cls, conf, t)
        if a:
            out.append((t, a))
        t += step
    return out


class TestArming(unittest.TestCase):
    def test_single_frame_spike_does_not_arm(self):
        """The classic false positive: one bright flicker. The sustain gate
        exists for exactly this, so it must not produce an alert."""
        eng = engine()
        self.assertIsNone(eng.evaluate_hazard("Z1", FIRE, 0.95, 100.0))
        # ...and the condition breaking resets the timer.
        self.assertIsNone(eng.evaluate_hazard("Z1", FIRE, 0.0, 100.5))
        self.assertIsNone(eng.evaluate_hazard("Z1", FIRE, 0.95, 101.0))

    def test_sustained_detection_arms_after_the_gate(self):
        eng = engine()
        alerts = feed(eng, "Z1", FIRE, 0.90, 100.0, 106.0, step=0.5)
        self.assertEqual(len(alerts), 1)
        t, a = alerts[0]
        self.assertEqual(a.rule_id, "R-10")
        self.assertEqual(a.severity, SEV_RED)
        self.assertEqual(a.kind, "FIRE")
        self.assertIn("FIRE", a.message)
        # hazard_sustain_seconds is 3.0, so it must not arm before then.
        self.assertGreaterEqual(t - 100.0, 3.0)

    def test_it_arms_once_not_every_frame(self):
        """An operator sent the same fire alert forty times stops reading them."""
        eng = engine()
        self.assertEqual(len(feed(eng, "Z1", FIRE, 0.9, 100.0, 160.0)), 1)

    def test_confidence_just_under_the_bar_never_arms(self):
        eng = engine()          # fire_on 0.60
        self.assertEqual(feed(eng, "Z1", FIRE, 0.59, 100.0, 200.0), [])


class TestClearing(unittest.TestCase):
    def test_it_clears_only_on_sustained_absence(self):
        eng = engine()
        feed(eng, "Z1", FIRE, 0.9, 100.0, 110.0)          # armed
        # One quiet frame must NOT clear it - that is the hysteresis.
        self.assertIsNone(eng.evaluate_hazard("Z1", FIRE, 0.0, 111.0))
        cleared = feed(eng, "Z1", FIRE, 0.0, 111.5, 120.0, step=0.5)
        self.assertEqual(len(cleared), 1)
        _, a = cleared[0]
        self.assertEqual(a.kind, "CLEAR")
        self.assertEqual(a.severity, SEV_INFO)

    def test_a_reading_between_off_and_on_holds_the_alert(self):
        """fire_off 0.35 / fire_on 0.60. A 0.5 reading is neither confident
        enough to arm nor quiet enough to clear, and an armed alert must stay
        armed - otherwise a fire that dims flickers the alarm on and off."""
        eng = engine()
        feed(eng, "Z1", FIRE, 0.9, 100.0, 110.0)
        self.assertEqual(feed(eng, "Z1", FIRE, 0.50, 111.0, 200.0), [])

    def test_never_fed_zero_it_can_never_clear(self):
        """Documents the trap named in the docstring: a caller that only calls
        on a detection gives the latch nothing to clear on."""
        eng = engine()
        feed(eng, "Z1", FIRE, 0.9, 100.0, 110.0)
        # Caller goes quiet instead of feeding 0.0. Nothing clears, by design.
        self.assertEqual(eng.evaluate_hazard("Z1", FIRE, 0.9, 500.0), None)


class TestIndependence(unittest.TestCase):
    def test_fire_and_smoke_latch_separately(self):
        """Smoke clearing must not clear a fire, and vice versa."""
        eng = engine()
        feed(eng, "Z1", FIRE, 0.9, 100.0, 110.0)
        feed(eng, "Z1", SMOKE, 0.9, 100.0, 110.0)
        # Smoke goes quiet; fire does not.
        smoke_cleared = feed(eng, "Z1", SMOKE, 0.0, 111.0, 120.0)
        self.assertTrue(any(a.kind == "CLEAR" for _, a in smoke_cleared))
        self.assertEqual(feed(eng, "Z1", FIRE, 0.9, 111.0, 120.0), [])

    def test_zones_latch_separately(self):
        eng = engine()
        feed(eng, "Z1", FIRE, 0.9, 100.0, 110.0)
        armed_z2 = feed(eng, "Z2", FIRE, 0.9, 100.0, 110.0)
        self.assertEqual(len(armed_z2), 1)

    def test_smoke_is_amber_and_has_a_higher_bar(self):
        eng = engine()
        # 0.62 clears fire_on (0.60) but not smoke_on (0.65).
        self.assertEqual(feed(eng, "Z1", SMOKE, 0.62, 100.0, 130.0), [])
        alerts = feed(eng, "Z1", SMOKE, 0.90, 200.0, 210.0)
        self.assertEqual(alerts[0][1].severity, SEV_AMBER)


class TestUnzonedAndReset(unittest.TestCase):
    def test_a_camera_with_no_zones_can_still_raise_a_hazard(self):
        """The case that would otherwise be silently unreportable: an outdoor
        camera with no polygons drawn."""
        eng = engine()
        out = []
        t = 100.0
        while t <= 110.0:
            a = eng.evaluate_hazard(None, FIRE, 0.9, t, camera_id="CAM-9")
            if a:
                out.append(a)
            t += 0.5
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].zone_id)
        self.assertEqual(out[0].camera_id, "CAM-9")

    def test_reset_scene_drops_the_latch(self):
        eng = engine()
        feed(eng, "Z1", FIRE, 0.9, 100.0, 110.0)
        eng.reset_scene()
        # Fresh scene: a still-burning fire must re-alert rather than be
        # suppressed by a latch armed before the gap.
        self.assertEqual(len(feed(eng, "Z1", FIRE, 0.9, 200.0, 210.0)), 1)

    def test_unknown_hazard_class_is_ignored_not_guessed(self):
        eng = engine()
        self.assertIsNone(eng.evaluate_hazard("Z1", "explosion", 0.99, 100.0))


class TestHazardEvents(unittest.TestCase):
    def _evt(self, etype=HAZARD_FIRE, **over):
        e = new_event(etype, "CAM-1", "SITE-1", 100.0, confidence=0.81,
                      zone_id="Z1", detections=2)
        e.update(over)
        return e

    def test_a_well_formed_hazard_event_validates(self):
        ok, errors = validate_event(self._evt())
        self.assertTrue(ok, errors)
        ok, errors = validate_event(self._evt(HAZARD_SMOKE))
        self.assertTrue(ok, errors)

    def test_confidence_is_required(self):
        e = self._evt()
        del e["confidence"]
        ok, errors = validate_event(e)
        self.assertFalse(ok)
        self.assertTrue(any("confidence" in x for x in errors))

    def test_confidence_must_be_a_probability(self):
        for bad in (1.4, -0.2):
            ok, errors = validate_event(self._evt(confidence=bad))
            self.assertFalse(ok, "accepted confidence %s" % bad)

    def test_zone_id_is_optional(self):
        """An unzoned camera must be able to report a hazard."""
        e = self._evt()
        del e["zone_id"]
        ok, errors = validate_event(e)
        self.assertTrue(ok, errors)

    def test_detections_cannot_be_negative(self):
        ok, _ = validate_event(self._evt(detections=-1))
        self.assertFalse(ok)

    def test_hazard_events_carry_no_person_ref(self):
        """A fire is not attributable to a person, and attaching one would put
        an individual next to a hazard alert for no reason."""
        e = self._evt()
        self.assertNotIn("person_ref", e)


class TestStdlibOnly(unittest.TestCase):
    def test_rules_and_events_import_nothing_heavy(self):
        """The constraint that keeps this suite headless.

        MUST run in a fresh interpreter. sys.modules is process-global, so
        checking it in-process only tells you whether some OTHER test in the
        same run has imported torch — which, in a full-suite run, it always
        has. The first version of this test did exactly that: it passed alone
        and failed in company, which is worse than not having it.
        """
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "import sys;"
            "sys.path.insert(0, %r);"
            "import finblade.rules, finblade.events;"
            "heavy=[m for m in ('torch','cv2','numpy','ultralytics','boxmot')"
            " if m in sys.modules];"
            "print(','.join(heavy))" % repo
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        leaked = out.stdout.strip()
        self.assertEqual(leaked, "",
                         "finblade.rules/events pulled in: %s" % leaked)


if __name__ == "__main__":
    unittest.main()
