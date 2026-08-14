"""Direction violations, group crossings and head-count alerts.

REQ-21 (occupancy threshold), REQ-23 (wrong way), REQ-24 (group entry).
"""

import unittest

from finblade.flowrules import (
    ALLOWED, UNPOLICED, WRONG_WAY,
    DirectionPolicy, GroupCrossingDetector, WrongWayDetector,
)
from finblade.rules import RuleEngine

REF = "pr_0123456789abcdef"
REF2 = "pr_fedcba9876543210"


class TestDirectionPolicy(unittest.TestCase):
    def setUp(self):
        # Declared: you may walk CONCOURSE -> PLATFORM. Nothing else declared.
        self.p = DirectionPolicy([("CONCOURSE", "PLATFORM")])

    def test_declared_route_is_allowed(self):
        self.assertEqual(self.p.verdict("CONCOURSE", "PLATFORM"), ALLOWED)

    def test_reverse_of_a_declared_route_is_a_violation(self):
        self.assertEqual(self.p.verdict("PLATFORM", "CONCOURSE"), WRONG_WAY)

    def test_undeclared_pairs_are_not_policed(self):
        # The important negative. Every ordinary two-way corridor in the
        # building lands here, and inventing a rule for it would alert on the
        # first person to walk back the way they came.
        self.assertEqual(self.p.verdict("LOBBY", "CORRIDOR"), UNPOLICED)
        self.assertEqual(self.p.verdict("CORRIDOR", "LOBBY"), UNPOLICED)

    def test_missing_zones_are_not_policed(self):
        self.assertEqual(self.p.verdict(None, "PLATFORM"), UNPOLICED)
        self.assertEqual(self.p.verdict("CONCOURSE", None), UNPOLICED)

    def test_built_from_zone_records(self):
        zones = [
            {"zone_id": "PLATFORM", "allowed_from": ["CONCOURSE"]},
            {"zone_id": "LOBBY"},
            {"zone_id": "OLD-GATE", "allowed_from": ["LOBBY"], "enabled": False},
        ]
        p = DirectionPolicy.from_zones(zones)
        self.assertEqual(p.verdict("CONCOURSE", "PLATFORM"), ALLOWED)
        self.assertEqual(p.verdict("PLATFORM", "CONCOURSE"), WRONG_WAY)
        self.assertEqual(p.verdict("LOBBY", "OLD-GATE"), UNPOLICED,
                         "a disabled zone stops being policed")


class TestWrongWayDetector(unittest.TestCase):
    def setUp(self):
        self.d = WrongWayDetector(DirectionPolicy([("CONCOURSE", "PLATFORM")]),
                                  cooldown_s=60.0)

    def test_violation_is_reported(self):
        v = self.d.check(REF, "PLATFORM", "CONCOURSE", 100.0)
        self.assertIsNotNone(v)
        self.assertEqual(v["zone_from"], "PLATFORM")
        self.assertEqual(v["zone_to"], "CONCOURSE")
        self.assertEqual(v["allowed_direction"], "CONCOURSE -> PLATFORM")
        self.assertEqual(self.d.stats["violations"], 1)

    def test_correct_direction_is_silent(self):
        self.assertIsNone(self.d.check(REF, "CONCOURSE", "PLATFORM", 100.0))
        self.assertEqual(self.d.stats["violations"], 0)

    def test_repeat_by_the_same_person_is_suppressed(self):
        self.assertIsNotNone(self.d.check(REF, "PLATFORM", "CONCOURSE", 100.0))
        self.assertIsNone(self.d.check(REF, "PLATFORM", "CONCOURSE", 110.0))
        self.assertEqual(self.d.stats["suppressed_repeat"], 1)

    def test_a_different_person_still_alerts(self):
        self.d.check(REF, "PLATFORM", "CONCOURSE", 100.0)
        self.assertIsNotNone(self.d.check(REF2, "PLATFORM", "CONCOURSE", 101.0))
        self.assertEqual(self.d.stats["violations"], 2)

    def test_repeat_after_the_cooldown_alerts_again(self):
        self.d.check(REF, "PLATFORM", "CONCOURSE", 100.0)
        self.assertIsNotNone(self.d.check(REF, "PLATFORM", "CONCOURSE", 200.0))

    def test_walking_it_correctly_rearms_the_latch(self):
        self.d.check(REF, "PLATFORM", "CONCOURSE", 100.0)      # violation
        self.d.check(REF, "CONCOURSE", "PLATFORM", 105.0)      # corrected
        self.assertIsNotNone(self.d.check(REF, "PLATFORM", "CONCOURSE", 110.0),
                             "a genuine second violation must not be swallowed")

    def test_unpoliced_pairs_never_alert(self):
        self.assertIsNone(self.d.check(REF, "LOBBY", "CORRIDOR", 100.0))
        self.assertEqual(self.d.stats["policed"], 0)


class TestGroupCrossing(unittest.TestCase):
    def setUp(self):
        self.g = GroupCrossingDetector(window_s=3.0, threshold=5, cooldown_s=10.0)

    def _cross(self, n, t0=100.0, zone="BAY", step=0.4):
        out = []
        for i in range(n):
            out.append(self.g.record(zone, f"pr_{i:016x}", t0 + i * step))
        return out

    def test_five_people_in_three_seconds_fires_once(self):
        results = self._cross(5)
        self.assertEqual([r for r in results if r][0]["count"], 5)
        self.assertEqual(sum(1 for r in results if r), 1)

    def test_four_people_do_not_fire(self):
        self.assertTrue(all(r is None for r in self._cross(4)))

    def test_people_spread_beyond_the_window_do_not_fire(self):
        self.assertTrue(all(r is None for r in self._cross(5, step=1.5)),
                        "five people over 7.5s is traffic, not a group")

    def test_one_person_oscillating_is_not_a_group(self):
        # The classic false positive: counting crossings instead of people.
        results = [self.g.record("BAY", REF, 100.0 + i * 0.2) for i in range(10)]
        self.assertTrue(all(r is None for r in results))
        self.assertEqual(self.g.recent_count("BAY", 101.8), 1)

    def test_cooldown_prevents_a_stream_of_group_events(self):
        self._cross(5)
        more = [self.g.record("BAY", f"pr_9{i:015x}", 103.0 + i * 0.2) for i in range(5)]
        self.assertTrue(all(r is None for r in more), "still inside the cooldown")

    def test_per_zone_thresholds_override_the_default(self):
        per_zone = {"DOORWAY": {"threshold": 2, "window_s": 3.0}}
        a = self.g.record("DOORWAY", REF, 100.0, per_zone)
        b = self.g.record("DOORWAY", REF2, 100.5, per_zone)
        self.assertIsNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(b["count"], 2)
        self.assertEqual(b["threshold"], 2)

    def test_zones_are_counted_independently(self):
        self._cross(4, zone="BAY")
        self.assertTrue(all(r is None for r in self._cross(4, zone="DOCK")))


class TestOccupancyThresholdRule(unittest.TestCase):
    """REQ-21 — alert on a head count, without needing area or capacity."""

    def setUp(self):
        self.eng = RuleEngine()

    def fire(self, zone, occ, threshold, t0=100.0):
        """Drive the latch past the engine-wide 10s sustain.

        Every rule here debounces before firing, so a single reading above the
        threshold is deliberately not an alert — the condition has to hold.
        """
        self.eng.evaluate_occupancy(zone, occ, threshold, t0)
        return self.eng.evaluate_occupancy(zone, occ, threshold, t0 + 11.0)

    def test_fires_once_the_count_is_sustained(self):
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 2, 3, 100.0))
        a = self.fire("BAY", 4, 3)
        self.assertIsNotNone(a)
        self.assertEqual(a.rule_id, "R-09")
        self.assertIn("4 people in BAY", a.message)

    def test_a_brief_spike_does_not_alert(self):
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 9, 3, 100.0))
        # Back down well before the sustain elapses.
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 1, 3, 103.0))
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 1, 3, 120.0))

    def test_hysteresis_prevents_flapping(self):
        self.assertIsNotNone(self.fire("BAY", 4, 3))
        # Falling to 3 stays above the clear threshold (3 * 0.8 = 2.4), so the
        # alert must not clear and must not re-fire.
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 3, 3, 130.0))
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 3, 3, 145.0))
        # Below the clear threshold, sustained.
        self.eng.evaluate_occupancy("BAY", 1, 3, 160.0)
        cleared = self.eng.evaluate_occupancy("BAY", 1, 3, 171.0)
        self.assertIsNotNone(cleared)
        self.assertEqual(cleared.kind, "CLEAR")

    def test_repeated_readings_above_threshold_alert_once(self):
        self.assertIsNotNone(self.fire("BAY", 5, 3))
        for i in range(10):
            self.assertIsNone(self.eng.evaluate_occupancy("BAY", 5, 3, 120.0 + i))

    def test_no_threshold_configured_is_a_no_op(self):
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 99, 0, 100.0))
        self.assertIsNone(self.eng.evaluate_occupancy("BAY", 99, None, 100.0))

    def test_zones_latch_independently(self):
        self.assertIsNotNone(self.fire("BAY", 4, 3))
        self.assertIsNotNone(self.fire("DOCK", 4, 3))


if __name__ == "__main__":
    unittest.main()
