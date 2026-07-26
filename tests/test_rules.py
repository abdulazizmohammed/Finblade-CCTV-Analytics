import unittest

from finblade.rules import (
    HysteresisLatch,
    RuleEngine,
    RuleThresholds,
)


class TestHysteresisLatch(unittest.TestCase):
    def test_arms_after_sustained_duration(self):
        latch = HysteresisLatch(2.0, 1.8, sustain_s=10.0)
        self.assertIsNone(latch.update(2.0, now=0.0))          # condition begins
        self.assertIsNone(latch.update(2.1, now=9.0))          # <10s, not yet
        self.assertEqual(latch.update(2.0, now=10.0), "FIRE")  # 10s sustained -> FIRE

    def test_brief_spike_does_not_fire(self):
        latch = HysteresisLatch(2.0, 1.8, sustain_s=10.0)
        latch.update(2.5, now=0.0)
        self.assertIsNone(latch.update(1.0, now=3.0))          # dropped -> timer reset
        self.assertIsNone(latch.update(2.5, now=6.0))          # restart timer
        self.assertIsNone(latch.update(2.5, now=15.0))         # only 9s from t=6
        self.assertEqual(latch.update(2.5, now=16.0), "FIRE")  # 10s from t=6

    def test_1_9_does_not_clear(self):
        latch = HysteresisLatch(2.0, 1.8, 10.0)
        latch.update(2.0, 0.0); latch.update(2.0, 10.0)        # armed
        self.assertIsNone(latch.update(1.9, now=25.0))         # 1.9 > off 1.8 -> stays
        self.assertTrue(latch.armed)

    def test_clears_after_sustained_below_off(self):
        latch = HysteresisLatch(2.0, 1.8, 10.0)
        latch.update(2.0, 0.0); latch.update(2.0, 10.0)        # armed
        self.assertIsNone(latch.update(1.7, now=20.0))         # below off, pending
        self.assertEqual(latch.update(1.7, now=30.0), "CLEAR")  # 10s sustained

    def test_no_refire_while_armed(self):
        latch = HysteresisLatch(2.0, 1.8, 10.0)
        latch.update(2.0, 0.0)
        self.assertEqual(latch.update(2.0, now=10.0), "FIRE")
        self.assertIsNone(latch.update(3.0, now=25.0))         # already armed
        self.assertIsNone(latch.update(2.5, now=45.0))

    def test_immediate_mode(self):
        latch = HysteresisLatch(2.0, 1.8, sustain_s=0.0)
        self.assertEqual(latch.update(2.0, now=0.0), "FIRE")


class TestSustainedNoFlap(unittest.TestCase):
    def test_rapid_oscillation_never_fires(self):
        latch = HysteresisLatch(2.0, 1.8, sustain_s=10.0)
        fires = sum(1 for i, v in enumerate([2.5, 1.0] * 6)
                    if latch.update(v, now=float(i)) == "FIRE")
        self.assertEqual(fires, 0)   # condition never holds 10s -> no alert

    def test_sustained_fires_exactly_once(self):
        latch = HysteresisLatch(2.0, 1.8, sustain_s=10.0)
        fires = sum(1 for i in range(25)
                    if latch.update(2.5, now=float(i)) == "FIRE")
        self.assertEqual(fires, 1)   # one FIRE at t=10, none after


class TestDensityRules(unittest.TestCase):
    def _sustain(self, eng, zone, dens, cap, t0, dur=10.0, **kw):
        eng.evaluate_zone(zone, dens, cap, t0, **kw)
        return eng.evaluate_zone(zone, dens, cap, t0 + dur, **kw)

    def test_amber_then_red(self):
        eng = RuleEngine()
        a = self._sustain(eng, "Z1", 2.5, 10, 0.0)
        self.assertTrue(any(al.rule_id == "R-01" and al.severity == "AMBER" for al in a))
        b = self._sustain(eng, "Z1", 4.5, 10, 100.0)
        self.assertTrue(any(al.rule_id == "R-02" and al.severity == "RED" for al in b))

    def test_capacity_rule(self):
        eng = RuleEngine()
        a = self._sustain(eng, "Z1", 0.1, 92.0, 0.0)
        self.assertTrue(any(al.rule_id == "R-03" for al in a))

    def test_brief_density_spike_does_not_fire(self):
        eng = RuleEngine()
        eng.evaluate_zone("Z1", density=3.0, capacity_pct=10, now=0.0)   # spike
        a = eng.evaluate_zone("Z1", density=0.5, capacity_pct=10, now=3.0)  # gone
        self.assertFalse(any(al.rule_id in ("R-01", "R-02") for al in a))

    def test_per_zone_density_thresholds(self):
        eng = RuleEngine()
        a = self._sustain(eng, "ZA", 1.6, 10, 0.0, warning_on=1.5, critical_on=3.0)
        self.assertTrue(any(al.rule_id == "R-01" and al.severity == "AMBER" for al in a))
        b = self._sustain(eng, "ZB", 1.6, 10, 0.0)          # global default 2.0
        self.assertFalse(any(al.rule_id == "R-01" for al in b))

    def test_per_zone_critical_threshold(self):
        eng = RuleEngine()
        a = self._sustain(eng, "ZC", 3.2, 10, 0.0, warning_on=1.5, critical_on=3.0)
        self.assertTrue(any(al.rule_id == "R-02" and al.severity == "RED" for al in a))

    def test_capacity_hysteresis_87_does_not_clear(self):
        eng = RuleEngine()
        self._sustain(eng, "Z1", 0.1, 92.0, 0.0)                            # armed R-03
        a = eng.evaluate_zone("Z1", 0.1, 87.0, now=100.0)                   # 87 > off 85
        self.assertFalse(any(al.rule_id == "R-03" and al.kind == "CLEAR" for al in a))
        eng.evaluate_zone("Z1", 0.1, 80.0, now=200.0)                       # below off
        a = eng.evaluate_zone("Z1", 0.1, 80.0, now=210.0)                   # sustained
        self.assertTrue(any(al.rule_id == "R-03" and al.kind == "CLEAR" for al in a))


class TestCameraOffline(unittest.TestCase):
    def test_29s_no_alert_31s_alert_recovery_clears(self):
        eng = RuleEngine()
        eng.camera.heartbeat("CAM-A-01", now=0.0)
        self.assertIsNone(eng.camera.check("CAM-A-01", now=29.0))       # 29s -> quiet
        fire = eng.camera.check("CAM-A-01", now=31.0)                    # 31s -> offline
        self.assertIsNotNone(fire)
        self.assertEqual(fire.rule_id, "R-07")
        self.assertEqual(fire.kind, "FIRE")
        # No duplicate while still offline.
        self.assertIsNone(eng.camera.check("CAM-A-01", now=45.0))
        clear = eng.camera.heartbeat("CAM-A-01", now=50.0)              # recovery
        self.assertIsNotNone(clear)
        self.assertEqual(clear.kind, "CLEAR")

    def test_unknown_camera_not_flagged(self):
        eng = RuleEngine()
        self.assertIsNone(eng.camera.check("NEVER-SEEN", now=1000.0))


class TestLoitering(unittest.TestCase):
    def test_fires_once_over_threshold(self):
        eng = RuleEngine(RuleThresholds(loiter_seconds=30.0))
        self.assertIsNone(eng.evaluate_loiter("pr_a", "Z1", dwell_s=29.0, now=29.0))
        a = eng.evaluate_loiter("pr_a", "Z1", dwell_s=31.0, now=31.0)
        self.assertIsNotNone(a)
        self.assertEqual(a.rule_id, "R-05")
        # Does not fire again for the same visit.
        self.assertIsNone(eng.evaluate_loiter("pr_a", "Z1", dwell_s=40.0, now=40.0))

    def test_refires_after_reset(self):
        eng = RuleEngine(RuleThresholds(loiter_seconds=30.0))
        eng.evaluate_loiter("pr_a", "Z1", dwell_s=31.0, now=31.0)
        eng.reset_loiter("pr_a", "Z1")
        self.assertIsNotNone(eng.evaluate_loiter("pr_a", "Z1", dwell_s=31.0, now=100.0))


class TestIntrusion(unittest.TestCase):
    def test_immediate_and_once_per_visit(self):
        eng = RuleEngine()
        a = eng.evaluate_intrusion("pr_a", "ZONE-02", restricted=True, now=0.0)
        self.assertIsNotNone(a)
        self.assertEqual(a.rule_id, "R-06")
        self.assertEqual(a.severity, "CRITICAL")
        # Still inside -> no repeat.
        self.assertIsNone(eng.evaluate_intrusion("pr_a", "ZONE-02", restricted=True, now=1.0))

    def test_leaving_and_reentering_refires(self):
        eng = RuleEngine()
        eng.evaluate_intrusion("pr_a", "ZONE-02", restricted=True, now=0.0)
        eng.evaluate_intrusion("pr_a", "ZONE-02", restricted=False, now=5.0)  # left
        self.assertIsNotNone(
            eng.evaluate_intrusion("pr_a", "ZONE-02", restricted=True, now=10.0))


class TestResetScene(unittest.TestCase):
    def test_reset_scene_re_fires_all(self):
        eng = RuleEngine(RuleThresholds(loiter_seconds=30.0))
        # arm intrusion + loiter latches
        self.assertIsNotNone(eng.evaluate_intrusion("p", "Z", restricted=True, now=0.0))
        self.assertIsNotNone(eng.evaluate_loiter("p", "Z", dwell_s=31.0, now=31.0))
        # both are latched now
        self.assertIsNone(eng.evaluate_intrusion("p", "Z", restricted=True, now=1.0))
        self.assertIsNone(eng.evaluate_loiter("p", "Z", dwell_s=40.0, now=40.0))
        # a reconnect / loop resets the scene -> still-true conditions re-alert
        eng.reset_scene()
        self.assertIsNotNone(eng.evaluate_intrusion("p", "Z", restricted=True, now=50.0))
        self.assertIsNotNone(eng.evaluate_loiter("p", "Z", dwell_s=41.0, now=51.0))


class TestDropPerson(unittest.TestCase):
    def test_clears_state_and_bounds_memory(self):
        eng = RuleEngine(RuleThresholds(loiter_seconds=30.0))
        eng.evaluate_intrusion("pr_a", "ZONE-02", restricted=True, now=0.0)
        eng.evaluate_loiter("pr_a", "ZONE-01", dwell_s=31.0, now=31.0)
        self.assertTrue(eng._intrusion_active)
        self.assertTrue(eng._loiter_fired)
        eng.drop_person("pr_a")
        self.assertFalse(eng._intrusion_active)   # forgotten -> bounded memory
        self.assertFalse(eng._loiter_fired)

    def test_drop_lets_reentry_realert(self):
        eng = RuleEngine()
        self.assertIsNotNone(eng.evaluate_intrusion("pr_a", "Z", True, now=0.0))
        self.assertIsNone(eng.evaluate_intrusion("pr_a", "Z", True, now=1.0))  # suppressed
        eng.drop_person("pr_a")                                                 # track left
        self.assertIsNotNone(eng.evaluate_intrusion("pr_a", "Z", True, now=2.0))  # re-alerts

    def test_drop_only_targets_named_person(self):
        eng = RuleEngine()
        eng.evaluate_intrusion("pr_a", "Z", True, now=0.0)
        eng.evaluate_intrusion("pr_b", "Z", True, now=0.0)
        eng.drop_person("pr_a")
        self.assertIn(("pr_b", "Z"), eng._intrusion_active)
        self.assertNotIn(("pr_a", "Z"), eng._intrusion_active)


if __name__ == "__main__":
    unittest.main()
