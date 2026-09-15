"""Region -> City -> Branch: the customer's hierarchy, end to end.

Four layers, each tested where it lives:
  * finblade/org.py      — validation, scope resolution, tree + roll-ups (pure)
  * the store contract   — InMemoryStore and PostgresStore answer alike,
                           including refusing to orphan a subtree
  * IngestService        — 404 vs 409 vs 422, idempotent import
  * the HTTP routes      — the query parameters actually narrow the reads

The rule under test throughout: cameras.site_id IS the branch id, and a
camera whose site_id matches no branch is UNASSIGNED — visible, counted in
the network total, in no region.
"""

import os
import sys
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade import org
from services.api.service import IngestService
from services.api.store import InMemoryStore
from tests import pgfixture


def cam(cid, site, state="ONLINE", people=0):
    return {"camera_id": cid, "site_id": site, "effective_state": state,
            "people_in_view": people, "source": "rtsp://u:p@h/x"}


TREE = {
    "tenant": {"name": "Wareed Medical Laboratories", "country": "KSA", "short": "Wareed"},
    "regions": [
        {"region_id": "CENTRAL", "name": "Central", "cities": [
            {"city_id": "RUH", "name": "Riyadh", "branches": [
                {"branch_id": "RUH-01", "name": "Riyadh Main Lab"},
                {"branch_id": "RUH-02", "name": "Olaya Collection",
                 "branch_type": "collection"}]}]},
        {"region_id": "WESTERN", "name": "Western", "cities": [
            {"city_id": "JED", "name": "Jeddah", "branches": [
                {"branch_id": "JED-01", "name": "Jeddah Main Lab"}]},
            {"city_id": "MAK", "name": "Makkah", "branches": []}]},
    ],
}


# ------------------------------------------------------------- pure logic --
class TestValidation(unittest.TestCase):
    def test_ids_are_url_safe_and_required(self):
        self.assertEqual("RUH-01", org.norm_id(" RUH-01 "))
        self.assertIsNone(org.norm_id("RUH 01"))
        self.assertIsNone(org.norm_id("a/b"))
        self.assertIsNone(org.norm_id(""))
        self.assertIsNone(org.norm_id(None))

    def test_region_name_defaults_to_id(self):
        row, errs = org.validate_region({"region_id": "WEST"})
        self.assertEqual([], errs)
        self.assertEqual("WEST", row["name"])

    def test_city_needs_an_existing_region(self):
        _, errs = org.validate_city({"city_id": "JED", "region_id": "WEST"}, [])
        self.assertTrue(any("unknown region_id" in e for e in errs))
        row, errs = org.validate_city({"city_id": "JED", "region_id": "WEST"}, ["WEST"])
        self.assertEqual([], errs)
        self.assertEqual("WEST", row["region_id"])

    def test_branch_needs_an_existing_city_and_a_known_type(self):
        _, errs = org.validate_branch({"branch_id": "JED-01", "city_id": "JED"}, [])
        self.assertTrue(any("unknown city_id" in e for e in errs))
        _, errs = org.validate_branch({"branch_id": "JED-01", "city_id": "JED",
                                      "branch_type": "SHOP"}, ["JED"])
        self.assertTrue(any("branch_type" in e for e in errs))
        row, errs = org.validate_branch({"branch_id": "JED-01", "city_id": "JED",
                                        "branch_type": "collection"}, ["JED"])
        self.assertEqual([], errs)
        self.assertEqual("COLLECTION", row["branch_type"])
        self.assertEqual("LAB", org.validate_branch(
            {"branch_id": "X", "city_id": "JED"}, ["JED"])[0]["branch_type"])

    def test_import_flattens_and_rejects_duplicates(self):
        regions, cities, branches, errs = org.flatten_import(TREE)
        self.assertEqual([], errs)
        self.assertEqual(["CENTRAL", "WESTERN"], [r["region_id"] for r in regions])
        self.assertEqual({"RUH", "JED", "MAK"}, {c["city_id"] for c in cities})
        self.assertEqual({"RUH-01", "RUH-02", "JED-01"}, {b["branch_id"] for b in branches})
        # region order is preserved through sort_order
        self.assertEqual([0, 1], [r["sort_order"] for r in regions])

        dup = {"regions": [
            {"region_id": "A", "cities": [{"city_id": "C", "branches": [{"branch_id": "B"}]}]},
            {"region_id": "A2", "cities": [{"city_id": "C2", "branches": [{"branch_id": "B"}]}]}]}
        _, _, _, errs = org.flatten_import(dup)
        self.assertTrue(any("duplicate branch id 'B'" in e for e in errs))


class TestScope(unittest.TestCase):
    def setUp(self):
        _, self.cities, self.branches, _ = org.flatten_import(TREE)

    def scope(self, **kw):
        return org.branches_in_scope(self.cities, self.branches, **kw)

    def test_no_filter_means_no_scope(self):
        self.assertIsNone(self.scope())

    def test_region_resolves_to_every_branch_beneath_it(self):
        self.assertEqual({"RUH-01", "RUH-02"}, self.scope(region_id="CENTRAL"))
        self.assertEqual({"JED-01"}, self.scope(region_id="WESTERN"))

    def test_city_and_branch(self):
        self.assertEqual({"RUH-01", "RUH-02"}, self.scope(city_id="RUH"))
        self.assertEqual(set(), self.scope(city_id="MAK"), "a city with no branches")
        self.assertEqual({"JED-01"}, self.scope(branch_id="JED-01"))

    def test_filters_intersect_and_typos_return_nothing(self):
        self.assertEqual(set(), self.scope(region_id="WESTERN", branch_id="RUH-01"))
        self.assertEqual({"RUH-01"}, self.scope(region_id="CENTRAL", branch_id="RUH-01"))
        self.assertEqual(set(), self.scope(region_id="NOPE"),
                         "an unknown id must not widen to the whole network")

    def test_in_scope_filters_on_site_id(self):
        rows = [{"site_id": "RUH-01"}, {"site_id": "JED-01"}, {"site_id": None}]
        self.assertEqual(rows, org.in_scope(rows, None))
        self.assertEqual([rows[0]], org.in_scope(rows, {"RUH-01"}))


class TestTree(unittest.TestCase):
    def setUp(self):
        self.regions, self.cities, self.branches, _ = org.flatten_import(TREE)

    def tree(self, **kw):
        return org.build_tree(self.regions, self.cities, self.branches, **kw)

    def test_shape_follows_region_city_branch(self):
        t = self.tree()
        self.assertEqual(["CENTRAL", "WESTERN"], [r["region_id"] for r in t["regions"]])
        central = t["regions"][0]
        self.assertEqual(["RUH"], [c["city_id"] for c in central["cities"]])
        self.assertEqual({"RUH-01", "RUH-02"},
                         {b["branch_id"] for b in central["cities"][0]["branches"]})
        self.assertEqual({"regions": 2, "cities": 3, "branches": 3,
                          "cameras_unassigned": 0}, t["counts"])

    def test_counts_roll_up_every_level(self):
        cams = [cam("C1", "RUH-01", "ONLINE", 3), cam("C2", "RUH-01", "OFFLINE"),
                cam("C3", "RUH-02", "DEGRADED", 1), cam("C4", "JED-01", "ONLINE", 2)]
        zones = [{"site_id": "RUH-01", "status": "WARNING"},
                 {"site_id": "RUH-01", "status": "NORMAL"},
                 {"site_id": "JED-01", "status": "CRITICAL"}]
        alerts = [{"site_id": "RUH-01", "severity": "AMBER"},
                  {"site_id": "RUH-02", "severity": "RED"},
                  {"site_id": "JED-01", "severity": "COMPLIANCE"}]
        t = self.tree(cameras=cams, zones=zones, alerts=alerts)
        central, western = t["regions"]
        ruh = central["cities"][0]
        ruh01 = next(b for b in ruh["branches"] if b["branch_id"] == "RUH-01")

        self.assertEqual(2, ruh01["rollup"]["cameras"])
        self.assertEqual(1, ruh01["rollup"]["cameras_online"])
        self.assertEqual(1, ruh01["rollup"]["cameras_offline"])
        self.assertEqual(3, ruh01["rollup"]["people_in_view"])
        self.assertEqual(2, ruh01["rollup"]["zones"])
        self.assertEqual(1, ruh01["rollup"]["zones_warning"])
        self.assertEqual(1, ruh01["rollup"]["alerts_warning"])

        self.assertEqual(3, ruh["rollup"]["cameras"], "city = sum of branches")
        self.assertEqual(1, ruh["rollup"]["cameras_degraded"])
        self.assertEqual(2, ruh["rollup"]["cameras_online"])
        self.assertEqual(1, ruh["rollup"]["alerts_critical"])
        self.assertEqual(3, central["rollup"]["cameras"], "region = sum of cities")
        self.assertEqual(1, western["rollup"]["alerts_compliance"])
        self.assertEqual(1, western["rollup"]["zones_critical"])
        self.assertEqual(4, t["rollup"]["cameras"], "network = sum of regions")
        self.assertEqual(3, t["rollup"]["alerts_open"])
        # DEGRADED people are not summed: only ONLINE cameras contribute.
        self.assertEqual(5, t["rollup"]["people_in_view"])

    def test_a_camera_with_no_branch_is_unassigned_not_dropped(self):
        cams = [cam("C1", "RUH-01"), cam("C9", "SITE-OLD"), cam("C0", None)]
        t = self.tree(cameras=cams)
        self.assertEqual(3, t["rollup"]["cameras"], "the network total is the truth")
        self.assertEqual(1, t["regions"][0]["rollup"]["cameras"])
        self.assertEqual([None, "SITE-OLD"], [u["site_id"] for u in t["unassigned"]])
        self.assertEqual(2, t["counts"]["cameras_unassigned"])

    def test_the_tree_carries_no_credentials(self):
        t = self.tree(cameras=[cam("C1", "RUH-01")])
        b = next(b for b in t["regions"][0]["cities"][0]["branches"]
                 if b["branch_id"] == "RUH-01")
        self.assertNotIn("source", b["cameras"][0])
        self.assertNotIn("stream_url", b["cameras"][0])


# --------------------------------------------------------- store contract --
class OrgStoreContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def test_round_trip_and_upsert(self):
        self.store.save_region({"region_id": "WEST", "name": "Western", "sort_order": 2})
        self.store.save_region({"region_id": "WEST", "name": "Western Region", "sort_order": 1})
        rows = self.store.list_regions()
        self.assertEqual(1, len(rows))
        self.assertEqual("Western Region", rows[0]["name"])
        self.assertEqual(1, int(rows[0]["sort_order"]))

        self.store.save_city({"city_id": "JED", "region_id": "WEST", "name": "Jeddah"})
        self.store.save_branch({"branch_id": "JED-01", "city_id": "JED",
                                "name": "Main Lab", "branch_type": "LAB",
                                "address": None, "timezone": "Asia/Riyadh"})
        self.assertEqual("WEST", self.store.list_cities()[0]["region_id"])
        b = self.store.list_branches()[0]
        self.assertEqual(("JED", "LAB", "Asia/Riyadh"),
                         (b["city_id"], b["branch_type"], b["timezone"]))

    def test_a_parent_with_children_is_not_deleted(self):
        self.store.save_region({"region_id": "WEST", "name": "Western"})
        self.store.save_city({"city_id": "JED", "region_id": "WEST", "name": "Jeddah"})
        self.store.save_branch({"branch_id": "JED-01", "city_id": "JED", "name": "Lab"})
        self.assertFalse(self.store.delete_region("WEST"))
        self.assertFalse(self.store.delete_city("JED"))
        self.assertEqual(1, len(self.store.list_regions()))
        self.assertEqual(1, len(self.store.list_cities()))
        # Leaf first, then upwards, and each step succeeds.
        self.assertTrue(self.store.delete_branch("JED-01"))
        self.assertTrue(self.store.delete_city("JED"))
        self.assertTrue(self.store.delete_region("WEST"))
        self.assertFalse(self.store.delete_region("WEST"), "absent = False, not an error")

    def test_meta_is_text_and_none_removes(self):
        self.store.set_org_meta({"tenant_name": "Wareed", "tenant_country": "KSA"})
        self.assertEqual({"tenant_name": "Wareed", "tenant_country": "KSA"},
                         self.store.get_org_meta())
        self.store.set_org_meta({"tenant_country": None})
        self.assertEqual({"tenant_name": "Wareed"}, self.store.get_org_meta())


class TestInMemoryOrgStore(OrgStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


@pgfixture.skip_without_pg
class TestPostgresOrgStore(OrgStoreContract, unittest.TestCase):
    def make_store(self):
        store, teardown = pgfixture.make_store("org")
        self.addCleanup(teardown)
        return store


# ---------------------------------------------------------------- service --
class TestService(unittest.TestCase):
    def setUp(self):
        self.svc = IngestService(InMemoryStore())

    def test_import_is_idempotent_and_sets_tenant(self):
        code, body = self.svc.import_org(TREE)
        self.assertEqual(200, code, body)
        self.assertEqual((2, 3, 3), (body["regions"], body["cities"], body["branches"]))
        code, _ = self.svc.import_org(TREE)
        self.assertEqual(200, code)
        idx = self.svc.org_index()
        self.assertEqual(3, len(idx["branches"]), "re-import did not duplicate")
        self.assertEqual("Wareed Medical Laboratories", idx["meta"]["tenant_name"])
        self.assertEqual("KSA", idx["meta"]["tenant_country"])

    def test_import_with_an_error_writes_nothing(self):
        bad = {"regions": [{"region_id": "A", "cities": [
            {"city_id": "C", "branches": [{"branch_id": "bad id"}]}]}]}
        code, body = self.svc.import_org(bad)
        self.assertEqual(422, code)
        self.assertEqual([], self.svc.org_index()["regions"])

    def test_child_of_unknown_parent_is_422(self):
        code, body = self.svc.save_city({"city_id": "JED", "region_id": "WEST"})
        self.assertEqual(422, code)
        self.assertTrue(any("unknown region_id" in e for e in body["errors"]))
        self.svc.save_region({"region_id": "WEST"})
        self.assertEqual(200, self.svc.save_city({"city_id": "JED", "region_id": "WEST"})[0])
        self.assertEqual(422, self.svc.save_branch({"branch_id": "X", "city_id": "RUH"})[0])
        self.assertEqual(200, self.svc.save_branch({"branch_id": "X", "city_id": "JED"})[0])

    def test_delete_distinguishes_missing_from_populated(self):
        self.svc.import_org(TREE)
        self.assertEqual(404, self.svc.delete_region("NOPE")[0])
        code, body = self.svc.delete_region("CENTRAL")
        self.assertEqual(409, code)
        self.assertIn("child", body["error"])
        self.assertEqual(409, self.svc.delete_city("RUH")[0])
        self.assertEqual(200, self.svc.delete_city("MAK")[0], "an empty city goes")

    def test_a_branch_that_owns_cameras_stays(self):
        self.svc.import_org(TREE)
        self.svc.upsert_camera({"camera_id": "CAM-1", "site_id": "JED-01"})
        code, body = self.svc.delete_branch("JED-01")
        self.assertEqual(409, code, body)
        self.svc.delete_camera("CAM-1")
        self.assertEqual(200, self.svc.delete_branch("JED-01")[0])

    def test_scope_and_tree_come_from_the_store(self):
        self.svc.import_org(TREE)
        self.assertEqual({"RUH-01", "RUH-02"}, self.svc.branches_in_scope(region_id="CENTRAL"))
        self.assertIsNone(self.svc.branches_in_scope())
        self.assertTrue(self.svc.branch_known("JED-01"))
        self.assertFalse(self.svc.branch_known("SITE-01"))
        t = self.svc.org_tree(cameras=[cam("C1", "JED-01"), cam("C2", "SITE-01")])
        self.assertEqual("Wareed Medical Laboratories", t["meta"]["tenant_name"])
        self.assertEqual(1, t["regions"][1]["rollup"]["cameras"])
        self.assertEqual(["SITE-01"], [u["site_id"] for u in t["unassigned"]])


# ------------------------------------------------------------- HTTP routes --
try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc as app_svc
    HAVE_APP = True
except Exception as _exc:                      # noqa: BLE001
    HAVE_APP = False
    IMPORT_ERROR = _exc


@unittest.skipUnless(HAVE_APP, "fastapi app not importable")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        # Start from nothing: the app's store is process-wide.
        for b in list(app_svc.store.list_branches()):
            for camr in app_svc.store.list_cameras():
                if camr.get("site_id") == b["branch_id"]:
                    app_svc.store.delete_camera(camr["camera_id"])
            app_svc.store.delete_branch(b["branch_id"])
        for ci in list(app_svc.store.list_cities()):
            app_svc.store.delete_city(ci["city_id"])
        for r in list(app_svc.store.list_regions()):
            app_svc.store.delete_region(r["region_id"])
        for camr in list(app_svc.store.list_cameras()):
            app_svc.store.delete_camera(camr["camera_id"])
        r = self.c.post("/api/v1/org/import", json=TREE)
        self.assertEqual(200, r.status_code, r.text)

    def test_tree_and_index(self):
        t = self.c.get("/api/v1/org").json()
        self.assertEqual(["CENTRAL", "WESTERN"], [r["region_id"] for r in t["regions"]])
        self.assertEqual("Wareed Medical Laboratories", t["meta"]["tenant_name"])
        idx = self.c.get("/api/v1/org/index").json()
        self.assertEqual(3, len(idx["branches"]))

    def test_crud_status_codes(self):
        self.assertEqual(422, self.c.post("/api/v1/org/cities",
                                          json={"city_id": "X", "region_id": "NOPE"}).status_code)
        self.assertEqual(200, self.c.post("/api/v1/org/regions",
                                          json={"region_id": "NORTH", "name": "Northern"}).status_code)
        self.assertEqual(200, self.c.post("/api/v1/org/cities",
                                          json={"city_id": "TUU", "region_id": "NORTH"}).status_code)
        self.assertEqual(200, self.c.post("/api/v1/org/branches",
                                          json={"branch_id": "TUU-01", "city_id": "TUU"}).status_code)
        self.assertEqual(409, self.c.delete("/api/v1/org/regions/NORTH").status_code)
        self.assertEqual(409, self.c.delete("/api/v1/org/cities/TUU").status_code)
        self.assertEqual(200, self.c.delete("/api/v1/org/branches/TUU-01").status_code)
        self.assertEqual(200, self.c.delete("/api/v1/org/cities/TUU").status_code)
        self.assertEqual(200, self.c.delete("/api/v1/org/regions/NORTH").status_code)
        self.assertEqual(404, self.c.delete("/api/v1/org/regions/NORTH").status_code)

    def test_camera_registration_flags_an_unknown_branch(self):
        r = self.c.post("/api/v1/cameras", json={"camera_id": "CAM-X", "site_id": "SITE-OLD"})
        self.assertEqual(200, r.status_code)
        self.assertIn("not a registered branch", r.json().get("warning", ""))
        r = self.c.post("/api/v1/cameras", json={"camera_id": "CAM-Y", "site_id": "JED-01"})
        self.assertNotIn("warning", r.json())
        t = self.c.get("/api/v1/org").json()
        self.assertEqual(["SITE-OLD"], [u["site_id"] for u in t["unassigned"]])

    def test_reads_narrow_by_region_city_branch(self):
        for cid, site in (("CAM-R1", "RUH-01"), ("CAM-R2", "RUH-02"), ("CAM-J1", "JED-01")):
            self.c.post("/api/v1/cameras", json={"camera_id": cid, "site_id": site})
        ids = lambda r: sorted(c["camera_id"] for c in r.json()["cameras"])  # noqa: E731
        self.assertEqual(["CAM-J1", "CAM-R1", "CAM-R2"], ids(self.c.get("/api/v1/cameras")))
        self.assertEqual(["CAM-R1", "CAM-R2"], ids(self.c.get("/api/v1/cameras?region_id=CENTRAL")))
        self.assertEqual(["CAM-R2"], ids(self.c.get("/api/v1/cameras?branch_id=RUH-02")))
        self.assertEqual(["CAM-J1"], ids(self.c.get("/api/v1/cameras?city_id=JED")))
        self.assertEqual([], ids(self.c.get("/api/v1/cameras?region_id=WESTERN&branch_id=RUH-01")))
        self.assertEqual([], ids(self.c.get("/api/v1/cameras?region_id=TYPO")))

        s = self.c.get("/api/v1/summary?region_id=CENTRAL&charts=0").json()
        self.assertEqual({"region_id": "CENTRAL", "city_id": None, "branch_id": None,
                          "sites": ["RUH-01", "RUH-02"]}, s["scope"])
        self.assertEqual(2, len(s["cameras"]))
        self.assertIsNone(self.c.get("/api/v1/summary?charts=0").json()["scope"]["sites"])

    def test_alerts_narrow_by_scope(self):
        app_svc.raise_alert({"rule_id": "R-01", "severity": "AMBER", "message": "m",
                             "zone_id": "Z", "camera_id": "C", "site_id": "RUH-01", "ts": 1.0})
        app_svc.raise_alert({"rule_id": "R-02", "severity": "RED", "message": "m",
                             "zone_id": "Z", "camera_id": "C", "site_id": "JED-01", "ts": 1.0})
        sev = lambda r: sorted(a["severity"] for a in r.json()["alerts"])  # noqa: E731
        self.assertEqual(["AMBER"], sev(self.c.get("/api/v1/alerts?region_id=CENTRAL")))
        self.assertEqual(["RED"], sev(self.c.get("/api/v1/alerts?city_id=JED")))
        self.assertEqual(["RED"], sev(self.c.get("/api/v1/history/alerts?region_id=WESTERN")))
        self.assertEqual([], sev(self.c.get("/api/v1/alerts?region_id=WESTERN&site_id=RUH-01")))


if __name__ == "__main__":
    unittest.main()
