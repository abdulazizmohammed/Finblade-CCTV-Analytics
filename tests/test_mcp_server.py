"""The MCP server: every tool reachable, correct against the in-memory API,
and the bearer gate in front of the HTTP transport.

Tools are exercised through MCPServer.call_tool — the same validation and
result shaping an MCP client goes through — with the Backend swapped for
FastAPI's TestClient so nothing opens a socket. The transport test starts the
real Starlette app under the TestClient and speaks JSON-RPC to /mcp.
"""

import asyncio
import json
import os
import sys
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc as app_svc
    from services.mcp.server import RULES, TestClientBackend, build_server, make_app
    HAVE = True
except Exception as _exc:                      # noqa: BLE001
    HAVE = False
    IMPORT_ERROR = _exc

T0 = time.time()
RUH = (24.7136, 46.6753)
ORG = {"tenant": {"name": "Wareed Medical Laboratories", "short": "Wareed", "country": "KSA"},
       "regions": [{"region_id": "CENTRAL", "name": "Central", "cities": [{"city_id": "RUH", "name": "Riyadh", "branches": [
           {"branch_id": "RUH-01", "name": "Riyadh Main Lab", "lat": RUH[0], "lon": RUH[1]}]}]},
           {"region_id": "WESTERN", "name": "Western", "cities": [{"city_id": "JED", "name": "Jeddah", "branches": [
               {"branch_id": "JED-01", "name": "Jeddah Main Lab", "lat": 21.4858, "lon": 39.1925}]}]}]}


def run(coro):
    # A fresh loop per call: get_event_loop() fails once another suite in the
    # same process has closed the default loop, and the server keeps no
    # loop-bound state between calls.
    return asyncio.run(coro)


def payload(result):
    """The JSON a client would parse out of a text tool result."""
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


_SEEDED = {}


def tearDownModule():
    """Leave the process-wide store as we found it: other suites (test_org,
    test_gps) assume no alerts, cameras or trackers of ours are lying around."""
    if not HAVE or not _SEEDED:
        return
    app_svc.store.delete_alerts("all")
    for camr in list(app_svc.store.list_cameras()):
        app_svc.store.delete_camera(camr["camera_id"])
    for t in list(app_svc.store.list_trackers()):
        app_svc.store.delete_tracker(t["tracker_id"])


@unittest.skipUnless(HAVE, "app/mcp not importable")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The app's store is process-wide and every class here shares it, so
        # the world is built once and reused; seeding twice doubled the alerts.
        if _SEEDED:
            cls.c, cls.server = _SEEDED["c"], _SEEDED["server"]
            return
        cls.c = TestClient(app)
        cls.server = build_server(TestClientBackend(cls.c))
        _SEEDED.update(c=cls.c, server=cls.server)
        # a small, known world — other suites in the same process leave
        # cameras, trackers and alerts behind, so clear those first
        for camr in list(app_svc.store.list_cameras()):
            app_svc.store.delete_camera(camr["camera_id"])
        for t in list(app_svc.store.list_trackers()):
            app_svc.store.delete_tracker(t["tracker_id"])
        app_svc.store.delete_alerts("all")
        assert cls.c.post("/api/v1/org/import", json=ORG).status_code == 200
        for cid, site in (("CAM-R1", "RUH-01"), ("CAM-J1", "JED-01")):
            cls.c.post("/api/v1/cameras", json={"camera_id": cid, "site_id": site, "name": f"{cid} lobby"})
        cls.c.post("/api/v1/cameras/health", json={"camera_id": "CAM-R1", "site_id": "RUH-01", "ts": T0,
                                                    "health": {"state": "ONLINE", "input_fps": 12.0, "people_in_view": 3}})
        cls.c.post("/api/v1/zones", json={"camera_id": "CAM-R1", "zones": [
            {"zone_id": "LOBBY", "zone_name": "Lobby", "zone_type": "MONITORED", "capacity_max": 20, "area_sqm": 40,
             "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]},
            {"zone_id": "STORE", "zone_name": "Sample store", "zone_type": "RESTRICTED", "restricted": True,
             "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}]})
        for zid, occ, st, restricted in (("LOBBY", 4, "WARNING", False), ("STORE", 1, "NORMAL", True)):
            cls.c.post("/api/v1/zones/state", json={
                "zone_id": zid, "camera_id": "CAM-R1", "site_id": "RUH-01", "zone_name": zid.title(),
                "occupancy": occ, "density": occ / 40.0, "capacity_pct": occ * 5.0, "status": st,
                "restricted": restricted, "inflow_per_min": 1.0, "outflow_per_min": 0.5, "ts": time.time()})
        app_svc.raise_alert({"rule_id": "R-01", "severity": "AMBER", "message": "density warning in LOBBY",
                             "zone_id": "LOBBY", "camera_id": "CAM-R1", "site_id": "RUH-01", "ts": T0, "kind": "FIRE"})
        app_svc.raise_alert({"rule_id": "R-06", "severity": "RED", "message": "intrusion in STORE",
                             "zone_id": "STORE", "camera_id": "CAM-R1", "site_id": "RUH-01", "ts": T0, "kind": "FIRE"})
        cls.c.post("/api/v1/trackers", json={"tracker_id": "VAN-1", "name": "Van 1", "home_branch_id": "RUH-01"})
        for i in range(2):
            cls.c.post("/api/v1/trackers/ingest", json={"tracker_id": "VAN-1", "lat": RUH[0], "lon": RUH[1],
                                                         "speed_kmh": 0, "ts": T0 - 100 + i * 10})

    def setUp(self):
        # The camera's heartbeat ages out after 30 s of wall clock; in a full
        # run this class starts minutes after the seed. Refresh it per test.
        self.c.post("/api/v1/cameras/health", json={"camera_id": "CAM-R1", "site_id": "RUH-01", "ts": time.time(),
                                                     "health": {"state": "ONLINE", "input_fps": 12.0, "people_in_view": 3}})

    def call(self, name, **args):
        return payload(run(self.server.call_tool(name, args)))


class TestSurface(Base):
    EXPECTED = {
        "network_overview", "org_index", "branch", "summary",
        "cameras", "camera", "camera_snapshot",
        "zones_live", "zone_config", "restricted_zones", "zone_history", "zone_at_time", "zone_duration", "zone_movement",
        "areas_live", "facility_occupancy", "people_counts",
        "alerts_active", "alerts_history", "alert", "incident_frame", "acknowledge_alert", "resolve_alert",
        "events_history", "occupancy_report", "reports_list",
        "vehicles", "vehicle", "vehicle_track", "vehicle_arrivals", "vehicles_at_branch",
        "find_people", "person_timeline",
        "system_health", "rules_reference",
    }

    def test_every_tool_is_registered_with_a_description(self):
        tools = run(self.server.list_tools())
        names = {t.name for t in tools}
        self.assertEqual(self.EXPECTED, names)
        for t in tools:
            self.assertTrue(len(t.description or "") > 40, f"{t.name} needs a real description")

    def test_the_caveats_are_in_the_descriptions_not_just_the_docs(self):
        desc = {t.name: t.description for t in run(self.server.list_tools())}
        self.assertIn("coverage", desc["zone_history"])
        self.assertIn("null", desc["zone_history"].lower())
        self.assertIn("unique only WITHIN a camera", desc["zone_history"])
        self.assertIn("never a person", desc["vehicles"])
        self.assertIn("double count", desc["cameras"])
        self.assertIn("never an identity", desc["find_people"])
        self.assertIn("gender, age or ethnicity", desc["find_people"])

    def test_find_people_runs_through_the_api(self):
        from finblade.events import PERSON_ATTRIBUTES, new_event
        e = new_event(PERSON_ATTRIBUTES, "CAM-R1", "RUH-01", time.time() - 30, person_ref="pr_" + "c" * 16,
                      attributes={"upper_colour": "blue", "headwear": "cap"},
                      confidences={"upper_colour": 0.9}, samples=3, description="blue top, cap")
        e["global_ref"] = "gp_mcp"
        self.assertEqual(202, self.c.post("/api/v1/events/ingest", json=e).status_code)
        r = self.call("find_people", upper_colour="blue", hours=1)
        self.assertEqual(1, r["count"])
        self.assertEqual("blue top, cap", r["people"][0]["description"])
        self.assertEqual(0, self.call("find_people", upper_colour="blue", region_id="WESTERN", hours=1)["count"])
        self.assertEqual(1, self.call("person_timeline", global_ref="gp_mcp", hours=1)["count"])

    def test_resources_and_prompt(self):
        res = run(self.server.list_resources())
        self.assertEqual({"finblade://data-notes", "finblade://rules", "finblade://capabilities"},
                         {str(r.uri) for r in res})
        notes = run(self.server.read_resource("finblade://data-notes"))
        self.assertIn("NOT a person", list(notes)[0].content)
        rules = json.loads(list(run(self.server.read_resource("finblade://rules")))[0].content)
        self.assertEqual(set(RULES), set(rules["rules"]))
        p = run(self.server.get_prompt("cctv_analyst", {"tenant": "Wareed"}))
        self.assertIn("Wareed", p.messages[0].content.text)
        self.assertIn("Never guess a number", p.messages[0].content.text)


class TestTools(Base):
    def test_network_and_branch(self):
        t = self.call("network_overview")
        self.assertEqual("Wareed Medical Laboratories", t["meta"]["tenant_name"])
        self.assertEqual(2, t["rollup"]["cameras"])
        b = self.call("branch", branch_id="RUH-01")
        self.assertEqual(("Central", "Riyadh"), (b["region"], b["city"]))
        self.assertEqual(["CAM-R1"], [c["camera_id"] for c in b["cameras"]])
        self.assertEqual(["VAN-1"], [v["tracker_id"] for v in b["vehicles_present"]])
        # Other suites import their own org trees into the same store, so
        # membership, not an exact count.
        self.assertLessEqual({"RUH-01", "JED-01"},
                             {x["branch_id"] for x in self.call("org_index")["branches"]})

    def test_scope_narrows_cameras_zones_alerts_vehicles(self):
        self.assertEqual(2, self.call("cameras")["count"])
        self.assertEqual(["CAM-J1"], [c["camera_id"] for c in self.call("cameras", region_id="WESTERN")["cameras"]])
        self.assertEqual(1, self.call("cameras", state="ONLINE")["count"])
        self.assertEqual(0, self.call("zones_live", region_id="WESTERN")["count"])
        z = self.call("zones_live", camera_id="CAM-R1")
        self.assertEqual(2, z["count"]); self.assertEqual(1, z["not_normal"])
        self.assertTrue(all(x["site_id"] == "RUH-01" for x in self.call("zones_live", branch_id="RUH-01")["zones"]))
        # camera-scoped: zone_live rows from other suites share the store
        self.assertEqual(1, self.call("zones_live", camera_id="CAM-R1", status="WARNING")["count"])
        self.assertEqual(0, self.call("alerts_active", city_id="JED")["count"])
        a = self.call("alerts_active", branch_id="RUH-01")
        # The RED one may already be dismissed by the lifecycle test; the
        # AMBER one is never touched.
        self.assertEqual(1, a["by_severity"].get("AMBER"))
        self.assertEqual(a["count"], sum(a["by_severity"].values()))
        self.assertEqual(1, self.call("vehicles", region_id="CENTRAL")["count"])
        self.assertEqual(0, self.call("vehicles", region_id="WESTERN")["count"])
        self.assertEqual("RUH-01", self.call("summary", branch_id="RUH-01")["scope"]["branch_id"])

    def test_restricted_zones_report_intrusions(self):
        r = self.call("restricted_zones")
        self.assertEqual(["STORE"], [z["zone_id"] for z in r["restricted_zones"]])
        self.assertEqual(["STORE"], [z["zone_id"] for z in r["intrusions_now"]])

    def test_camera_and_zone_config(self):
        c = self.call("camera", camera_id="CAM-R1")
        self.assertEqual(3, c["camera"]["people_in_view"])
        self.assertEqual(2, len(c["zones"]))
        cfg = self.call("zone_config", camera_id="CAM-R1")
        self.assertEqual({"LOBBY", "STORE"}, {z["zone_id"] for z in cfg["zones"]})
        self.assertNotIn("polygon", cfg["zones"][0], "geometry is noise to a chatbot")

    def test_history_tools_answer(self):
        h = self.call("zone_history", zone_id="LOBBY", camera_id="CAM-R1", hours=1, bucket_seconds=60)
        self.assertIn("coverage", h)
        at = self.call("zone_at_time", zone_id="LOBBY", camera_id="CAM-R1", ts=time.time())
        self.assertEqual(4, at["state"]["occupancy"])
        self.assertTrue(at["trustworthy"])
        d = self.call("zone_duration", zone_id="LOBBY", camera_id="CAM-R1", field="occupancy", op="gt", value=2, hours=1)
        self.assertIn("seconds", json.dumps(d))
        self.assertIn("flows", self.call("zone_movement", minutes=30))
        self.assertIn("zones", self.call("occupancy_report", hours=1))
        self.assertIn("reports", self.call("reports_list"))
        ev = self.call("events_history", hours=1, event_type="TRACKER_ARRIVED")
        self.assertEqual(["VAN-1"], [e["camera_id"] for e in ev["events"]])

    def test_facility_areas_counts_health_rules(self):
        self.assertIn("occupancy", self.call("facility_occupancy"))
        self.assertIn("areas", self.call("areas_live"))
        self.assertIn("live", self.call("people_counts"))
        self.assertIn("healthy", self.call("system_health"))
        self.assertEqual("AMBER", self.call("rules_reference")["rules"]["R-12"].split("—")[1].split(";")[0].strip())

    def test_vehicles(self):
        v = self.call("vehicle", tracker_id="VAN-1")
        self.assertEqual("RUH-01", v["at_branch_id"])
        self.assertEqual(2, len(self.call("vehicle_track", tracker_id="VAN-1", minutes=60)["positions"]))
        self.assertEqual(["VAN-1"], [x["tracker_id"] for x in self.call("vehicles_at_branch", branch_id="RUH-01")["vehicles"]])
        arr = self.call("vehicle_arrivals", hours=1)
        self.assertEqual(["TRACKER_ARRIVED"], [e["event_type"] for e in arr["events"]])

    def test_alert_lifecycle_through_tools(self):
        red = next(a for a in self.call("alerts_active")["alerts"]
                   if a["severity"] == "RED" and a["zone_id"] == "STORE")
        one = self.call("alert", alert_id=red["alert_id"])
        self.assertEqual("R-06", one["rule_id"])
        self.assertTrue(self.call("acknowledge_alert", alert_id=red["alert_id"], by="ops")["acknowledged"])
        self.assertEqual("ACK", self.call("alert", alert_id=red["alert_id"])["status"])
        self.assertTrue(self.call("resolve_alert", alert_id=red["alert_id"], action="dismissed", by="ops", note="drill")["ok"])
        self.assertNotIn(red["alert_id"], [a["alert_id"] for a in self.call("alerts_active")["alerts"]])
        hist = self.call("alerts_history", hours=1, status="DISMISSED")
        self.assertEqual([red["alert_id"]], [a["alert_id"] for a in hist["alerts"]])

    def test_errors_are_tool_errors_not_crashes(self):
        from mcp.server.mcpserver.exceptions import ToolError
        with self.assertRaises(ToolError) as cm:
            run(self.server.call_tool("branch", {"branch_id": "NOPE"}))
        self.assertIn("unknown branch_id", str(cm.exception))
        with self.assertRaises(ToolError):
            run(self.server.call_tool("camera_snapshot", {"camera_id": "CAM-J1"}))   # offline, no frame
        with self.assertRaises(ToolError):
            run(self.server.call_tool("zone_history", {"zone_id": "LOBBY", "hours": "soon"}))   # schema

    def test_nothing_leaks_a_credential(self):
        self.c.post("/api/v1/cameras", json={"camera_id": "CAM-S", "site_id": "RUH-01",
                                             "source": "rtsp://admin:hunter2@10.0.0.9/s"})
        try:
            blob = json.dumps(self.call("cameras")) + json.dumps(self.call("network_overview")) + json.dumps(self.call("summary"))
            self.assertNotIn("hunter2", blob)
        finally:
            self.c.delete("/api/v1/cameras/CAM-S")


@unittest.skipUnless(HAVE, "app/mcp not importable")
class TestTransport(unittest.TestCase):
    """The real Starlette app: bearer gate, then a JSON-RPC tools/list."""

    def rpc(self, client, headers, body):
        return client.post("/mcp", json=body, headers={"Accept": "application/json, text/event-stream",
                                                        "Content-Type": "application/json", **headers})

    def test_bearer_gate_and_tools_list(self):
        api = TestClient(app)
        server = build_server(TestClientBackend(api))
        mcp_app = make_app(server, token="s3cret")
        with TestClient(mcp_app) as m:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}}
            self.assertEqual(401, self.rpc(m, {}, init).status_code)
            self.assertEqual(401, self.rpc(m, {"Authorization": "Bearer wrong"}, init).status_code)
            ok = {"Authorization": "Bearer s3cret"}
            r = self.rpc(m, ok, init)
            self.assertEqual(200, r.status_code, r.text)
            self.rpc(m, ok, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            r = self.rpc(m, ok, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(200, r.status_code, r.text)
            names = {t["name"] for t in r.json()["result"]["tools"]}
            self.assertIn("network_overview", names)
            self.assertIn("vehicles", names)
            r = self.rpc(m, ok, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "rules_reference", "arguments": {}}})
            self.assertEqual(200, r.status_code, r.text)
            self.assertIn("R-12", r.json()["result"]["content"][0]["text"])

    def test_open_when_no_token_configured(self):
        api = TestClient(app)
        with TestClient(make_app(build_server(TestClientBackend(api)), token=None)) as m:
            r = self.rpc(m, {}, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}}})
            self.assertEqual(200, r.status_code, r.text)


if __name__ == "__main__":
    unittest.main()
