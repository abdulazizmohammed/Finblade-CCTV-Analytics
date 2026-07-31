"""The 5s aggregation tick must actually be every 5s.

ZoneStateAggregator.due() only stops returning True once something records that
an emission happened, and for a long time the only thing that recorded it was
snapshot(). The camera runner does not call snapshot() — it computes density and
capacity itself, from values it already has in hand — so _last_emit stayed None
for ever and due() answered True on EVERY frame.

The block it guards is commented "5s cadence" and carries density events,
zone-state posts, camera heartbeats and rule evaluation. All of it ran at the
frame rate instead.

Measured on the deployment before the fix, with two zones reporting:

    expected   0.40 rows/s   (2 zones / 5s)
    actual    21.33 rows/s   -> 1,843,200 rows/day

That is a 53x write amplification, and it is why the database reached 1.1 GB in
a day and a half. Retention would only have been mopping up after it.
"""

import unittest

from finblade.metrics import ZoneStateAggregator


class TestAggregatorCadence(unittest.TestCase):

    def test_first_call_is_due(self):
        """Nothing has been emitted yet, so the first tick must fire."""
        self.assertTrue(ZoneStateAggregator(period_s=5.0).due(1000.0))

    def test_mark_stops_it_being_due_immediately_after(self):
        """THE regression. A caller that marks the tick must not be told it is
        due again on the very next frame."""
        a = ZoneStateAggregator(period_s=5.0)
        a.mark(1000.0)
        self.assertFalse(a.due(1000.067))   # next frame at 15fps
        self.assertFalse(a.due(1001.0))
        self.assertFalse(a.due(1004.9))

    def test_due_again_once_the_period_has_elapsed(self):
        """Suppressing the tick must not become suppressing it for ever."""
        a = ZoneStateAggregator(period_s=5.0)
        a.mark(1000.0)
        self.assertTrue(a.due(1005.0))
        self.assertTrue(a.due(1010.0))

    def test_emissions_over_a_minute_match_the_period(self):
        """Walk a minute of frames at 15fps and count how often the tick fires.
        Twelve, not nine hundred."""
        a = ZoneStateAggregator(period_s=5.0)
        fired = 0
        t = 1000.0
        for _ in range(60 * 15):
            if a.due(t):
                a.mark(t)
                fired += 1
            t += 1.0 / 15.0
        self.assertEqual(fired, 12, f"expected 12 ticks in 60s, got {fired}")

    def test_snapshot_also_claims_the_tick(self):
        """The original mechanism still works, so callers using snapshot() are
        unaffected."""
        a = ZoneStateAggregator(period_s=5.0)

        class _Zone:
            zone_id = "ZONE-01"
            area_sqm = 10.0
            capacity_max = 10
            warning_density = 2.0
            critical_density = 4.0

        class _Flow:
            def inflow_per_min(self, *_):
                return 0.0

            def outflow_per_min(self, *_):
                return 0.0

        a.snapshot(_Zone(), 1, _Flow(), 1000.0)
        self.assertFalse(a.due(1000.5))


if __name__ == "__main__":
    unittest.main()
