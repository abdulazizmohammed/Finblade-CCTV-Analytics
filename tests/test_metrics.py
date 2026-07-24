import unittest

from finblade.metrics import (
    DwellTracker,
    FlowCounter,
    ZoneStateAggregator,
    capacity_pct,
    density_per_sqm,
    density_status,
)
from finblade.zones import Zone


class TestDensityMath(unittest.TestCase):
    def test_density(self):
        self.assertAlmostEqual(density_per_sqm(12, 60.0), 0.2)

    def test_density_zero_area(self):
        self.assertEqual(density_per_sqm(5, 0.0), 0.0)

    def test_capacity_pct(self):
        self.assertAlmostEqual(capacity_pct(36, 40), 90.0)

    def test_capacity_zero(self):
        self.assertEqual(capacity_pct(5, 0), 0.0)

    def test_density_status_bands(self):
        self.assertEqual(density_status(1.0), "NORMAL")
        self.assertEqual(density_status(2.5), "AMBER")
        self.assertEqual(density_status(4.5), "RED")
        self.assertEqual(density_status(2.0), "NORMAL")  # strictly greater than on


class TestDwell(unittest.TestCase):
    def test_accumulates(self):
        d = DwellTracker()
        self.assertEqual(d.update(1, "Z1", now=100.0), 0.0)
        self.assertEqual(d.update(1, "Z1", now=105.0), 5.0)
        self.assertEqual(d.update(1, "Z1", now=112.5), 12.5)

    def test_resets_on_zone_change(self):
        d = DwellTracker()
        d.update(1, "Z1", now=100.0)
        d.update(1, "Z1", now=110.0)
        self.assertEqual(d.update(1, "Z2", now=111.0), 0.0)  # new zone -> reset
        self.assertEqual(d.update(1, "Z2", now=113.0), 2.0)

    def test_resets_on_exit(self):
        d = DwellTracker()
        d.update(1, "Z1", now=100.0)
        d.update(1, "Z1", now=110.0)
        self.assertEqual(d.update(1, None, now=111.0), 0.0)
        # Re-entering starts fresh.
        self.assertEqual(d.update(1, "Z1", now=120.0), 0.0)


class TestFlow(unittest.TestCase):
    def test_inflow_rate_per_min(self):
        f = FlowCounter(window_s=60.0)
        for t in range(6):  # 6 entries within the window
            f.record_entry("Z1", now=float(t))
        # 6 entries / 60s * 60 = 6 per min
        self.assertAlmostEqual(f.inflow_per_min("Z1", now=6.0), 6.0)

    def test_window_eviction(self):
        f = FlowCounter(window_s=10.0)
        f.record_entry("Z1", now=0.0)
        f.record_entry("Z1", now=1.0)
        # At t=100 both are far outside the 10s window.
        self.assertEqual(f.inflow_per_min("Z1", now=100.0), 0.0)

    def test_outflow_separate_from_inflow(self):
        f = FlowCounter(window_s=60.0)
        f.record_entry("Z1", now=1.0)
        f.record_exit("Z1", now=2.0)
        self.assertAlmostEqual(f.inflow_per_min("Z1", now=3.0), 1.0)
        self.assertAlmostEqual(f.outflow_per_min("Z1", now=3.0), 1.0)


class TestAggregator(unittest.TestCase):
    def test_period_gating(self):
        agg = ZoneStateAggregator(period_s=5.0)
        self.assertTrue(agg.due(now=0.0))
        z = Zone("Z1", "Z1", False, 40, 60.0, [(0, 0), (1, 0), (1, 1)])
        agg.snapshot(z, occupancy=12, flow=FlowCounter(), now=0.0)
        self.assertFalse(agg.due(now=3.0))
        self.assertTrue(agg.due(now=5.0))

    def test_snapshot_fields(self):
        agg = ZoneStateAggregator(period_s=5.0)
        z = Zone("Z1", "Lobby", False, 40, 60.0, [(0, 0), (1, 0), (1, 1)])
        s = agg.snapshot(z, occupancy=12, flow=FlowCounter(), now=10.0)
        self.assertEqual(s.occupancy, 12)
        self.assertAlmostEqual(s.density, 0.2)
        self.assertAlmostEqual(s.capacity_pct, 30.0)
        self.assertEqual(s.status, "NORMAL")


if __name__ == "__main__":
    unittest.main()
