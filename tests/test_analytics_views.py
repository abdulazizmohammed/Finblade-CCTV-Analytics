"""The SQL views FinBlade's chatbot queries.

Executed against SQLite here because that runs everywhere. The Postgres dialect
is checked by text, and exercised for real by scripts/pg_verify_views.py
against a live server — these tests cannot start one.

The interesting assertions are about semantics, not syntax: that the timeline
is a union rather than a join, that a reading knows how long it stood, and that
no view can reach a credential.
"""

import sqlite3
import unittest

from services.api.analytics_views import (DIALECTS, POSTGRES, SQLITE,
                                          create_all, drop_sql,
                                          view_definitions, view_names)

SCHEMA = """
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

T0 = 1_700_000_000.0


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO zones(camera_id, zone_id, zone_name, zone_type, "
            "restricted, capacity_max, area_sqm) "
            "VALUES ('CAM-01','ZONE-01','Lobby','MONITORED',0,40,60.0)")
        self.conn.execute(
            "INSERT INTO cameras(camera_id, site_id, source, stream_url) VALUES "
            "('CAM-01','SITE-01','rtsp://admin:hunter2@10.0.0.5:554/s1',"
            "'rtsp://admin:hunter2@10.0.0.5:554/s1')")

    def state(self, ts, occupancy, status="NORMAL", zone="ZONE-01", cam="CAM-01"):
        self.conn.execute(
            "INSERT INTO zone_state_ts(zone_id, camera_id, zone_name, ts, "
            "occupancy, density, capacity_pct, status, site_id, restricted) "
            "VALUES (?,?,?,?,?,?,?,?,?,0)",
            (zone, cam, "Lobby", ts, occupancy, occupancy / 60.0,
             occupancy * 2.5, status, "SITE-01"))

    def build(self, **kw):
        return create_all(self.conn, temp=True, **kw)

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()


class TestZoneIntervals(Base):
    def test_a_reading_knows_how_long_it_stood(self):
        """The point of the view. Since writes became event-driven a row means
        'and it stayed that way', and duration is what every real question
        needs."""
        self.state(T0, 0)
        self.state(T0 + 600, 4)
        self.state(T0 + 900, 0)
        self.build()
        got = self.rows("SELECT occupancy, duration_seconds FROM v_zone_intervals "
                        "ORDER BY valid_from")
        self.assertEqual([(0, 600.0), (4, 300.0), (0, None)],
                         [(r["occupancy"], r["duration_seconds"]) for r in got])

    def test_the_newest_reading_is_open_not_stale(self):
        """It has no successor because it is current, which is the opposite of
        being unobserved. Conflating them would mark every zone stale."""
        self.state(T0, 3)
        self.build()
        r = self.rows("SELECT is_open, is_stale FROM v_zone_intervals")[0]
        self.assertEqual(1, r["is_open"])
        self.assertEqual(0, r["is_stale"])

    def test_an_implausibly_long_reading_is_flagged_stale(self):
        """A killed worker emits no CAMERA_OFFLINE, so an interval far longer
        than the keepalive is the only trace it leaves."""
        self.state(T0, 5)
        self.state(T0 + 100_000, 5)
        self.build(max_hold=600.0)
        got = self.rows("SELECT is_stale FROM v_zone_intervals ORDER BY valid_from")
        self.assertEqual([1, 0], [r["is_stale"] for r in got])

    def test_intervals_are_per_camera_and_zone(self):
        """Two cameras' ZONE-01 are different places. A window partitioned on
        zone_id alone would compute durations across both."""
        self.state(T0, 1, cam="CAM-01")
        self.state(T0 + 10, 9, cam="CAM-02")
        self.state(T0 + 600, 2, cam="CAM-01")
        self.build()
        got = self.rows("SELECT camera_id, duration_seconds FROM v_zone_intervals "
                        "WHERE camera_id='CAM-01' ORDER BY valid_from")
        self.assertEqual(600.0, got[0]["duration_seconds"])

    def test_zone_config_is_joined_without_multiplying(self):
        """The one join that IS wanted: a zone has exactly one definition, so
        this enriches rather than duplicating."""
        self.state(T0, 1)
        self.state(T0 + 60, 2)
        self.build()
        got = self.rows("SELECT capacity_max, area_sqm, zone_name "
                        "FROM v_zone_intervals")
        self.assertEqual(2, len(got), "two readings in, two rows out")
        self.assertEqual(40, got[0]["capacity_max"])
        self.assertEqual(60.0, got[0]["area_sqm"])

    def test_a_reading_for_an_unconfigured_zone_still_appears(self):
        """History outlives the polygon; a LEFT JOIN keeps it visible."""
        self.state(T0, 1, zone="ZONE-99")
        self.build()
        got = self.rows("SELECT zone_id, capacity_max FROM v_zone_intervals "
                        "WHERE zone_id='ZONE-99'")
        self.assertEqual(1, len(got))
        self.assertIsNone(got[0]["capacity_max"])

    def test_the_time_weighted_average_is_one_expression(self):
        """4 people for 10 minutes then 0 for 50. Averaging the two rows says
        2; weighting by duration says 0.67."""
        self.state(T0, 4)
        self.state(T0 + 600, 0)
        self.state(T0 + 3600, 0)
        self.build()
        r = self.rows("""
            SELECT AVG(occupancy) naive,
                   SUM(occupancy * duration_seconds)
                     / SUM(duration_seconds) weighted
            FROM v_zone_intervals WHERE duration_seconds IS NOT NULL""")[0]
        self.assertAlmostEqual(2.0, r["naive"])
        self.assertAlmostEqual(4 * 600 / 3600, r["weighted"], places=4)

    def test_point_in_time_matches_exactly_one_interval(self):
        for i, occ in enumerate([0, 3, 7, 0]):
            self.state(T0 + i * 300, occ)
        self.build()
        at = T0 + 650
        got = self.rows("""
            SELECT occupancy FROM v_zone_intervals
            WHERE valid_from <= ? AND (valid_to > ? OR valid_to IS NULL)""",
                        (at, at))
        self.assertEqual(1, len(got))
        self.assertEqual(7, got[0]["occupancy"])


class TestTimeline(Base):
    def setUp(self):
        super().setUp()
        self.state(T0, 1)
        self.state(T0 + 60, 0)
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, site_id, "
            "zone_id, person_ref, ts) VALUES "
            "('e1','ZONE_ENTRY','CAM-01','SITE-01','ZONE-01','pr_aaaa',?)", (T0 + 5,))
        self.conn.execute(
            "INSERT INTO alerts(rule_id, severity, message, zone_id, camera_id, "
            "ts, status, site_id) VALUES "
            "('R-06','CRITICAL','intrusion','ZONE-01','CAM-01',?,'OPEN','SITE-01')",
            (T0 + 30,))
        self.build()

    def test_it_is_a_union_not_a_join(self):
        """The measured alternative on live data was 5.66 trillion rows from a
        1.15 GB source. This is the sum: 2 + 1 + 1."""
        n = self.rows("SELECT COUNT(*) c FROM v_timeline")[0]["c"]
        self.assertEqual(4, n)

    def test_each_record_type_is_labelled(self):
        got = {r["record_type"]: r["c"] for r in self.rows(
            "SELECT record_type, COUNT(*) c FROM v_timeline GROUP BY record_type")}
        self.assertEqual({"zone_state": 2, "event": 1, "alert": 1}, got)

    def test_inapplicable_columns_are_null_not_zero(self):
        """An alert has no occupancy. Reporting 0 would put it on a chart."""
        r = self.rows("SELECT occupancy, density FROM v_timeline "
                      "WHERE record_type='alert'")[0]
        self.assertIsNone(r["occupancy"])
        self.assertIsNone(r["density"])

    def test_it_orders_as_one_stream(self):
        got = [r["record_type"] for r in self.rows(
            "SELECT record_type FROM v_timeline ORDER BY ts")]
        self.assertEqual(["zone_state", "event", "alert", "zone_state"], got)

    def test_a_window_query_spans_all_three_types(self):
        got = self.rows("SELECT COUNT(*) c FROM v_timeline WHERE ts BETWEEN ? AND ?",
                        (T0 + 1, T0 + 59))[0]["c"]
        self.assertEqual(2, got, "the event and the alert")


class TestZoneEventsNamesTheZone(Base):
    """A movement event leaves zone_id NULL and carries the zone in zone_to or
    zone_from. Joining on zone_id alone therefore resolved zone_name to NULL for
    every ZONE_ENTRY, ZONE_EXIT and ZONE_TRANSITION — the three types anyone
    asking "who entered" needs — and `WHERE zone_name = '...'` returned nothing
    with no error. Live, a query that should have answered 15 answered 0."""

    def event(self, eid, etype, zone_id=None, zone_from=None, zone_to=None,
              person="pr_a", gref=None, ts=T0, cam="CAM-01"):
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, site_id, "
            "zone_id, zone_from, zone_to, person_ref, global_ref, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, etype, cam, "SITE-01", zone_id, zone_from, zone_to,
             person, gref, ts))

    def test_an_arrival_is_named_by_the_zone_it_arrived_in(self):
        self.event("e1", "ZONE_ENTRY", zone_to="ZONE-01")
        self.build()
        got = self.rows("SELECT zone_name, zone_ref FROM v_zone_events")
        self.assertEqual("Lobby", got[0]["zone_name"])
        self.assertEqual("ZONE-01", got[0]["zone_ref"])

    def test_a_departure_is_named_by_the_zone_it_left(self):
        self.event("e1", "ZONE_EXIT", zone_from="ZONE-01")
        self.build()
        self.assertEqual("Lobby", self.rows(
            "SELECT zone_name FROM v_zone_events")[0]["zone_name"])

    def test_a_transition_is_attributed_to_where_they_went(self):
        # Arrival wins over departure: a person belongs to where they are now.
        self.conn.execute(
            "INSERT INTO zones(camera_id, zone_id, zone_name) "
            "VALUES ('CAM-01','ZONE-02','Corridor')")
        self.event("e1", "ZONE_TRANSITION", zone_from="ZONE-01", zone_to="ZONE-02")
        self.build()
        self.assertEqual("Corridor", self.rows(
            "SELECT zone_name FROM v_zone_events")[0]["zone_name"])

    def test_an_in_place_event_still_uses_zone_id(self):
        self.event("e1", "LOITERING_START", zone_id="ZONE-01")
        self.build()
        self.assertEqual("Lobby", self.rows(
            "SELECT zone_name FROM v_zone_events")[0]["zone_name"])

    def test_filtering_by_zone_name_finds_movement_events(self):
        """The exact query that returned 0 on the live server."""
        for i in range(3):
            self.event(f"e{i}", "ZONE_ENTRY", zone_to="ZONE-01", ts=T0 + i)
        self.build()
        got = self.rows("SELECT COUNT(*) AS n FROM v_zone_events "
                        "WHERE event_type = 'ZONE_ENTRY' AND zone_name = 'Lobby'")
        self.assertEqual(3, got[0]["n"])

    def test_global_ref_is_exposed(self):
        self.event("e1", "ZONE_ENTRY", zone_to="ZONE-01", gref="gp_abc")
        self.build()
        self.assertEqual("gp_abc", self.rows(
            "SELECT global_ref FROM v_zone_events")[0]["global_ref"])

    def test_an_unconfigured_zone_falls_back_to_its_id(self):
        self.event("e1", "ZONE_ENTRY", zone_to="ZONE-NOPE")
        self.build()
        self.assertEqual("ZONE-NOPE", self.rows(
            "SELECT zone_name FROM v_zone_events")[0]["zone_name"])

    def test_the_zone_join_is_per_camera(self):
        # Zone ids are unique only within a camera; CAM-02's ZONE-01 is a
        # different place and must not borrow CAM-01's name.
        self.event("e1", "ZONE_ENTRY", zone_to="ZONE-01", cam="CAM-02")
        self.build()
        self.assertEqual("ZONE-01", self.rows(
            "SELECT zone_name FROM v_zone_events")[0]["zone_name"])


class TestZoneEntries(Base):
    """Counting arrivals without having to know the derived-event rule."""

    def entry(self, eid, gref=None, person="pr_a", zone="ZONE-01",
              cam="CAM-01", ts=T0):
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, site_id, "
            "zone_to, person_ref, global_ref, ts) "
            "VALUES (?,'ZONE_ENTRY',?,?,?,?,?,?)",
            (eid, cam, "SITE-01", zone, person, gref, ts))

    def transition(self, eid, ts=T0):
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, site_id, "
            "zone_from, zone_to, person_ref, ts) "
            "VALUES (?,'ZONE_TRANSITION','CAM-01','SITE-01','ZONE-02',"
            "'ZONE-01','pr_a',?)", (eid, ts))

    def test_a_transition_does_not_double_count(self):
        """The reason this view exists. A move emits ZONE_TRANSITION plus a
        derived ZONE_ENTRY/ZONE_EXIT pair; counting entries AND transitions
        counts the movement twice, and the inflated number looks plausible."""
        self.entry("e1-derived", gref="gp_1")      # the derived half
        self.transition("e1")                      # the authoritative record
        self.build()
        self.assertEqual(1, self.rows(
            "SELECT COUNT(*) AS n FROM v_zone_entries")[0]["n"])

    def test_distinct_people_uses_the_identity_that_survives_track_breaks(self):
        # One human, three tracker refs, one global ref.
        for i, pr in enumerate(("pr_a", "pr_b", "pr_c")):
            self.entry(f"e{i}", gref="gp_1", person=pr, ts=T0 + i)
        self.build()
        got = self.rows("SELECT COUNT(DISTINCT person_key) AS people, "
                        "COUNT(DISTINCT person_ref) AS tracks FROM v_zone_entries")
        self.assertEqual(1, got[0]["people"])
        self.assertEqual(3, got[0]["tracks"], "person_ref measures churn")

    def test_unresolved_tracks_are_scoped_to_their_camera(self):
        # Two different people, both track 17, on two cameras. Without the
        # camera prefix they would collapse into one.
        self.entry("e1", person="pr_17", cam="CAM-01")
        self.entry("e2", person="pr_17", cam="CAM-02")
        self.build()
        self.assertEqual(2, self.rows(
            "SELECT COUNT(DISTINCT person_key) AS n FROM v_zone_entries")[0]["n"])

    def test_resolution_is_reported_so_a_count_can_be_qualified(self):
        self.entry("e1", gref="gp_1")
        self.entry("e2", ts=T0 + 1)
        self.build()
        got = self.rows("SELECT identity_resolved, COUNT(*) AS n "
                        "FROM v_zone_entries GROUP BY identity_resolved "
                        "ORDER BY identity_resolved")
        self.assertEqual([(0, 1), (1, 1)],
                         [(r["identity_resolved"], r["n"]) for r in got])

    def test_it_carries_the_zone_name(self):
        self.entry("e1", gref="gp_1")
        self.build()
        self.assertEqual("Lobby", self.rows(
            "SELECT zone_name FROM v_zone_entries")[0]["zone_name"])

    def test_only_arrivals_appear(self):
        self.entry("e1", gref="gp_1")
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, camera_id, zone_from, "
            "person_ref, ts) VALUES ('e2','ZONE_EXIT','CAM-01','ZONE-01','pr_a',?)",
            (T0,))
        self.build()
        self.assertEqual(1, self.rows(
            "SELECT COUNT(*) AS n FROM v_zone_entries")[0]["n"])

    def test_the_question_that_started_this(self):
        """'How many unique people entered <zone> today', as one query."""
        for i in range(4):
            self.entry(f"a{i}", gref="gp_1", person=f"pr_{i}", ts=T0 + i)
        for i in range(2):
            self.entry(f"b{i}", gref="gp_2", person=f"pr_x{i}", ts=T0 + 10 + i)
        self.entry("c0", ts=T0 + 20)                      # never resolved
        self.build()
        got = self.rows("""
            SELECT COUNT(DISTINCT person_key) AS people,
                   COUNT(DISTINCT global_ref)  AS identified,
                   COUNT(*)                    AS entries
            FROM v_zone_entries
            WHERE zone_name = 'Lobby'
              AND event_ts >= ? AND event_ts < ?""", (T0, T0 + 86400))
        self.assertEqual(3, got[0]["people"], "two identified plus one unresolved")
        self.assertEqual(2, got[0]["identified"])
        self.assertEqual(7, got[0]["entries"])


class TestNoCredentialsAnywhere(Base):
    """cameras.source holds RTSP URLs with passwords. It reached a read-only
    key once already; no view may select it."""

    def test_no_view_selects_a_credential_column(self):
        self.state(T0, 1)
        names = self.build()
        for name in names:
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({name})")]
            for banned in ("source", "stream_url", "rtsp_url"):
                self.assertNotIn(banned, cols, f"{name} exposes {banned}")

    def test_no_view_returns_a_url_with_userinfo(self):
        self.state(T0, 1)
        for name in self.build():
            for row in self.conn.execute(f"SELECT * FROM {name}"):
                for value in tuple(row):
                    if isinstance(value, str):
                        self.assertNotIn("hunter2", value, f"{name} leaked a password")

    def test_the_cameras_table_is_not_referenced_at_all(self):
        """Belt and braces: the safest way not to leak that column is for no
        view to touch the table it lives in.

        Comments are stripped before the check. This used to scan the raw text
        and fired on a comment containing the word "cameras" — prose, not a
        reference. A guard that cries wolf gets weakened by whoever hits it
        next, so it checks executable SQL instead. The two tests above are the
        real protection: they inspect the columns each view actually exposes and
        the values it actually returns.
        """
        for _name, sql in view_definitions(SQLITE):
            code = "\n".join(line.split("--")[0] for line in sql.splitlines())
            self.assertNotIn(" cameras", code)
            self.assertNotIn("\tcameras", code)

    def test_the_guard_still_catches_a_real_reference(self):
        """The stripping must not have made the check unable to fail."""
        sql = "CREATE VIEW v AS SELECT c.name FROM cameras c  -- harmless words"
        code = "\n".join(line.split("--")[0] for line in sql.splitlines())
        self.assertIn(" cameras", code)


class TestDialects(unittest.TestCase):
    def test_both_dialects_define_the_same_views(self):
        self.assertEqual(view_names(SQLITE), view_names(POSTGRES))

    def test_postgres_uses_to_timestamp_and_sqlite_uses_unixepoch(self):
        pg = dict(view_definitions(POSTGRES))["v_zone_intervals"]
        lite = dict(view_definitions(SQLITE))["v_zone_intervals"]
        self.assertIn("to_timestamp(", pg)
        self.assertNotIn("unixepoch", pg)
        self.assertIn("unixepoch", lite)

    def test_postgres_emits_real_booleans_sqlite_emits_integers(self):
        pg = dict(view_definitions(POSTGRES))["v_zone_intervals"]
        lite = dict(view_definitions(SQLITE))["v_zone_intervals"]
        self.assertNotIn("CASE WHEN", pg.split("is_stale")[0][-120:])
        self.assertIn("CASE WHEN", lite)

    def test_an_unknown_dialect_is_refused(self):
        with self.assertRaises(ValueError):
            view_definitions("mysql")

    def test_drop_statements_are_idempotent_and_reversed(self):
        drops = drop_sql(SQLITE)
        self.assertTrue(all(d.startswith("DROP VIEW IF EXISTS") for d in drops))
        self.assertEqual(list(reversed(view_names(SQLITE))),
                         [d.rsplit(" ", 1)[1] for d in drops])

    def test_temp_views_are_marked_temp(self):
        """How these get tested against production without writing to it."""
        for _n, sql in view_definitions(SQLITE, temp=True):
            self.assertTrue(sql.startswith("CREATE TEMP VIEW"))

    def test_the_dialect_list_is_what_it_claims(self):
        self.assertEqual(("sqlite", "postgres"), DIALECTS)


class TestRebuildIsIdempotent(Base):
    def test_creating_twice_does_not_fail(self):
        self.state(T0, 1)
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
