"""SQL views FinBlade's chatbot queries directly.

The ask: store the analytics in a real relational server and put a view on top
that a bot can query. This module holds those view definitions in one place and
emits them for either engine, so the SQL we test on SQLite is the SQL that runs
on Postgres.

WHY THERE IS A `v_timeline` AND NOT ONE WIDE JOIN

"All the data in one view" has two possible readings, and only one of them is a
view rather than an incident.

Joining the three fact tables on (camera_id, zone_id) MULTIPLIES them. Measured
on the live database: 1,674,955 state rows x 1,677,532 events x 332 alerts
comes to 5.66 trillion rows — about 1,900 TB from a 1.15 GB source. A time
series joined to a stream of discrete occurrences does not combine; every state
row pairs with every event in its zone.

UNION ALL onto a shared time axis is the other reading, and it is the useful
one: 3,352,819 rows, the SUM. One row per thing that happened, a `record_type`
discriminator, and the columns that are common to all three filled in. That is
`v_timeline`, and it is what a chatbot should point at for "what happened
between X and Y".

The per-domain views underneath it are richer and are what you want for real
analysis:

  v_zone_intervals  the important one. Since writes became event-driven, a row
                    in zone_state_ts means "and it stayed that way until the
                    next row". This view makes that explicit — valid_from,
                    valid_to and duration_seconds per reading — which turns
                    three awkward questions into ordinary SQL:

                      at a time    WHERE valid_from <= T AND valid_to > T
                      duration     SUM(duration_seconds) WHERE occupancy > 0
                      true average SUM(occupancy * duration_seconds)
                                     / SUM(duration_seconds)

                    The last one is not a nicety. AVG(occupancy) over rows is
                    wrong once rows cover unequal time, and on live data the
                    two answers differ by up to 8x.

  v_zone_events     events with zone and camera context attached.
  v_alerts          alerts with their lifecycle state resolved.
  v_zone_current    one row per zone, the live reading.

WHAT THE VIEWS CANNOT DO FOR YOU

`is_stale` marks a reading that stood for longer than a sample is allowed to
speak for, which is how a killed camera worker shows up — it emits no
CAMERA_OFFLINE, so its gap is invisible except as an implausibly long interval.
Treat those rows as "not observed" rather than as a long quiet period. The
threshold is a deployment setting (FINBLADE_STATE_KEEPALIVE, x2), so it is
baked in when the views are created rather than read from a column.

NO CREDENTIALS IN ANY VIEW. cameras.source holds RTSP URLs with embedded
passwords. It leaked to a read-only key once already; every view here selects
columns explicitly, and none of them selects that one.
"""

from typing import List, Tuple

SQLITE = "sqlite"
POSTGRES = "postgres"
DIALECTS = (SQLITE, POSTGRES)

# Default: twice FINBLADE_STATE_KEEPALIVE (300s). A reading that stood longer
# than this is more likely a dead worker than a quiet zone.
DEFAULT_MAX_HOLD = 600.0


def _utc(dialect: str, column: str) -> str:
    """Epoch seconds -> a real timestamp, for humans and BI tools.

    Timestamps are stored as epoch doubles because that is what the API speaks
    end to end, and converting at the storage boundary was a bug source. The
    conversion belongs in the view, where it costs nothing and gives a SQL
    client something it can put in a WHERE clause.
    """
    if dialect == POSTGRES:
        return f"to_timestamp({column})"
    return f"datetime({column}, 'unixepoch')"


def _bool(dialect: str, expr: str) -> str:
    """SQLite has no boolean type; Postgres does."""
    return expr if dialect == POSTGRES else f"CASE WHEN {expr} THEN 1 ELSE 0 END"


def view_definitions(dialect: str = SQLITE,
                     max_hold: float = DEFAULT_MAX_HOLD,
                     temp: bool = False) -> List[Tuple[str, str]]:
    """[(view_name, CREATE VIEW sql)], in dependency order.

    `temp` emits CREATE TEMP VIEW, which is how these get exercised against the
    production database without writing to it: the view lives in the session's
    temp schema while the real file stays open read-only. Unqualified table
    names still resolve to it, so the SQL under test is the SQL that ships.
    """
    if dialect not in DIALECTS:
        raise ValueError(f"dialect must be one of {DIALECTS}")

    ts = lambda col: _utc(dialect, col)          # noqa: E731
    views: List[Tuple[str, str]] = []

    # ---------------------------------------------------------------- zones --
    # Config joined onto every reading. This join is one-to-one — a zone has
    # exactly one definition — so it enriches without multiplying. It is the
    # join that "do not join the data" does NOT mean.
    views.append(("v_zone_intervals", f"""
CREATE VIEW v_zone_intervals AS
SELECT
    s.camera_id,
    s.zone_id,
    s.site_id,
    COALESCE(z.zone_name, s.zone_name)          AS zone_name,
    COALESCE(z.zone_type, s.zone_type)          AS zone_type,
    s.ts                                        AS valid_from,
    {ts('s.ts')}                                AS valid_from_utc,
    LEAD(s.ts) OVER w                           AS valid_to,
    {ts('LEAD(s.ts) OVER w')}                   AS valid_to_utc,
    LEAD(s.ts) OVER w - s.ts                    AS duration_seconds,
    s.occupancy,
    s.density,
    s.capacity_pct,
    s.status,
    s.trend,
    s.inflow,
    s.outflow,
    z.capacity_max,
    z.area_sqm,
    {_bool(dialect, 'COALESCE(z.restricted, s.restricted) = 1')} AS restricted,
    -- A reading that stood for longer than one sample may speak for. The
    -- camera worker was almost certainly not running; treat the span as
    -- unobserved rather than as a long quiet period.
    {_bool(dialect, f'LEAD(s.ts) OVER w - s.ts > {max_hold}')}   AS is_stale,
    -- The newest reading per zone has no successor. It is current, not stale.
    {_bool(dialect, 'LEAD(s.ts) OVER w IS NULL')}                AS is_open
FROM zone_state_ts s
LEFT JOIN zones z
       ON z.zone_id = s.zone_id AND z.camera_id = s.camera_id
WINDOW w AS (PARTITION BY s.camera_id, s.zone_id ORDER BY s.ts)
""".strip()))

    views.append(("v_zone_current", f"""
CREATE VIEW v_zone_current AS
SELECT
    l.camera_id, l.zone_id, l.site_id,
    COALESCE(z.zone_name, l.zone_name) AS zone_name,
    COALESCE(z.zone_type, l.zone_type) AS zone_type,
    l.occupancy, l.density, l.capacity_pct, l.status, l.trend,
    l.peak_occupancy, l.inflow, l.outflow,
    z.capacity_max, z.area_sqm,
    {_bool(dialect, 'COALESCE(z.restricted, l.restricted) = 1')} AS restricted,
    l.ts                AS reading_ts,
    {ts('l.ts')}        AS reading_utc
FROM zone_live l
LEFT JOIN zones z ON z.zone_id = l.zone_id AND z.camera_id = l.camera_id
""".strip()))

    # --------------------------------------------------------------- events --
    views.append(("v_zone_events", f"""
CREATE VIEW v_zone_events AS
SELECT
    e.event_id, e.event_type, e.camera_id, e.site_id,
    e.zone_id, e.zone_from, e.zone_to,
    -- An anonymous, per-session hash. Not a person, not stable across a
    -- restart, and not joinable to anything outside this database.
    e.person_ref,
    e.ts            AS event_ts,
    {ts('e.ts')}    AS event_utc,
    COALESCE(z.zone_name, e.zone_id) AS zone_name,
    z.zone_type,
    {_bool(dialect, 'z.restricted = 1')} AS restricted
FROM events e
LEFT JOIN zones z ON z.zone_id = e.zone_id AND z.camera_id = e.camera_id
""".strip()))

    # --------------------------------------------------------------- alerts --
    views.append(("v_alerts", f"""
CREATE VIEW v_alerts AS
SELECT
    a.alert_id, a.rule_id, a.severity, a.message,
    a.camera_id, a.zone_id, a.site_id,
    COALESCE(a.status, 'OPEN')  AS status,
    a.acknowledged_by, a.acknowledged_at,
    a.resolved_by, a.resolved_at, a.note,
    a.ts            AS raised_ts,
    {ts('a.ts')}    AS raised_utc,
    {_bool(dialect, "COALESCE(a.status,'OPEN') IN ('OPEN','ACK')")} AS is_active,
    COALESCE(z.zone_name, a.zone_id) AS zone_name
FROM alerts a
LEFT JOIN zones z ON z.zone_id = a.zone_id AND z.camera_id = a.camera_id
""".strip()))

    # ------------------------------------------------------------- timeline --
    # The single view the chatbot points at. UNION ALL, not a join: this is the
    # SUM of the three tables (3.3M rows), where joining them is the PRODUCT
    # (5.66 trillion). Columns that do not apply to a record type are NULL,
    # which is the honest encoding — an alert has no occupancy.
    views.append(("v_timeline", f"""
CREATE VIEW v_timeline AS
SELECT 'zone_state' AS record_type,
       s.ts AS ts, {ts('s.ts')} AS ts_utc,
       s.camera_id, s.zone_id, s.site_id,
       s.status                AS detail,
       s.occupancy, s.density, s.capacity_pct,
       CAST(NULL AS TEXT)      AS event_type,
       CAST(NULL AS TEXT)      AS person_ref,
       CAST(NULL AS TEXT)      AS rule_id,
       CAST(NULL AS TEXT)      AS severity,
       CAST(NULL AS TEXT)      AS message
FROM zone_state_ts s
UNION ALL
SELECT 'event',
       e.ts, {ts('e.ts')},
       e.camera_id, e.zone_id, e.site_id,
       e.event_type,
       NULL, NULL, NULL,
       e.event_type, e.person_ref,
       NULL, NULL, NULL
FROM events e
UNION ALL
SELECT 'alert',
       a.ts, {ts('a.ts')},
       a.camera_id, a.zone_id, a.site_id,
       COALESCE(a.status, 'OPEN'),
       NULL, NULL, NULL,
       NULL, NULL,
       a.rule_id, a.severity, a.message
FROM alerts a
""".strip()))

    if temp:
        views = [(name, sql.replace("CREATE VIEW ", "CREATE TEMP VIEW ", 1))
                 for name, sql in views]
    return views


def view_names(dialect: str = SQLITE) -> List[str]:
    return [name for name, _ in view_definitions(dialect)]


def drop_sql(dialect: str = SQLITE) -> List[str]:
    """DROP statements, reverse dependency order. Idempotent."""
    return [f"DROP VIEW IF EXISTS {name}" for name in reversed(view_names(dialect))]


def create_all(conn, dialect: str = SQLITE, max_hold: float = DEFAULT_MAX_HOLD,
               temp: bool = False) -> List[str]:
    """(Re)create every view on an open DB-API connection. Returns the names."""
    names = []
    for stmt in drop_sql(dialect):
        conn.execute(stmt)
    for name, sql in view_definitions(dialect, max_hold=max_hold, temp=temp):
        conn.execute(sql)
        names.append(name)
    return names
