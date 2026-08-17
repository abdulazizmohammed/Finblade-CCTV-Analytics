"""The chatbot role can read the views, and cannot reach what would mislead it.

A text-to-SQL bot wrote COUNT(DISTINCT person_ref) against these views and got a
plausible wrong number back — person_ref is a hash of the tracker id, so it
counts track fragments rather than people. Prompting a model not to do that is
advice; not granting the column is a rule. These tests pin the rule.

The allowlist checks run everywhere. The privilege checks need a real server and
skip cleanly without one, because column-level GRANT has no SQLite equivalent
and asserting it in the abstract would prove nothing.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.api.analytics_views import (POSTGRES, SAFE_COLUMNS,  # noqa: E402
                                          comment_sql, create_all, SQLITE,
                                          view_definitions, view_names)
from scripts.pg_grants import statements, verify                    # noqa: E402


def _pg_dsn():
    for var in ("FINBLADE_TEST_DSN", "DATABASE_URL"):
        if os.environ.get(var):
            return os.environ[var]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pglib, pgdata = os.path.join(repo, ".pgtest"), os.path.join(repo, ".pgdata")
    if not os.path.isdir(pglib) or not os.path.isdir(pgdata):
        return None
    if pglib not in sys.path:
        sys.path.insert(0, pglib)
    try:
        import pgserver
        return pgserver.get_server(pgdata).get_uri()
    except Exception:                                   # noqa: BLE001
        return None


PG_DSN = _pg_dsn()


class TestTheAllowlistItself(unittest.TestCase):
    """No server needed. These are properties of the list."""

    def test_every_view_has_an_allowlist(self):
        # A view with no entry is not granted at all, which fails closed — but
        # silently. Forgetting one should break a test, not a chatbot.
        self.assertEqual(set(view_names(POSTGRES)), set(SAFE_COLUMNS),
                         "a view was added without deciding what may be read")

    def test_person_ref_is_granted_nowhere(self):
        for view, cols in SAFE_COLUMNS.items():
            self.assertNotIn("person_ref", cols,
                             f"{view} exposes the tracker hash to a counting role")

    def test_person_key_is_offered_wherever_person_ref_was_withheld(self):
        # Withholding the wrong column only helps if the right one is present.
        for view in ("v_zone_events", "v_zone_entries"):
            self.assertIn("person_key", SAFE_COLUMNS[view])

    def test_no_credential_column_is_listed(self):
        for view, cols in SAFE_COLUMNS.items():
            for banned in ("source", "stream_url", "rtsp_url", "payload"):
                self.assertNotIn(banned, cols, f"{view} exposes {banned}")

    def test_every_listed_column_actually_exists(self):
        """An allowlist naming a column the view lost is a grant that errors."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        create_all(conn, dialect=SQLITE)
        for view, cols in SAFE_COLUMNS.items():
            real = {r[1] for r in conn.execute(f"PRAGMA table_info({view})")}
            for c in cols:
                self.assertIn(c, real, f"{view} has no column {c}")

    def test_the_statements_never_grant_a_table(self):
        sql = " ".join(statements("somebot", database="finblade"))
        for table in ("cameras", "events", "zones", "zone_state_ts", "alerts"):
            self.assertNotRegex(
                sql, rf"\bON {table}\b",
                f"a base table ({table}) appears in the grant statements")

    def test_it_revokes_before_granting(self):
        """A previous hand-typed grant must not survive a run."""
        sql = statements("somebot")
        revoke = next(i for i, s in enumerate(sql) if s.startswith("REVOKE"))
        first_grant = next(i for i, s in enumerate(sql)
                           if s.startswith("GRANT SELECT ("))
        self.assertLess(revoke, first_grant)

    def test_the_role_name_is_quoted(self):
        # Role names can be case-sensitive or contain characters that need it.
        for s in statements("Mixed-Case"):
            if "Mixed-Case" in s:
                self.assertIn('"Mixed-Case"', s)


class TestComments(unittest.TestCase):
    def test_comments_are_postgres_only(self):
        self.assertEqual([], comment_sql(SQLITE),
                         "SQLite has no COMMENT ON; returning [] lets a caller "
                         "apply comments unconditionally")
        self.assertTrue(comment_sql(POSTGRES))

    def test_every_view_is_described(self):
        sql = " ".join(comment_sql(POSTGRES))
        for view in view_names(POSTGRES):
            self.assertIn(f"COMMENT ON VIEW {view} IS", sql)

    def test_the_sharpest_traps_are_named_in_the_comments(self):
        """The comment has to say what NOT to do; 'use person_key' is
        forgettable, 'never COUNT(DISTINCT person_ref)' is not."""
        sql = " ".join(comment_sql(POSTGRES)).lower()
        for phrase in ("person_key", "person_ref", "time-weighted",
                       "avg(occupancy)", "never sum(occupancy)"):
            self.assertIn(phrase, sql, f"no comment warns about {phrase}")

    def test_quotes_inside_a_comment_cannot_break_the_statement(self):
        from services.api.analytics_views import _quote
        self.assertEqual("'it''s fine'", _quote("it's fine"))


@unittest.skipIf(PG_DSN is None, "no Postgres available")
class TestAgainstARealServer(unittest.TestCase):
    """Privileges are only real if a connection is refused."""

    SCHEMA = "grant_test"
    ROLE = "grant_test_bot"
    PASSWORD = "grant_test_pw"

    @classmethod
    def setUpClass(cls):
        import psycopg
        with psycopg.connect(PG_DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {cls.SCHEMA} CASCADE")
            c.execute(f"CREATE SCHEMA {cls.SCHEMA}")
            c.execute(f'DROP ROLE IF EXISTS "{cls.ROLE}"')
            c.execute(f"CREATE ROLE \"{cls.ROLE}\" LOGIN PASSWORD '{cls.PASSWORD}'")

        cls.owner_dsn = cls._with_schema(PG_DSN)
        with psycopg.connect(cls.owner_dsn, autocommit=True) as c:
            ddl = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "services", "api", "ddl_pg.sql")
            c.execute(open(ddl).read())
            create_all(c, dialect=POSTGRES)
            c.execute("INSERT INTO cameras(camera_id, source) VALUES "
                      "('CAM-01','rtsp://admin:hunter2@10.0.0.5:554/s1')")
            c.execute("INSERT INTO zones(camera_id, zone_id, zone_name) "
                      "VALUES ('CAM-01','Z1','Lobby')")
            c.execute("INSERT INTO events(event_id, event_type, camera_id, "
                      "zone_to, person_ref, global_ref, ts) VALUES "
                      "('e1','ZONE_ENTRY','CAM-01','Z1','pr_a','gp_1',1786900000)")
            for s in statements(cls.ROLE, cls.SCHEMA):
                c.execute(s)
            for s in comment_sql(POSTGRES):
                c.execute(s)

        cls.bot_dsn = cls._with_schema(
            re.sub(r"postgresql://[^@/]*@",
                   f"postgresql://{cls.ROLE}:{cls.PASSWORD}@", PG_DSN))

    @classmethod
    def _with_schema(cls, dsn):
        sep = "&" if "?" in dsn else "?"
        return f"{dsn}{sep}options=-csearch_path%3D{cls.SCHEMA}"

    @classmethod
    def tearDownClass(cls):
        import psycopg
        with psycopg.connect(PG_DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {cls.SCHEMA} CASCADE")
            c.execute(f'DROP ROLE IF EXISTS "{cls.ROLE}"')

    def bot(self):
        import psycopg
        return psycopg.connect(self.bot_dsn, autocommit=True)

    def test_the_right_query_works(self):
        with self.bot() as c:
            n = c.execute("SELECT COUNT(DISTINCT person_key) "
                          "FROM v_zone_entries").fetchone()[0]
            self.assertEqual(1, n)

    def test_the_wrong_query_is_refused_rather_than_answered(self):
        """The whole point. This used to return a plausible wrong number."""
        import psycopg
        with self.bot() as c:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT COUNT(DISTINCT person_ref) FROM v_zone_entries")

    def test_person_ref_is_refused_on_every_view_that_has_it(self):
        import psycopg
        for view in ("v_zone_events", "v_zone_entries", "v_timeline"):
            with self.subTest(view=view), self.bot() as c:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"SELECT person_ref FROM {view} LIMIT 1")

    def test_no_base_table_is_reachable(self):
        import psycopg
        for table in ("cameras", "events", "zones", "zone_state_ts", "alerts",
                      "facility_presence"):
            with self.subTest(table=table), self.bot() as c:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"SELECT * FROM {table} LIMIT 1")

    def test_the_rtsp_password_is_unreachable_by_any_route(self):
        with self.bot() as c:
            for view, cols in SAFE_COLUMNS.items():
                for row in c.execute(
                        f"SELECT {', '.join(cols)} FROM {view}").fetchall():
                    for value in row:
                        if isinstance(value, str):
                            self.assertNotIn("hunter2", value)

    def test_every_view_is_readable_through_its_allowlist(self):
        with self.bot() as c:
            for view, cols in SAFE_COLUMNS.items():
                with self.subTest(view=view):
                    c.execute(f"SELECT {', '.join(cols)} FROM {view} LIMIT 1")

    def test_select_star_is_refused_where_a_column_is_withheld(self):
        """A consequence worth knowing, not a defect. `*` expands to every
        column including the withheld one, so the role is refused — which is
        why a bot must name its columns. Views with nothing withheld are
        unaffected, and the split below is the honest record of which is which.
        """
        import psycopg
        withheld = ("v_zone_events", "v_zone_entries", "v_timeline")
        with self.bot() as c:
            for view in withheld:
                with self.subTest(view=view):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        c.execute(f"SELECT * FROM {view} LIMIT 1")
            for view in ("v_zone_current", "v_zone_intervals", "v_alerts"):
                with self.subTest(view=view):
                    c.execute(f"SELECT * FROM {view} LIMIT 1")

    def test_writes_are_refused(self):
        import psycopg
        with self.bot() as c:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("INSERT INTO events(event_id) VALUES ('nope')")

    def test_verify_reports_a_clean_state(self):
        import psycopg
        with psycopg.connect(self.owner_dsn) as c:
            _granted, problems = verify(c, self.ROLE, self.SCHEMA)
        self.assertEqual([], problems)

    def test_verify_notices_a_base_table_leak(self):
        """The check must be able to fail, or it is decoration."""
        import psycopg
        with psycopg.connect(self.owner_dsn, autocommit=True) as c:
            c.execute(f'GRANT SELECT ON cameras TO "{self.ROLE}"')
            try:
                _granted, problems = verify(c, self.ROLE, self.SCHEMA)
                self.assertTrue(any("cameras" in p for p in problems), problems)
            finally:
                c.execute(f'REVOKE ALL ON cameras FROM "{self.ROLE}"')

    def test_the_comments_reached_the_database(self):
        import psycopg
        with psycopg.connect(self.bot_dsn) as c:
            got = c.execute(
                "SELECT obj_description('v_zone_entries'::regclass)").fetchone()[0]
            self.assertIn("person_key", got)

    def test_a_reapply_of_the_views_drops_grants_AND_comments(self):
        """The trap the script's docstring warns about. DROP VIEW discards both
        privileges and comments, so pg_apply.py must always be followed by
        pg_grants.py — which is why that script applies both together.

        This test found its own bug: it originally restored only the grants, so
        every test that ran after it saw a database with no comments. Exactly
        the failure it describes, one layer up.
        """
        import psycopg
        with psycopg.connect(self.owner_dsn, autocommit=True) as c:
            create_all(c, dialect=POSTGRES)          # drop + recreate

            _granted, problems = verify(c, self.ROLE, self.SCHEMA)
            self.assertTrue(problems, "grants unexpectedly survived a re-apply")
            self.assertIsNone(
                c.execute("SELECT obj_description('v_zone_entries'::regclass)"
                          ).fetchone()[0],
                "comments unexpectedly survived a re-apply")

            for stmt in statements(self.ROLE, self.SCHEMA) + comment_sql(POSTGRES):
                c.execute(stmt)

            _granted, problems = verify(c, self.ROLE, self.SCHEMA)
            self.assertEqual([], problems)
            self.assertIn("person_key", c.execute(
                "SELECT obj_description('v_zone_entries'::regclass)").fetchone()[0])


_SCHEMA = """
CREATE TABLE zone_state_ts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT, camera_id TEXT,
  zone_name TEXT, zone_type TEXT, restricted INTEGER, ts REAL,
  occupancy INTEGER, density REAL, capacity_pct REAL, peak_occupancy INTEGER,
  avg_occupancy REAL, trend TEXT, extra TEXT, inflow REAL, outflow REAL,
  status TEXT, site_id TEXT);
CREATE TABLE zone_live(
  camera_id TEXT, zone_id TEXT, site_id TEXT, zone_name TEXT, zone_type TEXT,
  restricted INTEGER, ts REAL, occupancy INTEGER, density REAL,
  capacity_pct REAL, peak_occupancy INTEGER, avg_occupancy REAL, trend TEXT,
  extra TEXT, inflow REAL, outflow REAL, status TEXT,
  PRIMARY KEY (camera_id, zone_id));
CREATE TABLE events(
  event_id TEXT PRIMARY KEY, event_type TEXT, camera_id TEXT, site_id TEXT,
  zone_id TEXT, zone_from TEXT, zone_to TEXT, person_ref TEXT,
  global_ref TEXT, ts REAL, frame TEXT, payload TEXT);
CREATE TABLE alerts(
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, severity TEXT,
  message TEXT, zone_id TEXT, camera_id TEXT, person_ref TEXT, ts REAL,
  frame TEXT, kind TEXT, acknowledged_by TEXT, acknowledged_at REAL,
  status TEXT DEFAULT 'OPEN', note TEXT, resolved_by TEXT, resolved_at REAL,
  site_id TEXT);
CREATE TABLE zones(
  camera_id TEXT, zone_id TEXT, zone_name TEXT, zone_type TEXT,
  restricted INTEGER, capacity_max INTEGER, area_sqm REAL,
  warning_density REAL, critical_density REAL, loitering_threshold_sec REAL,
  colour TEXT, enabled INTEGER, normalized_polygon TEXT, polygon TEXT,
  adjacency_list TEXT, updated_at REAL);
CREATE TABLE cameras(
  camera_id TEXT PRIMARY KEY, site_id TEXT, last_seen REAL, name TEXT,
  state TEXT, source TEXT, stream_url TEXT);
"""


if __name__ == "__main__":
    unittest.main()
