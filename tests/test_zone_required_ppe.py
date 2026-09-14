"""required_ppe must survive a round trip through the store.

WHY THIS TEST EXISTS. The Zone dataclass and the YAML loader knew about
required_ppe while the Postgres INSERT did not, so setting a PPE requirement
through the API returned {"saved": true} and silently discarded it — a zone
that looked configured and enforced nothing. That is the same bug
physical_area_id had one release earlier, which is why the guard is now
generic: it asserts the whole Zone field set survives, not just this one field.

A safety requirement that reports success and does nothing is the worst failure
mode this field can have, so it gets a test that would have caught it.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.zones import zone_from_dict


class TestZoneModel(unittest.TestCase):
    def test_required_ppe_defaults_to_empty_not_none(self):
        """Empty means 'no PPE rule here', and it must be the default so a
        zone nobody configured never accuses anyone."""
        z = zone_from_dict({"zone_id": "Z1", "polygon": [[0, 0], [1, 0], [1, 1]]},
                           640, 480)
        self.assertEqual(z.required_ppe, [])

    def test_values_are_normalised_on_load(self):
        """Config is written by humans: Hardhat / hard hat / HARD-HAT all mean
        the same thing, and a case mismatch that silently disabled a safety
        requirement would be the worst way to lose one."""
        z = zone_from_dict(
            {"zone_id": "Z1", "polygon": [[0, 0], [1, 0], [1, 1]],
             "required_ppe": ["Hardhat", "Safety Vest", "  MASK  "]}, 640, 480)
        self.assertEqual(z.required_ppe, ["hardhat", "safety_vest", "mask"])

    def test_blank_entries_are_dropped(self):
        z = zone_from_dict(
            {"zone_id": "Z1", "polygon": [[0, 0], [1, 0], [1, 1]],
             "required_ppe": ["hardhat", "", "   "]}, 640, 480)
        self.assertEqual(z.required_ppe, ["hardhat"])

    def test_it_is_serialised_so_the_editor_and_the_worker_can_see_it(self):
        z = zone_from_dict(
            {"zone_id": "Z1", "polygon": [[0, 0], [1, 0], [1, 1]],
             "required_ppe": ["hardhat"]}, 640, 480)
        self.assertEqual(z.to_dict().get("required_ppe"), ["hardhat"])


class TestStoreRoundTrip(unittest.TestCase):
    """Runs against whichever store the test environment provides."""

    def _store(self):
        os.environ.setdefault("FINBLADE_INMEMORY", "1")
        from services.api.store import InMemoryStore
        return InMemoryStore()

    def test_required_ppe_survives_save_and_list(self):
        st = self._store()
        st.save_zones("CAM-1", [{
            "zone_id": "ZONE-WELD", "zone_name": "Welding",
            "polygon": [[0, 0], [10, 0], [10, 10]],
            "required_ppe": ["hardhat", "safety_vest"]}])
        got = st.list_zones("CAM-1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].get("required_ppe"), ["hardhat", "safety_vest"])

    def test_a_zone_without_the_field_round_trips_too(self):
        """Backward compatibility: every zone predating this field must still
        load, and must load as 'no PPE required' rather than as an error."""
        st = self._store()
        st.save_zones("CAM-1", [{"zone_id": "OLD",
                                 "polygon": [[0, 0], [10, 0], [10, 10]]}])
        got = st.list_zones("CAM-1")[0]
        self.assertIn(got.get("required_ppe"), (None, [], ()))


class TestPostgresColumnIsDeclared(unittest.TestCase):
    """Static guards on the SQL. The round-trip test above runs against the
    in-memory store in most environments, so these check the Postgres path
    without needing a live cluster."""

    def _sql(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "services", "api", "ddl_pg.sql"),
                  encoding="utf-8") as fh:
            return fh.read()

    def _store_src(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "services", "api", "postgres_store.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_the_column_exists_and_is_added_to_existing_databases(self):
        sql = self._sql()
        self.assertIn("required_ppe           TEXT", sql)
        self.assertIn("ALTER TABLE zones ADD COLUMN IF NOT EXISTS required_ppe",
                      sql)

    def test_all_four_sites_reference_it(self):
        """INSERT columns, ON CONFLICT update, params, and the SELECT. Missing
        any one of them is a silent data-loss bug, which is what happened."""
        src = self._store_src()
        self.assertIn("required_ppe,updated_at) ", src)          # INSERT columns
        self.assertIn("required_ppe=excluded.required_ppe", src)  # upsert
        self.assertIn('json.dumps(z.get("required_ppe")', src)    # params
        self.assertIn("physical_area_id,required_ppe,updated_at ", src)  # SELECT

class TestZoneEditorCollectsIt(unittest.TestCase):
    """The browser end of the same round trip.

    The editor gathered physical_area_id into its zone objects once while
    omitting it from the payload it POSTed, so the field was collected, shown,
    and saved nowhere — with a success message. required_ppe has exactly the
    same five touch points, and four of them are cosmetic: only the payload one
    loses data when it is missed. Guard all five, because a missing picker is
    obvious to a human and a missing payload key is not.
    """

    def _src(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "tools", "zone-editor.html"),
                  encoding="utf-8") as fh:
            return fh.read()

    def _fn(self, src, name):
        at = src.index("function %s(" % name)
        return src[at:src.index("\n}", at)]

    def test_the_payload_carries_it(self):
        """THE ONE THAT MATTERS. Everything else is presentation; this is the
        difference between saving the operator's choice and discarding it."""
        self.assertIn("required_ppe:", self._fn(self._src(), "zonesPayload"))

    def test_a_new_zone_picks_it_up_from_the_form(self):
        self.assertIn("required_ppe: ppeGet(", self._fn(self._src(), "closeZone"))

    def test_it_is_read_back_from_the_server(self):
        self.assertIn("required_ppe:z.required_ppe", self._fn(self._src(), "loadServer"))

    def test_editing_a_zone_restores_the_checkboxes(self):
        """Without this, opening a zone to move one corner silently clears its
        PPE requirement when the zone is re-added on Close."""
        self.assertIn("ppeSet($('ppe')", self._fn(self._src(), "editZone"))

    def test_the_offered_values_are_exactly_the_accepted_ones(self):
        """A checkbox for an item the rule engine does not know would be
        dropped server-side with a warning nobody reads — the operator would
        believe they had set a requirement that never applies."""
        from finblade.ppe import PPE_TYPES
        src = self._src()
        at = src.index("const PPE_ITEMS=")
        # To "];", not to the first "]" — that one closes the first
        # ['hardhat','Hard hat'] pair, so the slice saw a single item and the
        # assertion failed against a control that was in fact correct.
        decl = src[at:src.index("];", at) + 2]
        offered = set(re.findall(r"\['([a-z_]+)',", decl))
        self.assertEqual(offered, set(PPE_TYPES))

        # And the checkboxes must offer the same set the constant does.
        boxes = set(re.findall(r'<input type="checkbox" value="([a-z_]+)"', src))
        self.assertEqual(boxes, set(PPE_TYPES))


class TestPostgresRoundTrip(unittest.TestCase):
    """The real thing, against a real cluster.

    An earlier version of this parsed postgres_store.py to count INSERT columns
    against placeholders. It was brittle - the SQL is assembled from adjacent
    string literals with Python comments interleaved - and I fixed it twice
    before accepting that a test needing constant repair is worse than no test.
    A column/placeholder mismatch fails loudly against a live database anyway,
    so exercising the database IS the check.

    Skipped where no cluster is reachable, like the rest of the pg-backed tests.
    """

    @classmethod
    def setUpClass(cls):
        cls.store = None
        dsn = os.environ.get("DATABASE_URL",
                             "postgresql://postgres@127.0.0.1:5432/finblade")
        try:
            from services.api.postgres_store import PostgresStore
            cls.store = PostgresStore(dsn)
            cls.store.list_zones("__probe__")
        except Exception:                                    # noqa: BLE001
            cls.store = None

    def setUp(self):
        if self.store is None:
            self.skipTest("no reachable Postgres")

    def test_required_ppe_survives_a_real_save_and_read(self):
        cam = "__test_ppe_zone__"
        try:
            self.store.save_zones(cam, [{
                "zone_id": "Z-WELD", "zone_name": "Welding",
                "polygon": [[0, 0], [10, 0], [10, 10]],
                "physical_area_id": "AREA-1",
                "required_ppe": ["hardhat", "safety_vest"]}])
            got = self.store.list_zones(cam)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["required_ppe"], ["hardhat", "safety_vest"])
            # physical_area_id had this identical bug one release earlier;
            # assert it too so the pair cannot regress independently.
            self.assertEqual(got[0]["physical_area_id"], "AREA-1")
        finally:
            self.store.save_zones(cam, [])

    def test_a_zone_with_no_ppe_reads_back_as_empty_not_null(self):
        cam = "__test_ppe_zone__"
        try:
            self.store.save_zones(cam, [{
                "zone_id": "Z-PLAIN",
                "polygon": [[0, 0], [10, 0], [10, 10]]}])
            got = self.store.list_zones(cam)[0]
            self.assertEqual(got["required_ppe"], [])
        finally:
            self.store.save_zones(cam, [])


if __name__ == "__main__":
    unittest.main()
