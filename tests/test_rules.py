import unittest

from finblade.rules import (
    HysteresisLatch,
    RuleEngine,
    RuleThresholds,
)


class TestHysteresisLatch(unittest.TestCase):
    def test_arms_on_threshold(self):
        latch = HysteresisLatch(on_threshold=2.0, off_threshold=1.8, debounce_s=10.0)
        self.assertEqual(latch.update(2.0, now=0.0), "FIRE")

    def test_1_9_does_not_clear(self):
        latch = HysteresisLatch(2.0, 1.8, 10.0)
        latch.update(2.0, now=0.0)                       # armed
        self.assertIsNone(latch.update(1.9, now=20.0))   # above off -> stays armed
        self.assertTrue(latch.armed)

    def test_falling_below_off_clears(self):
        latch = HysteresisLatch(2.0, 1.8, 10.0)
        latch.update(2.0, now=0.0)
        self.assertEqual(latch.update(1.7, now=20.0), "CLEAR")

    def test_no_refire_while_armed(self):
        latch = HysteresisLatch(2.0, 1.8, 10.0)
        self.assertEqual(latch.update(2.0, now=0.0), "FIRE")
        self.assertIsNone(latch.update(3.0, now=20.0))   # already armed
        self.assertIsNone(latch.update(2.5, now=40.0))


class TestTenSecondDebounce(unittest.TestCase):
    def test_rapid_oscillation_one_alert(self):
        latch = HysteresisLatch(2.0, 1.8, debounce_s=10.0)
        fires = 0
        clears = 0
        # Oscillate hard across BOTH thresholds every 1s for 8s.
        seq = [2.5, 1.0, 2.5, 1.0, 2.5, 1.0, 2.5, 1.0]
        for i, v in enumerate(seq):
            r = latch.update(v, now=float(i))  # times 0..7, all within 10s
            fires += (r == "FIRE")
            clears += (r == "CLEAR")
        self.assertEqual(fires, 1, "should fire exactly once despite oscillation")
        self.assertEqual(clears, 0, "clear within debounce window is suppressed")

    def test_clear_allowed_after_debounce_window(self):
        latch = HysteresisLatch(2.0, 1.8, debounce_s=10.0)
        self.assertEqual(latch.update(2.5, now=0.0), "FIRE")
        self.assertIsNone(latch.update(1.0, now=5.0))     # too soon -> suppressed
        self.assertEqual(latch.update(1.0, now=11.0), "CLEAR")  # >10s later


class TestDensityRules(unittest.TestCase):
    def test_amber_then_red(self):
        eng = RuleEngine()
        a = eng.evaluate_zone("Z1", density=2.5, capacity_pct=10, now=0.0)
        self.assertTrue(any(al.rule_id == "R-01" and al.severity == "AMBER" for al in a))
        b = eng.evaluate_zone("Z1", density=4.5, capacity_pct=10, now=100.0)
        self.assertTrue(any(al.rule_id == "R-02" and al.severity == "RED" for al in b))

    def test_capacity_rule(self):
        eng = RuleEngine()
        a = eng.evaluate_zone("Z1", density=0.1, capacity_pct=92.0, now=0.0)
        self.assertTrue(any(al.rule_id == "R-03" for al in a))

    def test_capacity_hysteresis_87_does_not_clear(self):
        eng = RuleEngine()
        eng.evaluate_zone("Z1", density=0.1, capacity_pct=92.0, now=0.0)
        a = eng.evaluate_zone("Z1", density=0.1, capacity_pct=87.0, now=100.0)
        self.assertFalse(any(al.rule_id == "R-03" and al.kind == "CLEAR" for al in a))
        a = eng.evaluate_zone("Z1", density=0.1, capacity_pct=80.0, now=200.0)
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


if __name__ == "__main__":
    unittest.main()
