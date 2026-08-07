"""DENSITY_UPDATE emission policy — Part A step 5.

The event was 99.85% of the events table: 1,674,979 rows against 2,553 for every
other type combined, duplicating the zone_state_ts row written on the next line
at the same microsecond. Thresholding keeps the crossing — the transition an
operator reacts to — and drops the repetition.
"""

import unittest

from finblade.emission import ALWAYS, OFF, THRESHOLD, DensityUpdateGate


class TestThresholdMode(unittest.TestCase):
    def setUp(self):
        self.gate = DensityUpdateGate()

    def test_threshold_is_the_default(self):
        self.assertEqual(THRESHOLD, DensityUpdateGate().mode)

    def test_first_sighting_of_a_zone_always_emits(self):
        """A consumer joining the stream needs a starting value. Without this a
        zone sitting at NORMAL all day would never appear at all."""
        self.assertTrue(self.gate.should_emit("ZONE-01", "NORMAL"))

    def test_unchanged_status_is_suppressed(self):
        self.gate.should_emit("ZONE-01", "NORMAL")
        for _ in range(100):
            self.assertFalse(self.gate.should_emit("ZONE-01", "NORMAL"))

    def test_every_crossing_emits(self):
        seq = ["NORMAL", "WARNING", "CRITICAL", "WARNING", "NORMAL"]
        emitted = [self.gate.should_emit("ZONE-01", s) for s in seq]
        self.assertEqual([True] * 5, emitted)

    def test_a_crossing_and_back_between_ticks_still_emits_both_ends(self):
        self.gate.should_emit("ZONE-01", "NORMAL")
        self.assertTrue(self.gate.should_emit("ZONE-01", "CRITICAL"))
        self.assertTrue(self.gate.should_emit("ZONE-01", "NORMAL"))

    def test_zones_are_tracked_independently(self):
        """ZONE-01 crossing must not suppress or trigger ZONE-02."""
        self.gate.should_emit("ZONE-01", "NORMAL")
        self.gate.should_emit("ZONE-02", "NORMAL")
        self.assertTrue(self.gate.should_emit("ZONE-01", "WARNING"))
        self.assertFalse(self.gate.should_emit("ZONE-02", "NORMAL"))

    def test_none_status_is_a_real_value_not_a_missing_one(self):
        """A zone with no thresholds configured reports status None. That is a
        legitimate state and its first sighting must emit, exactly once."""
        self.assertTrue(self.gate.should_emit("ZONE-01", None))
        self.assertFalse(self.gate.should_emit("ZONE-01", None))
        self.assertTrue(self.gate.should_emit("ZONE-01", "NORMAL"))

    def test_reset_re_establishes_every_zone(self):
        """A camera reconnecting, or a looping clip starting over. Suppressing
        against a status from before the gap would leave the consumer with a
        reading that predates the outage."""
        self.gate.should_emit("ZONE-01", "WARNING")
        self.assertFalse(self.gate.should_emit("ZONE-01", "WARNING"))
        self.gate.reset()
        self.assertTrue(self.gate.should_emit("ZONE-01", "WARNING"))


class TestOtherModes(unittest.TestCase):
    def test_always_restores_the_old_behaviour(self):
        """One environment variable, if FinBlade comes back and says they need
        every tick."""
        gate = DensityUpdateGate(ALWAYS)
        self.assertTrue(all(gate.should_emit("ZONE-01", "NORMAL")
                            for _ in range(50)))

    def test_off_emits_nothing_at_all(self):
        gate = DensityUpdateGate(OFF)
        self.assertFalse(gate.should_emit("ZONE-01", "NORMAL"))
        self.assertFalse(gate.should_emit("ZONE-01", "CRITICAL"))

    def test_an_unrecognised_mode_falls_back_rather_than_raising(self):
        """A typo in a deployment's environment must not stop a camera
        starting — but it must be visible, not silent."""
        gate = DensityUpdateGate("thresold")
        self.assertEqual(THRESHOLD, gate.mode)
        self.assertEqual("thresold", gate.invalid_mode)

    def test_case_and_whitespace_are_tolerated(self):
        self.assertEqual(ALWAYS, DensityUpdateGate("  ALWAYS ").mode)

    def test_empty_or_none_uses_the_default(self):
        for value in ("", None):
            self.assertEqual(THRESHOLD, DensityUpdateGate(value).mode)
            self.assertIsNone(DensityUpdateGate(value).invalid_mode)


class TestVolume(unittest.TestCase):
    def test_a_quiet_day_collapses_to_a_handful(self):
        """A thirty-zone site emits 21,600 of these an hour today. A zone that
        never crosses a threshold should emit exactly one, ever."""
        gate = DensityUpdateGate()
        ticks_per_day = 17_280                       # one per 5s
        emitted = sum(gate.should_emit("ZONE-01", "NORMAL")
                      for _ in range(ticks_per_day))
        self.assertEqual(1, emitted)

    def test_a_busy_zone_emits_once_per_crossing_not_per_tick(self):
        gate = DensityUpdateGate()
        statuses = (["NORMAL"] * 100 + ["WARNING"] * 100
                    + ["CRITICAL"] * 100 + ["NORMAL"] * 100)
        emitted = sum(gate.should_emit("ZONE-01", s) for s in statuses)
        self.assertEqual(4, emitted, "one per crossing, four crossings")


if __name__ == "__main__":
    unittest.main()
