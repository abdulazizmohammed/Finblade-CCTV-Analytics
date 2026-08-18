"""SQL views FinBlade's chatbot queries directly.

The ask: store the analytics in a real relational server and put a view on top
that a bot can query. This module is the single definition of those views.

It used to emit for two engines, so the SQL exercised on SQLite was the SQL that
ran on Postgres. With SQLite removed the second dialect had no deployment behind
it and was kept alive only by its own tests, so it is gone; these views are
Postgres, and the tests run against a real server.

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

# One backend. The dual-dialect emitter these views used to carry existed so the
# SQL exercised on SQLite was the SQL that ran on Postgres; with SQLite removed
# it was a second code path kept alive purely by its own tests.

# Default: twice FINBLADE_STATE_KEEPALIVE (300s). A reading that stood longer
# than this is more likely a dead worker than a quiet zone.
DEFAULT_MAX_HOLD = 600.0

# Silence after which a camera reads OFFLINE. Matches OFFLINE_S in
# services/api/app.py and RuleThresholds.offline_seconds in finblade/rules.py —
# the API derives this state rather than storing it, so v_camera_status has to
# re-implement the rule and must not disagree with them.
DEFAULT_OFFLINE_AFTER = 30.0

# How long a person can go unseen on ONE camera before their next appearance is
# a new fragment rather than a continuation.
#
# This exists because a global_ref survives 300s of absence (FINBLADE_REID_TTL),
# so somebody who leaves a camera and comes back four minutes later keeps the
# same ref and would otherwise collapse into a single fragment spanning a gap
# they were not there for. That fragment would claim an exit zone from before
# the gap and an arrival after it, and every link built on it would be wrong.
#
# 60s is well above a normal tracking dropout (occlusion, a missed frame or two)
# and well below the ReID TTL, so it splits on real absences and not on noise.
DEFAULT_FRAGMENT_GAP = 60.0


def _utc(column: str) -> str:
    """Epoch seconds -> a real timestamp, for humans and BI tools.

    Timestamps are stored as epoch doubles because that is what the API speaks
    end to end, and converting at the storage boundary was a bug source. The
    conversion belongs in the view, where it costs nothing and gives a SQL
    client something it can put in a WHERE clause.
    """
    return f"to_timestamp({column})"


def _bool(expr: str) -> str:
    """Kept as a seam. Postgres has a real boolean, so this is the identity -
    but every boolean column in these views goes through it, which is where a
    future dialect or a CASE wrapper would land."""
    return expr


def _now() -> str:
    """Current time as epoch seconds, matching how the application stores it."""
    return "EXTRACT(EPOCH FROM now())"


def _json_text(column: str, key: str) -> str:
    """Pull one key out of a JSON text column.

    `events.payload` is TEXT holding the whole event. Facility crossings keep
    the doorway and the resulting headcount in there and nowhere else, so a view
    that does not reach into it cannot answer "who crossed which door".
    """
    return f"(({column})::jsonb ->> '{key}')"


def view_definitions(max_hold: float = DEFAULT_MAX_HOLD,
                     offline_after: float = DEFAULT_OFFLINE_AFTER,
                     fragment_gap: float = DEFAULT_FRAGMENT_GAP,
                     temp: bool = False) -> List[Tuple[str, str]]:
    """[(view_name, CREATE VIEW sql)], in dependency order.

    `temp` emits CREATE TEMP VIEW, which is how these get exercised against the
    production database without writing to it: the view lives in the session's
    temp schema while the real file stays open read-only. Unqualified table
    names still resolve to it, so the SQL under test is the SQL that ships.
    """
    ts = _utc
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
    {_bool('COALESCE(z.restricted, s.restricted) = 1')} AS restricted,
    -- A reading that stood for longer than one sample may speak for. The
    -- camera worker was almost certainly not running; treat the span as
    -- unobserved rather than as a long quiet period.
    -- COALESCE because LEAD is NULL on the newest reading, and a NULL here is
    -- worse than useless: `WHERE NOT is_stale` would then silently drop the
    -- live row from every zone, which is the row most questions are about.
    {_bool(f'COALESCE(LEAD(s.ts) OVER w - s.ts > {max_hold}, FALSE)')} AS is_stale,
    -- The newest reading per zone has no successor. It is current, not stale.
    {_bool('LEAD(s.ts) OVER w IS NULL')}                AS is_open
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
    {_bool('COALESCE(z.restricted, l.restricted) = 1')} AS restricted,
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
    {_bool('z.restricted = 1')} AS restricted
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
    {_bool('e.global_ref IS NOT NULL')} AS identity_resolved,
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
    {_bool("COALESCE(a.status,'OPEN') IN ('OPEN','ACK')")} AS is_active,
    COALESCE(z.zone_name, a.zone_id) AS zone_name
FROM alerts a
LEFT JOIN zones z ON z.zone_id = a.zone_id AND z.camera_id = a.camera_id
""".strip()))

    # ------------------------------------------------------------- facility --
    # THE BUILDING, as opposed to any polygon in it. Zone occupancy is derived
    # per frame and is blind the moment somebody steps into a corridor nobody
    # watches; this layer is event-sourced at the doors and keeps counting them.
    #
    # These six views exist because the roster and the crossings lived only in
    # base tables. A read-only role granted the analytics views could not answer
    # "how many people are in the building" at all — the single question the
    # system is most often asked.
    now = _now()

    views.append(("v_facility_current", f"""
CREATE VIEW v_facility_current AS
SELECT
    -- People this system watched walk in and has not seen leave.
    (SELECT COUNT(*) FROM facility_presence)                       AS observed_inside,
    -- Declared opening headcount: people already inside before counting
    -- started. Drains as unmatched exits are observed. 0 unless an operator
    -- set one.
    COALESCE((SELECT value FROM facility_meta WHERE key = 'baseline'), 0)
                                                                   AS baseline,
    (SELECT COUNT(*) FROM facility_presence)
      + COALESCE((SELECT value FROM facility_meta WHERE key = 'baseline'), 0)
                                                                   AS people_inside,
    -- Lifetime counters. admitted counts admissions that grew the roster, which
    -- is fewer than the FACILITY_ENTRY events: someone already inside
    -- re-triggering the entrance is ignored.
    COALESCE((SELECT value FROM facility_meta WHERE key = 'admitted'), 0)
                                                                   AS admitted_total,
    COALESCE((SELECT value FROM facility_meta WHERE key = 'discharged'), 0)
                                                                   AS discharged_total,
    -- Somebody left who was never seen to arrive. Expected in bulk after a cold
    -- start; a standing rate afterwards means entrances are being missed.
    COALESCE((SELECT value FROM facility_meta WHERE key = 'discharge_unknown'), 0)
                                                                   AS unmatched_exits,
    -- A two-way door crossing where neither side was observed, so the direction
    -- could not be established. Rising means a door needs an interior zone.
    COALESCE((SELECT value FROM facility_meta WHERE key = 'ambiguous_crossings'), 0)
                                                                   AS ambiguous_crossings,
    -- Roster entries nobody has seen for over an hour: either a person in
    -- unmonitored space or a missed exit. The data cannot tell which.
    (SELECT COUNT(*) FROM facility_presence WHERE last_seen < {now} - 3600)
                                                                   AS unseen_over_1h,
    {now}                                                          AS as_of_ts
""".strip()))

    views.append(("v_facility_roster", f"""
CREATE VIEW v_facility_roster AS
SELECT
    -- An anonymous session hash. The system cannot answer WHO; this identifies
    -- one person only within the current run.
    p.ref,
    p.admitted_at,
    {ts('p.admitted_at')}                       AS admitted_utc,
    p.last_seen,
    {ts('p.last_seen')}                         AS last_seen_utc,
    ({now} - p.admitted_at) / 60.0              AS minutes_inside,
    ({now} - p.last_seen)  / 60.0               AS minutes_unseen,
    p.entry_zone,
    p.last_zone,
    p.sightings,
    -- Discharge is strict: the roster only falls when a crossing out is
    -- observed. An entry nobody has seen for an hour is the drift signal.
    {_bool(f'p.last_seen < {now} - 3600')} AS possibly_stale
FROM facility_presence p
""".strip()))

    views.append(("v_facility_crossings", f"""
CREATE VIEW v_facility_crossings AS
SELECT
    e.event_id,
    e.event_type,                                -- FACILITY_ENTRY | FACILITY_EXIT
    e.camera_id,
    e.site_id,
    -- Which boundary was crossed. Lives in the payload, not a column.
    {_json_text('e.payload', 'door_zone_id')}   AS door_zone_id,
    -- The building headcount AFTER this crossing, so the occupancy curve is
    -- reconstructable from the event stream alone.
    CAST({_json_text('e.payload', 'occupancy')} AS INTEGER)
                                                         AS occupancy_after,
    -- Facility events carry person_ref only; the roster is keyed on the
    -- cross-camera identity internally but does not stamp it here, so these
    -- rows cannot be joined to a person's zone movements. A known gap.
    e.person_ref,
    e.ts            AS event_ts,
    {ts('e.ts')}    AS event_utc
FROM events e
WHERE e.event_type IN ('FACILITY_ENTRY', 'FACILITY_EXIT')
""".strip()))

    views.append(("v_facility_doors", """
CREATE VIEW v_facility_doors AS
SELECT
    d.door_zone_id,
    d.entries,
    d.exits,
    -- Cumulative net through THIS doorway over its lifetime. NOT building
    -- occupancy: other doors exist and the roster is the authority.
    d.entries - d.exits AS net
FROM facility_doors d
""".strip()))

    # ----------------------------------------------------------- areas ------
    # A physical area is a real room; a zone is one camera's polygon of it.
    # Occupancy here is COUNT(DISTINCT person) across every zone mapped to the
    # room, so a person standing where two cameras overlap counts once.
    views.append(("v_area_current", f"""
CREATE VIEW v_area_current AS
SELECT
    a.area_id,
    a.name              AS area_name,
    a.area_type,
    a.site_id,
    a.capacity_max,
    a.area_sqm,
    s.occupancy,                    -- distinct people, already de-duplicated
    s.capacity_pct,
    s.density,
    s.camera_count,
    -- What a naive per-camera sum would have said. The difference is the
    -- double-count that de-duplication removed.
    s.summed_observations,
    s.summed_observations - s.occupancy AS duplicates_removed,
    s.ts                AS reading_ts,
    {ts('s.ts')}        AS reading_utc
FROM physical_areas a
LEFT JOIN area_state_ts s
       ON s.area_id = a.area_id
      AND s.ts = (SELECT MAX(ts) FROM area_state_ts x WHERE x.area_id = a.area_id)
""".strip()))

    views.append(("v_area_intervals", f"""
CREATE VIEW v_area_intervals AS
SELECT
    s.area_id,
    a.name                                      AS area_name,
    a.capacity_max,
    a.area_sqm,
    s.ts                                        AS valid_from,
    {ts('s.ts')}                                AS valid_from_utc,
    LEAD(s.ts) OVER w                           AS valid_to,
    {ts('LEAD(s.ts) OVER w')}                   AS valid_to_utc,
    LEAD(s.ts) OVER w - s.ts                    AS duration_seconds,
    s.occupancy,
    s.density,
    s.capacity_pct,
    s.camera_count,
    s.summed_observations,
    -- Same rule as v_zone_intervals: weight every average by duration_seconds.
    {_bool(f'COALESCE(LEAD(s.ts) OVER w - s.ts > {max_hold}, FALSE)')} AS is_stale,
    {_bool('LEAD(s.ts) OVER w IS NULL')}              AS is_open
FROM area_state_ts s
LEFT JOIN physical_areas a ON a.area_id = s.area_id
WINDOW w AS (PARTITION BY s.area_id ORDER BY s.ts)
""".strip()))

    # ---------------------------------------------------------- cameras -----
    # Online/offline is DERIVED, not stored — the API computes it and a SQL
    # client otherwise has to re-implement the rule. It is implemented here once
    # so every consumer agrees.
    #
    # cameras.source and cameras.stream_url are deliberately absent: they hold
    # RTSP URLs with embedded passwords. This is the only view that touches the
    # cameras table, and it selects columns explicitly for that reason.
    views.append(("v_camera_status", f"""
CREATE VIEW v_camera_status AS
SELECT
    c.camera_id,
    c.name,
    c.site_id,
    c.state                                     AS reported_state,
    CASE
      WHEN c.enabled = 0 THEN 'DISABLED'
      WHEN COALESCE(c.health_ts, c.last_seen) IS NULL THEN 'OFFLINE'
      WHEN COALESCE(c.health_ts, c.last_seen) < {now} - {offline_after}
           THEN 'OFFLINE'
      ELSE COALESCE(c.state, 'ONLINE')
    END                                         AS effective_state,
    {_bool(f"c.enabled <> 0 AND COALESCE(c.health_ts, c.last_seen) >= {now} - {offline_after}")}
                                                AS is_online,
    c.last_seen,
    {ts('c.last_seen')}                         AS last_seen_utc,
    {now} - COALESCE(c.health_ts, c.last_seen)  AS seconds_since_seen,
    c.input_fps,
    c.resolution,
    c.dropped_frames,
    c.reconnects,
    -- Detections, not people. The same human on two cameras appears in both.
    c.people_in_view,
    c.people_in_zones,
    -- Whether this camera's counts can be believed. counts_reliable is
    -- TRI-STATE: NULL means the worker reported nothing, which is not the same
    -- as reliable.
    c.tracking_quality,
    c.counts_reliable,
    c.counting_mode,
    c.mean_confidence,
    c.track_churn_per_min,
    c.detector_saturation
FROM cameras c
""".strip()))

    views.append(("v_zone_config", f"""
CREATE VIEW v_zone_config AS
SELECT
    z.camera_id,
    z.zone_id,                      -- unique only WITHIN a camera
    z.zone_name,
    z.zone_type,
    {_bool('z.restricted = 1')} AS restricted,
    {_bool('z.enabled <> 0')}   AS enabled,
    z.capacity_max,
    z.area_sqm,
    z.warning_density,
    z.critical_density,
    z.loitering_threshold_sec,
    -- The real room this polygon looks at. NULL means a single-camera zone.
    z.physical_area_id,
    a.name          AS area_name,
    z.updated_at,
    {ts('z.updated_at')} AS updated_utc
FROM zones z
LEFT JOIN physical_areas a ON a.area_id = z.physical_area_id
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

    # ------------------------------------------------------------- journeys --
    # Reconstructing where a person went WITHOUT trusting cross-camera identity.
    #
    # ReID splits: one person picks up several global_refs over a walk through
    # the building, so "SELECT ... WHERE global_ref = ?" returns a third of the
    # journey and gives no sign that the rest exists. These three views rebuild
    # the path from time and physics instead, and they have two advantages the
    # live matcher structurally cannot have:
    #
    #   1. They see the future. The matcher must decide the instant a track
    #      appears, not knowing whether a better candidate walks in two seconds
    #      later. Running over history, every candidate is already present.
    #   2. They can decline. The matcher must return something. These link two
    #      fragments only when nothing else competes, and leave a gap otherwise.
    #
    # The result is partial traces you can trust plus honest holes, rather than
    # a complete trace you cannot check. Confidence is not a tuned parameter
    # here: it is how many other people could have been that hop, which is a
    # count, and it comes out of the data.
    #
    # These read history and are not on any live path. Nothing about ingest,
    # occupancy, alerts or the dashboard depends on them.
    views.append(("v_journey_fragments", f"""
CREATE VIEW v_journey_fragments AS
WITH ev AS (
    SELECT
        {person_key} AS person_key,
        e.global_ref,
        e.camera_id,
        e.site_id,
        {zone_ref} AS zone_ref,
        e.ts
    FROM events e
    WHERE (e.person_ref IS NOT NULL OR e.global_ref IS NOT NULL)
      -- Person-bearing events only. A DENSITY_UPDATE or CAMERA_HEARTBEAT has
      -- no one in it, and including them would extend a fragment past the
      -- point the person was last actually seen.
      AND e.event_type IN ('ZONE_ENTRY', 'ZONE_EXIT', 'ZONE_TRANSITION')
),
gapped AS (
    SELECT ev.*,
           ev.ts - LAG(ev.ts) OVER (PARTITION BY person_key, camera_id
                                    ORDER BY ts) AS gap
    FROM ev
),
marked AS (
    SELECT gapped.*,
           -- Running count of gaps: increments on the first row and on every
           -- absence longer than the threshold, so it numbers the fragments.
           SUM(CASE WHEN gap IS NULL OR gap > {fragment_gap} THEN 1 ELSE 0 END)
               OVER (PARTITION BY person_key, camera_id ORDER BY ts
                     ROWS UNBOUNDED PRECEDING) AS fragment_no
    FROM gapped
)
SELECT
    camera_id || '#' || person_key || '#' || fragment_no  AS fragment_id,
    person_key,
    camera_id,
    MIN(site_id)                                         AS site_id,
    MIN(ts)                                              AS appeared,
    {ts('MIN(ts)')}                                      AS appeared_utc,
    MAX(ts)                                              AS vanished,
    {ts('MAX(ts)')}                                      AS vanished_utc,
    MAX(ts) - MIN(ts)                                    AS duration_seconds,
    COUNT(*)                                             AS event_count,
    -- Where they came in and where they left from. These are what the next
    -- fragment has to be reachable from.
    (array_agg(zone_ref ORDER BY ts)
        FILTER (WHERE zone_ref IS NOT NULL))[1]          AS entry_zone,
    (array_agg(zone_ref ORDER BY ts DESC)
        FILTER (WHERE zone_ref IS NOT NULL))[1]          AS exit_zone,
    COUNT(DISTINCT zone_ref)                             AS zones_touched,
    BOOL_OR(global_ref IS NOT NULL)                      AS identity_resolved
FROM marked
GROUP BY camera_id, person_key, fragment_no
""".strip()))

    # Every hop a fragment COULD be followed by, with the competition counted.
    #
    # successor_options / predecessor_options are the honest confidence: how
    # many fragments contend for this link in each direction. One in both means
    # nothing else is possible and the hop is certain on physics alone. Five
    # means it is a guess, and the view says so rather than picking.
    #
    # This makes trace quality a function of HOW BUSY THE BUILDING WAS, not of
    # how good the crops were - which is why it works best after hours and on
    # near-empty floors, exactly when tracing matters most.
    #
    # OVERLAPPING PAIRS ARE EXCLUDED. Two cameras watching the same floor see
    # one person at the same instant; that is one place, not a hop, and adding
    # it would insert a phantom step into every trace crossing the overlap.
    # Worse, it is feasible in BOTH directions, so a pair of co-present
    # fragments would each be the other's unique neighbour, forming a two-cycle
    # that v_journey_traces then drops entirely - a silent hole rather than a
    # visible one. Deduplicating an overlap is a different question from
    # tracing a path, and mixing them serves neither.
    views.append(("v_journey_links", """
CREATE VIEW v_journey_links AS
WITH candidate AS (
    SELECT
        a.fragment_id               AS from_fragment,
        b.fragment_id               AS to_fragment,
        a.person_key                AS from_person_key,
        b.person_key                AS to_person_key,
        a.camera_id                 AS from_camera,
        b.camera_id                 AS to_camera,
        a.exit_zone                 AS from_zone,
        b.entry_zone                AS to_zone,
        a.vanished                  AS left_at,
        b.appeared                  AS arrived_at,
        b.appeared - a.vanished     AS gap_seconds,
        t.min_seconds,
        t.max_seconds,
        t.pair_kind
    FROM v_journey_fragments a
    JOIN camera_transits t
      ON t.from_camera = a.camera_id
    JOIN v_journey_fragments b
      ON b.camera_id = t.to_camera
    WHERE b.fragment_id <> a.fragment_id
      AND t.pair_kind <> 'overlapping'
      -- The physics gate, and the only filter that matters. An absent row in
      -- camera_transits already means "no route", so this join does the work
      -- of finblade/topology.py feasible() without re-stating its rules.
      AND (b.appeared - a.vanished) BETWEEN t.min_seconds AND t.max_seconds
),
reduced AS (
    -- Transitive reduction, and without it this view does not work on a real
    -- site. Surveyed windows are wide — CAM-01 to CAM-05 is anything from 17
    -- to 240 seconds — so on a three-hop walk the direct first-to-last hop is
    -- ALSO feasible. That shortcut gives the first fragment two successors,
    -- uniqueness collapses, and a journey that is plainly one path chains into
    -- nothing. Wide windows are correct; the shortcut is the artefact.
    --
    -- So: drop A->C when some B exists with A->B and B->C. The person was
    -- SEEN at B in between, and the physics gate already said both halves are
    -- walkable, so the path went through B. Keeping the shortcut would mean
    -- claiming they skipped a camera that observed them.
    --
    -- BOTH LEGS MUST BE ACTUAL MOVEMENT. Without that condition the reduction
    -- is not just imprecise, it manufactures merges. Two strangers pass CAM-01
    -- two seconds apart, which is a legitimate same-camera re-acquisition
    -- candidate (A->B); the reduction then reads A->B->C as explaining A->C,
    -- drops the true link, and chains two people into one journey. A
    -- re-acquisition is not a detour through anywhere, so it cannot license
    -- dropping a real hop. Only a genuine change of camera can.
    --
    -- THE REMAINING ASSUMPTION, stated plainly: that the intermediate sighting
    -- is the same person. If B is a stranger, this stitches them into the path.
    -- It is self-limiting — a stranger at B usually has candidates of their
    -- own, which makes B ambiguous and breaks the chain there instead — but in
    -- a quiet building with two people it can be wrong. The mitigations are the
    -- ones used everywhere here: predecessor_options says how much competition
    -- there was, and identity_agrees says whether ReID concurred.
    SELECT c.*
    FROM candidate c
    WHERE NOT EXISTS (
        SELECT 1
        FROM candidate m
        JOIN candidate n ON n.from_fragment = m.to_fragment
        WHERE m.from_fragment = c.from_fragment
          AND n.to_fragment   = c.to_fragment
          AND m.from_camera <> m.to_camera
          AND n.from_camera <> n.to_camera
    )
)
SELECT
    c.*,
    COUNT(*) OVER (PARTITION BY c.from_fragment)  AS successor_options,
    COUNT(*) OVER (PARTITION BY c.to_fragment)    AS predecessor_options,
    (COUNT(*) OVER (PARTITION BY c.from_fragment) = 1
     AND COUNT(*) OVER (PARTITION BY c.to_fragment) = 1) AS is_unique,
    -- Did cross-camera ReID independently reach the same conclusion? Useful
    -- both ways: agreement corroborates the hop, and a unique link that ReID
    -- missed is a concrete example of a split worth investigating.
    (c.from_person_key = c.to_person_key)         AS identity_agrees
FROM reduced c
""".strip()))

    # The traces themselves: chains of mutually unique links.
    #
    # A journey starts at a fragment nothing uniquely leads to, and extends for
    # as long as each step has exactly one possible continuation. Ambiguity ends
    # the chain rather than guessing through it, so a busy period yields many
    # short journeys and a quiet one yields few long ones. That is the correct
    # behaviour, not a limitation.
    #
    # journey_id is the first fragment's id, so it is stable as long as history
    # is - but it is DERIVED, not stored: adding events in the middle of a
    # window can merge or split journeys retroactively. Cite it in a report with
    # the time range, never as a durable identifier for a person.
    views.append(("v_journey_traces", """
CREATE VIEW v_journey_traces AS
WITH RECURSIVE uniq AS (
    SELECT from_fragment, to_fragment
    FROM v_journey_links
    WHERE is_unique
),
roots AS (
    -- Nothing uniquely arrives here, so this is where a chain begins. A
    -- fragment with no links at all is its own single-hop journey, which is
    -- honest: it happened, and we cannot say what came before or after.
    SELECT f.fragment_id
    FROM v_journey_fragments f
    LEFT JOIN uniq u ON u.to_fragment = f.fragment_id
    WHERE u.to_fragment IS NULL
),
walk AS (
    SELECT fragment_id AS journey_id, fragment_id, 0 AS hop_no
    FROM roots
    UNION ALL
    SELECT w.journey_id, u.to_fragment, w.hop_no + 1
    FROM walk w
    JOIN uniq u ON u.from_fragment = w.fragment_id
    -- Belt and braces. A cycle cannot be entered (every member of one has a
    -- unique predecessor, so none is a root), but a recursive CTE that is
    -- wrong about that does not error - it hangs.
    WHERE w.hop_no < 50
)
SELECT
    w.journey_id,
    w.hop_no,
    f.fragment_id,
    f.person_key,
    f.camera_id,
    f.site_id,
    f.entry_zone,
    f.exit_zone,
    f.appeared,
    f.appeared_utc,
    f.vanished,
    f.vanished_utc,
    f.duration_seconds,
    f.identity_resolved,
    COUNT(*)        OVER (PARTITION BY w.journey_id) AS journey_hops,
    MIN(f.appeared) OVER (PARTITION BY w.journey_id) AS journey_start,
    MAX(f.vanished) OVER (PARTITION BY w.journey_id) AS journey_end
FROM walk w
JOIN v_journey_fragments f ON f.fragment_id = w.fragment_id
""".strip()))

    if temp:
        views = [(name, sql.replace("CREATE VIEW ", "CREATE TEMP VIEW ", 1))
                 for name, sql in views]
    return views


def view_names() -> List[str]:
    return [name for name, _ in view_definitions()]


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
    # -- the facility layer. Without these a read-only role could not answer
    # "how many people are in the building", because the roster and the
    # crossings live only in base tables and no table is ever granted.
    "v_facility_current": (
        "observed_inside", "baseline", "people_inside", "admitted_total",
        "discharged_total", "unmatched_exits", "ambiguous_crossings",
        "unseen_over_1h", "as_of_ts",
    ),
    "v_facility_roster": (
        "ref", "admitted_at", "admitted_utc", "last_seen", "last_seen_utc",
        "minutes_inside", "minutes_unseen", "entry_zone", "last_zone",
        "sightings", "possibly_stale",
    ),
    "v_facility_crossings": (
        "event_id", "event_type", "camera_id", "site_id", "door_zone_id",
        "occupancy_after", "event_ts", "event_utc",
    ),
    "v_facility_doors": ("door_zone_id", "entries", "exits", "net"),
    "v_area_current": (
        "area_id", "area_name", "area_type", "site_id", "capacity_max",
        "area_sqm", "occupancy", "capacity_pct", "density", "camera_count",
        "summed_observations", "duplicates_removed", "reading_ts", "reading_utc",
    ),
    "v_area_intervals": (
        "area_id", "area_name", "capacity_max", "area_sqm", "valid_from",
        "valid_from_utc", "valid_to", "valid_to_utc", "duration_seconds",
        "occupancy", "density", "capacity_pct", "camera_count",
        "summed_observations", "is_stale", "is_open",
    ),
    # Deliberately omits source and stream_url — RTSP URLs with passwords.
    "v_camera_status": (
        "camera_id", "name", "site_id", "reported_state", "effective_state",
        "is_online", "last_seen", "last_seen_utc", "seconds_since_seen",
        "input_fps", "resolution", "dropped_frames", "reconnects",
        "people_in_view", "people_in_zones", "tracking_quality",
        "counts_reliable", "counting_mode", "mean_confidence",
        "track_churn_per_min", "detector_saturation",
    ),
    "v_zone_config": (
        "camera_id", "zone_id", "zone_name", "zone_type", "restricted",
        "enabled", "capacity_max", "area_sqm", "warning_density",
        "critical_density", "loitering_threshold_sec", "physical_area_id",
        "area_name", "updated_at", "updated_utc",
    ),
    # Journey reconstruction. person_key appears here for the same reason it
    # does above — it is the column that can be counted correctly — and
    # person_ref does not appear at all. fragment_id embeds person_key, so it
    # inherits exactly the same exposure and no more.
    "v_journey_fragments": (
        "fragment_id", "person_key", "camera_id", "site_id",
        "appeared", "appeared_utc", "vanished", "vanished_utc",
        "duration_seconds", "event_count", "entry_zone", "exit_zone",
        "zones_touched", "identity_resolved",
    ),
    "v_journey_links": (
        "from_fragment", "to_fragment", "from_person_key", "to_person_key",
        "from_camera", "to_camera", "from_zone", "to_zone",
        "left_at", "arrived_at", "gap_seconds",
        "min_seconds", "max_seconds", "pair_kind",
        "successor_options", "predecessor_options", "is_unique",
        "identity_agrees",
    ),
    "v_journey_traces": (
        "journey_id", "hop_no", "fragment_id", "person_key", "camera_id",
        "site_id", "entry_zone", "exit_zone",
        "appeared", "appeared_utc", "vanished", "vanished_utc",
        "duration_seconds", "identity_resolved",
        "journey_hops", "journey_start", "journey_end",
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
    "v_facility_current": (
        "THE building count. observed_inside + baseline = people_inside. This is "
        "the authoritative site occupancy; never SUM zone occupancy for it."),
    "v_facility_roster": (
        "One row per person currently inside. ref is an anonymous session hash — "
        "the system cannot answer WHO. possibly_stale flags a likely missed exit."),
    "v_facility_crossings": (
        "Entering and leaving the BUILDING. Distinct from ZONE_ENTRY/ZONE_EXIT, "
        "which describe a polygon. occupancy_after rebuilds the occupancy curve."),
    "v_facility_doors": (
        "Cumulative traffic per doorway. NOT occupancy — other doors exist and "
        "the roster is the authority."),
    "v_area_current": (
        "Room-level occupancy, already de-duplicated across cameras. "
        "duplicates_removed is what a naive per-camera sum would have added."),
    "v_area_intervals": (
        "Room occupancy history. Weight averages by duration_seconds, same rule "
        "as v_zone_intervals. Exclude is_stale."),
    "v_camera_status": (
        "Camera health with online/offline DERIVED (30s silence). Contains no "
        "credentials. people_in_view is detections, not people."),
    "v_zone_config": (
        "Zone definitions and their thresholds. zone_id is unique only within a "
        "camera — always pair it with camera_id."),
    "v_timeline": (
        "Every record on one time axis: a UNION, not a join. Joining the fact "
        "tables instead would produce 5.66 trillion rows from a 1.15GB source. "
        "zone_id is the raw column, so movement events contribute NULL — use "
        "v_zone_events when the zone matters."),
    "v_journey_fragments": (
        "One unbroken appearance of one person on one camera. The reliable unit "
        "— tracking within a camera is solid; it is the seams between cameras "
        "that break. Fragments are what the journey views stitch."),
    "v_journey_links": (
        "Every hop a fragment could be followed by, gated on surveyed walk "
        "times. successor_options/predecessor_options count the competition: "
        "1 and 1 means nothing else was possible. Filter is_unique for hops "
        "that need no guessing. Excludes overlapping camera pairs — those are "
        "one place, not a hop."),
    "v_journey_traces": (
        "Reconstructed paths, built from time and building physics rather than "
        "appearance — so they survive the ReID splits that break global_ref. "
        "A chain stops where it becomes ambiguous instead of guessing, so gaps "
        "are honest. journey_id is derived from the queried history and is NOT "
        "a durable id for a person."),
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
    ("v_facility_current", "people_inside"): (
        "The building count: observed_inside plus any declared baseline. Use "
        "this for 'how many people are on site', never a sum of zone occupancy."),
    ("v_facility_current", "unmatched_exits"): (
        "Somebody left who was never seen to arrive. Bulk after a cold start is "
        "normal; a standing rate means entrances are being missed."),
    ("v_area_current", "occupancy"): (
        "DISTINCT people across every camera watching this room. Already "
        "de-duplicated; do not add camera figures to it."),
    ("v_camera_status", "people_in_view"): (
        "Detections in frame, not people. The same human on two cameras appears "
        "in both. Never SUM across cameras."),
    ("v_camera_status", "counts_reliable"): (
        "TRI-STATE. NULL means the worker reported nothing, which is not the "
        "same as reliable."),
    ("v_alerts", "is_active"): (
        "True for OPEN and ACK. Use this rather than status, which is NULL "
        "until somebody touches the alert."),
    ("v_journey_links", "is_unique"): (
        "Nothing else could have been this hop, in either direction. These need "
        "no appearance matching and no threshold — filter on this for links you "
        "can state as fact."),
    ("v_journey_links", "predecessor_options"): (
        "How many fragments could have arrived here. THIS IS THE CONFIDENCE: 1 "
        "is certain, 5 means one in five. It reflects how busy the building "
        "was, not how good the camera was."),
    ("v_journey_links", "gap_seconds"): (
        "Unseen time between the two fragments. Negative is impossible here — "
        "overlapping camera pairs are excluded from this view."),
    ("v_journey_links", "pair_kind"): (
        "'surveyed' means the window came from paced walk times; 'default' "
        "means nobody has surveyed that pair and the window is a guess. Weight "
        "a hop accordingly."),
    ("v_journey_links", "identity_agrees"): (
        "ReID independently reached the same conclusion. A unique link where "
        "this is false is a concrete example of a cross-camera identity split."),
    ("v_journey_traces", "journey_id"): (
        "The first fragment of the chain. Derived from whatever history is "
        "present — adding events can merge or split journeys retroactively. "
        "Always cite it with a time range; it is not a person's id."),
    ("v_journey_traces", "hop_no"): (
        "0 is where the chain starts, which is where evidence starts and not "
        "necessarily where the person entered the building."),
    ("v_journey_traces", "journey_hops"): (
        "Fragments in this chain. 1 means an isolated appearance nothing could "
        "be linked to — real, but it tells you nothing about a path."),
}


def comment_sql() -> List[str]:
    """COMMENT ON statements for the views and their sharpest columns.

    """
    out = []
    for view, text in _VIEW_COMMENTS.items():
        out.append(f"COMMENT ON VIEW {view} IS {_quote(text)}")
    for (view, column), text in _COLUMN_COMMENTS.items():
        out.append(f"COMMENT ON COLUMN {view}.{column} IS {_quote(text)}")
    return out


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def drop_sql() -> List[str]:
    """DROP statements, reverse dependency order. Idempotent."""
    return [f"DROP VIEW IF EXISTS {name}" for name in reversed(view_names())]


def create_all(conn, max_hold: float = DEFAULT_MAX_HOLD,
               offline_after: float = DEFAULT_OFFLINE_AFTER,
               fragment_gap: float = DEFAULT_FRAGMENT_GAP,
               temp: bool = False) -> List[str]:
    """(Re)create every view on an open DB-API connection. Returns the names."""
    names = []
    for stmt in drop_sql():
        conn.execute(stmt)
    for name, sql in view_definitions(max_hold=max_hold,
                                      offline_after=offline_after,
                                      fragment_gap=fragment_gap,
                                      temp=temp):
        conn.execute(sql)
        names.append(name)
    return names
