"""GPS trackers: device dialects, geofences, silence, and the HTTP path.

  * finblade/gps.py     — parse OsmAnd / OpenGTS-gprmc / JSON; haversine;
                          the arrive/depart state machine with hysteresis
  * the store contract  — positions history + live row on both backends
  * IngestService       — register, ingest -> events, R-12 silence rule
  * the HTTP routes     — Traccar Client's real request shape lands

Fixture geography: Riyadh Main Lab at 24.7136, 46.6753 with a 150 m fence.
0.001° of latitude is ~111 m; 0.01° is ~1.1 km.
"""

import os
import sys
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade import gps
from finblade.events import TRACKER_ARRIVED, TRACKER_DEPARTED
from services.api.service import IngestService
from services.api.store import InMemoryStore
from tests import pgfixture

RUH = (24.7136, 46.6753)
JED = (21.4858, 39.1925)
T0 = 1_800_000_000.0


def pos(tid="V-1", lat=RUH[0], lon=RUH[1], ts=T0, **kw):
    return gps.Position(tracker_id=tid, ts=ts, lat=lat, lon=lon, **kw)


# ---------------------------------------------------------------- parsing --
class TestDialects(unittest.TestCase):
    def test_osmand_as_traccar_client_sends_it(self):
        # Speed in KNOTS on the wire, km/h in the row.
        p, errs = gps.parse_osmand({"id": "V-1", "timestamp": "1800000000", "lat": "24.7136",
                                    "lon": "46.6753", "speed": "10", "bearing": "90",
                                    "altitude": "612", "accuracy": "8", "batt": "77",
                                    "driverUniqueId": "someone"}, now=T0 + 5)
        self.assertEqual([], errs)
        self.assertEqual(("V-1", T0, 24.7136, 46.6753), (p.tracker_id, p.ts, p.lat, p.lon))
        self.assertEqual(18.5, p.speed_kmh)
        self.assertEqual((90.0, 612.0, 8.0, 77.0), (p.heading, p.altitude_m, p.accuracy_m, p.battery_pct))
        self.assertEqual("osmand", p.dialect)
        self.assertNotIn("driver", str(p.to_dict()).lower(), "no driver identity, ever")

    def test_osmand_milliseconds_and_missing_timestamp(self):
        p, _ = gps.parse_osmand({"id": "V-1", "lat": "24.7", "lon": "46.7",
                                 "timestamp": str(int(T0 * 1000))}, now=T0 + 99)
        self.assertEqual(T0, p.ts)
        p, _ = gps.parse_osmand({"id": "V-1", "lat": "24.7", "lon": "46.7"}, now=T0 + 99)
        self.assertEqual(T0 + 99, p.ts, "no device time = server time")

    def test_osmand_rejects_no_fix_and_bad_ids(self):
        for params, want in (({"id": "V-1", "lat": "0", "lon": "0"}, "not a fix"),
                             ({"id": "V 1", "lat": "24", "lon": "46"}, "id is required"),
                             ({"id": "V-1", "lat": "95", "lon": "46"}, "out of range"),
                             ({"id": "V-1"}, "required numbers")):
            p, errs = gps.parse_osmand(params, now=T0)
            self.assertIsNone(p)
            self.assertTrue(any(want in e for e in errs), (params, errs))

    def test_gprmc_as_opengts_accepts_it(self):
        s = "$GPRMC,120000.00,A,2442.816,N,04640.518,E,5.0,180.0,150926,,,A*6A"
        p, errs = gps.parse_gprmc({"dev": "TK-9", "acct": "wareed", "gprmc": s}, now=T0)
        self.assertEqual([], errs)
        self.assertAlmostEqual(24.7136, p.lat, places=3)
        self.assertAlmostEqual(46.6753, p.lon, places=3)
        self.assertEqual(9.3, p.speed_kmh)
        self.assertEqual(180.0, p.heading)
        self.assertEqual("gprmc", p.dialect)
        # 2026-09-15 12:00:00 UTC
        self.assertEqual(1789473600.0, p.ts)

    def test_gprmc_no_fix_is_rejected(self):
        s = "$GPRMC,120000.00,V,,,,,,,150926,,,N*53"
        p, errs = gps.parse_gprmc({"dev": "TK-9", "gprmc": s}, now=T0)
        self.assertIsNone(p)
        self.assertTrue(any("no fix" in e for e in errs))

    def test_json_dialect_speed_is_kmh(self):
        p, errs = gps.parse_json({"tracker_id": "V-1", "lat": 24.7, "lon": 46.7,
                                  "speed_kmh": 42.0, "ts": T0}, now=T0 + 1)
        self.assertEqual([], errs)
        self.assertEqual(42.0, p.speed_kmh)
        self.assertEqual("json", p.dialect)


class TestGeometry(unittest.TestCase):
    def test_haversine_riyadh_to_jeddah(self):
        d = gps.haversine_m(*RUH, *JED)
        self.assertAlmostEqual(848_000, d, delta=8_000)   # ~848 km great-circle

    def test_hundred_metres(self):
        self.assertAlmostEqual(111.0, gps.haversine_m(RUH[0], RUH[1], RUH[0] + 0.001, RUH[1]), delta=1.0)


# --------------------------------------------------------------- geofence --
class TestGeofence(unittest.TestCase):
    def setUp(self):
        self.geo = gps.GeofenceEngine([gps.Fence("RUH-01", *RUH, 150.0)])

    def drive(self, lats, tid="V-1"):
        out = []
        for i, dlat in enumerate(lats):
            out += self.geo.observe(pos(tid, RUH[0] + dlat, RUH[1], T0 + i * 10))
        return [(t.kind, t.branch_id) for t in out]

    def test_one_reading_inside_does_not_arrive_two_do(self):
        self.assertEqual([], self.drive([0.0]))
        self.assertEqual([("ARRIVED", "RUH-01")], self.drive([0.0]))
        self.assertEqual("RUH-01", self.geo.at("V-1")[0])

    def test_a_drive_past_produces_nothing(self):
        # 1 km out, one fix inside, 1 km out the other side.
        self.assertEqual([], self.drive([-0.01, 0.0, 0.01, 0.02]))

    def test_hysteresis_holds_a_vehicle_parked_on_the_boundary(self):
        self.drive([0.0, 0.0])                       # arrived
        # 160 m and 200 m are outside 150 but inside 225 (150 * 1.5): dead band.
        self.assertEqual([], self.drive([0.00145, 0.0018, 0.00145, 0.0018]))
        self.assertEqual("RUH-01", self.geo.at("V-1")[0])

    def test_departure_needs_two_readings_beyond_the_exit_radius_and_reports_dwell(self):
        self.drive([0.0, 0.0])
        out = []
        for i, dlat in enumerate([0.003, 0.003]):    # ~330 m
            out += self.geo.observe(pos("V-1", RUH[0] + dlat, RUH[1], T0 + 600 + i * 10))
        self.assertEqual(1, len(out))
        self.assertEqual(("DEPARTED", "RUH-01"), (out[0].kind, out[0].branch_id))
        self.assertAlmostEqual(600.0, out[0].dwell_s, delta=1.0)
        self.assertEqual((None, None), self.geo.at("V-1"))

    def test_moving_straight_from_one_branch_to_another(self):
        self.geo.set_fences([gps.Fence("A", *RUH, 150.0),
                             gps.Fence("B", RUH[0] + 0.005, RUH[1], 150.0)])   # 550 m apart
        self.assertEqual([("ARRIVED", "A")], self.drive([0.0, 0.0]))
        self.assertEqual([("DEPARTED", "A"), ("ARRIVED", "B")], self.drive([0.005, 0.005]))

    def test_restore_after_restart_does_not_re_arrive(self):
        self.geo.restore("V-1", "RUH-01", T0 - 3600)
        self.assertEqual([], self.drive([0.0, 0.0, 0.0]))
        self.assertEqual(("RUH-01", T0 - 3600), self.geo.at("V-1"))

    def test_trackers_are_independent(self):
        self.assertEqual([("ARRIVED", "RUH-01")], self.drive([0.0, 0.0], tid="V-1"))
        self.assertEqual([], self.drive([0.0], tid="V-2"))

    def test_fences_come_only_from_placed_branches(self):
        fences = gps.fences_from_branches([
            {"branch_id": "A", "lat": 1.0, "lon": 2.0, "geofence_m": 300},
            {"branch_id": "B", "lat": None, "lon": None},
            {"branch_id": "C", "lat": 3.0, "lon": 4.0}])
        self.assertEqual([("A", 300.0), ("C", gps.DEFAULT_GEOFENCE_M)],
                         [(f.branch_id, f.radius_m) for f in fences])


# ---------------------------------------------------------- store contract --
class GpsStoreContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def test_tracker_round_trip_and_delete(self):
        self.store.save_tracker({"tracker_id": "V-1", "name": "Van 1", "kind": "GPS",
                                 "asset_type": "VEHICLE", "asset_label": "ABC 1234",
                                 "home_branch_id": "RUH-01", "enabled": True})
        self.store.save_tracker({"tracker_id": "V-1", "name": "Van One", "kind": "GPS",
                                 "asset_type": "VEHICLE", "home_branch_id": "RUH-01",
                                 "enabled": False})
        rows = self.store.list_trackers()
        self.assertEqual(1, len(rows))
        self.assertEqual(("Van One", False, "RUH-01"),
                         (rows[0]["name"], rows[0]["enabled"], rows[0]["home_branch_id"]))
        self.assertTrue(self.store.delete_tracker("V-1"))
        self.assertFalse(self.store.delete_tracker("V-1"))

    def test_positions_history_and_live_row(self):
        for i in range(5):
            self.store.save_position(pos(ts=T0 + i * 10, lat=RUH[0] + i * 0.001).to_dict(),
                                     at_branch_id="RUH-01" if i < 2 else None,
                                     at_since=T0 if i < 2 else None)
        hist = self.store.positions_range("V-1", T0, T0 + 100)
        self.assertEqual([T0 + i * 10 for i in range(5)], [r["ts"] for r in hist])
        self.assertEqual("RUH-01", hist[0]["site_id"])
        self.assertIsNone(hist[4]["site_id"])
        live = self.store.latest_positions()
        self.assertEqual(1, len(live))
        self.assertEqual(T0 + 40, live[0]["ts"])
        self.assertEqual(5, int(live[0]["positions"]))
        self.assertIsNone(live[0]["at_branch_id"])
        self.assertEqual([], self.store.positions_range("V-9", T0, T0 + 100))

    def test_an_unregistered_reporter_can_be_removed(self):
        self.store.save_position(pos("PHONE-7", ts=T0).to_dict())
        self.assertEqual(1, len(self.store.latest_positions()))
        self.assertTrue(self.store.delete_tracker("PHONE-7"), "no trackers row, but it was listed")
        self.assertEqual([], self.store.latest_positions())
        self.assertFalse(self.store.delete_tracker("PHONE-7"))

    def test_a_late_report_does_not_rewind_the_live_row(self):
        self.store.save_position(pos(ts=T0 + 50, lat=RUH[0] + 0.005).to_dict())
        self.store.save_position(pos(ts=T0 + 10, lat=RUH[0]).to_dict())
        live = self.store.latest_positions()[0]
        self.assertEqual(T0 + 50, live["ts"])
        self.assertAlmostEqual(RUH[0] + 0.005, live["lat"])
        self.assertEqual(2, int(live["positions"]), "still counted")
        self.assertEqual(2, len(self.store.positions_range("V-1", T0, T0 + 100)))

    def test_retention_prunes_positions(self):
        self.store.save_position(pos(ts=T0).to_dict())
        self.store.save_position(pos(ts=T0 + 1000).to_dict())
        deleted = self.store.delete_before(T0 + 500)
        self.assertEqual(1, deleted["tracker_positions"])


class TestInMemoryGpsStore(GpsStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


@pgfixture.skip_without_pg
class TestPostgresGpsStore(GpsStoreContract, unittest.TestCase):
    def make_store(self):
        store, teardown = pgfixture.make_store("gps")
        self.addCleanup(teardown)
        return store


# ---------------------------------------------------------------- service --
ORG = {"regions": [{"region_id": "CENTRAL", "cities": [{"city_id": "RUH", "branches": [
    {"branch_id": "RUH-01", "name": "Riyadh Main Lab", "lat": RUH[0], "lon": RUH[1]},
    {"branch_id": "RUH-02", "name": "Unplaced"}]}]}]}


class TestService(unittest.TestCase):
    def setUp(self):
        self.svc = IngestService(InMemoryStore())
        self.assertEqual(200, self.svc.import_org(ORG)[0])

    def test_register_validates(self):
        code, body = self.svc.register_tracker({"tracker_id": "V 1"})
        self.assertEqual(422, code)
        code, body = self.svc.register_tracker({"tracker_id": "V-1", "home_branch_id": "NOPE"})
        self.assertEqual(422, code)
        code, body = self.svc.register_tracker({"tracker_id": "V-1", "name": "Van 1",
                                                "home_branch_id": "RUH-01",
                                                "asset_label": "ABC 1234"})
        self.assertEqual(200, code, body)
        t = self.svc.trackers(now=T0)[0]
        self.assertEqual(("V-1", True, "NEVER_SEEN", "RUH-01"),
                         (t["tracker_id"], t["registered"], t["state"], t["site_id"]))

    def test_ingest_emits_arrival_and_departure_events(self):
        self.svc.register_tracker({"tracker_id": "V-1", "home_branch_id": "RUH-01"})
        r = [self.svc.ingest_position(pos(ts=T0 + i * 10)) for i in range(2)]
        self.assertEqual([202, 202], [c for c, _ in r])
        self.assertEqual([], r[0][1]["events"])
        self.assertEqual(["TRACKER_ARRIVED"], r[1][1]["events"])
        self.assertEqual("RUH-01", r[1][1]["at_branch_id"])
        for i in range(2):
            code, body = self.svc.ingest_position(pos(ts=T0 + 600 + i * 10, lat=RUH[0] + 0.01))
        self.assertEqual(["TRACKER_DEPARTED"], body["events"])
        evs = sorted(self.svc.events_history(0, T0 + 10_000), key=lambda e: e["timestamp"])
        kinds = [e["event_type"] for e in evs]
        self.assertEqual([TRACKER_ARRIVED, TRACKER_DEPARTED], kinds)
        dep = evs[1]
        self.assertEqual(("V-1", "RUH-01", "V-1"), (dep["tracker_id"], dep["branch_id"], dep["camera_id"]))
        self.assertAlmostEqual(600.0, dep["dwell_s"], delta=1.0)
        self.assertEqual([], self.svc.cameras(), "a tracker event must not mint a camera row")

    def test_unregistered_reporter_is_kept_and_labelled(self):
        self.svc.ingest_position(pos("PHONE-7", ts=T0))
        t = self.svc.trackers(now=T0 + 5)[0]
        self.assertEqual(("PHONE-7", False, "STOPPED"), (t["tracker_id"], t["registered"], t["state"]))
        self.assertAlmostEqual(5.0, t["seconds_since_seen"], delta=0.2)

    def test_state_reflects_speed_and_silence(self):
        self.svc.ingest_position(pos(ts=T0, speed_kmh=40.0))
        self.assertEqual("MOVING", self.svc.trackers(now=T0 + 1)[0]["state"])
        self.assertEqual("OFFLINE", self.svc.trackers(now=T0 + 301)[0]["state"])

    def test_r12_fires_once_and_clears_on_the_next_report(self):
        self.svc.register_tracker({"tracker_id": "V-1", "name": "Van 1", "home_branch_id": "RUH-01"})
        self.svc.ingest_position(pos(ts=T0))
        self.assertEqual([], self.svc.check_silent_trackers(now=T0 + 200))
        self.assertEqual(["V-1"], self.svc.check_silent_trackers(now=T0 + 400))
        self.assertEqual([], self.svc.check_silent_trackers(now=T0 + 500), "once")
        open_ = [a for a in self.svc.list_alerts() if a["rule_id"] == "R-12"]
        self.assertEqual(1, len(open_))
        self.assertEqual(("AMBER", "V-1", "RUH-01"),
                         (open_[0]["severity"], open_[0]["camera_id"], open_[0]["site_id"]))
        self.assertIn("Van 1", open_[0]["message"])
        self.svc.ingest_position(pos(ts=T0 + 600))
        self.assertEqual([], [a for a in self.svc.list_alerts() if a["rule_id"] == "R-12"
                              and a["kind"] != "CLEAR"])

    def test_r12_survives_a_restart_without_re_raising_and_folds_old_duplicates(self):
        # The dedupe flag lived in memory: every API restart re-raised R-12
        # for a phone that had been off for days — 200 open alerts on the
        # Wareed instance. The store is the record that survives.
        self.svc.register_tracker({"tracker_id": "V-1", "name": "Van 1", "home_branch_id": "RUH-01"})
        self.svc.ingest_position(pos(ts=T0))
        self.assertEqual(["V-1"], self.svc.check_silent_trackers(now=T0 + 400))
        # "restart": a fresh service over the same store
        from services.api.service import IngestService
        svc2 = IngestService(self.svc.store)
        self.assertEqual([], svc2.check_silent_trackers(now=T0 + 800), "already open in the store")
        r12 = [a for a in svc2.list_alerts() if a["rule_id"] == "R-12" and a["kind"] != "CLEAR"]
        self.assertEqual(1, len(r12))
        # and the duplicates that already exist from earlier restarts fold into the first
        for i in range(3):
            svc2.raise_alert({"rule_id": "R-12", "severity": "AMBER", "kind": "FIRE", "camera_id": "V-1",
                              "message": "dup", "ts": T0 + 900 + i})
        self.assertEqual(4, len([a for a in svc2.list_alerts() if a["rule_id"] == "R-12" and a["kind"] != "CLEAR"]))
        self.assertEqual(3, svc2.dedupe_silent_tracker_alerts(now=T0 + 1000))
        left = [a for a in svc2.list_alerts() if a["rule_id"] == "R-12" and a["kind"] != "CLEAR"]
        self.assertEqual(1, len(left))
        self.assertAlmostEqual(T0 + 400, float(left[0]["ts"]), delta=1.0, msg="the earliest survives")
        # recovery still clears it
        svc2.ingest_position(pos(ts=T0 + 1100))
        self.assertEqual([], [a for a in svc2.list_alerts() if a["rule_id"] == "R-12" and a["kind"] != "CLEAR"])

    def test_never_seen_and_disabled_trackers_never_alert(self):
        self.svc.register_tracker({"tracker_id": "V-NEW"})
        self.svc.register_tracker({"tracker_id": "V-OFF", "enabled": False})
        self.svc.ingest_position(pos("V-OFF", ts=T0))
        self.assertEqual([], self.svc.check_silent_trackers(now=T0 + 9999))

    def test_track_window(self):
        for i in range(10):
            self.svc.ingest_position(pos(ts=T0 + i * 60, lat=RUH[0] + i * 0.01))
        rows = self.svc.tracker_track("V-1", T0 + 120, T0 + 300)
        self.assertEqual([T0 + 120, T0 + 180, T0 + 240, T0 + 300], [r["ts"] for r in rows])


# ------------------------------------------------------------- HTTP routes --
try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc as app_svc
    HAVE_APP = True
except Exception as _exc:                      # noqa: BLE001
    HAVE_APP = False


@unittest.skipUnless(HAVE_APP, "fastapi app not importable")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        for t in list(app_svc.store.list_trackers()):
            app_svc.store.delete_tracker(t["tracker_id"])
        app_svc.store._tracker_live.clear()
        app_svc.store._positions.clear()
        self.c.post("/api/v1/org/import", json=ORG)

    def test_traccar_client_post_with_query_string_and_empty_body(self):
        r = self.c.post("/api/v1/trackers/ingest?id=V-1&timestamp=1800000000&lat=24.7136"
                        "&lon=46.6753&speed=0.0&bearing=0.0&altitude=600&batt=90")
        self.assertEqual(202, r.status_code, r.text)
        self.assertEqual("V-1", r.json()["tracker_id"])
        r = self.c.get("/api/v1/trackers")
        t = r.json()["trackers"][0]
        self.assertEqual(24.7136, t["position"]["lat"])
        self.assertEqual(90.0, t["position"]["battery_pct"])

    def test_gprmc_and_json_and_form_bodies(self):
        s = "$GPRMC,120000.00,A,2442.816,N,04640.518,E,0.0,0.0,150926,,,A*6A"
        r = self.c.get(f"/api/v1/trackers/ingest?dev=TK-9&gprmc={s}")
        self.assertEqual(202, r.status_code, r.text)
        r = self.c.post("/api/v1/trackers/ingest",
                        json={"tracker_id": "WEB-1", "lat": 24.7, "lon": 46.7, "speed_kmh": 5})
        self.assertEqual(202, r.status_code, r.text)
        r = self.c.post("/api/v1/trackers/ingest", data={"id": "FORM-1", "lat": "24.7", "lon": "46.7"})
        self.assertEqual(202, r.status_code, r.text)
        ids = sorted(t["tracker_id"] for t in self.c.get("/api/v1/trackers").json()["trackers"])
        self.assertEqual(["FORM-1", "TK-9", "WEB-1"], ids)

    def test_no_fix_is_422(self):
        r = self.c.post("/api/v1/trackers/ingest?id=V-1&lat=0&lon=0")
        self.assertEqual(422, r.status_code)

    def test_register_list_scope_track_delete(self):
        r = self.c.post("/api/v1/trackers", json={"tracker_id": "V-1", "name": "Van 1",
                                                  "home_branch_id": "RUH-01"})
        self.assertEqual(200, r.status_code, r.text)
        self.c.post("/api/v1/trackers", json={"tracker_id": "V-2", "name": "Van 2"})
        for i in range(3):
            self.c.post(f"/api/v1/trackers/ingest?id=V-1&lat={24.7 + i * 0.01}&lon=46.7"
                        f"&timestamp={int(T0) + i * 60}")
        ids = lambda r: sorted(t["tracker_id"] for t in r.json()["trackers"])  # noqa: E731
        self.assertEqual(["V-1", "V-2"], ids(self.c.get("/api/v1/trackers")))
        self.assertEqual(["V-1"], ids(self.c.get("/api/v1/trackers?region_id=CENTRAL")))
        tr = self.c.get(f"/api/v1/trackers/V-1/track?from={T0}&to={T0 + 1000}").json()
        self.assertEqual(3, len(tr["positions"]))
        self.assertEqual(200, self.c.delete("/api/v1/trackers/V-2").status_code)
        self.assertEqual(404, self.c.delete("/api/v1/trackers/V-2").status_code)
        s = self.c.get("/api/v1/summary?charts=0").json()
        self.assertEqual(["V-1"], [t["tracker_id"] for t in s["trackers"]])


if __name__ == "__main__":
    unittest.main()
