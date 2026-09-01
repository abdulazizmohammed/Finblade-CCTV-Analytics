"""The five remaining routes with no test reference anywhere in tests/.

Found by walking every @app route and grepping the suite for it. None of these
is exotic — they are a roster read, a drift report, a forwarder trigger and two
report formats — which is exactly why they were missed: nothing about them
looked risky enough to write a test for, and so a 404 or a 500 in any of them
would have shipped green.

Coverage-shaped rather than deep. Each asserts the route exists, returns the
documented shape, and fails in the documented way.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

try:
    from fastapi.testclient import TestClient

    from services.api.app import app, svc
    HAVE_APP = True
except Exception as _exc:                      # noqa: BLE001
    HAVE_APP = False
    IMPORT_ERROR = _exc


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestFacilityReads(unittest.TestCase):
    def setUp(self):
        svc.roster.clear()
        self.client = TestClient(app)

    def test_members_is_a_list_when_empty(self):
        r = self.client.get("/api/v1/facility/members")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["members"], [])

    def test_members_lists_who_is_inside(self):
        svc.roster.admit("gp_" + "a" * 16, 1000.0, "ZONE-LOBBY")
        r = self.client.get("/api/v1/facility/members")
        refs = [m["ref"] for m in r.json()["members"]]
        self.assertEqual(refs, ["gp_" + "a" * 16])

    def test_stale_echoes_its_own_threshold(self):
        r = self.client.get("/api/v1/facility/stale", params={"older_than": 42})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["older_than_s"], 42)
        self.assertIsInstance(r.json()["stale"], list)

    def test_stale_reports_but_never_removes(self):
        # The drift report is deliberately read-only: an entry here is either a
        # person in an unmonitored space or a missed exit, and no data here
        # separates them. A human decides.
        ref = "gp_" + "b" * 16
        svc.roster.admit(ref, 0.0, "ZONE-LOBBY")
        before = svc.roster.occupancy()
        self.client.get("/api/v1/facility/stale", params={"older_than": 1})
        self.assertEqual(svc.roster.occupancy(), before)
        self.assertTrue(svc.roster.contains(ref))


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestForwarderFlush(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_flush_is_409_when_forwarding_is_disabled(self):
        # Disabled is the default — FINBLADE_URL is unset. A 409 naming the
        # missing variable is the difference between "misconfigured" and
        # "broken", which is what an integrator needs to know.
        from services.api.app import forwarder
        if forwarder.enabled:
            self.skipTest("forwarding is enabled in this environment")
        r = self.client.post("/api/v1/finblade/flush")
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.json()["ok"])
        self.assertIn("FINBLADE_URL", r.json()["error"])

    def test_status_is_readable_whether_or_not_it_is_enabled(self):
        r = self.client.get("/api/v1/finblade/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("enabled", r.json())


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not available")
class TestReports(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_generate_defaults_to_the_last_hour(self):
        r = self.client.post("/api/v1/reports/generate", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("report_id", r.json())

    def test_generate_accepts_an_explicit_window(self):
        r = self.client.post("/api/v1/reports/generate",
                             json={"from": 0, "to": 1000})
        self.assertEqual(r.status_code, 200)

    def test_generate_survives_a_body_that_is_not_json(self):
        # The handler swallows a parse failure and falls back to the default
        # window; that must not become a 500.
        r = self.client.post("/api/v1/reports/generate",
                             content=b"not json",
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)

    def test_generated_report_is_retrievable(self):
        rid = self.client.post("/api/v1/reports/generate", json={}).json()["report_id"]
        r = self.client.get(f"/api/v1/reports/{rid}")
        self.assertEqual(r.status_code, 200)

    def test_csv_is_served_as_a_download(self):
        r = self.client.get("/api/v1/reports/occupancy.csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertIn("finblade_occupancy.csv",
                      r.headers.get("content-disposition", ""))

    def test_csv_and_json_describe_the_same_window(self):
        params = {"from": 0, "to": 1000}
        j = self.client.get("/api/v1/reports/occupancy.json", params=params).json()
        c = self.client.get("/api/v1/reports/occupancy.csv", params=params).text
        # One header line plus one row per zone in the JSON.
        rows = [ln for ln in c.strip().splitlines() if ln]
        self.assertEqual(len(rows), len(j["zones"]) + 1)

    def test_csv_has_a_header_even_with_no_zones(self):
        c = self.client.get("/api/v1/reports/occupancy.csv",
                            params={"from": 0, "to": 1}).text
        self.assertTrue(c.strip().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
