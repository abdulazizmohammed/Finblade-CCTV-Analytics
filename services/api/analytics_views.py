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

  v_zone_events     events with zone and camera context attached. Resolves the
                    zone from whichever column carried it, so movement events
                    are filterable by name like everything else.
  v_zone_entries    one row per arrival in a zone, with a person_key that is
                    safe to COUNT(DISTINCT). Encodes the derived-event rule so
                    nobody has to remember it.
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
    # WHICH COLUMN HOLDS THE ZONE depends on the event type, and getting this
    # wrong is silent. A movement event leaves `zone_id` NULL and puts the zone
    # in `zone_to` (arrival) or `zone_from` (departure); only the in-place
    # events — density, loitering, restricted, capacity — populate `zone_id`.
    #
    # This view used to join on e.zone_id alone. For every ZONE_ENTRY, ZONE_EXIT
    # and ZONE_TRANSITION row the join therefore missed, zone_name resolved to
    # NULL, and `WHERE zone_name = 'GF Ele-Stairs'` returned zero rows with no
    # error — for the three event types anyone asking "who entered" actually
    # needs. Observed live: a query that should have returned 15 returned 0.
    #
    # zone_ref picks the zone the row is ABOUT: zone_id when present, else the
    # arrival, else the departure. Arrival before departure because a person is
    # attributed to where they went.
    zone_ref = "COALESCE(e.zone_id, e.zone_to, e.zone_from)"
    # What to COUNT(DISTINCT) when counting people. Defined once and used
    # by both event views, because two spellings of "who is this" is how
    # they end up disagreeing.
    person_key = "COALESCE(e.global_ref, e.camera_id || ':' || e.person_ref)"
    views.append(("v_zone_events", f"""
CREATE VIEW v_zone_events AS
SELECT
    e.event_id, e.event_type, e.camera_id, e.site_id,
    e.zone_id, e.zone_from, e.zone_to,
    -- The zone this row is about, whichever column carried it.
    {zone_ref} AS zone_ref,
    -- An anonymous, per-session hash of the TRACKER id. Not a person: it
    -- changes every time tracking breaks, so COUNT(DISTINCT person_ref)
    -- measures track churn. Counting people needs person_key below.
    e.person_ref,
    -- The safe thing to COUNT(DISTINCT). Same expression as v_zone_entries.
    {person_key} AS person_key,
    -- The cross-camera identity, and the only ref that survives a track break
    -- or a walk between cameras. NULL when ReID had not resolved the track —
    -- and NULL on every row written before this column existed, so a count
    -- over old history reads zero rather than wrong.
    e.global_ref,
    e.ts            AS event_ts,
    {ts('e.ts')}    AS event_utc,
    COALESCE(z.zone_name, {zone_ref}) AS zone_name,
    z.zone_type,
    {_bool(dialect, 'z.restricted = 1')} AS restricted
FROM events e
LEFT JOIN zones z
       ON z.zone_id = {zone_ref} AND z.camera_id = e.camera_id
""".strip()))

    # One row per person ARRIVING in a zone, already de-duplicated.
    #
    # Exists because the obvious query is wrong in a way that looks right. A
    # confirmed move between zones emits THREE rows: the authoritative
    # ZONE_TRANSITION plus a derived ZONE_EXIT/ZONE_ENTRY pair. Counting
    # ZONE_ENTRY *and* ZONE_TRANSITION therefore counts every movement twice,
    # and the inflated figure is entirely plausible. Counting ZONE_ENTRY alone
    # is correct — the derived pair means every transition already has one — but
    # that is a rule you have to know, and nothing enforced it.
    views.append(("v_zone_entries", f"""
CREATE VIEW v_zone_entries AS
SELECT
    e.event_id,
    e.camera_id,
    e.site_id,
    e.zone_to                          AS zone_id,
    COALESCE(z.zone_name, e.zone_to)   AS zone_name,
    z.zone_type,
    e.person_ref,
    e.global_ref,
    -- What to count as one person. Falls back to a camera-scoped tracker ref
    -- when ReID has not resolved the track, so two unresolved people are never
    -- merged by both happening to be track 17 on different cameras. Unresolved
    -- entries OVER-count rather than under-count, which is the same bias the
    -- rest of the system takes.
    {person_key} AS person_key,
    {_bool(dialect, 'e.global_ref IS NOT NULL')} AS identity_resolved,
    e.ts            AS event_ts,
    {ts('e.ts')}    AS event_utc
FROM events e
LEFT JOIN zones z
       ON z.zone_id = e.zone_to AND z.camera_id = e.camera_id
WHERE e.event_type = 'ZONE_ENTRY'
  AND e.zone_to IS NOT NULL
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
-- zone_id here is the raw column, so a movement event contributes NULL. That
-- is deliberate: v_timeline answers "what happened between X and Y" on one
-- time axis, and inventing a zone for it would make the column mean different
-- things per record_type. Use v_zone_events when the zone matters.
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


# --------------------------------------------------------------------------
# What a read-only or chatbot role may see.
#
# Prompting a model not to use a column is advice; not granting it is a rule.
# A text-to-SQL bot wrote COUNT(DISTINCT person_ref) against this schema and got
# a plausible wrong number back — that failure is silent, and the same prompt
# will be edited by someone who was not there when it was explained. Column
# privileges turn it into "permission denied", which the bot can react to and a
# human will notice.
#
# person_ref is excluded from every view. It is a hash of the tracker id, so
# counting it measures churn; person_key is the column that answers the same
# question correctly and is exposed in its place.
#
# Nothing here grants a base table. cameras.source holds RTSP URLs with embedded
# passwords, and a view runs with its OWNER's privileges — so a role granted
# SELECT on the views needs no access to the tables underneath and must not have
# any.
SAFE_COLUMNS = {
    "v_zone_current": (
        "camera_id", "zone_id", "site_id", "zone_name", "zone_type",
        "occupancy", "density", "capacity_pct", "status", "trend",
        "peak_occupancy", "inflow", "outflow", "capacity_max", "area_sqm",
        "restricted", "reading_ts", "reading_utc",
    ),
    "v_zone_intervals": (
        "camera_id", "zone_id", "site_id", "zone_name", "zone_type",
        "valid_from", "valid_from_utc", "valid_to", "valid_to_utc",
        "duration_seconds", "occupancy", "density", "capacity_pct", "status",
        "trend", "inflow", "outflow", "capacity_max", "area_sqm", "restricted",
        "is_stale", "is_open",
    ),
    "v_zone_events": (
        "event_id", "event_type", "camera_id", "site_id",
        "zone_id", "zone_from", "zone_to", "zone_ref",
        "person_key", "global_ref", "event_ts", "event_utc",
        "zone_name", "zone_type", "restricted",
    ),
    "v_zone_entries": (
        "event_id", "camera_id", "site_id", "zone_id", "zone_name", "zone_type",
        "person_key", "global_ref", "identity_resolved", "event_ts", "event_utc",
    ),
    "v_alerts": (
        "alert_id", "rule_id", "severity", "message", "camera_id", "zone_id",
        "site_id", "status", "acknowledged_by", "acknowledged_at",
        "resolved_by", "resolved_at", "note", "raised_ts", "raised_utc",
        "is_active", "zone_name",
    ),
    "v_timeline": (
        "record_type", "ts", "ts_utc", "camera_id", "zone_id", "site_id",
        "detail", "occupancy", "density", "capacity_pct", "event_type",
        "rule_id", "severity", "message",
    ),
}


# Warnings that travel WITH the schema rather than in a prompt somebody has to
# remember to paste. Most text-to-SQL tooling reads these when it introspects,
# and unlike documentation they cannot drift from the view they describe.
#
# Each one states the wrong query, because "use person_key" is forgettable and
# "never COUNT(DISTINCT person_ref), it measures churn" is not.
_VIEW_COMMENTS = {
    "v_zone_current": (
        "Live reading, one row per zone per camera. Never SUM(occupancy) for a "
        "site total: two cameras on one room both report the person in the "
        "overlap. Use the facility roster instead."),
    "v_zone_intervals": (
        "History, one row per reading with the time it stayed valid. Averages "
        "MUST be time-weighted: SUM(occupancy*duration_seconds)/"
        "SUM(duration_seconds). Plain AVG(occupancy) has been wrong by 8x on "
        "this data. Exclude is_stale."),
    "v_zone_events": (
        "All zone events with the zone resolved from whichever column carried "
        "it. For counting arrivals use v_zone_entries, which already excludes "
        "the duplicate rows a transition emits."),
    "v_zone_entries": (
        "One row per arrival in a zone. Already handles the derived-event rule "
        "— NEVER union this with ZONE_TRANSITION, a single movement emits "
        "both and the total doubles. COUNT(DISTINCT person_key) for people."),
    "v_alerts": (
        "Alerts with lifecycle resolved. status is NULL on an untouched alert, "
        "so filter is_active rather than status='OPEN'."),
    "v_timeline": (
        "Every record on one time axis: a UNION, not a join. Joining the fact "
        "tables instead would produce 5.66 trillion rows from a 1.15GB source. "
        "zone_id is the raw column, so movement events contribute NULL — use "
        "v_zone_events when the zone matters."),
}

_COLUMN_COMMENTS = {
    ("v_zone_entries", "person_key"): (
        "COUNT(DISTINCT this) to count people. Falls back to a camera-scoped "
        "tracker ref when ReID has not resolved the track, so unresolved "
        "entries over-count rather than merging two strangers."),
    ("v_zone_entries", "global_ref"): (
        "Cross-camera identity. NULL when ReID had not resolved the track, and "
        "NULL on all history written before the column existed."),
    ("v_zone_entries", "identity_resolved"): (
        "Whether this row has a real identity behind it. Report the share that "
        "is true alongside any count; a camera at 0% gives an upper bound."),
    ("v_zone_events", "person_key"): (
        "COUNT(DISTINCT this) to count people, never person_ref."),
    ("v_zone_events", "zone_ref"): (
        "The zone this row is about: zone_id when present, else zone_to, else "
        "zone_from. Movement events leave zone_id NULL."),
    ("v_zone_intervals", "duration_seconds"): (
        "How long this reading stood. NULL on the newest row per zone, which "
        "is open rather than zero-length. Weight every average by this."),
    ("v_zone_intervals", "is_stale"): (
        "The reading stood longer than a sample may speak for — almost always "
        "a dead camera worker. Treat as unobserved, not as a quiet period."),
    ("v_zone_current", "occupancy"): (
        "People in THIS camera's polygon. Not additive across cameras."),
    ("v_alerts", "is_active"): (
        "True for OPEN and ACK. Use this rather than status, which is NULL "
        "until somebody touches the alert."),
}


def comment_sql(dialect: str = POSTGRES) -> List[str]:
    """COMMENT ON statements for the views and their sharpest columns.

    Postgres only — SQLite has no COMMENT ON and returns an empty list rather
    than raising, so a caller can apply comments unconditionally on whichever
    backend it finds.
    """
    if dialect != POSTGRES:
        return []
    out = []
    for view, text in _VIEW_COMMENTS.items():
        out.append(f"COMMENT ON VIEW {view} IS {_quote(text)}")
    for (view, column), text in _COLUMN_COMMENTS.items():
        out.append(f"COMMENT ON COLUMN {view}.{column} IS {_quote(text)}")
    return out


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


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
