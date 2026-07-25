import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade.events import DENSITY_UPDATE, ZONE_ENTRY, ZONE_TRANSITION, new_event
from finblade.identity import PersonRefHasher
from services.api.bus import InMemoryBus
from services.api.report import render_report_html
from services.api.schema import validate_zone_state
from services.api.service import IngestService
from services.api.store import InMemoryStore

PR = PersonRefHasher(session_salt="fixed").ref(1)


def _svc():
    return IngestService(InMemoryStore(), InMemoryBus())


class TestIngest(unittest.TestCase):
    def test_valid_event_accepted_and_published(self):
        svc = _svc()
        e = new_event(ZONE_ENTRY, "CAM-A-01", "SITE-1", 1.0,
                      zone_to="Z1", person_ref=PR, confidence=0.9)
        code, body = svc.ingest_event(e)
        self.assertEqual(code, 202)
        self.assertTrue(body["accepted"])
        self.assertEqual(svc.store.event_count(), 1)
        self.assertEqual(len(svc.bus.consume()), 1)

    def test_malformed_event_rejected_not_stored(self):
        svc = _svc()
        e = new_event(DENSITY_UPDATE, "CAM-A-01", "SITE-1", 1.0,
                      zone_id="Z1", occupancy=-3, density=0.1)
        code, body = svc.ingest_event(e)
        self.assertEqual(code, 422)
        self.assertFalse(body["accepted"])
        self.assertEqual(svc.store.event_count(), 0)
        self.assertEqual(len(svc.bus.consume()), 0)


class TestZoneState(unittest.TestCase):
    def _state(self, **over):
        base = dict(zone_id="Z1", camera_id="CAM-A-01", occupancy=3, density=0.05,
                    capacity_pct=7.5, inflow_per_min=1.0, outflow_per_min=0.0,
                    status="NORMAL", ts=100.0)
        base.update(over)
        return base

    def test_valid_state_accepted(self):
        svc = _svc()
        code, _ = svc.record_zone_state(self._state())
        self.assertEqual(code, 202)

    def test_bad_status_rejected(self):
        ok, errs = validate_zone_state(self._state(status="GREEN"))
        self.assertFalse(ok)

    def test_latest_state_and_range(self):
        svc = _svc()
        svc.record_zone_state(self._state(ts=100.0, occupancy=3))
        svc.record_zone_state(self._state(ts=105.0, occupancy=5))
        latest = svc.zone_states()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["occupancy"], 5)
        rng = svc.zone_state_range("Z1", 99.0, 101.0)
        self.assertEqual(len(rng), 1)
        self.assertEqual(rng[0]["occupancy"], 3)


class TestAlertsAck(unittest.TestCase):
    def test_ack_flow(self):
        svc = _svc()
        aid = svc.raise_alert({"rule_id": "R-02", "severity": "RED",
                               "message": "critical", "zone_id": "Z1", "ts": 10.0})
        self.assertEqual(len(svc.list_alerts(unacked_only=True)), 1)
        code, body = svc.acknowledge(aid, "operator-jane", ts=20.0)
        self.assertEqual(code, 200)
        self.assertEqual(body["acknowledged_by"], "operator-jane")
        self.assertEqual(len(svc.list_alerts(unacked_only=True)), 0)

    def test_double_ack_conflicts(self):
        svc = _svc()
        aid = svc.raise_alert({"rule_id": "R-02", "severity": "RED",
                               "message": "x", "ts": 10.0})
        svc.acknowledge(aid, "op", ts=20.0)
        code, _ = svc.acknowledge(aid, "op2", ts=21.0)
        self.assertEqual(code, 409)

    def test_ack_requires_who(self):
        svc = _svc()
        aid = svc.raise_alert({"rule_id": "R-02", "severity": "RED", "message": "x", "ts": 1.0})
        code, _ = svc.acknowledge(aid, "", ts=2.0)
        self.assertEqual(code, 400)


class TestMovementAndFilters(unittest.TestCase):
    def _tx(self, svc, frm, to, ts, pr="pr_x", cam="CAM-A"):
        svc.store.save_event(new_event(ZONE_TRANSITION, cam, "S", ts,
                                       zone_from=frm, zone_to=to, person_ref=pr))

    def test_movement_aggregates_transitions(self):
        svc = _svc()
        self._tx(svc, "Z1", "Z2", 100.0)
        self._tx(svc, "Z1", "Z2", 110.0)
        self._tx(svc, "Z2", "Z3", 120.0)
        flows = svc.movement(0, 1e12)
        by = {(f["zone_from"], f["zone_to"]): f["count"] for f in flows}
        self.assertEqual(by[("Z1", "Z2")], 2)
        self.assertEqual(by[("Z2", "Z3")], 1)

    def test_events_person_ref_filter(self):
        svc = _svc()
        self._tx(svc, "Z1", "Z2", 100.0, pr="pr_a")
        self._tx(svc, "Z1", "Z2", 101.0, pr="pr_b")
        got = svc.events_history(0, 1e12, person_ref="pr_a")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["person_ref"], "pr_a")


class TestZones(unittest.TestCase):
    def _payload(self, **over):
        base = {"camera_id": "CAM-A", "zones": [
            {"zone_id": "Z1", "zone_name": "Lobby", "zone_type": "MONITORED",
             "capacity_max": 40, "area_sqm": 60.0,
             "normalized_polygon": [[0.1, 0.3], [0.9, 0.3], [0.9, 0.7], [0.1, 0.7]]},
            {"zone_id": "Z2", "zone_name": "Bay", "zone_type": "RESTRICTED",
             "restricted": True, "area_sqm": 12.0,
             "normalized_polygon": [[0.7, 0.1], [0.9, 0.1], [0.9, 0.3]]},
        ]}
        base.update(over)
        return base

    def test_save_and_list_roundtrip(self):
        svc = _svc()
        code, body = svc.save_zones(self._payload())
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 2)
        zones = svc.list_zones("CAM-A")
        self.assertEqual(len(zones), 2)
        self.assertEqual({z["zone_id"] for z in zones}, {"Z1", "Z2"})
        self.assertEqual(zones[0]["camera_id"], "CAM-A")

    def test_save_replaces_previous(self):
        svc = _svc()
        svc.save_zones(self._payload())
        svc.save_zones(self._payload(zones=[
            {"zone_id": "Z9", "zone_name": "New", "zone_type": "MONITORED",
             "normalized_polygon": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}]))
        zones = svc.list_zones("CAM-A")
        self.assertEqual([z["zone_id"] for z in zones], ["Z9"])

    def test_reject_bad_zone(self):
        svc = _svc()
        code, body = svc.save_zones({"camera_id": "CAM-A", "zones": [
            {"zone_id": "Zx", "zone_type": "BOGUS", "normalized_polygon": [[0, 0]]}]})
        self.assertEqual(code, 422)
        self.assertFalse(body["saved"])

    def test_reject_missing_camera(self):
        svc = _svc()
        code, _ = svc.save_zones({"zones": []})
        self.assertEqual(code, 422)


class TestReport(unittest.TestCase):
    def test_report_html_has_no_hardcoded_hex(self):
        html_out = render_report_html(
            [{"zone_id": "Z1", "occupancy": 5, "density": 0.08, "capacity_pct": 12.5,
              "inflow_per_min": 1.0, "outflow_per_min": 0.5, "status": "WARNING"}],
            generated_at=0.0)
        self.assertIn("finblade-theme.css", html_out)
        self.assertIn("fb-num", html_out)          # tabular numerals class
        self.assertIn("fb-pill--warning", html_out)  # amber via theme class
        # No inline hex colour literals anywhere in the report body.
        import re
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}", html_out), [])


class TestCameraHealth(unittest.TestCase):
    def test_health_snapshot_stored_and_listed(self):
        svc = _svc()
        health = {"state": "ONLINE", "input_fps": 18.0, "resolution": (640, 480),
                  "dropped_frames": 3, "reconnects": 1, "frozen": False,
                  "enabled": True, "stream_url": "http://h:8080/stream"}
        code, body = svc.record_camera_health(
            {"camera_id": "CAM-A-01", "site_id": "SITE-1", "ts": 100.0, "health": health})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["control"]["simulate"])   # default: no simulate
        cams = {c["camera_id"]: c for c in svc.cameras()}
        c = cams["CAM-A-01"]
        self.assertEqual(c["state"], "ONLINE")
        self.assertEqual(c["input_fps"], 18.0)
        self.assertEqual(c["last_seen"], 100.0)

    def test_missing_camera_id_rejected(self):
        svc = _svc()
        code, body = svc.record_camera_health({"health": {"state": "ONLINE"}})
        self.assertEqual(code, 422)
        self.assertFalse(body["ok"])

    def test_simulate_flag_returned_as_control(self):
        svc = _svc()
        svc.record_camera_health({"camera_id": "CAM-A-01", "ts": 1.0,
                                  "health": {"state": "ONLINE"}})
        svc.set_camera_sim("CAM-A-01", True)
        _, body = svc.record_camera_health({"camera_id": "CAM-A-01", "ts": 2.0,
                                            "health": {"state": "ONLINE"}})
        self.assertTrue(body["control"]["simulate"])     # runner should fail-over
        svc.set_camera_sim("CAM-A-01", False)
        _, body = svc.record_camera_health({"camera_id": "CAM-A-01", "ts": 3.0,
                                            "health": {"state": "ONLINE"}})
        self.assertFalse(body["control"]["simulate"])

    def test_upsert_and_delete(self):
        svc = _svc()
        code, _ = svc.upsert_camera({"camera_id": "CAM-B-02", "name": "Dock",
                                     "site_id": "SITE-1"})
        self.assertEqual(code, 200)
        self.assertIn("CAM-B-02", {c["camera_id"] for c in svc.cameras()})
        code, body = svc.delete_camera("CAM-B-02")
        self.assertEqual(code, 200)
        self.assertNotIn("CAM-B-02", {c["camera_id"] for c in svc.cameras()})
        code, _ = svc.delete_camera("CAM-B-02")   # already gone
        self.assertEqual(code, 404)

    def test_upsert_requires_camera_id(self):
        svc = _svc()
        code, body = svc.upsert_camera({"name": "no id"})
        self.assertEqual(code, 422)


if __name__ == "__main__":
    unittest.main()
